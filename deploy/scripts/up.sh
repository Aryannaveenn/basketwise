#!/usr/bin/env bash
# Start / update the API + Caddy containers with the server's env file.
#   /home/ubuntu/basketwise/deploy/scripts/up.sh            build + (re)start everything
#   /home/ubuntu/basketwise/deploy/scripts/up.sh logs -f    any other compose subcommand
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${BASKETWISE_ENV:-/srv/basketwise/basketwise.env}"
compose=(docker compose --env-file "$ENV_FILE" -f "$REPO/deploy/compose.yml")
if [[ $# -eq 0 ]]; then
  "${compose[@]}" up -d --build
  "${compose[@]}" ps
else
  "${compose[@]}" "$@"
fi
