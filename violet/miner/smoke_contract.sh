#!/usr/bin/env bash
#
# smoke_contract.sh — Verify everything validators / the router dial on the miner.
#
# Public surface (MINER_PORT, default 8091) — this is what must be captured:
#   GET  /health
#   GET  /capacity
#   GET  /violet/info
#   POST /transcribe
#   WS   /realtime/transcribe
#   POST /v1/audio/speech/stream
#   GET  /v1/voices                 (optional; warn if missing)
#   WS   /v1/audio/speech/stream/ws (optional smoke)
#
# Upstream health (local only; not announced):
#   ASR :9090  TTS :8002
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
MINER="http://127.0.0.1:${MINER_PORT}"

pass=0
fail=0
warn_n=0

ok()   { echo "  OK    $*"; pass=$((pass + 1)); }
bad()  { echo "  FAIL  $*"; fail=$((fail + 1)); }
warn() { echo "  WARN  $*"; warn_n=$((warn_n + 1)); }

check_get() {
  local label="$1" url="$2"
  if curl -fsS --max-time 15 "$url" >/dev/null 2>&1; then
    ok "$label"
  else
    bad "$label ($url)"
  fi
}

make_wav() {
  local path="$1"
  python3 - "$path" <<'PY' 2>/dev/null || return 1
import sys, wave, math, struct
path = sys.argv[1]
rate, n = 16000, 16000
with wave.open(path, "w") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
    for i in range(n):
        v = int(8000 * math.sin(2 * math.pi * 440 * i / rate))
        w.writeframes(struct.pack("<h", v))
PY
}

echo "==> contract smoke (services=${SERVICES})"
echo "    miner public surface → ${MINER}"
echo

# --- local upstreams (not announced) --------------------------------------
if [[ ",${SERVICES}," == *",asr,"* ]]; then
  check_get "upstream asr GET /health" "http://127.0.0.1:${ASR_PORT}/health"
fi
if [[ ",${SERVICES}," == *",tts,"* ]]; then
  code="$(curl -sS -o /tmp/tts_up_smoke.pcm -w '%{http_code}' --max-time 60 \
    -H 'Content-Type: application/json' \
    -d '{"text":"smoke","speaker_id":"eng_female_1","temperature":0.7}' \
    "http://127.0.0.1:${TTS_PORT}/v1/audio/speech/stream" 2>/dev/null || echo "000")"
  if [[ "$code" == "200" ]]; then
    ok "upstream tts POST /v1/audio/speech/stream"
  else
    bad "upstream tts POST /v1/audio/speech/stream (HTTP ${code})"
  fi
fi

# --- validator / router facing (must all work on :8091) -------------------
echo
echo "==> miner public API (validators dial these)"

check_get "miner GET /health"      "${MINER}/health"
check_get "miner GET /capacity"    "${MINER}/capacity"
check_get "miner GET /violet/info" "${MINER}/violet/info"

# Capacity must report at least one accepted GPU for emissions.
if command -v python3 >/dev/null 2>&1; then
  python3 - <<PY 2>/dev/null && ok "miner capacity_units > 0" || bad "miner capacity_units is 0 (nvidia-smi / --gpu missing?)"
import json, urllib.request
data = json.load(urllib.request.urlopen("${MINER}/capacity", timeout=10))
units = float(data.get("capacity_units") or data.get("capacity", {}).get("capacity_units") or 0)
gpus = data.get("gpus") or data.get("capacity", {}).get("gpus") or []
raise SystemExit(0 if units > 0 and gpus else 1)
PY
fi

tmp="$(mktemp -d)"
wav="${tmp}/tone.wav"
if make_wav "$wav"; then
  if [[ ",${SERVICES}," == *",asr,"* ]]; then
    code="$(curl -sS -o /tmp/miner_asr.json -w '%{http_code}' --max-time 120 \
      -F "file=@${wav};type=audio/wav" \
      -F "language=eng" \
      -F "response_format=json" \
      "${MINER}/transcribe" 2>/dev/null || echo "000")"
    if [[ "$code" == "200" ]]; then
      ok "miner POST /transcribe"
    else
      bad "miner POST /transcribe (HTTP ${code})"
    fi
  fi
  if [[ ",${SERVICES}," == *",tts,"* ]]; then
    code="$(curl -sS -o /tmp/miner_tts.pcm -w '%{http_code}' --max-time 120 \
      -H 'Content-Type: application/json' \
      -d '{"text":"Hello cathedral.","speaker_id":"eng_female_1","temperature":0.7}' \
      "${MINER}/v1/audio/speech/stream" 2>/dev/null || echo "000")"
    if [[ "$code" == "200" ]]; then
      ok "miner POST /v1/audio/speech/stream"
    else
      bad "miner POST /v1/audio/speech/stream (HTTP ${code})"
    fi
    code="$(curl -sS -o /tmp/miner_voices.json -w '%{http_code}' --max-time 15 \
      "${MINER}/v1/voices" 2>/dev/null || echo "000")"
    if [[ "$code" == "200" ]]; then
      ok "miner GET /v1/voices"
    else
      warn "miner GET /v1/voices → HTTP ${code} (optional catalogue)"
    fi
  fi
else
  warn "could not synthesize WAV — skipped POST smokes"
fi
rm -rf "$tmp"

# Streaming ASR (batch_proxy emits partials; needs python websockets if available)
if [[ ",${SERVICES}," == *",asr,"* ]] && python3 -c "import websockets" 2>/dev/null; then
  if python3 - <<'PY' 2>/dev/null
import asyncio, json, os
import websockets

async def main():
    uri = f"ws://127.0.0.1:{os.environ.get('MINER_PORT','8091')}/realtime/transcribe?language=eng"
    async with websockets.connect(uri, open_timeout=5) as ws:
        # ~0.3s of silence/PCM
        await ws.send(b"\x00" * 9600)
        await ws.send(b"\x00" * 9600)
        msg = await asyncio.wait_for(ws.recv(), timeout=8)
        data = json.loads(msg) if isinstance(msg, str) else {}
        if not (data.get("text") or "").strip() and data.get("type") not in {"partial", "final"}:
            # empty text on silence is ok if typed partial/final arrived
            if "type" not in data and "text" not in data:
                raise SystemExit(1)
    raise SystemExit(0)

asyncio.run(main())
PY
  then
    ok "miner WS /realtime/transcribe (partial/final frame)"
  else
    bad "miner WS /realtime/transcribe (no frame within 8s)"
  fi
else
  warn "skip WS /realtime/transcribe (install websockets or run qualification)"
fi

echo
echo "==> ${pass} passed, ${fail} failed, ${warn_n} warnings"
echo
echo "Provide checklist (validators / router):"
echo "  [ ] Public TCP ${MINER_PORT} open"
echo "  [ ] MINER_PUBLIC_ENDPOINT announced on chain"
echo "  [ ] Registered hotkey (uid assigned)"
echo "  [ ] /health /capacity /violet/info /transcribe /speech/stream OK"
echo "  [ ] capacity_units > 0 (accepted GPU)"
echo "  [ ] python scripts/run_qualification.py ${MINER} --services ${SERVICES}"
[[ "$fail" -eq 0 ]]
