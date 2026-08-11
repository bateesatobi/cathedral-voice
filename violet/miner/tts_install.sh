#!/usr/bin/env bash
#
# tts_install.sh — Install & run cathedral-voice TTS (Spark-TTS frontend).
#
# GPU rules (via gpu_env.sh / GPU_PLAN_MODE):
#   tts  (default when run alone) — ALL host GPUs for TTS; none idle
#   both — only the TTS partition; share GPU 0 when N=1
#
# MODEL_POOL_SIZE defaults to assigned GPU count so every card holds work.
# When sharing a single GPU with STT (plan=both, N=1), VRAM util is lowered.
#
# Usage:
#   ./violet/miner/tts_install.sh
#   GPU_PLAN_MODE=both TTS_GPU_DEVICES=2,3 ./violet/miner/tts_install.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=gpu_env.sh
source "${SCRIPT_DIR}/gpu_env.sh"

DEFAULT_HF_TOKEN="${HF_TOKEN:-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}"
IMAGE_TAG="${TTS_IMAGE:-simonallanachuka/spark-tts-frontend:latest}"
CONTAINER_NAME="${TTS_CONTAINER_NAME:-cathedral-spark-tts}"
TTS_HOST_PORT="${TTS_HOST_PORT:-8002}"
MODEL_NAME="${MODEL_NAME:-phosai/phosai_tts_v1}"
TOKENIZER_REPO="${TOKENIZER_REPO:-phosai/phosai_tts_v1}"
SPARK_TTS_DTYPE="${SPARK_TTS_DTYPE:-f32}"
# Prefer a named Docker volume so nested Docker (shell in container, daemon on
# host) does not fail with "no such file or directory" on bind mounts.
MODELS_VOLUME="${TTS_MODELS_VOLUME:-cathedral-tts-models}"
MODELS_DIR="${TTS_MODELS_DIR:-}"  # optional host bind; empty → use MODELS_VOLUME
TTS_READY_TRIES="${TTS_READY_TRIES:-900}"  # ~30 min first pull
GPU_PLAN_MODE="${GPU_PLAN_MODE:-tts}"
SHM_SIZE="${TTS_SHM_SIZE:-4gb}"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[tts_install]${NC} $*"; }
warn() { echo -e "${YELLOW}[tts_install warn]${NC} $*"; }
err()  { echo -e "${RED}[tts_install error]${NC} $*" >&2; }

wait_http() {
  local name="$1" url="$2" tries="${3:-90}"
  log "Waiting for ${name} (${url}) up to $((tries * 2))s..."
  local i
  for ((i = 1; i <= tries; i++)); do
    if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then
      log "${name} is up"
      return 0
    fi
    if (( i % 20 == 0 )); then
      log "  still waiting (${i}/${tries}) — model download / load may be in progress"
      sudo docker logs --tail=15 "${CONTAINER_NAME}" 2>/dev/null || true
    fi
    sleep 2
  done
  err "${name} did not become ready: ${url}"
  sudo docker logs --tail=100 "${CONTAINER_NAME}" || true
  return 1
}

smoke_tts() {
  local base="http://127.0.0.1:${TTS_HOST_PORT}"
  log "Contract smoke: GET ${base}/health"
  curl -fsS --max-time 10 "${base}/health" >/dev/null

  local code
  code="$(curl -sS -o /tmp/tts_voices.json -w '%{http_code}' --max-time 30 \
    "${base}/v1/audio/voices" 2>/dev/null || echo "000")"
  if [[ "$code" == "200" ]]; then
    log "Contract smoke: GET /v1/audio/voices → 200"
  else
    warn "GET /v1/audio/voices → HTTP ${code}"
  fi

  code="$(curl -sS -o /tmp/tts_smoke.pcm -w '%{http_code}' --max-time 180 \
    -H 'Content-Type: application/json' \
    -d '{"text":"Hello cathedral voice smoke test.","speaker_id":"eng_female_1","temperature":0.7}' \
    "${base}/v1/audio/speech/stream" 2>/dev/null || echo "000")"
  if [[ "$code" == "200" ]]; then
    local bytes
    bytes="$(wc -c </tmp/tts_smoke.pcm | tr -d ' ')"
    log "Contract smoke: POST /v1/audio/speech/stream → 200 (${bytes} bytes)"
  else
    warn "Speech stream smoke → HTTP ${code} (health OK; check model if scoring fails)"
  fi
}

install_docker() {
  if command -v docker &>/dev/null; then
    log "Docker already installed: $(docker --version)"
    return 0
  fi
  log "Installing Docker..."
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  sudo sh /tmp/get-docker.sh
  rm -f /tmp/get-docker.sh
  sudo usermod -aG docker "${USER}" || true
}

