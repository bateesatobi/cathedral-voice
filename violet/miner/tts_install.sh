#!/usr/bin/env bash
#
# install-tts.sh - Self-contained bootstrapper for the etoil-tts stack
#                   (spark-tts-streaming)
#
# This single file installs Docker if needed, writes out docker-compose.yml
# and .env, then brings the stack up.
#
# Usage:
#   chmod +x install-tts.sh
#   ./install-tts.sh
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="${PROJECT_DIR}"
# shellcheck source=gpu_env.sh
source "${SCRIPT_DIR}/gpu_env.sh"
# shellcheck source=install_lib.sh
INSTALL_LOG_PREFIX="tts_install"
source "${SCRIPT_DIR}/install_lib.sh"

ENV_FILE="${PROJECT_DIR}/.env"
COMPOSE_FILE="${PROJECT_DIR}/docker-compose.yml"

# start.sh compatibility (optional)
TTS_HOST_PORT="${TTS_HOST_PORT:-${HOST_PORT:-${TTS_PORT:-8002}}}"
CUDA_DEV=""
DEFAULT_HF_TOKEN="$(default_hf_token)"

log()  { echo -e "\033[1;32m[install]\033[0m $*"; }
warn() { echo -e "\033[1;33m[warn]\033[0m $*"; }
err()  { echo -e "\033[1;31m[error]\033[0m $*" >&2; }

require_root_or_sudo() {
    if [[ $EUID -ne 0 ]]; then
        SUDO="sudo"
    else
        SUDO=""
    fi
}

nested_docker() {
    [[ -f /.dockerenv ]]
}

first_gpu_id() {
    local devices="$1"
    echo "${devices%%,*}" | tr -d '[:space:]'
}

gpu_index_valid() {
    local idx="$1" count="$2"
    [[ "$idx" =~ ^[0-9]+$ ]] && (( idx >= 0 && idx < count ))
}

resolve_cuda_dev() {
    local requested n
    requested="${TTS_GPU_DEVICES:-${CUDA_VISIBLE_DEVICES:-}}"

    if [[ -z "$requested" ]]; then
        n="$(detect_gpu_count)"
        if nested_docker || (( n < 3 )); then
            requested="0"
        else
            requested="2"
        fi
    fi

    CUDA_DEV="$(first_gpu_id "$requested")"
    n="$(detect_gpu_count)"

    if (( n > 0 )); then
        if ! gpu_index_valid "$CUDA_DEV" "$n"; then
            warn "GPU ${CUDA_DEV} not found (detected ${n} GPU(s)); using GPU 0"
            CUDA_DEV="0"
        fi
    elif nested_docker; then
        warn "nvidia-smi unavailable in this shell; defaulting TTS to host GPU 0"
        CUDA_DEV="0"
    fi

    # Inside a single-GPU container, CUDA always sees device 0 (see stt_install.sh).
    CONTAINER_CUDA="0"

    log "TTS host GPU index: ${CUDA_DEV} (container CUDA_VISIBLE_DEVICES=${CONTAINER_CUDA})"
    export CUDA_DEV CONTAINER_CUDA
}

smoke_test_docker_gpu() {
    log "Checking Docker GPU access..."
    if ! docker run --rm --gpus all nvidia/cuda:12.0.0-base-ubuntu22.04 nvidia-smi -L >/dev/null 2>&1; then
        err "Docker cannot access GPUs (docker run --gpus all nvidia-smi failed)."
        if nested_docker; then
            err "Nested Docker: run tts_install.sh on the bare-metal GPU host, not inside a GPU dev container."
        fi
        exit 1
    fi
    log "Docker GPU smoke test OK."
}

# ---------------------------------------------------------------------------
# 1. Install Docker Engine + Compose plugin (if missing)
# ---------------------------------------------------------------------------
install_docker() {
    if command -v docker &>/dev/null; then
        log "Docker already installed: $(docker --version)"
    else
        log "Installing Docker Engine..."
        curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
        $SUDO sh /tmp/get-docker.sh
        rm -f /tmp/get-docker.sh
        $SUDO usermod -aG docker "$USER" || true
        warn "Added $USER to the docker group. Log out/in (or run 'newgrp docker') for it to take effect."
    fi

    if ! docker compose version &>/dev/null; then
        err "docker compose plugin not found even after Docker install. Check your Docker installation."
        exit 1
    else
        log "docker compose plugin OK: $(docker compose version)"
    fi
}

