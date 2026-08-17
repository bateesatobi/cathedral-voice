#!/bin/bash
# ==============================================================================
# Phosai Spark-TTS Streaming Service - Production Deployment Script
# Supports: Bare-metal GPU servers, Cloud VMs, and GPU Container Environments
#
# GPU rules (via gpu_env.sh / GPU_PLAN_MODE) — same as stt_install.sh:
#   tts  (default when run alone) — ALL host GPUs for TTS; none idle
#   both — only the TTS partition from the shared plan (start.sh locks this)
#
# Usage:
#   ./violet/miner/tts_install.sh
#   GPU_PLAN_MODE=both TTS_GPU_DEVICES=2 ./violet/miner/tts_install.sh
# ==============================================================================

set -euo pipefail

# Force non-interactive apt-get installs to bypass prompt questions on clean VMs
export DEBIAN_FRONTEND=noninteractive

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=gpu_env.sh
source "${SCRIPT_DIR}/gpu_env.sh"
# shellcheck source=install_lib.sh
INSTALL_LOG_PREFIX="tts_install"
source "${SCRIPT_DIR}/install_lib.sh"

# Define Colors for Output
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# Validate that this is being run from Linux/WSL
if [[ "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* || "$(uname -s 2>/dev/null || echo)" == MINGW* || "$(uname -s 2>/dev/null || echo)" == MSYS* ]]; then
    printf '%b\n' "${RED}[ERROR] $(date +'%Y-%m-%d %H:%M:%S')${NC} This deployment script is designed for Linux/WSL Ubuntu, not Git Bash or MinGW."
    printf '%b\n' "${YELLOW}[INFO] $(date +'%Y-%m-%d %H:%M:%S')${NC} Run inside Ubuntu/WSL2 or on your remote GPU server:"
    echo "  sudo ./violet/miner/tts_install.sh"
    exit 1
fi

if [ "$(id -u 2>/dev/null || echo 0)" -eq 0 ]; then
    SUDO_CMD=""
else
    if ! command -v sudo >/dev/null 2>&1; then
        echo -e "${RED}[ERROR] $(date +'%Y-%m-%d %H:%M:%S')${NC} sudo is not available. Run this script as root or in a sudo-enabled user environment."
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

log_step() {
    echo -e "\n${CYAN}${BOLD}===> $1${NC}"
}

nested_docker() {
    [[ -f /.dockerenv ]]
}

# Host GPU ids → CUDA_VISIBLE_DEVICES remapped to 0..N-1 inside the container
# (same mapping stt_install.sh uses for speaches).
container_cuda_devices() {
    local devices="$1"
    local n
    n="$(gpu_device_count "$devices")"
    if (( n < 1 )); then
        echo "0"
        return 0
    fi
    gpu_index_list "$n"
}

# --gpus device=0,1  (never "all" when a partition is planned — STT may own the rest)
docker_gpus_flag() {
    local devices="$1"
    if [[ -z "$devices" ]]; then
        echo "all"
        return 0
    fi
    echo "device=${devices}"
}

# ------------------------------------------------------------------------------
# 1. System & GPU Verification
# ------------------------------------------------------------------------------
log_step "Step 1: Verifying System & GPU Compatibility"
if [ -f /etc/os-release ]; then
    . /etc/os-release
    log_info "Detected OS: $NAME $VERSION"
    if [[ "$ID" != "ubuntu" && "$ID" != "debian" ]]; then
        log_warn "This script is optimized for Ubuntu/Debian. Other distributions may require manual adjustments."
    fi
fi

if command -v nvidia-smi &> /dev/null; then
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -n1 || echo "NVIDIA GPU")
    DRIVER_VER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n1 || echo "Unknown")
    log_info "NVIDIA GPU Detected: ${GPU_NAME} (Driver: ${DRIVER_VER})"
    log_info "Host GPU count: $(detect_gpu_count)"
elif lspci 2>/dev/null | grep -i nvidia > /dev/null; then
    log_info "NVIDIA GPU hardware detected via lspci."
else
    log_warn "No NVIDIA GPU detected via nvidia-smi or lspci. Ensure GPU passthrough is enabled."
fi

if nested_docker; then
    warn_nested_docker "TTS_ALLOW_NESTED_DOCKER"
    if command -v nvidia-smi &>/dev/null && [[ "$(detect_gpu_count)" -gt 0 ]]; then
        log_warn "Nested Docker + GPU in this shell. vLLM V1 EngineCore often fails here; using VLLM_USE_V1=0."
        log_warn "STT/speaches works in this setup because it does not fork a vLLM EngineCore."
        export VLLM_USE_V1="${VLLM_USE_V1:-0}"
    else
        log_info "Nested Docker without local GPU — sibling containers use the host Docker daemon GPUs."
    fi
fi

