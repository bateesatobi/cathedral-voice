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
TTS_PROJECT_DIR="${TTS_PROJECT_DIR:-${SCRIPT_DIR}/tts-stack}"
# shellcheck source=gpu_env.sh
source "${SCRIPT_DIR}/gpu_env.sh"
# shellcheck source=install_lib.sh
INSTALL_LOG_PREFIX="tts_install"
source "${SCRIPT_DIR}/install_lib.sh"

ENV_FILE="${TTS_PROJECT_DIR}/.env"
COMPOSE_FILE="${TTS_PROJECT_DIR}/docker-compose.yml"

# start.sh compatibility (optional)
TTS_HOST_PORT="${TTS_HOST_PORT:-${HOST_PORT:-${TTS_PORT:-8002}}}"
CUDA_DEV=""
DEFAULT_HF_TOKEN="$(default_hf_token)"
GPU_PLAN_MODE="${GPU_PLAN_MODE:-tts}"

# Set by resolve_install_profile
TTS_INSTALL_PROFILE="bare_metal"
VLLM_EXTRA_ENV=""

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

resolve_install_profile() {
    if nested_docker; then
        if command -v nvidia-smi &>/dev/null && [[ "$(detect_gpu_count)" -gt 0 ]]; then
            TTS_INSTALL_PROFILE="dind_gpu_shell"
            # vLLM V1 EngineCore is a separate CUDA process — often fails in DinD while
            # single-process stacks (speaches/STT) still work. Force legacy in-proc engine.
            VLLM_EXTRA_ENV="      VLLM_USE_V1: \"0\""
            warn "Nested Docker + GPU in this shell (profile=dind_gpu_shell)."
            warn "Using VLLM_USE_V1=0 (STT/speaches works here because it does not fork a vLLM EngineCore)."
            warn "Set TTS_ALLOW_NESTED_DOCKER=1 to silence nested-Docker warnings."
        else
            TTS_INSTALL_PROFILE="dind_socket_only"
            log "Nested Docker without local GPU (profile=dind_socket_only) — sibling containers use host GPUs."
        fi
    else
        TTS_INSTALL_PROFILE="bare_metal"
        log "Bare-metal / VM install (profile=bare_metal)."
    fi
    export TTS_INSTALL_PROFILE VLLM_EXTRA_ENV
}

resolve_cuda_dev() {
    local requested n
    requested="${TTS_GPU_DEVICES:-${CUDA_VISIBLE_DEVICES:-}}"

    if [[ -z "$requested" ]]; then
        plan_gpu_devices "${GPU_PLAN_MODE}"
        requested="${TTS_GPU_DEVICES:-}"
    fi

    if [[ -z "$requested" ]]; then
        n="$(detect_gpu_count)"
        if (( n < 3 )); then
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
    log "Checking Docker GPU access (same check STT relies on)..."
    if ! docker run --rm --gpus all nvidia/cuda:12.0.0-base-ubuntu22.04 nvidia-smi -L >/dev/null 2>&1; then
        err "Docker cannot access GPUs (docker run --gpus all nvidia-smi failed)."
        if nested_docker; then
            err "Nested Docker: ensure the host Docker daemon has NVIDIA Container Toolkit configured."
        fi
        exit 1
    fi
    log "Docker GPU smoke test OK."
}

smoke_test_vllm_cuda_subprocess() {
    [[ "$TTS_INSTALL_PROFILE" != "dind_gpu_shell" ]] && return 0
    log "Checking vLLM-style CUDA subprocess inside a throwaway container..."
    if ! docker run --rm \
        --gpus all \
        --ipc=host \
        -e NVIDIA_VISIBLE_DEVICES="${CUDA_DEV}" \
        -e CUDA_VISIBLE_DEVICES=0 \
        -e VLLM_USE_V1=0 \
        -e VLLM_WORKER_MULTIPROC_METHOD=spawn \
        simonallanachuka/spark-tts-streaming:v1.5 \
        python3 -c "import torch; assert torch.cuda.is_available(); print(torch.cuda.get_device_name(0))" \
        >/dev/null 2>&1; then
        warn "vLLM/CUDA subprocess smoke test failed in DinD."
        warn "STT may still work (single-process CUDA). TTS needs host install or a non-GPU dev shell + docker.sock only."
        if [[ "${TTS_STRICT:-1}" == "1" ]]; then
            err "TTS_STRICT=1 — aborting. Run on bare-metal host or use a dev container with docker.sock only (no GPU passthrough)."
            exit 1
        fi
    else
        log "vLLM/CUDA subprocess smoke test OK."
    fi
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
    else
        log "nvidia-smi found:"
        nvidia-smi -L || true
    fi
    install_nvidia_toolkit_if_needed "$SUDO"
    if docker info 2>/dev/null | grep -qi "nvidia"; then
        log "NVIDIA Container Toolkit already configured with Docker."
    else
        warn "NVIDIA Container Toolkit does not appear to be configured with Docker (nvidia runtime not found in 'docker info'). etoil-tts may fail to start."
    fi
}