# ---------------------------------------------------------------------------
# 2. Check NVIDIA Container Toolkit (assumed already installed on this host)
# ---------------------------------------------------------------------------
check_nvidia_toolkit() {
    if ! command -v nvidia-smi &>/dev/null; then
        warn "nvidia-smi not found on this machine. etoil-tts requests GPUs and will fail to start without a working NVIDIA driver + toolkit."
        return
    fi
    log "nvidia-smi found:"
    nvidia-smi -L || true

    if docker info 2>/dev/null | grep -qi "nvidia"; then
        log "NVIDIA Container Toolkit already configured with Docker."
    else
        warn "NVIDIA Container Toolkit does not appear to be configured with Docker (nvidia runtime not found in 'docker info'). etoil-tts may fail to start."
    fi
}

# ---------------------------------------------------------------------------
# 3. Write docker-compose.yml (secrets pulled from .env, never hardcoded)
# ---------------------------------------------------------------------------
write_compose_file() {
    local cache_vol="./cache:/app/cache"
    local tokenizer_vol="./Spark-TTS-0.5B:/app/Spark-TTS-0.5B"
    local streaming_vol=""
    local named_volumes_block=""

    if nested_docker; then
        cache_vol="etoil-tts-cache:/app/cache"
        tokenizer_vol="etoil-tts-spark-tokenizer:/app/Spark-TTS-0.5B"
        named_volumes_block="
volumes:
  etoil-tts-cache:
  etoil-tts-spark-tokenizer:"
        warn "Nested Docker detected — using named volumes etoil-tts-cache and etoil-tts-spark-tokenizer instead of bind mounts"
        warn "Nested Docker: skipping spark_tts_streaming.py bind mount (using image default)"
    elif [[ -f "${PROJECT_DIR}/spark_tts_streaming.py" ]]; then
        streaming_vol="      - ./spark_tts_streaming.py:/app/spark_tts_streaming.py:ro"
    fi

    log "Writing ${COMPOSE_FILE}"
    local gpu_devices_yaml
    gpu_devices_yaml="$(gpu_compose_device_ids_yaml "$CUDA_DEV")"
    if nested_docker; then
        warn "Nested Docker: using deploy GPU reservations + ipc:host (prefer bare-metal host if CUDA init fails)"
    fi
    cat > "$COMPOSE_FILE" <<EOF
services:
  etoil-tts:
    image: simonallanachuka/spark-tts-streaming:v1.5
    container_name: etoil-tts
    ports:
      - '${TTS_HOST_PORT}:8002'
    ipc: host
    environment:
      - PYTHONPATH=/app
      - NVIDIA_VISIBLE_DEVICES=${CUDA_DEV}
      - CUDA_VISIBLE_DEVICES=${CONTAINER_CUDA}
      - NVIDIA_DRIVER_CAPABILITIES=compute,utility
      - GPU_MEMORY_UTILIZATION=0.65
      - VLLM_WORKER_MULTIPROC_METHOD=spawn
      - MODEL_NAME=phosai/phosai_tts_v1
      - TOKENIZER_REPO=unsloth/Spark-TTS-0.5B
      - TOKENIZER_CACHE_DIR=Spark-TTS-0.5B
      - SPARK_TTS_REPO_PATH=Spark-TTS
      - HF_TOKEN=\${HF_TOKEN}
      - NCCL_DEBUG=WARN
      - NCCL_SOCKET_IFNAME=lo
      - NCCL_IB_DISABLE=1
      - NCCL_P2P_DISABLE=1
      - NCCL_NET_GDR_LEVEL=0
      - NCCL_SHM_DISABLE=1
      - NCCL_TREE_THRESHOLD=0
      - NCCL_RING_THRESHOLD=8388608
    volumes:
      - ${cache_vol}
      - ${tokenizer_vol}
${streaming_vol}
    shm_size: '8gb'
    deploy:
      resources:
        reservations:
          devices:
${gpu_devices_yaml}
    restart: unless-stopped
    healthcheck:
      test: ['CMD', 'curl', '-f', 'http://localhost:8002/']
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 120s${named_volumes_block}
EOF
    log "docker-compose.yml written."
}

