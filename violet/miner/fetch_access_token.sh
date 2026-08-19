#!/usr/bin/env bash
# Fetch MINER_ACCESS_TOKEN from Avoices ASRAPI (signed hotkey challenge).
#
# Usage (from repo root):
#   ./violet/miner/fetch_access_token.sh test --write-env
#   VIOLET_TOKEN_API_URL=https://api.phosaico.com ./violet/miner/fetch_access_token.sh test --write-env
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

NETWORK="${1:-test}"
shift || true
if [[ "$NETWORK" == "prod" || "$NETWORK" == "production" ]]; then
  NETWORK="finney"
fi

# Prefer module entrypoint (works after git pull + pip install -e ".[chain]")
if python3 -c "import violet.miner.fetch_access_token" 2>/dev/null; then
  exec python3 -m violet.miner.fetch_access_token \
    --network "$NETWORK" \
    "$@"
fi

# Fallback to scripts/ path
exec python3 scripts/fetch_miner_access_token.py \
  --network "$NETWORK" \
  "$@"
