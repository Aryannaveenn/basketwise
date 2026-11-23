#!/usr/bin/env bash
# Daily pipeline for a single EC2 box: crawl -> build -> validate -> publish -> Supabase.
#
# Run by systemd (deploy/systemd/basketwise-daily.timer). Safe to run by hand:
#   /home/ubuntu/basketwise/deploy/scripts/daily.sh            full run
#   /home/ubuntu/basketwise/deploy/scripts/daily.sh --no-crawl skip crawling; merge + rebuild from existing hyper_*.db
#
# Crawl policy (keeps the box inside free-tier CPU/RAM):
#   aldi, harrisfarm  full every day (listing API has everything; ~1 min each)
#   coles             listing daily, full detail on Sundays (adds barcodes, nutrition)
#   woolworths        listing only. Its detail phase also fetches ~500k marketplace
#                     listings, which takes many hours. Set WOOLWORTHS_DETAIL=1 to force.
#
# A failed crawl keeps that store's previous data. A failed validation keeps the
# previous build online. Supabase sync runs last, so it can't block publishing.
set -Eeuo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCRAPER="$REPO/scraper"
SRV="${BASKETWISE_SRV:-/srv/basketwise}"
export UV_PROJECT_ENVIRONMENT=.venv-hs
export PATH="$HOME/.local/bin:/usr/local/go/bin:$PATH"
cd "$SCRAPER"

log() { printf '\n=== %s  %s ===\n' "$(date -u +%FT%TZ)" "$*"; }
ping_hc() { [[ -n "${HEALTHCHECK_URL:-}" ]] && curl -fsS -m 10 --retry 3 "$HEALTHCHECK_URL$1" >/dev/null || true; }
trap 'ping_hc /fail' ERR
ping_hc /start

crawl() { # store phase [extra args...]
  local store=$1 phase=$2; shift 2
  log "crawl $store ($phase)"
  timeout 4h ./hyperscrape -store "$store" -phase "$phase" -start-rate auto \
      -stats-interval 30s "$@" >"data/$store.ndjson" \
    || echo "$store crawl exited $? -- keeping previous data"
}

if [[ "${1:-}" != "--no-crawl" ]]; then
  crawl aldi all
  crawl harrisfarm all

  if [[ "$(date +%u)" == 7 ]]; then crawl coles all; else crawl coles list; fi

  log "woolworths cookies"
  uv run tui/harvest_cookies.py --store woolworths --out data/cookies_woolworths.json \
    || echo "cookie harvest failed; trying direct tier"
  ck=(); [[ -s data/cookies_woolworths.json ]] && ck=(-cookies data/cookies_woolworths.json)
  if [[ "${WOOLWORTHS_DETAIL:-0}" == 1 ]]; then crawl woolworths all "${ck[@]}"; else crawl woolworths list "${ck[@]}"; fi
fi

log "merge -> master.db"
uv run tui/build_master.py

log "canonicalise -> basketwise.db"
uv run tui/build_api_db.py

log "validate"
uv run python "$REPO/deploy/scripts/validate.py" data/basketwise.db "$SRV/state/last_build.json"

log "publish"
mkdir -p "$SRV/data"
cp data/basketwise.db "$SRV/data/basketwise.db.new"
rm -rf "$SRV/data/warm.new"
cp -r data/warm "$SRV/data/warm.new"
chmod 444 "$SRV/data/basketwise.db.new"
chmod -R a-w "$SRV/data/warm.new"
mv -f "$SRV/data/basketwise.db.new" "$SRV/data/basketwise.db"
if [[ -d "$SRV/data/warm" ]]; then
  chmod -R u+w "$SRV/data/warm"
  rm -rf "$SRV/data/warm.old"
  mv "$SRV/data/warm" "$SRV/data/warm.old"
fi
mv "$SRV/data/warm.new" "$SRV/data/warm"
chmod -R u+w "$SRV/data/warm.old" 2>/dev/null || true
rm -rf "$SRV/data/warm.old"

docker restart basketwise-api >/dev/null
healthy=0
for _ in $(seq 1 30); do
  sleep 2
  if docker exec basketwise-api python -c \
      "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2)" 2>/dev/null; then
    healthy=1; echo "api healthy"; break
  fi
done
[[ $healthy == 1 ]] || { echo "api did not come back healthy -- check: docker logs basketwise-api"; false; }

log "supabase"
if [[ -n "${SUPABASE_URL:-}" && -n "${SUPABASE_SECRET_KEY:-}" ]]; then
  uv run python "$REPO/deploy/scripts/sync_supabase.py" catalogue
  uv run python "$REPO/deploy/scripts/sync_supabase.py" push-artifact --srv "$SRV/data"
else
  echo "SUPABASE_URL / SUPABASE_SECRET_KEY not set; skipping"
fi

log "done"
ping_hc ""
