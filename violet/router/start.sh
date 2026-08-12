#!/usr/bin/env bash
# Start the Violet Router HTTP service (chain discovery + miner routing).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

: "${VIOLET_ROUTER_HOST:=0.0.0.0}"
: "${VIOLET_ROUTER_PORT:=8090}"

exec python -m violet.router.run --host "$VIOLET_ROUTER_HOST" --port "$VIOLET_ROUTER_PORT"