# ---------------------------------------------------------------------------
# 4. Write .env (HF token for compose variable substitution)
# ---------------------------------------------------------------------------
sanitize_hf_token() {
    local raw="${1:-}"
    # Strip whitespace and any non-ASCII chars (smart quotes, em-dashes, etc.).
    printf '%s' "$raw" | tr -d '[:space:]' | LC_ALL=C tr -cd '[:print:]'
}

setup_env_file() {
    local token
    token="$(sanitize_hf_token "${HF_TOKEN:-$DEFAULT_HF_TOKEN}")"

    if [[ -z "$token" ]]; then
        err "HF_TOKEN is empty. Export HF_TOKEN=hf_... or add it to ${ENV_FILE} before running."
        exit 1
    fi
    if [[ "$token" != hf_* ]]; then
        warn "HF_TOKEN does not start with hf_ — Hugging Face downloads may fail."
    fi

    log "Writing ${ENV_FILE}"
    printf 'HF_TOKEN=%s\n' "$token" > "$ENV_FILE"

    chmod 600 "$ENV_FILE"
    log ".env written and locked to 600 permissions."

    if ! grep -qxF ".env" "${PROJECT_DIR}/.gitignore" 2>/dev/null; then
        echo ".env" >> "${PROJECT_DIR}/.gitignore"
        log "Added .env to .gitignore"
    fi
}

# ---------------------------------------------------------------------------
# 5. Misc setup
# ---------------------------------------------------------------------------
setup_dirs() {
    mkdir -p "${PROJECT_DIR}/cache"
    mkdir -p "${PROJECT_DIR}/Spark-TTS-0.5B"
    log "Ensured ./cache and ./Spark-TTS-0.5B directories exist."

    if [[ ! -f "${PROJECT_DIR}/spark_tts_streaming.py" ]]; then
        if nested_docker; then
            warn "spark_tts_streaming.py not found locally; nested Docker will use the copy baked into the image."
        else
            warn "spark_tts_streaming.py not found in ${PROJECT_DIR}. It's mounted read-only into the container — the service will fail to start without it."
        fi
    fi
}

# ---------------------------------------------------------------------------
# 6. Bring the stack up
# ---------------------------------------------------------------------------
stop_existing_stack() {
    local name="${TTS_CONTAINER_NAME:-etoil-tts}"

    if [[ -f "$COMPOSE_FILE" ]]; then
        log "Stopping any existing TTS compose stack..."
        docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" down --remove-orphans 2>/dev/null || true
    fi

    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -Fxq "$name"; then
        warn "Removing leftover container '${name}'..."
        docker rm -f "$name" 2>/dev/null || true
    fi

    for legacy in spark-tts-frontend cathedral-spark-tts; do
        if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -Fxq "$legacy"; then
            warn "Removing legacy TTS container '${legacy}'..."
            docker rm -f "$legacy" 2>/dev/null || true
        fi
    done
}

start_stack() {
    stop_existing_stack
    smoke_test_docker_gpu

    log "Pulling images..."
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" pull

    log "Starting stack..."
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d

    log "Stack started. Current status:"
    docker compose -f "$COMPOSE_FILE" ps
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    require_root_or_sudo
    install_docker
    check_nvidia_toolkit
    resolve_cuda_dev
    write_compose_file
    setup_env_file
    setup_dirs
    start_stack

    log "Done. Useful commands:"
    echo "    docker compose -f ${COMPOSE_FILE} logs -f etoil-tts"
    echo "    docker compose -f ${COMPOSE_FILE} down"
}

main "$@"
