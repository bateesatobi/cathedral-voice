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
#   TTS_FORCE_CPU=1 ./violet/miner/tts_install.sh          # skip GPU entirely
#   TTS_AUTO_FORCE_CPU=1 ./violet/miner/tts_install.sh    # CPU if preflight probe fails
#   TTS_CUDA_PROBE=0 ./violet/miner/tts_install.sh        # skip container CUDA probe
# ==============================================================================

set -euo pipefail

# Force non-interactive apt-get installs to bypass prompt questions on clean VMs
export DEBIAN_FRONTEND=noninteractive

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TTS_PROJECT_DIR="${TTS_PROJECT_DIR:-${SCRIPT_DIR}/tts-stack}"
COMPOSE_FILE="${TTS_PROJECT_DIR}/docker-compose.yml"
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

# Prefer GPU UUIDs so host docker targets the same card this job sees (DinD index 0
# is not always host index 0).
gpu_ids_for_compose() {
    local devices="$1" id uuid out=()
    IFS=',' read -r -a arr <<<"$devices"
    for id in "${arr[@]}"; do
        id="$(echo "$id" | tr -d '[:space:]')"
        [[ -z "$id" ]] && continue
        uuid="$(nvidia-smi -i "$id" --query-gpu=uuid --format=csv,noheader 2>/dev/null | tr -d '[:space:]' || true)"
        if [[ -n "$uuid" ]]; then
            out+=("$uuid")
        else
            out+=("$id")
        fi
    done
    local IFS=,
    echo "${out[*]}"
}

# Host-side GPU snapshot (runs outside spark-tts container).
log_gpu_host_snapshot() {
    if ! command -v nvidia-smi &>/dev/null; then
        log_warn "nvidia-smi unavailable — skipping host GPU snapshot."
        return 0
    fi
    log_info "Host GPU snapshot (outside TTS container):"
    while IFS= read -r line; do
        [[ -n "$line" ]] && log_info "  ${line}"
    done < <(nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu \
        --format=csv,noheader 2>/dev/null || true)
    local procs
    procs="$(nvidia-smi --query-compute-apps=gpu_bus_id,pid,process_name,used_memory \
        --format=csv,noheader 2>/dev/null || true)"
    if [[ -n "$procs" ]]; then
        log_info "GPU compute processes on host:"
        while IFS= read -r line; do
            [[ -n "$line" ]] && log_info "  ${line}"
        done <<<"$procs"
    else
        log_info "No GPU compute processes visible on host."
    fi
}

# Probe CUDA the same way TTS will: docker run + GPU reservation + PyTorch dual alloc.
# Prints: ok | skipped_cpu_mode | no_cuda | dual_fail | probe_error
probe_cuda_dual_alloc_in_container() {
    local devices="$1"
    local image="$2"
    local compose_ids cuda_inside gpu_flag out rc=0

    if [[ -z "$devices" ]]; then
        echo "skipped_cpu_mode"
        return 0
    fi
    if ! command -v docker &>/dev/null; then
        echo "probe_error"
        log_warn "docker unavailable — skipping CUDA container probe." >&2
        return 1
    fi

    compose_ids="$(gpu_ids_for_compose "$devices")"
    [[ -z "$compose_ids" ]] && compose_ids="$devices"
    cuda_inside="$(container_cuda_devices "$devices")"
    gpu_flag="device=${compose_ids}"

    log_info "CUDA probe: docker run --gpus ${gpu_flag} (CUDA_VISIBLE_DEVICES=${cuda_inside})" >&2

    set +e
    out="$(${SUDO_CMD} docker run --rm \
        --gpus "$gpu_flag" \
        -e "CUDA_VISIBLE_DEVICES=${cuda_inside}" \
        -e "NVIDIA_VISIBLE_DEVICES=${devices}" \
        -e NVIDIA_DRIVER_CAPABILITIES=compute,utility \
        --entrypoint /opt/nvidia/nvidia_entrypoint.sh \
        "$image" \
        python3 -c "
import sys
try:
    import torch
except Exception as exc:
    print('PROBE_ERROR import:', exc)
    sys.exit(3)
if not torch.cuda.is_available():
    print('PROBE_NO_CUDA')
    sys.exit(2)
try:
    torch.cuda.init()
    n = torch.cuda.device_count()
    name = torch.cuda.get_device_name(0) if n else 'none'
    print(f'PROBE_CUDA devices={n} name={name}')
    a = torch.nn.Linear(4096, 4096).cuda()
    x = torch.randn(8, 4096, device='cuda')
    _ = a(x)
    torch.cuda.synchronize()
    b = torch.nn.Linear(2048, 2048).cuda()
    _ = b(x[:, :2048])
    torch.cuda.synchronize()
    print('PROBE_OK dual_cuda')
except Exception as exc:
    print('PROBE_FAIL dual_cuda:', exc)
    sys.exit(1)
" 2>&1)"
    rc=$?
    set -e

    if echo "$out" | grep -q 'PROBE_OK dual_cuda'; then
        echo "ok"
        log_info "CUDA probe passed: second GPU allocation succeeded (BiCodec-style load)." >&2
        return 0
    fi
    if echo "$out" | grep -q 'PROBE_NO_CUDA'; then
        echo "no_cuda"
        log_warn "CUDA probe: PyTorch sees no CUDA inside a GPU-reserved container." >&2
        echo "$out" | grep -E 'PROBE_|CUDA' | head -5 | while read -r line; do log_warn "  ${line}" >&2; done
        return 1
    fi
    if echo "$out" | grep -q 'PROBE_FAIL'; then
        echo "dual_fail"
        log_warn "CUDA probe: second CUDA allocation failed (same pattern as BiCodec DevicesUnavailable)." >&2
        echo "$out" | grep -E 'PROBE_|CUDA|busy|unavailable' | head -8 | while read -r line; do log_warn "  ${line}" >&2; done
        return 1
    fi
    echo "probe_error"
    log_warn "CUDA probe inconclusive (docker exit ${rc})." >&2
    echo "$out" | tail -8 | while read -r line; do log_warn "  ${line}" >&2; done
    return 1
}

