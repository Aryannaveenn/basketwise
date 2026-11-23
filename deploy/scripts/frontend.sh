#!/usr/bin/env bash
# Build the React app inside a throwaway Node container and publish it to
# /srv/basketwise/www, which Caddy serves. No Node install needed on the host.
#
#   /home/ubuntu/basketwise/deploy/scripts/frontend.sh
#
# The app calls the API at /api (same origin, so no CORS), which Caddy proxies.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRV="${BASKETWISE_SRV:-/srv/basketwise}"

docker run --rm \
  --user "$(id -u):$(id -g)" \
  -v "$REPO/frontend":/app -w /app \
  -e HOME=/tmp -e COREPACK_HOME=/tmp/corepack -e COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
  -e CI=true -e VITE_API_BASE=/api \
  -e NODE_OPTIONS=--max-old-space-size=1024 \
  node:24-slim \
  sh -c "corepack pnpm install --frozen-lockfile && corepack pnpm build"

mkdir -p "$SRV/www"
rsync -a --delete "$REPO/frontend/dist/" "$SRV/www/"
echo "frontend published to $SRV/www"
