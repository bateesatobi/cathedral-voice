#!/usr/bin/env bash
#
# inference_run_install.sh — ASR/TTS via standalone docker run (no compose stack).
#
# "Single-container" / host-socket mode: one container per service, port-mapped
# on localhost. Avoids docker-compose bridge networks that break on some GPU
# tenants while still using the host Docker daemon's GPU injection.
#
# Use when detect_gpu_space.sh reports host_socket_gpu_ok or bare_gpu_ok with
# MINER_INFERENCE_DEPLOY=run.
#
# Usage:
#   ./violet/miner/inference_run_install.sh              # both (from MINER_SERVICES)
#   ./violet/miner/inference_run_install.sh stt
#   ./violet/miner/inference_run_install.sh tts
#   GPU_PLAN_MODE=stt ./violet/miner/inference_run_install.sh stt
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=gpu_env.sh
source "${SCRIPT_DIR}/gpu_env.sh"
# shellcheck source=install_lib.sh
INSTALL_LOG_PREFIX="inference_run"
source "${SCRIPT_DIR}/install_lib.sh"

load_repo_dotenv "${SCRIPT_DIR}" || true

TARGET="${1:-both}"
ETOIL_HOST_PORT="${ETOIL_HOST_PORT:-${ASR_PORT:-9090}}"
SPEACHES_HOST_PORT="${SPEACHES_HOST_PORT_BASE:-9000}"
TTS_HOST_PORT="${TTS_HOST_PORT:-${HOST_PORT:-${TTS_PORT:-8002}}}"
SPEACHES_MODEL="${SPEACHES_TRANSCRIPTION_MODEL:-Achuka/etoil-whisper-stt}"
TTS_IMAGE="${TTS_IMAGE:-simonallanachuka/spark-tts-frontend:latest}"
TTS_CONTAINER="${TTS_CONTAINER_NAME:-spark-tts-frontend}"
HF_TOKEN="$(default_hf_token)"
STT_FORCE_CPU="${STT_FORCE_CPU:-0}"

log()  { echo -e "\033[1;32m[inference_run]\033[0m $*"; }
warn() { echo -e "\033[1;33m[inference_run warn]\033[0m $*"; }
err()  { echo -e "\033[1;31m[inference_run error]\033[0m $*" >&2; }

SUDO=""
[[ ${EUID} -ne 0 ]] && SUDO="sudo"

require_docker_usable || exit 1

want_stt=0 want_tts=0
case "$TARGET" in
  stt|asr) want_stt=1 ;;
  tts) want_tts=1 ;;
  both|all)
    want_stt=1
    want_tts=1
    ;;
  *)
    err "unknown target: $TARGET (use stt, tts, or both)"
    exit 2
    ;;
esac

if [[ "${STT_FORCE_CPU}" == "1" ]]; then
  want_stt=1
else
  plan_gpu_devices "${GPU_PLAN_MODE:-both}"
fi

run_cuda_gate() {
  if [[ "${STT_FORCE_CPU}" == "1" ]]; then
    warn "STT_FORCE_CPU=1 — skipping CUDA gate for STT"
    return 0
  fi
  classify_gpu_space
  case "$GPU_SPACE_CLASS" in
    bare_gpu_ok|host_socket_gpu_ok|shell_only_gpu) return 0 ;;
    nvml_only)
      err "NVML-only GPU space — CUDA compute blocked. Run: ./violet/miner/detect_gpu_space.sh"
      exit 1
      ;;
    *)
      err "No viable GPU for inference."
      exit 1
      ;;
  esac
}

speaches_image() {
  if [[ "${STT_FORCE_CPU}" == "1" ]]; then
    echo "ghcr.io/speaches-ai/speaches:latest-cpu"
  else
    echo "ghcr.io/speaches-ai/speaches:latest-cuda"
  fi
}