# Optional nvidia-smi inside a minimal CUDA image (driver/runtime wiring only).
probe_nvidia_smi_in_container() {
    local devices="$1"
    local compose_ids gpu_flag out

    if [[ -z "$devices" ]] || ! command -v docker &>/dev/null; then
        return 0
    fi
    compose_ids="$(gpu_ids_for_compose "$devices")"
    [[ -z "$compose_ids" ]] && compose_ids="$devices"
    gpu_flag="device=${compose_ids}"

    set +e
    out="$(${SUDO_CMD} docker run --rm --gpus "$gpu_flag" \
        nvidia/cuda:12.1.0-base-ubuntu22.04 nvidia-smi -L 2>&1)"
    set -e
    if echo "$out" | grep -qi 'GPU'; then
        log_info "nvidia-smi inside probe container: OK" >&2
        while IFS= read -r line; do
            [[ -n "$line" ]] && log_info "  ${line}" >&2
        done <<<"$out"
        return 0
    fi
    log_warn "nvidia-smi inside probe container failed:" >&2
    echo "$out" | tail -5 | while read -r line; do log_warn "  ${line}" >&2; done
    return 1
}

apply_cuda_preflight() {
    local probe_result="${1:-probe_error}"

    case "$probe_result" in
        ok|skipped_cpu_mode)
            return 0
            ;;
        dual_fail)
            if nested_docker; then
                log_warn "Nested Docker detected: text model may load on GPU; BiCodec often falls back to CPU."
                log_warn "If logs show 'keeping on CPU', startup can still succeed — wait for port ${HOST_PORT}."
            fi
            if [[ "${TTS_AUTO_FORCE_CPU:-}" == "1" ]]; then
                log_warn "TTS_AUTO_FORCE_CPU=1 — reconfiguring compose for CPU-only TTS."
                TTS_GPU_DEVICES=""
                CUDA_VISIBLE_DEVICES=""
                export TTS_GPU_DEVICES CUDA_VISIBLE_DEVICES
                write_compose_file ""
                return 0
            fi
            log_warn "Continuing with GPU text + CPU BiCodec fallback (default). Set TTS_FORCE_CPU=1 for all-CPU."
            ;;
        no_cuda)
            log_warn "Container GPU passthrough looks broken. Check NVIDIA Container Toolkit and driver."
            if [[ "${TTS_AUTO_FORCE_CPU:-}" == "1" ]]; then
                log_warn "TTS_AUTO_FORCE_CPU=1 — falling back to CPU-only TTS."
                TTS_GPU_DEVICES=""
                CUDA_VISIBLE_DEVICES=""
                export TTS_GPU_DEVICES CUDA_VISIBLE_DEVICES
                write_compose_file ""
            fi
            ;;
        probe_error)
            log_warn "CUDA preflight inconclusive — continuing with planned GPU settings."
            ;;
    esac
    return 0
}