# ------------------------------------------------------------------------------
# 2. Docker Engine Installation
# ------------------------------------------------------------------------------
log_step "Step 2: Checking Docker Engine"
if ! command -v docker &> /dev/null; then
    log_info "Docker not found. Installing Docker Engine..."
    ${SUDO_CMD} apt-get update
    ${SUDO_CMD} apt-get install -y -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" \
        ca-certificates curl gnupg lsb-release

    ${SUDO_CMD} mkdir -p /etc/apt/keyrings
    if [ ! -f /etc/apt/keyrings/docker.gpg ]; then
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg | ${SUDO_CMD} gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    fi

    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | \
        ${SUDO_CMD} tee /etc/apt/sources.list.d/docker.list > /dev/null
    ${SUDO_CMD} apt-get update
    ${SUDO_CMD} apt-get install -y -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    
    if [ -n "${USER:-}" ]; then
        ${SUDO_CMD} usermod -aG docker "$USER" || true
    fi
    log_info "Docker installed successfully."
else
    log_info "Docker is already installed: $(docker --version)"
fi

# ------------------------------------------------------------------------------
# 3. NVIDIA Container Toolkit Installation
# ------------------------------------------------------------------------------
log_step "Step 3: Checking NVIDIA Container Toolkit"
if ! command -v nvidia-ctk &> /dev/null; then
    log_info "NVIDIA Container Toolkit not found. Installing..."
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
        ${SUDO_CMD} gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
        sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
        ${SUDO_CMD} tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

    ${SUDO_CMD} apt-get update
    ${SUDO_CMD} apt-get install -y -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" \
        nvidia-container-toolkit
    
    log_info "Configuring Docker daemon for NVIDIA runtime..."
    ${SUDO_CMD} nvidia-ctk runtime configure --runtime=docker
    ${SUDO_CMD} systemctl restart docker || ${SUDO_CMD} service docker restart || true
    log_info "NVIDIA Container Toolkit installed and configured."
else
    log_info "NVIDIA Container Toolkit is already installed: $(nvidia-ctk --version)"
fi
install_nvidia_toolkit_if_needed "${SUDO_CMD}"

# ------------------------------------------------------------------------------
# 4. Configuration & Deployment Parameters
# ------------------------------------------------------------------------------
log_step "Step 4: Configuring Service Environment"

# start.sh compatibility
HOST_PORT="${TTS_HOST_PORT:-${HOST_PORT:-${TTS_PORT:-8002}}}"
APP_PORT="${APP_PORT:-8002}"
IMAGE_TAG="${IMAGE_TAG:-simonallanachuka/spark-tts-streaming:latest}"
CONTAINER_NAME="${CONTAINER_NAME:-${TTS_CONTAINER_NAME:-spark-tts-streaming}}"
MODEL_NAME="${MODEL_NAME:-phosai/phosai_tts_v1}"
GPU_PLAN_MODE="${GPU_PLAN_MODE:-tts}"

# Dynamic GPU assignment (solo TTS → all cards; both → TTS partition).
plan_gpu_devices "${GPU_PLAN_MODE}"
if [[ -z "${TTS_GPU_DEVICES:-}" ]]; then
    log_warn "No TTS GPU partition from plan (count=$(detect_gpu_count)). Falling back to GPU 0 / --gpus all."
    TTS_GPU_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
fi
export TTS_GPU_DEVICES

NVIDIA_VISIBLE_DEVICES="${TTS_GPU_DEVICES}"
CUDA_VISIBLE_DEVICES="$(container_cuda_devices "${TTS_GPU_DEVICES}")"
GPUS_SPEC="$(docker_gpus_flag "${TTS_GPU_DEVICES}")"

log_info "Image:              ${IMAGE_TAG}"
log_info "Container Name:     ${CONTAINER_NAME}"
log_info "Model:              ${MODEL_NAME}"
log_info "Port Mapping:       ${HOST_PORT} -> ${APP_PORT}"
log_info "GPU plan mode:      ${GPU_PLAN_MODE} (host count=$(detect_gpu_count))"
log_info "TTS host GPUs:      ${TTS_GPU_DEVICES}  (--gpus ${GPUS_SPEC})"
log_info "NVIDIA_VISIBLE:     ${NVIDIA_VISIBLE_DEVICES}"
log_info "CUDA inside ctr:    ${CUDA_VISIBLE_DEVICES}"

# ------------------------------------------------------------------------------
# 5. Pull Docker Image
# ------------------------------------------------------------------------------
log_step "Step 5: Pulling Docker Image (${IMAGE_TAG})"
${SUDO_CMD} docker pull "${IMAGE_TAG}"

