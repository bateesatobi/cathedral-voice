#!/usr/bin/env bash
#
# smoke_contract.sh — Quick API-contract checks beyond /health.
#
# Usage:
#   ./violet/miner/smoke_contract.sh
#   MINER_PORT=8091 ASR_PORT=9090 TTS_PORT=8002 ./violet/miner/smoke_contract.sh
#
set -euo pipefail

ASR_PORT="${ASR_PORT:-9090}"
TTS_PORT="${TTS_PORT:-8002}"
MINER_PORT="${MINER_PORT:-8091}"
SERVICES="${MINER_SERVICES:-asr,tts}"

pass=0
fail=0

ok()  { echo "  OK  $*"; pass=$((pass + 1)); }
bad() { echo "  FAIL $*"; fail=$((fail + 1)); }

check_get() {
  local name="$1" url="$2"
  if curl -fsS --max-time 15 "$url" >/dev/null 2>&1; then
    ok "$name GET $url"
  else
    bad "$name GET $url"
  fi
}

echo "==> contract smoke (services=${SERVICES})"

if [[ ",${SERVICES}," == *",asr,"* ]]; then
  check_get "asr" "http://127.0.0.1:${ASR_PORT}/health"
fi
if [[ ",${SERVICES}," == *",tts,"* ]]; then
  check_get "tts" "http://127.0.0.1:${TTS_PORT}/health"
  if curl -fsS --max-time 20 "http://127.0.0.1:${TTS_PORT}/v1/audio/voices" >/dev/null 2>&1; then
    ok "tts GET /v1/audio/voices"
  else
    bad "tts GET /v1/audio/voices"
  fi
fi

check_get "miner" "http://127.0.0.1:${MINER_PORT}/health"
if curl -fsS --max-time 10 "http://127.0.0.1:${MINER_PORT}/capacity" >/dev/null 2>&1; then
  ok "miner GET /capacity"
else
  bad "miner GET /capacity"
fi

echo "==> ${pass} passed, ${fail} failed"
[[ "$fail" -eq 0 ]]