# Spark BiCodec .to(cuda) often raises DevicesUnavailable in DinD after the text
# model loads (second CUDA allocation). Retry, then fall back to CPU for that module.
# Injected via exec() before the app starts — sitecustomize.py is unreliable here
# (nvidia_entrypoint may use a different python prefix than the patch path).
cuda_to_retry_patch() {
    cat <<'PY'
import time
import warnings

try:
    import torch
    from torch.nn.modules.module import Module
except Exception:
    raise SystemExit(0)

print("[tts_install] CUDA retry/CPU-fallback patch active", flush=True)

def _cuda_busy(exc):
    msg = str(exc).lower()
    return "busy or unavailable" in msg or "devicesunavailable" in msg

def _to_cpu_args(args, kwargs):
    new_args = list(args)
    for i, a in enumerate(new_args):
        if a == "cuda" or a == "cuda:0":
            new_args[i] = "cpu"
        elif isinstance(a, torch.device) and a.type == "cuda":
            new_args[i] = torch.device("cpu")
    kwargs = dict(kwargs)
    dev = kwargs.get("device")
    if dev == "cuda" or dev == "cuda:0":
        kwargs["device"] = "cpu"
    elif isinstance(dev, str) and dev.startswith("cuda"):
        kwargs["device"] = "cpu"
    elif isinstance(dev, torch.device) and dev.type == "cuda":
        kwargs["device"] = torch.device("cpu")
    return tuple(new_args), kwargs

def _to_with_retry(orig_to, self, *args, **kwargs):
    for attempt in range(6):
        try:
            return orig_to(self, *args, **kwargs)
        except Exception as exc:
            if not _cuda_busy(exc):
                raise
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass
            time.sleep(0.4 * (attempt + 1))
    warnings.warn(
        "CUDA busy/unavailable after retries; using CPU for this tensor/module. "
        "Common in nested Docker when the host GPU is already in use.",
        RuntimeWarning,
        stacklevel=2,
    )
    cpu_args, cpu_kwargs = _to_cpu_args(args, kwargs)
    return orig_to(self, *cpu_args, **cpu_kwargs)

_orig_module_to = Module.to
def _module_to_patched(self, *args, **kwargs):
    return _to_with_retry(_orig_module_to, self, *args, **kwargs)
Module.to = _module_to_patched

_orig_tensor_to = torch.Tensor.to
def _tensor_to_patched(self, *args, **kwargs):
    return _to_with_retry(_orig_tensor_to, self, *args, **kwargs)
torch.Tensor.to = _tensor_to_patched
PY
}