# ---------------------------------------------------------------------------
# 3. Write docker-compose.yml (secrets pulled from .env, never hardcoded)
# ---------------------------------------------------------------------------
ensure_tts_project_dir() {
    mkdir -p "${TTS_PROJECT_DIR}"
}

write_compose_file() {
    ensure_tts_project_dir
    local streaming_vol=""
    # Always named volumes (same idea as STT hf-hub-cache) — safe on bare metal and DinD.
    local cache_vol="etoil-tts-cache:/app/cache"
    local tokenizer_vol="etoil-tts-spark-tokenizer:/app/Spark-TTS-0.5B"

    if [[ -f "${PROJECT_DIR}/spark_tts_streaming.py" && "$TTS_INSTALL_PROFILE" == "bare_metal" ]]; then
        streaming_vol="      - ${PROJECT_DIR}/spark_tts_streaming.py:/app/spark_tts_streaming.py:ro"
    elif [[ ! -f "${PROJECT_DIR}/spark_tts_streaming.py" ]]; then
        warn "spark_tts_streaming.py not found — using copy baked into the image."
    fi

    if nested_docker && [[ -z "${TTS_ALLOW_NESTED_DOCKER:-}" ]]; then
        warn_nested_docker "TTS_ALLOW_NESTED_DOCKER"
    fi

    log "Writing ${COMPOSE_FILE}"
    local gpu_devices_yaml
    gpu_devices_yaml="$(gpu_compose_device_ids_yaml "$CUDA_DEV")"
    cat > "$COMPOSE_FILE" <<EOF
# Generated by tts_install.sh — do not edit by hand.
name: cathedral-voice-tts

services:
  etoil-tts:
    image: simonallanachuka/spark-tts-streaming:v1.5
    container_name: etoil-tts
    ports:
      - '${TTS_HOST_PORT}:8002'
    ipc: host
    environment:
      HF_TOKEN: \${HF_TOKEN}
      PYTHONPATH: /app
      NVIDIA_VISIBLE_DEVICES: "${CUDA_DEV}"
      CUDA_VISIBLE_DEVICES: "${CONTAINER_CUDA}"
      NVIDIA_DRIVER_CAPABILITIES: compute,utility
      VLLM_WORKER_MULTIPROC_METHOD: spawn
      MODEL_NAME: phosai/phosai_tts_v1
      TOKENIZER_REPO: unsloth/Spark-TTS-0.5B
      TOKENIZER_CACHE_DIR: Spark-TTS-0.5B
      SPARK_TTS_REPO_PATH: Spark-TTS
${VLLM_EXTRA_ENV}
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
      start_period: 180s

volumes:
  etoil-tts-cache:
  etoil-tts-spark-tokenizer:
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

    mkdir -p "${TTS_PROJECT_DIR}"
    log "Writing ${ENV_FILE}"
    cat > "$ENV_FILE" <<EOF
HF_TOKEN=${token}
TTS_HOST_PORT=${TTS_HOST_PORT}
TTS_GPU_DEVICES=${CUDA_DEV}
GPU_PLAN_MODE=${GPU_PLAN_MODE}
EOF

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
    ensure_tts_project_dir
    log "TTS stack directory: ${TTS_PROJECT_DIR}"
}

wait_for_tts() {
    local url="http://127.0.0.1:${TTS_HOST_PORT}/"
    log "Waiting for TTS health on ${url} (model load can take several minutes)..."
    if ! wait_http_ok "etoil-tts" "$url" 600 "etoil-tts"; then
        err "TTS did not become healthy on :${TTS_HOST_PORT}"
        docker compose -f "${COMPOSE_FILE}" logs --tail=80 etoil-tts || true
        if [[ "$TTS_INSTALL_PROFILE" == "dind_gpu_shell" ]]; then
            err "DinD + GPU dev shell: STT/speaches often works; vLLM TTS may require bare-metal host install."
        fi
        return 1
    fi
    log "TTS is healthy on :${TTS_HOST_PORT}"
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
    smoke_test_vllm_cuda_subprocess

    log "Pulling images..."
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" pull

    log "Starting stack..."
    if ! docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --remove-orphans; then
        err "docker compose up failed."
        if nested_docker; then
            err "If you are inside a GPU dev container, try bare-metal host install or TTS_STRICT=0 to continue debugging."
        fi
        docker compose -f "$COMPOSE_FILE" ps || true
        docker compose -f "$COMPOSE_FILE" logs --tail=60 etoil-tts || true
        exit 1
    fi

    log "Stack started. Current status:"
    docker compose -f "$COMPOSE_FILE" ps
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    require_root_or_sudo
    resolve_install_profile
    install_docker
    check_nvidia_toolkit
    resolve_cuda_dev
    write_compose_file
    setup_env_file
    setup_dirs
    start_stack
    wait_for_tts || { [[ "${TTS_STRICT:-1}" == "1" ]] && exit 1; }

    log "Done. Useful commands:"
    echo "  TTS URL : http://127.0.0.1:${TTS_HOST_PORT}"
    echo "  Logs    : docker compose -f ${COMPOSE_FILE} logs -f etoil-tts"
    echo "  Stop    : docker compose -f ${COMPOSE_FILE} down"
    echo "  Profile : ${TTS_INSTALL_PROFILE}"
}

main "$@"
