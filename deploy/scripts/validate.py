#!/usr/bin/env python3
"""Refuse to publish a broken artifact. Same checks as scraper/deploy/nightly.sh.

  python validate.py <basketwise.db> <state.json>

Exit 0 = safe to publish (and records the product count in state.json for the
next run's "dropped >20%" check). Exit 1 = keep serving the previous build.
Thresholds can be lowered with env vars while you bootstrap a first deploy.
"""
import json
import os
import pathlib
import sqlite3
import sys

MIN_PRODUCTS = int(os.getenv("BW_MIN_PRODUCTS", "20000"))
MIN_OFFERS = int(os.getenv("BW_MIN_OFFERS", "20000"))
MIN_COMPARABLE = int(os.getenv("BW_MIN_COMPARABLE", "2000"))
MIN_RETAILERS = int(os.getenv("BW_MIN_RETAILERS", "2"))

db_path, state_path = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
n_prod = db.execute("SELECT COUNT(*) FROM products").fetchone()[0]
n_off = db.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
n_cat = db.execute("SELECT COUNT(*) FROM categories WHERE product_count>0").fetchone()[0]
n_multi = db.execute("SELECT COUNT(*) FROM products WHERE retailer_count>=2").fetchone()[0]
stores = [r[0] for r in db.execute("SELECT DISTINCT retailer FROM offers")]

fail = []
if n_prod < MIN_PRODUCTS:
    fail.append(f"only {n_prod} products (min {MIN_PRODUCTS})")
if n_off < MIN_OFFERS:
    fail.append(f"only {n_off} offers (min {MIN_OFFERS})")
if n_cat < 10:
    fail.append(f"only {n_cat} non-empty categories")
if n_multi < MIN_COMPARABLE:
    fail.append(f"only {n_multi} comparable products (min {MIN_COMPARABLE})")
if len(stores) < MIN_RETAILERS:
    fail.append(f"only {len(stores)} retailers: {stores}")
for store, n, avail in db.execute(
        "SELECT retailer, COUNT(*), SUM(COALESCE(is_available,1)) FROM offers GROUP BY retailer"):
    if avail < n * 0.5:
        fail.append(f"{store}: only {avail}/{n} offers available")
if state_path.exists():
    old = json.loads(state_path.read_text())
    if n_prod < old["products"] * 0.8:
        fail.append(f"products dropped {old['products']:,} -> {n_prod:,} (>20%)")

print(f"products={n_prod:,} offers={n_off:,} comparable={n_multi:,} retailers={stores}")
if fail:
    print("VALIDATION FAILED:")
    for f in fail:
        print("  -", f)
    sys.exit(1)
state_path.parent.mkdir(parents=True, exist_ok=True)
state_path.write_text(json.dumps({"products": n_prod, "offers": n_off}))
print("validation ok")