install_stt_run() {
  local img vol_speaches vol_etoil net="cathedral-inference"
  img="$(speaches_image)"
  vol_speaches="cathedral-speaches-hf"
  vol_etoil="cathedral-etoil-audio"

  docker network create "$net" >/dev/null 2>&1 || true
  docker volume create "$vol_speaches" >/dev/null 2>&1 || true
  docker volume create "$vol_etoil" >/dev/null 2>&1 || true

  log "Pulling ${img} and simonallanachuka/etoil-api:latest"
  docker pull "$img"
  docker pull simonallanachuka/etoil-api:latest

  docker rm -f cathedral-speaches cathedral-etoil-api 2>/dev/null || true

  local -a run_cmd=(docker run -d --name cathedral-speaches --restart unless-stopped --network "$net")
  if [[ "${STT_FORCE_CPU}" != "1" ]]; then
    run_cmd+=(--gpus "device=${STT_GPU_DEVICES:-0}")
  fi
  run_cmd+=(
    -p "127.0.0.1:${SPEACHES_HOST_PORT}:8000"
    -v "${vol_speaches}:/home/ubuntu/.cache/huggingface/hub"
    -e HF_TOKEN="${HF_TOKEN}"
    -e NVIDIA_DRIVER_CAPABILITIES=compute,utility
  )
  if [[ "${STT_FORCE_CPU}" == "1" ]]; then
    run_cmd+=(-e WHISPER__INFERENCE_DEVICE=cpu -e WHISPER__CPU_THREADS="${STT_CPU_THREADS:-4}")
  fi
  run_cmd+=("$img")

  log "Starting cathedral-speaches (run mode, :${SPEACHES_HOST_PORT})"
  ${SUDO} "${run_cmd[@]}"

  wait_http_ok "speaches" "http://127.0.0.1:${SPEACHES_HOST_PORT}/health" 120 cathedral-speaches

  log "Starting cathedral-etoil-api (run mode, :${ETOIL_HOST_PORT})"
  ${SUDO} docker run -d \
    --name cathedral-etoil-api \
    --restart unless-stopped \
    --network "$net" \
    # Bind 0.0.0.0 (not 127.0.0.1) so the miner sidecar can reach etoil via
    # host.docker.internal on Linux Docker.
    -p "${ETOIL_HOST_PORT}:8000" \
    -v "${vol_etoil}:/app/audio" \
    -e EXTERNAL_API_URL="http://cathedral-speaches:8000" \
    -e SPEACHES_BASE_URL="http://cathedral-speaches:8000" \
    -e SPEACHES_API_KEY=empty \
    -e SPEACHES_TRANSCRIPTION_MODEL="${SPEACHES_MODEL}" \
    -e SPEACHES_OPEN_TIMEOUT=60 \
    -e PYTHONUNBUFFERED=1 \
    simonallanachuka/etoil-api:latest

  wait_http_ok "etoil-api" "http://127.0.0.1:${ETOIL_HOST_PORT}/health" 300 cathedral-etoil-api

  local tmp wav code
  tmp="$(mktemp -d)"
  wav="${tmp}/tone.wav"
  if make_smoke_wav "$wav"; then
    code="$(curl -sS -o /tmp/inference_run_stt.json -w '%{http_code}' --max-time 300 \
      -F "file=@${wav};type=audio/wav" \
      -F "language=eng" \
      -F "response_format=json" \
      "http://127.0.0.1:${ETOIL_HOST_PORT}/transcribe" 2>/dev/null || echo "000")"
    if [[ "$code" == "200" ]]; then
      log "STT smoke: POST /transcribe → 200"
    else
      warn "STT smoke returned HTTP ${code}"
      docker logs --tail=40 cathedral-speaches 2>/dev/null || true
    fi
  fi
  rm -rf "$tmp"

  log "STT run-mode ready: http://127.0.0.1:${ETOIL_HOST_PORT}"
}

install_tts_run() {
  local gpu_dev="${TTS_GPU_DEVICES:-0}"
  local gpu_dev_first="${gpu_dev%%,*}"

  log "Pulling ${TTS_IMAGE}"
  docker pull "${TTS_IMAGE}"

  docker rm -f "${TTS_CONTAINER}" spark-tts-streaming 2>/dev/null || true

  log "Starting ${TTS_CONTAINER} (run mode, :${TTS_HOST_PORT})"
  ${SUDO} docker run -d \
    --name "${TTS_CONTAINER}" \
    --restart unless-stopped \
    --gpus "device=${gpu_dev_first}" \
    -p "127.0.0.1:${TTS_HOST_PORT}:8002" \
    -e CUDA_VISIBLE_DEVICES="${gpu_dev_first}" \
    -e NVIDIA_VISIBLE_DEVICES="${gpu_dev_first}" \
    -e NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    -e PORT=8002 \
    --shm-size=4gb \
    "${TTS_IMAGE}"

  wait_speech_ready "${TTS_HOST_PORT}" 180 "${TTS_CONTAINER}" || {
    docker logs --tail=60 "${TTS_CONTAINER}" 2>/dev/null || true
    exit 1
  }
  log "TTS run-mode ready: http://127.0.0.1:${TTS_HOST_PORT}"
}

main() {
  log "Single-container inference install (docker run, no compose)"
  if is_nested_docker; then
    warn "Nested shell detected — run mode uses host Docker socket (not inner dockerd)."
  fi

  (( want_stt || want_tts )) || { err "nothing to install"; exit 2; }

  if (( want_stt )); then
    if [[ "${STT_FORCE_CPU}" != "1" ]]; then
      run_cuda_gate
    fi
    install_stt_run
  fi

  if (( want_tts )); then
    if [[ "${STT_FORCE_CPU}" != "1" ]]; then
      run_cuda_gate
    fi
    install_tts_run
  fi

  log "Done. Set MINER_INFERENCE_DEPLOY=run in .env for start.sh to reuse this path."
}

main "$@"
