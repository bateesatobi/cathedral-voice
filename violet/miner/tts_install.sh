#!/bin/bash
# ==============================================================================
# Spark-TTS VM Deployment Script (miner TTS installer)
# Installs Docker + NVIDIA Container Toolkit if needed, pulls Spark-TTS, runs it.
#
# Models mount:
#   Default = Docker named volume (works under nested Docker / docker.sock).
#   Optional host bind: TTS_MODELS_DIR=/absolute/host/path
#   Optional local bind: TTS_USE_BIND_MOUNT=1 (fails if path only exists in a shell container)
# ==============================================================================

set -euo pipefail

# Miner start.sh compatibility
if [[ -z "${HOST_PORT:-}" ]]; then
  HOST_PORT="${TTS_HOST_PORT:-${TTS_PORT:-}}"
fi
[[ -n "${HOST_PORT:-}" ]] && export HOST_PORT
if [[ -z "${CUDA_VISIBLE_DEVICES:-}" && -n "${TTS_GPU_DEVICES:-}" ]]; then
  export CUDA_VISIBLE_DEVICES="${TTS_GPU_DEVICES}"
fi

export DEBIAN_FRONTEND=noninteractive

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

if [[ "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* || "$(uname -s 2>/dev/null || echo)" == MINGW* || "$(uname -s 2>/dev/null || echo)" == MSYS* ]]; then
    printf '%b\n' "${RED}[ERROR] $(date +'%Y-%m-%d %H:%M:%S')${NC} This deployment script is designed for Linux/WSL Ubuntu, not Git Bash or MinGW."
    exit 1
fi

if [ "$(id -u 2>/dev/null || echo 0)" -eq 0 ]; then
    SUDO_CMD=""
else
    if ! command -v sudo >/dev/null 2>&1; then
        echo -e "${RED}[ERROR] $(date +'%Y-%m-%d %H:%M:%S')${NC} sudo is not available. Run as root or install sudo."
        exit 1
    fi
    SUDO_CMD="sudo"
fi

log_info() { echo -e "${GREEN}[INFO] $(date +'%Y-%m-%d %H:%M:%S')${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN] $(date +'%Y-%m-%d %H:%M:%S')${NC} $1"; }
log_error() { echo -e "${RED}[ERROR] $(date +'%Y-%m-%d %H:%M:%S')${NC} $1"; }

log_info "Verifying system compatibility..."
if [ -f /etc/os-release ]; then
    . /etc/os-release
    log_info "Detected OS: $NAME $VERSION"
    if [[ "$ID" != "ubuntu" && "$ID" != "debian" ]]; then
        log_warn "This script is optimized for Ubuntu/Debian. It may fail or require manual steps on other distributions."
    fi
else
    log_warn "Could not detect OS version. Proceeding anyway..."
fi

log_info "Checking for NVIDIA GPU..."
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
    log_info "NVIDIA GPU detected via nvidia-smi."
elif command -v lspci >/dev/null 2>&1 && lspci 2>/dev/null | grep -qi nvidia; then
    log_info "NVIDIA GPU detected via lspci."
else
    log_warn "No GPU via nvidia-smi/lspci (common inside shell containers). Continuing; --gpus all may still work."
fi

if ! command -v docker &> /dev/null; then
    log_info "Docker is not installed. Installing Docker..."
    ${SUDO_CMD} apt-get update
    ${SUDO_CMD} apt-get install -y -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" ca-certificates curl gnupg lsb-release
    ${SUDO_CMD} mkdir -p /etc/apt/keyrings
    if [ ! -f /etc/apt/keyrings/docker.gpg ]; then
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg | ${SUDO_CMD} gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    fi
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | ${SUDO_CMD} tee /etc/apt/sources.list.d/docker.list > /dev/null
    ${SUDO_CMD} apt-get update
    ${SUDO_CMD} apt-get install -y -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    ${SUDO_CMD} usermod -aG docker "$USER" || true
    log_info "Docker successfully installed."
else
    log_info "Docker is already installed: $(docker --version)"
fi

if ! command -v nvidia-ctk &> /dev/null; then
    log_info "NVIDIA Container Toolkit is not installed. Setting up repositories and installing..."
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | ${SUDO_CMD} gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
      sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
      ${SUDO_CMD} tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
    ${SUDO_CMD} apt-get update
    ${SUDO_CMD} apt-get install -y -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" nvidia-container-toolkit
    log_info "Configuring NVIDIA Container Toolkit for Docker..."
    ${SUDO_CMD} nvidia-ctk runtime configure --runtime=docker
    ${SUDO_CMD} systemctl restart docker
    log_info "NVIDIA Container Toolkit successfully installed and configured."
else
    log_info "NVIDIA Container Toolkit is already installed: $(nvidia-ctk --version)"
fi

log_info "Configuring environment variables..."
HF_TOKEN="${HF_TOKEN:-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}"
MODEL_NAME="${MODEL_NAME:-phosai/phosai_tts_v1}"
TOKENIZER_REPO="${TOKENIZER_REPO:-phosai/phosai_tts_v1}"
MODEL_POOL_SIZE="${MODEL_POOL_SIZE:-1}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.5}"
SPARK_TTS_DTYPE="${SPARK_TTS_DTYPE:-f32}"
CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
VLLM_BACKEND_URL="${VLLM_BACKEND_URL:-http://localhost:8000}"
RUST_LOG="${RUST_LOG:-info}"
HOST_PORT="${HOST_PORT:-8002}"
APP_PORT="${APP_PORT:-8002}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# violet/miner -> violet-subnet repo root
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MODELS_VOLUME="${TTS_MODELS_VOLUME:-cathedral-tts-models}"
MODELS_DIR="${TTS_MODELS_DIR:-}"

log_info "Preparing models storage..."
# Default: named volume (required when shell is in a container talking to host docker).
if [[ -n "${MODELS_DIR}" ]]; then
    mkdir -p "${MODELS_DIR}/Spark-TTS-0.5B"
    MODELS_MOUNT="${MODELS_DIR}:/app/models"
    log_info "Using host bind mount TTS_MODELS_DIR=${MODELS_DIR}"
elif [[ "${TTS_USE_BIND_MOUNT:-0}" == "1" ]]; then
    mkdir -p "${REPO_ROOT}/models/Spark-TTS-0.5B"
    MODELS_MOUNT="${REPO_ROOT}/models:/app/models"
    log_warn "TTS_USE_BIND_MOUNT=1 — bind ${REPO_ROOT}/models (fails under nested Docker if path is not on host)"
else
    ${SUDO_CMD} docker volume create "${MODELS_VOLUME}" >/dev/null
    MODELS_MOUNT="${MODELS_VOLUME}:/app/models"
    if [[ -f /.dockerenv ]]; then
        log_warn "Nested Docker (/.dockerenv) — using named volume ${MODELS_VOLUME}"
    else
        log_info "Using named volume ${MODELS_VOLUME} (set TTS_MODELS_DIR or TTS_USE_BIND_MOUNT=1 to override)"
    fi
fi

IMAGE_TAG="simonallanachuka/spark-tts-frontend:latest"
log_info "Pulling Docker image: $IMAGE_TAG..."
${SUDO_CMD} docker pull "$IMAGE_TAG"

log_info "Starting Spark-TTS container..."
if ${SUDO_CMD} docker ps -a --format '{{.Names}}' | grep -Eq "^spark-tts-frontend$"; then
    log_warn "Existing container 'spark-tts-frontend' found. Stopping and removing..."
    ${SUDO_CMD} docker stop spark-tts-frontend || true
    ${SUDO_CMD} docker rm spark-tts-frontend || true
fi
if ${SUDO_CMD} docker ps -a --format '{{.Names}}' | grep -Eq "^cathedral-spark-tts$"; then
    log_warn "Removing legacy container cathedral-spark-tts..."
    ${SUDO_CMD} docker stop cathedral-spark-tts || true
    ${SUDO_CMD} docker rm cathedral-spark-tts || true
fi

${SUDO_CMD} docker run -d \
  --name spark-tts-frontend \
  --restart unless-stopped \
  --gpus all \
  -p "${HOST_PORT}:${APP_PORT}" \
  -e HF_TOKEN="$HF_TOKEN" \
  -e MODEL_NAME="$MODEL_NAME" \
  -e TOKENIZER_REPO="$TOKENIZER_REPO" \
  -e CUDA_VISIBLE_DEVICES="$CUDA_VISIBLE_DEVICES" \
  -e NVIDIA_VISIBLE_DEVICES="all" \
  -e NVIDIA_DRIVER_CAPABILITIES="compute,utility" \
  -e VLLM_BACKEND_URL="$VLLM_BACKEND_URL" \
  -e MODEL_DIR="/app/models/Spark-TTS-0.5B" \
  -e PORT="${APP_PORT}" \
  -e RUST_LOG="$RUST_LOG" \
  -e GPU_MEMORY_UTILIZATION="$GPU_MEMORY_UTILIZATION" \
  -e MODEL_POOL_SIZE="$MODEL_POOL_SIZE" \
  -e SPARK_TTS_DTYPE="$SPARK_TTS_DTYPE" \
  -v "${MODELS_MOUNT}" \
  -v hf_cache:/root/.cache/huggingface \
  --shm-size=4gb \
  "$IMAGE_TAG"

log_info "Container started successfully! It is running in the background."
log_info "You can view logs using: ${SUDO_CMD} docker logs -f spark-tts-frontend"
log_info "Spark-TTS frontend is listening on port ${HOST_PORT}."
log_info "Models mount: ${MODELS_MOUNT}"
