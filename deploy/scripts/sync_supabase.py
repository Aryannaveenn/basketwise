#!/usr/bin/env python3
"""Sync BasketWise data to Supabase.

  catalogue      upsert each store's grocery catalogue into store_products and
                 append new price changes to price_history (from scraper/data/hyper_*.db)
  push-artifact  upload basketwise.db + warm/ as artifacts/latest.tar.gz
  pull-artifact  download that archive and install it into --srv (disaster recovery)

Needs SUPABASE_URL and SUPABASE_SECRET_KEY in the environment (the secret /
service-role key: it bypasses RLS). Run from scraper/ so uv uses its venv:

  cd scraper
  UV_PROJECT_ENVIRONMENT=.venv-hs uv run python ../deploy/scripts/sync_supabase.py catalogue
  UV_PROJECT_ENVIRONMENT=.venv-hs uv run python ../deploy/scripts/sync_supabase.py push-artifact --srv /srv/basketwise/data

Free-plan friendly: Woolworths marketplace rows (is_market=1) are skipped, products
are re-uploaded only when their content hash changes, and price_history only gets
rows the crawler recorded as a price change. All writes are idempotent, so losing
the local state file just means one larger (but harmless) re-sync.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "scraper" / "data"
STATE = DATA / ".supabase_state.json"

STORES = {"coles": "Coles", "woolworths": "Woolworths", "aldi": "ALDI", "harrisfarm": "Harris Farm Markets"}
HOSTS = {
    "coles": "https://www.coles.com.au",
    "woolworths": "https://www.woolworths.com.au",
    "aldi": "https://www.aldi.com.au",
    "harrisfarm": "https://www.harrisfarm.com.au",
}
BATCH = 500
BUCKET = "artifacts"
OBJECT = "latest.tar.gz"
MAX_OBJECT = 50 * 1024 * 1024  # Supabase free plan per-file upload limit


def log(msg: str) -> None:
    print(f"[sync] {msg}", flush=True)


def client():
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SECRET_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not (url and key):
        sys.exit("SUPABASE_URL and SUPABASE_SECRET_KEY must be set (see /srv/basketwise/basketwise.env)")
    from supabase import create_client
    return create_client(url, key)


def retry(fn, tries: int = 5):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 -- network blips, 5xx, rate limits
            if i == tries - 1:
                raise
            wait = 2 ** i
            log(f"retry in {wait}s: {e}")
            time.sleep(wait)


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(STATE)


def first_image(raw: str | None) -> str | None:
    try:
        arr = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return None
    return arr[0] if isinstance(arr, list) and arr else None


def store_ids(sb) -> dict[str, int]:
    have = {r["code"]: r["id"] for r in sb.table("stores").select("id,code").execute().data}
    missing = [{"code": c, "name": n} for c, n in STORES.items() if c not in have]
    if missing:
        sb.table("stores").insert(missing).execute()
        have = {r["code"]: r["id"] for r in sb.table("stores").select("id,code").execute().data}
    return have


def remote_ids(sb, store_id: int) -> dict[str, int]:
    """external_product_id -> store_products.id, paged (PostgREST caps at 1000 rows)."""
    out: dict[str, int] = {}
    start = 0
    while True:
        page = retry(lambda s=start: sb.table("store_products").select("id,external_product_id")
                     .eq("store_id", store_id).order("id").range(s, s + 999).execute().data)
        out.update({str(r["external_product_id"]): r["id"] for r in page})
        if len(page) < 1000:
            return out
        start += 1000


def sync_store(sb, store: str, store_id: int, state: dict) -> None:
    from postgrest import ReturnMethod

    path = DATA / f"hyper_{store}.db"
    if not path.exists():
        log(f"{store}: no {path.name}, skipping")
        return
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    hashes: dict[str, int] = state.setdefault("hash", {}).setdefault(store, {})
    rows = db.execute(
        "SELECT product_id, name, brand, size, description, image_urls, category_path, is_weighted, "
        "unit_measure, barcode, url, in_stock, last_seen, COALESCE(content_hash, 0) AS h "
        "FROM products WHERE deprecated = 0 AND COALESCE(is_market, 0) = 0").fetchall()
    changed = [r for r in rows if hashes.get(r["product_id"]) != r["h"]]
    log(f"{store}: {len(rows):,} grocery products, {len(changed):,} new/changed")

    for i in range(0, len(changed), BATCH):
        chunk = changed[i:i + BATCH]
        payload = [{
            "store_id": store_id,
            "external_product_id": r["product_id"],
            "name": r["name"],
            "brand": r["brand"],
            "description": r["description"],
            "image_url": first_image(r["image_urls"]),
            "package_size": r["size"],
            "source_category": r["category_path"],
            "is_weighted": bool(r["is_weighted"]),
            "unit": r["unit_measure"],
            "barcode": r["barcode"] or None,
            "url": (HOSTS[store] + r["url"]) if r["url"] and r["url"].startswith("/") else r["url"],
            "in_stock": None if r["in_stock"] is None else bool(r["in_stock"]),
            "last_seen": r["last_seen"],
        } for r in chunk]
        retry(lambda p=payload: sb.table("store_products").upsert(
            p, on_conflict="store_id,external_product_id", returning=ReturnMethod.minimal).execute())
        hashes.update({r["product_id"]: r["h"] for r in chunk})
        save_state(state)  # progress survives a crash mid-store

    idmap = remote_ids(sb, store_id)
    since = state.setdefault("price_ts", {}).get(store, "")
    changes = db.execute(
        "SELECT product_id, ts, price_cents, was_price_cents FROM price_history "
        "WHERE store = ? AND ts > ? AND price_cents > 0 ORDER BY ts", (store, since)).fetchall()
    ph = [{
        "store_product_id": idmap[r["product_id"]],
        "price": r["price_cents"] / 100,
        "was_price": (r["was_price_cents"] / 100) if r["was_price_cents"] else None,
        "captured_at": r["ts"],
    } for r in changes if r["product_id"] in idmap]
    log(f"{store}: {len(ph):,} price changes since {since or 'the beginning'}")
    for i in range(0, len(ph), BATCH):
        chunk = ph[i:i + BATCH]
        retry(lambda c=chunk: sb.table("price_history").upsert(
            c, on_conflict="store_product_id,captured_at", ignore_duplicates=True,
            returning=ReturnMethod.minimal).execute())
    if changes:
        state["price_ts"][store] = changes[-1]["ts"]
        save_state(state)
    db.close()


def cmd_catalogue(_args) -> int:
    sb = client()
    ids = store_ids(sb)
    state = load_state()
    for store in STORES:
        sync_store(sb, store, ids[store], state)
    log("catalogue done")
    return 0


def cmd_push(args) -> int:
    srv = Path(args.srv)
    dbf, warm = srv / "basketwise.db", srv / "warm"
    if not dbf.exists() or not warm.is_dir():
        sys.exit(f"nothing to push: need {dbf} and {warm}/")
    with tempfile.TemporaryDirectory() as tmp:
        arc = Path(tmp) / OBJECT
        with tarfile.open(arc, "w:gz", compresslevel=9) as tf:
            tf.add(dbf, arcname="basketwise.db")
            tf.add(warm, arcname="warm")
        size = arc.stat().st_size
        if size > MAX_OBJECT:
            log(f"artifact is {size / 1048576:.1f} MB, over the 50 MB free-plan upload limit; not uploaded")
            return 0
        sb = client()
        data = arc.read_bytes()
        retry(lambda: sb.storage.from_(BUCKET).upload(
            OBJECT, data, {"content-type": "application/gzip", "upsert": "true"}))
    log(f"uploaded {BUCKET}/{OBJECT} ({size / 1048576:.1f} MB)")
    return 0


def cmd_pull(args) -> int:
    srv = Path(args.srv)
    srv.mkdir(parents=True, exist_ok=True)
    sb = client()
    data = retry(lambda: sb.storage.from_(BUCKET).download(OBJECT))
    with tempfile.TemporaryDirectory(dir=srv) as tmp:
        arc = Path(tmp) / OBJECT
        arc.write_bytes(data)
        out = Path(tmp) / "x"
        with tarfile.open(arc, "r:gz") as tf:
            tf.extractall(out, filter="data")
        (srv / "basketwise.db").unlink(missing_ok=True)
        shutil.move(str(out / "basketwise.db"), srv / "basketwise.db")
        if (srv / "warm").exists():
            make_writable(srv / "warm")
            shutil.rmtree(srv / "warm")
        shutil.move(str(out / "warm"), srv / "warm")
    os.chmod(srv / "basketwise.db", 0o444)
    log(f"installed artifact into {srv} ({len(data) / 1048576:.1f} MB archive)")
    return 0


def make_writable(path: Path) -> None:
    """daily.sh leaves warm/ read-only; make it removable again."""
    for p in [path, *path.rglob("*")]:
        try:
            p.chmod(p.stat().st_mode | 0o200)
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("catalogue", help="sync store_products + price_history").set_defaults(fn=cmd_catalogue)
    for name, fn in (("push-artifact", cmd_push), ("pull-artifact", cmd_pull)):
        p = sub.add_parser(name)
        p.add_argument("--srv", default="/srv/basketwise/data", help="folder holding basketwise.db + warm/")
        p.set_defaults(fn=fn)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
