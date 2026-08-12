#!/usr/bin/env bash
# Fetch MINER_ACCESS_TOKEN from Avoices ASRAPI (signed hotkey challenge).
#
# Usage (from violet-subnet repo root, wallet present):
#   ./violet/miner/fetch_access_token.sh test
#   VIOLET_TOKEN_API_URL=https://phosai-backend-api-1.onrender.com ./violet/miner/fetch_access_token.sh prod --write-env
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

NETWORK="${1:-test}"
shift || true

if [[ "$NETWORK" == "prod" || "$NETWORK" == "production" ]]; then
  NETWORK="finney"
fi

exec python3 scripts/fetch_miner_access_token.py \
  --network "$NETWORK" \
  --write-env \
  "$@"