# Same GPU recipe as stt_install.sh, plus UUID pin + CUDA .to() retry (DinD).
# Named volumes only. No ipc:host / no CUDA worker spawn.
write_compose_file() {
    local devices="$1"
    local compose_ids cuda_inside device_yaml patch_b64 nvidia_visible gpu_block=""
    compose_ids="$(gpu_ids_for_compose "$devices")"
    [[ -z "$compose_ids" ]] && compose_ids="$devices"
    cuda_inside="$(container_cuda_devices "$devices")"
    nvidia_visible="${devices}"
    patch_b64="$(cuda_to_retry_patch | base64 | tr -d '\n')"
    if [[ -n "$devices" ]]; then
        device_yaml="$(gpu_compose_device_ids_yaml "$compose_ids")"
        gpu_block="    deploy:
      resources:
        reservations:
          devices:
${device_yaml}"
    else
        cuda_inside=""
        nvidia_visible=""
        log_warn "No GPU reservation in compose (CPU-only mode)."
    fi
    mkdir -p "${TTS_PROJECT_DIR}"
    cat > "${COMPOSE_FILE}" <<EOF
# Generated by tts_install.sh — do not edit by hand.
name: cathedral-voice-tts

services:
  spark-tts:
    image: ${IMAGE_TAG}
    container_name: ${CONTAINER_NAME}
    restart: unless-stopped
    ports:
      - "${HOST_PORT}:${APP_PORT}"
    environment:
      MODEL_NAME: "${MODEL_NAME}"
      HOST: "0.0.0.0"
      PORT: "${APP_PORT}"
      CUDA_VISIBLE_DEVICES: "${cuda_inside}"
      NVIDIA_VISIBLE_DEVICES: "${nvidia_visible}"
      NVIDIA_DRIVER_CAPABILITIES: compute,utility
      TTS_CUDA_PATCH_B64: "${patch_b64}"
    volumes:
      - hf-cache:/root/.cache/huggingface
    shm_size: "2gb"
${gpu_block}
    entrypoint: ["/bin/bash", "-c"]
    command:
      - |
        exec /opt/nvidia/nvidia_entrypoint.sh python3 -c "
        import os, base64, runpy
        exec(base64.b64decode(os.environ['TTS_CUDA_PATCH_B64']))
        runpy.run_path('/app/spark_tts_streaming.py', run_name='__main__')
        "
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:${APP_PORT}/"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 180s

volumes:
  hf-cache:
EOF
    log_info "Compose GPU ids: ${compose_ids:-none} (CUDA inside container: ${cuda_inside:-cpu})"
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

# DinD warning (see write_compose_file / patch for mitigations).
if nested_docker; then
    warn_nested_docker "TTS_ALLOW_NESTED_DOCKER"
    log_info "Nested Docker: launching TTS the same way as STT (compose GPU reservation, named volume, no ipc:host)."
    log_warn "Remote spaces share one GPU between your shell container and spark-tts-streaming."
    log_warn "TTS loads two CUDA models (text + BiCodec); the second often fails with DevicesUnavailable."
    log_warn "If startup still fails: stop STT/other GPU jobs first, or run with TTS_FORCE_CPU=1."
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
if [[ -n "${TTS_FORCE_CPU:-}" ]]; then
    log_warn "TTS_FORCE_CPU=1 — running spark-tts without GPU (CPU-only fallback)."
    TTS_GPU_DEVICES=""
else
    plan_gpu_devices "${GPU_PLAN_MODE}"
    if [[ -z "${TTS_GPU_DEVICES:-}" ]]; then
        log_warn "No TTS GPU partition from plan (count=$(detect_gpu_count)). Falling back to GPU 0 / --gpus all."
        TTS_GPU_DEVICES="${CUDA_VISIBLE_DEVICES:-0}"
    fi
fi
export TTS_GPU_DEVICES

if [[ -n "${TTS_GPU_DEVICES:-}" ]]; then
    CUDA_VISIBLE_DEVICES="$(container_cuda_devices "${TTS_GPU_DEVICES}")"
else
    CUDA_VISIBLE_DEVICES=""
fi

log_info "Image:              ${IMAGE_TAG}"
log_info "Container Name:     ${CONTAINER_NAME}"
log_info "Model:              ${MODEL_NAME}"
log_info "Port Mapping:       ${HOST_PORT} -> ${APP_PORT}"
log_info "GPU plan mode:      ${GPU_PLAN_MODE} (count=$(detect_gpu_count))"
log_info "TTS host GPUs:      ${TTS_GPU_DEVICES}"
log_info "CUDA inside ctr:    ${CUDA_VISIBLE_DEVICES}"
log_info "Compose file:       ${COMPOSE_FILE}"

# ------------------------------------------------------------------------------
# 5. Pull image, CUDA preflight (outside container), write compose
# ------------------------------------------------------------------------------
log_step "Step 5: Pulling ${IMAGE_TAG}"
${SUDO_CMD} docker pull "${IMAGE_TAG}"

log_step "Step 5b: CUDA preflight (host + container probes)"
log_gpu_host_snapshot
TTS_CUDA_PROBE="${TTS_CUDA_PROBE:-1}"
if [[ -n "${TTS_FORCE_CPU:-}" ]]; then
    log_info "TTS_FORCE_CPU=1 — skipping CUDA container probes."
    PROBE_RESULT="skipped_cpu_mode"
elif [[ "$TTS_CUDA_PROBE" != "1" ]]; then
    log_info "TTS_CUDA_PROBE=0 — skipping CUDA container probes."
    PROBE_RESULT="skipped_cpu_mode"
elif ! command -v nvidia-smi &>/dev/null; then
    log_warn "No nvidia-smi on host — skipping CUDA container probes."
    PROBE_RESULT="skipped_cpu_mode"
else
    probe_nvidia_smi_in_container "${TTS_GPU_DEVICES}" || true
    PROBE_RESULT="$(probe_cuda_dual_alloc_in_container "${TTS_GPU_DEVICES}" "${IMAGE_TAG}" || true)"
fi
apply_cuda_preflight "${PROBE_RESULT}"

log_step "Step 5c: Writing compose"
write_compose_file "${TTS_GPU_DEVICES}"

# ------------------------------------------------------------------------------
# 6. Stop and Remove Previous Container
# ------------------------------------------------------------------------------
log_step "Step 6: Managing Container State"
${SUDO_CMD} docker compose -f "${COMPOSE_FILE}" down --remove-orphans >/dev/null 2>&1 || true
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

# ------------------------------------------------------------------------------
# 7. Run Container (same GPU reservation path as cathedral-speaches)
# ------------------------------------------------------------------------------
log_step "Step 7: Launching Container with GPU Acceleration"
if ! ${SUDO_CMD} docker compose -f "${COMPOSE_FILE}" up -d --remove-orphans; then
    log_error "docker compose up failed."
    ${SUDO_CMD} docker compose -f "${COMPOSE_FILE}" logs --tail=80 || true
    exit 1
fi
${SUDO_CMD} docker compose -f "${COMPOSE_FILE}" ps

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
    echo -e "${GREEN}Container Logs:${NC}     ${SUDO_CMD} docker compose -f ${COMPOSE_FILE} logs -f"
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
