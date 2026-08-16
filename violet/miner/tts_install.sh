#!/bin/bash
# ==============================================================================
# Spark-TTS VM Deployment Script
# This script installs Docker, NVIDIA Container Toolkit, pulls the Spark-TTS
# frontend image, and runs it on a GPU-enabled VM.
# ==============================================================================

set -euo pipefail

# Miner start.sh compatibility: map prior installer env names onto this script.
if [[ -z "${HOST_PORT:-}" ]]; then
  HOST_PORT="${TTS_HOST_PORT:-${TTS_PORT:-}}"
fi
if [[ -n "${HOST_PORT:-}" ]]; then
  export HOST_PORT
fi
if [[ -z "${CUDA_VISIBLE_DEVICES:-}" && -n "${TTS_GPU_DEVICES:-}" ]]; then
  export CUDA_VISIBLE_DEVICES="${TTS_GPU_DEVICES}"
fi

# Force non-interactive apt-get installs to bypass prompt questions on clean VMs
export DEBIAN_FRONTEND=noninteractive

# Define Colors for Output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Validate that this is being run from Linux/WSL, not Git Bash/MinGW.
if [[ "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* || "$(uname -s 2>/dev/null || echo)" == MINGW* || "$(uname -s 2>/dev/null || echo)" == MSYS* ]]; then
    printf '%b\n' "${RED}[ERROR] $(date +'%Y-%m-%d %H:%M:%S')${NC} This deployment script is designed for Linux/WSL Ubuntu, not Git Bash or MinGW."
    printf '%b\n' "${YELLOW}[INFO] $(date +'%Y-%m-%d %H:%M:%S')${NC} Open WSL2 and run:"
    echo "  wsl --install -d Ubuntu"
    echo "  cd /path/to/deploy-tts-rs"
    echo "  sudo ./deploy_vm.sh"
    exit 1
fi

if [ "$(id -u 2>/dev/null || echo 0)" -eq 0 ]; then
    SUDO_CMD=""
else
    if ! command -v sudo >/dev/null 2>&1; then
        echo -e "${RED}[ERROR] $(date +'%Y-%m-%d %H:%M:%S')${NC} sudo is not available on this machine. Run this script inside WSL2 Ubuntu or as root on Linux."
        exit 1
    fi
    SUDO_CMD="sudo"
fi

log_info() {
    echo -e "${GREEN}[INFO] $(date +'%Y-%m-%d %H:%M:%S')${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN] $(date +'%Y-%m-%d %H:%M:%S')${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR] $(date +'%Y-%m-%d %H:%M:%S')${NC} $1"
}

# 1. Check System Compatibility (Ubuntu/Debian recommended)
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

# 2. Check for GPU presence
log_info "Checking for NVIDIA GPU..."
if lspci | grep -i nvidia > /dev/null; then
    log_info "NVIDIA GPU detected."
else
    log_warn "No NVIDIA GPU detected via lspci. If this is a VM, make sure GPU passthrough is configured."
fi

# 3. Install Docker if not present
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
    
    # Configure docker to run without sudo
    ${SUDO_CMD} usermod -aG docker "$USER" || true
    log_info "Docker successfully installed."
else
    log_info "Docker is already installed: $(docker --version)"
fi

# 4. Install NVIDIA Container Toolkit if not present
if ! command -v nvidia-ctk &> /dev/null; then
    log_info "NVIDIA Container Toolkit is not installed. Setting up repositories and installing..."
    
    # Add NVIDIA Container Toolkit repository
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

# 5. Configure environment variables (fully automated)
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

# 6. Ensure models directory exists
log_info "Preparing local models directory..."
mkdir -p "$PWD/models/Spark-TTS-0.5B"

# Pre-download tokenizer.json on the host from the specified TOKENIZER_REPO to avoid container curl issues
if [ ! -f "$PWD/models/Spark-TTS-0.5B/tokenizer.json" ]; then
    log_info "Pre-downloading tokenizer.json from $TOKENIZER_REPO..."
    if [ -n "$HF_TOKEN" ]; then
        curl -H "Authorization: Bearer $HF_TOKEN" -L -sS "https://huggingface.co/${TOKENIZER_REPO}/resolve/main/tokenizer.json" -o "$PWD/models/Spark-TTS-0.5B/tokenizer.json" || true
    else
        curl -L -sS "https://huggingface.co/${TOKENIZER_REPO}/resolve/main/tokenizer.json" -o "$PWD/models/Spark-TTS-0.5B/tokenizer.json" || true
    fi
fi

# 7. Pull the Spark-TTS Docker image
IMAGE_TAG="simonallanachuka/spark-tts-frontend:latest"
log_info "Pulling Docker image: $IMAGE_TAG..."
${SUDO_CMD} docker pull "$IMAGE_TAG"

# 8. Run the Container
log_info "Starting Spark-TTS container..."

# Remove old container if it exists
if ${SUDO_CMD} docker ps -a --format '{{.Names}}' | grep -Eq "^spark-tts-frontend$"; then
    log_warn "Existing container 'spark-tts-frontend' found. Stopping and removing..."
    ${SUDO_CMD} docker stop spark-tts-frontend || true
    ${SUDO_CMD} docker rm spark-tts-frontend || true
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
  -v "$PWD/models:/app/models" \
  -v hf_cache:/root/.cache/huggingface \
  --shm-size=4gb \
  "$IMAGE_TAG"

log_info "Container started successfully! It is running in the background."
log_info "You can view logs using: ${SUDO_CMD} docker logs -f spark-tts-frontend"
log_info "Spark-TTS frontend is listening on port ${HOST_PORT} of the VM."