# ------------------------------------------------------------------------------
# 6. Stop and Remove Previous Container
# ------------------------------------------------------------------------------
log_step "Step 6: Managing Container State"
if ${SUDO_CMD} docker ps -a --format '{{.Names}}' | grep -Eq "^${CONTAINER_NAME}$"; then
    log_warn "Found existing container '${CONTAINER_NAME}'. Stopping and removing..."
    ${SUDO_CMD} docker stop "${CONTAINER_NAME}" >/dev/null 2>&1 || true
    ${SUDO_CMD} docker rm "${CONTAINER_NAME}" >/dev/null 2>&1 || true
fi
for legacy in etoil-tts spark-tts-frontend cathedral-spark-tts; do
    if ${SUDO_CMD} docker ps -a --format '{{.Names}}' | grep -Eq "^${legacy}$"; then
        log_warn "Removing leftover TTS container '${legacy}'..."
        ${SUDO_CMD} docker rm -f "${legacy}" >/dev/null 2>&1 || true
    fi
done

# Named volume only (DinD-safe; host daemon cannot see shell bind paths).
${SUDO_CMD} docker volume create hf_cache >/dev/null 2>&1 || true

# ------------------------------------------------------------------------------
# 7. Run Container
# ------------------------------------------------------------------------------
log_step "Step 7: Launching Container with GPU Acceleration"
DOCKER_ENV_EXTRA=()
if [[ -n "${VLLM_USE_V1:-}" ]]; then
    DOCKER_ENV_EXTRA+=(-e "VLLM_USE_V1=${VLLM_USE_V1}")
fi
${SUDO_CMD} docker run -d \
  --name "${CONTAINER_NAME}" \
  --restart unless-stopped \
  --gpus "${GPUS_SPEC}" \
  --ipc=host \
  --shm-size=4gb \
  -p "${HOST_PORT}:${APP_PORT}" \
  -e MODEL_NAME="${MODEL_NAME}" \
  -e CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES}" \
  -e NVIDIA_VISIBLE_DEVICES="${NVIDIA_VISIBLE_DEVICES}" \
  -e NVIDIA_DRIVER_CAPABILITIES="compute,utility" \
  -e VLLM_WORKER_MULTIPROC_METHOD=spawn \
  -e HOST="0.0.0.0" \
  -e PORT="${APP_PORT}" \
  "${DOCKER_ENV_EXTRA[@]}" \
  -v hf_cache:/root/.cache/huggingface \
  "${IMAGE_TAG}"

# ------------------------------------------------------------------------------
# 8. Healthcheck & Verification
# ------------------------------------------------------------------------------
log_step "Step 8: Waiting for Service Health"
MAX_RETRIES="${TTS_READY_TRIES:-180}"
RETRY_COUNT=0
HEALTHY=false

echo -n "Waiting for Spark-TTS to initialize..."
while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if curl -s -f "http://localhost:${HOST_PORT}/" >/dev/null 2>&1; then
        HEALTHY=true
        echo " Ready!"
        break
    fi
    if ! ${SUDO_CMD} docker ps --format '{{.Names}}' | grep -Eq "^${CONTAINER_NAME}$"; then
        echo ""
        log_error "Container '${CONTAINER_NAME}' exited while waiting for health."
        ${SUDO_CMD} docker logs --tail=80 "${CONTAINER_NAME}" || true
        exit 1
    fi
    echo -n "."
    sleep 2
    RETRY_COUNT=$((RETRY_COUNT + 1))
done

if [ "$HEALTHY" = true ]; then
    echo ""
    log_info "================================================================="
    log_info "Phosai Spark-TTS Service successfully deployed and healthy!"
    log_info "================================================================="
    echo -e "${GREEN}HTTP Endpoint:${NC}      http://localhost:${HOST_PORT}/v1/audio/speech/stream"
    echo -e "${GREEN}WebSocket Endpoint:${NC} ws://localhost:${HOST_PORT}/v1/audio/speech/stream/ws"
    echo -e "${GREEN}Voices Endpoint:${NC}    http://localhost:${HOST_PORT}/v1/voices"
    echo -e "${GREEN}Container Logs:${NC}     ${SUDO_CMD} docker logs -f ${CONTAINER_NAME}"
    echo ""
    echo "Quick Test Command:"
    echo "  curl -X POST http://localhost:${HOST_PORT}/v1/audio/speech/stream \\"
    echo "    -H 'Content-Type: application/json' \\"
    echo "    -d '{\"text\": \"Oli otya, nkulamusizza okuva e Uganda.\", \"speaker_id\": \"lug_female_4\"}' \\"
    echo "    --output output.pcm"
    echo ""
else
    echo ""
    log_error "Service did not respond within timeout period."
    log_warn "Check container logs for troubleshooting:"
    echo "  ${SUDO_CMD} docker logs -f ${CONTAINER_NAME}"
    ${SUDO_CMD} docker logs --tail=80 "${CONTAINER_NAME}" || true
    exit 1
fi
