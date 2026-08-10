#!/bin/sh
# Selects which sample service this container runs.
set -eu

case "${SERVICE:-asr}" in
  asr) MODULE="mock_asr:app" ;;
  tts) MODULE="mock_tts:app" ;;
  *)
    echo "SERVICE must be 'asr' or 'tts', got '${SERVICE}'" >&2
    exit 2
    ;;
esac

exec uvicorn "$MODULE" \
  --host 0.0.0.0 \
  --port "${PORT:-9000}" \
  --log-level "${LOG_LEVEL:-info}" \
  --ws-ping-interval 20 \
  --ws-ping-timeout 20