main() {
  plan_gpu_devices "${GPU_PLAN_MODE}"
  local devices="${TTS_GPU_DEVICES}"
  local n
  n="$(gpu_device_count "$devices")"
  if [[ -z "$devices" || "$n" -lt 1 ]]; then
    err "No GPUs assigned to TTS. Install NVIDIA drivers or set TTS_GPU_DEVICES."
    exit 1
  fi

  if [[ -n "${MODELS_DIR}" ]]; then
    check_disk_gb 40 "${MODELS_DIR}" || warn "Low disk — TTS model pulls may fail"
  else
    check_disk_gb 40 / || warn "Low disk — TTS model pulls may fail"
  fi

  local pool="${MODEL_POOL_SIZE:-$n}"
  # Shared single-GPU with STT: leave headroom. Solo / dedicated cards: use more VRAM.
  local mem_util="${GPU_MEMORY_UTILIZATION:-}"
  if [[ -z "$mem_util" ]]; then
    if [[ "$GPU_PLAN_MODE" == "both" && "$n" -eq 1 && "$GPU_COUNT" -eq 1 ]]; then
      mem_util="0.35"
      warn "Single-GPU host sharing STT+TTS — GPU_MEMORY_UTILIZATION=${mem_util}"
    else
      mem_util="0.85"
    fi
  fi

  log "TTS GPUs: ${devices} (pool=${pool}, mem_util=${mem_util}, plan=${GPU_PLAN_MODE})"

  if [[ -f /.dockerenv ]] && [[ -z "${TTS_ALLOW_NESTED_DOCKER:-}" ]]; then
    warn "Shell appears to be inside a container (/.dockerenv)."
    warn "Run tts_install on the GPU host VM when possible."
    warn "Using named volume '${MODELS_VOLUME}' for /app/models (avoids bind-mount DinD failures)."
    warn "Set TTS_ALLOW_NESTED_DOCKER=1 to silence this warning."
  fi

  install_docker
  install_nvidia_toolkit_if_needed "sudo"

  local models_mount
  if [[ -n "${MODELS_DIR}" ]]; then
    mkdir -p "${MODELS_DIR}"
    models_mount="${MODELS_DIR}:/app/models"
    log "Models bind mount: ${MODELS_DIR}"
  else
    models_mount="${MODELS_VOLUME}:/app/models"
    log "Models named volume: ${MODELS_VOLUME}"
  fi

  log "Pulling ${IMAGE_TAG}..."
  sudo docker pull "${IMAGE_TAG}"

  if sudo docker ps -a --format '{{.Names}}' | grep -Eq "^${CONTAINER_NAME}\$"; then
    warn "Replacing existing container ${CONTAINER_NAME}"
    sudo docker stop "${CONTAINER_NAME}" || true
    sudo docker rm "${CONTAINER_NAME}" || true
  fi

  local cuda_inside
  cuda_inside="$(gpu_index_list "$n")"

  log "Starting ${CONTAINER_NAME} on host port ${TTS_HOST_PORT}..."
  sudo docker run -d \
    --name "${CONTAINER_NAME}" \
    --restart unless-stopped \
    --gpus "device=${devices}" \
    -p "${TTS_HOST_PORT}:8002" \
    -e HF_TOKEN="${DEFAULT_HF_TOKEN}" \
    -e MODEL_NAME="${MODEL_NAME}" \
    -e TOKENIZER_REPO="${TOKENIZER_REPO}" \
    -e NVIDIA_VISIBLE_DEVICES="${devices}" \
    -e CUDA_VISIBLE_DEVICES="${cuda_inside}" \
    -e MODEL_POOL_SIZE="${pool}" \
    -e SPARK_TTS_DTYPE="${SPARK_TTS_DTYPE}" \
    -e GPU_MEMORY_UTILIZATION="${mem_util}" \
    -e PORT=8002 \
    -v "${models_mount}" \
    -v cathedral_spark_hf_cache:/root/.cache/huggingface \
    --shm-size="${SHM_SIZE}" \
    "${IMAGE_TAG}"

  wait_http "spark-tts" "http://127.0.0.1:${TTS_HOST_PORT}/health" "${TTS_READY_TRIES}"
  smoke_tts || true

  log "Done."
  echo "  TTS API         : http://127.0.0.1:${TTS_HOST_PORT}"
  echo "  Miner upstream  : MINER_TTS_UPSTREAM=http://127.0.0.1:${TTS_HOST_PORT}"
  echo "  TTS GPUs        : ${TTS_GPU_DEVICES}"
  echo "  Health          : curl -fsS http://127.0.0.1:${TTS_HOST_PORT}/health"
  echo "  Stream test     : TTS_URL=http://127.0.0.1:${TTS_HOST_PORT} python ${SCRIPT_DIR}/tts_test_stream.py"
  echo "  Logs            : sudo docker logs -f ${CONTAINER_NAME}"
}

main "$@"
