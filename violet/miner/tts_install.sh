#!/bin/bash
# ==============================================================================
# Spark-TTS VM / miner deploy (spark-tts-frontend)
#
# Self-contained image — models are baked in, no Hugging Face token needed.
# Installs Docker + NVIDIA Container Toolkit if missing, pulls the image,
# and runs it with --gpus all (same path as WSL / bare-metal GPU VMs).
#
# GPU_PLAN_MODE (via gpu_env.sh, same as stt_install.sh):
#   tts  (default when run alone) — CUDA_VISIBLE_DEVICES from plan / 0
#   both — TTS partition from start.sh (GPU_PLAN_LOCKED=1)
#
# Usage:
#   ./violet/miner/tts_install.sh
#   GPU_PLAN_MODE=both TTS_GPU_DEVICES=0 ./violet/miner/tts_install.sh
#   TTS_FORCE_CPU=1 ./violet/miner/tts_install.sh    # skip GPU (slow; not recommended)
#   TTS_SKIP_CUDA_PROBE=1 ./violet/miner/tts_install.sh  # unsafe override
#
# CUDA compute is probed before pull/run. Nested GPU jobs that only expose
# nvidia-smi are rejected so TTS does not crash mid-load.
# ==============================================================================

set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=gpu_env.sh
source "${SCRIPT_DIR}/gpu_env.sh"
# shellcheck source=install_lib.sh
INSTALL_LOG_PREFIX="tts_install"
source "${SCRIPT_DIR}/install_lib.sh"

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

if [[ "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* || "$(uname -s 2>/dev/null || echo)" == MINGW* || "$(uname -s 2>/dev/null || echo)" == MSYS* ]]; then
    printf '%b\n' "${RED}[ERROR] $(date +'%Y-%m-%d %H:%M:%S')${NC} This deployment script is designed for Linux/WSL Ubuntu, not Git Bash or MinGW."
    printf '%b\n' "${YELLOW}[INFO] $(date +'%Y-%m-%d %H:%M:%S')${NC} Open WSL2 or a GPU Linux VM and run:"
    echo "  sudo ./violet/miner/tts_install.sh"
    exit 1
fi

if [ "$(id -u 2>/dev/null || echo 0)" -eq 0 ]; then
    SUDO_CMD=""
else
    if ! command -v sudo >/dev/null 2>&1; then
        echo -e "${RED}[ERROR] $(date +'%Y-%m-%d %H:%M:%S')${NC} sudo is not available. Run as root or in WSL2 Ubuntu."
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

# nvidia-smi (NVML) can succeed while CUDA compute is blocked (nested Docker).
# Fail closed unless the operator explicitly opts into CPU TTS.
abort_tts_cuda_environment() {
    local detail="${1:-CUDA compute probe failed}"
    echo
    echo -e "${RED}${BOLD}=================================================================${NC}"
    echo -e "${RED}${BOLD} TTS install stopped — this host cannot run Spark-TTS on GPU${NC}"
    echo -e "${RED}${BOLD}=================================================================${NC}"
    echo
    log_error "${detail}"
    echo
    echo "What this means"
    echo "  nvidia-smi can list the GPU while Docker still cannot allocate CUDA."
    echo "  Spark-TTS will hang, crash, or run on CPU with unusable latency."
    echo "  Install is aborted so the miner is not left half-broken."
    echo
    echo "Typical cause"
    echo "  Nested Docker: this shell is already a container (/.dockerenv) and inner"
    echo "  'docker run --gpus' only gets NVML, not a CUDA context"
    echo "  (cudaErrorDevicesUnavailable / DevicesUnavailable)."
    echo
    echo "Where TTS does work"
    echo "  • Ubuntu GPU VM where Docker is the host (not a job-in-a-container)"
    echo "  • WSL2 Ubuntu with NVIDIA Container Toolkit"
    echo "  • Bare metal with nvidia-ctk + --gpus all"
    echo
    echo "Required check (must print ALLOC_OK, not DevicesUnavailable):"
    echo "  docker run --rm --gpus all -e NVIDIA_DRIVER_CAPABILITIES=compute,utility \\"
    echo "    python:3.11-slim-bookworm python3 -c \"import ctypes,sys; lib=ctypes.CDLL('libcuda.so.1');"
    echo "    r=lib.cuInit(0); sys.exit(0 if r==0 else 1)\""
    echo
    echo "What to do now"
    echo "  ASR-only on this host:"
    echo "    MINER_SERVICES=asr ./violet/miner/start.sh prod --gpu --no-follow"
    echo "  TTS (or ASR+TTS) on a real GPU VM / WSL, then:"
    echo "    ./violet/miner/tts_install.sh"
    echo "  CPU TTS is not recommended (same quality, much higher latency)."
    echo "    TTS_FORCE_CPU=1 ./violet/miner/tts_install.sh"
    echo
    exit 1
}

# Driver API: cuInit + context + 4KiB alloc. Catches DevicesUnavailable that
# nvidia-smi never reports. No bind mounts (nested Docker cannot see host paths).
cuda_compute_probe_py() {
    cat <<'PY'
import ctypes
import sys

def load():
    for name in ("libcuda.so.1", "libcuda.so"):
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    print("PROBE_FAIL libcuda.so.1 missing (GPU not injected into the container)")
    sys.exit(2)

lib = load()

lib.cuInit.restype = ctypes.c_int
lib.cuInit.argtypes = [ctypes.c_uint]
rc = lib.cuInit(0)
print("cuInit", rc, flush=True)
if rc != 0:
    print("PROBE_FAIL cuInit rc=%s (GPU compute blocked)" % rc)
    sys.exit(3)

dev = ctypes.c_int()
lib.cuDeviceGet.restype = ctypes.c_int
lib.cuDeviceGet.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int]
rc = lib.cuDeviceGet(ctypes.byref(dev), 0)
print("cuDeviceGet", rc, "device", dev.value, flush=True)
if rc != 0:
    print("PROBE_FAIL cuDeviceGet rc=%s" % rc)
    sys.exit(4)

ctx = ctypes.c_void_p()
ctx_rc = -1
for sym in ("cuCtxCreate_v2", "cuCtxCreate"):
    if not hasattr(lib, sym):
        continue
    fn = getattr(lib, sym)
    fn.restype = ctypes.c_int
    fn.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint, ctypes.c_int]
    ctx_rc = fn(ctypes.byref(ctx), 0, dev)
    print(sym, ctx_rc, flush=True)
    if ctx_rc == 0:
        break
else:
    print("PROBE_FAIL cuCtxCreate rc=%s (nested Docker often returns busy/unavailable)" % ctx_rc)
    sys.exit(5)
if ctx_rc != 0:
    print("PROBE_FAIL cuCtxCreate rc=%s (nested Docker often returns busy/unavailable)" % ctx_rc)
    sys.exit(5)

dptr = ctypes.c_void_p()
mem_rc = -1
for sym in ("cuMemAlloc_v2", "cuMemAlloc"):
    if not hasattr(lib, sym):
        continue
    fn = getattr(lib, sym)
    fn.restype = ctypes.c_int
    fn.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_size_t]
    mem_rc = fn(ctypes.byref(dptr), 4096)
    print(sym, mem_rc, flush=True)
    if mem_rc == 0:
        print("ALLOC_OK")
        sys.exit(0)
print("PROBE_FAIL cuMemAlloc rc=%s" % mem_rc)
sys.exit(6)
PY
}

run_cuda_compute_probe() {
    local out rc=0
    local probe_image="${TTS_CUDA_PROBE_IMAGE:-python:3.11-slim-bookworm}"
    local probe_py
    probe_py="$(cuda_compute_probe_py)"

    log_info "CUDA compute gate: context + 4KiB alloc inside Docker (not nvidia-smi)."
    log_info "Probe image: ${probe_image}"

    if ! ${SUDO_CMD} docker pull "${probe_image}"; then
        abort_tts_cuda_environment "Could not pull CUDA probe image ${probe_image}."
    fi

    set +e
    out="$(
        ${SUDO_CMD} docker run --rm \
            "${GPU_RUN_ARGS[@]}" \
            -e NVIDIA_DRIVER_CAPABILITIES=compute,utility \
            -e CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}" \
            -e NVIDIA_VISIBLE_DEVICES="${NVIDIA_VISIBLE:-all}" \
            --entrypoint python3 \
            "${probe_image}" \
            -c "${probe_py}" 2>&1
    )"
    rc=$?
    set -e

    echo "${out}" | while IFS= read -r line; do
        [[ -n "$line" ]] && log_info "  probe: ${line}"
    done

    if [[ "$rc" -eq 0 ]] && echo "${out}" | grep -q 'ALLOC_OK'; then
        log_info "CUDA compute gate passed — Spark-TTS may use this GPU."
        return 0
    fi
    abort_tts_cuda_environment "Docker CUDA alloc failed (probe exit ${rc}). nvidia-smi is not sufficient."
}

# ------------------------------------------------------------------------------
# 1. System compatibility
# ------------------------------------------------------------------------------
log_step "Step 1: Verifying system compatibility"
if [ -f /etc/os-release ]; then
    . /etc/os-release
    log_info "Detected OS: $NAME $VERSION"
    if [[ "$ID" != "ubuntu" && "$ID" != "debian" ]]; then
        log_warn "This script is optimized for Ubuntu/Debian. Other distributions may need manual steps."
    fi
else
    log_warn "Could not detect OS version. Proceeding anyway..."
fi

log_info "Checking for NVIDIA GPU..."
if command -v nvidia-smi &>/dev/null; then
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -n1 || echo "NVIDIA GPU")
    DRIVER_VER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n1 || echo "Unknown")
    log_info "NVIDIA GPU detected: ${GPU_NAME} (Driver: ${DRIVER_VER})"
elif lspci 2>/dev/null | grep -i nvidia >/dev/null; then
    log_info "NVIDIA GPU hardware detected via lspci."
else
    log_warn "No NVIDIA GPU detected via nvidia-smi or lspci. If this is a VM, enable GPU passthrough."
fi

if [[ -f /.dockerenv ]]; then
    warn_nested_docker "TTS_ALLOW_NESTED_DOCKER"
    log_warn "Nested Docker detected. TTS will only continue if the CUDA compute gate passes."
    log_warn "Many GPU 'spaces' list the card in nvidia-smi but cannot allocate CUDA in inner containers."
fi

# ------------------------------------------------------------------------------
# 2. Docker Engine
# ------------------------------------------------------------------------------
log_step "Step 2: Checking Docker Engine"
if ! command -v docker &>/dev/null; then
    log_info "Docker is not installed. Installing Docker..."
    ${SUDO_CMD} apt-get update
    ${SUDO_CMD} apt-get install -y -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" \
        ca-certificates curl gnupg lsb-release

    ${SUDO_CMD} mkdir -p /etc/apt/keyrings
    if [ ! -f /etc/apt/keyrings/docker.gpg ]; then
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg | ${SUDO_CMD} gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    fi

    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | \
        ${SUDO_CMD} tee /etc/apt/sources.list.d/docker.list >/dev/null
    ${SUDO_CMD} apt-get update
    ${SUDO_CMD} apt-get install -y -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

    if [ -n "${USER:-}" ]; then
        ${SUDO_CMD} usermod -aG docker "$USER" || true
    fi
    log_info "Docker successfully installed."
else
    log_info "Docker is already installed: $(docker --version)"
fi

# ------------------------------------------------------------------------------
# 3. NVIDIA Container Toolkit
# ------------------------------------------------------------------------------
log_step "Step 3: Checking NVIDIA Container Toolkit"
if ! command -v nvidia-ctk &>/dev/null; then
    log_info "NVIDIA Container Toolkit is not installed. Setting up repositories and installing..."
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
        ${SUDO_CMD} gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
        sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
        ${SUDO_CMD} tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

    ${SUDO_CMD} apt-get update
    ${SUDO_CMD} apt-get install -y -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" \
        nvidia-container-toolkit

    log_info "Configuring NVIDIA Container Toolkit for Docker..."
    ${SUDO_CMD} nvidia-ctk runtime configure --runtime=docker
    ${SUDO_CMD} systemctl restart docker || ${SUDO_CMD} service docker restart || true
    log_info "NVIDIA Container Toolkit successfully installed and configured."
else
    log_info "NVIDIA Container Toolkit is already installed: $(nvidia-ctk --version)"
fi
install_nvidia_toolkit_if_needed "${SUDO_CMD}"

# ------------------------------------------------------------------------------
# 4. Environment (miner-compatible)
# ------------------------------------------------------------------------------
log_step "Step 4: Configuring environment"

HOST_PORT="${TTS_HOST_PORT:-${HOST_PORT:-${TTS_PORT:-8002}}}"
APP_PORT="${APP_PORT:-8002}"
IMAGE_TAG="${IMAGE_TAG:-simonallanachuka/spark-tts-frontend:latest}"
CONTAINER_NAME="${CONTAINER_NAME:-${TTS_CONTAINER_NAME:-spark-tts-frontend}}"
MODEL_POOL_SIZE="${MODEL_POOL_SIZE:-1}"
SPARK_TTS_DTYPE="${SPARK_TTS_DTYPE:-f32}"
RUST_LOG="${RUST_LOG:-info}"
MODEL_DIR="${MODEL_DIR:-/app/models/Spark-TTS-0.5B}"
GPU_PLAN_MODE="${GPU_PLAN_MODE:-tts}"

plan_gpu_devices "${GPU_PLAN_MODE}"

# Do not inherit an empty CUDA_VISIBLE_DEVICES from the parent shell (forces CPU).
if [[ -z "${CUDA_VISIBLE_DEVICES:-}" ]]; then
    if [[ -n "${TTS_GPU_DEVICES:-}" ]]; then
        CUDA_VISIBLE_DEVICES="${TTS_GPU_DEVICES}"
    else
        CUDA_VISIBLE_DEVICES="0"
    fi
fi

GPU_RUN_ARGS=()
NVIDIA_VISIBLE="all"
if [[ -n "${TTS_FORCE_CPU:-}" ]]; then
    log_warn "TTS_FORCE_CPU=1 — starting without --gpus (CPU only, high latency)."
    CUDA_VISIBLE_DEVICES=""
    NVIDIA_VISIBLE=""
else
    if [[ -n "${TTS_GPU_DEVICES:-}" ]]; then
        GPU_RUN_ARGS=(--gpus "device=${TTS_GPU_DEVICES}")
        NVIDIA_VISIBLE="${TTS_GPU_DEVICES}"
        log_info "GPU reservation: --gpus device=${TTS_GPU_DEVICES}"
    else
        GPU_RUN_ARGS=(--gpus all)
        log_info "GPU reservation: --gpus all"
    fi
fi

log_info "Image:              ${IMAGE_TAG}"
log_info "Container:          ${CONTAINER_NAME}"
log_info "Port mapping:       ${HOST_PORT} -> ${APP_PORT}"
log_info "MODEL_DIR:          ${MODEL_DIR} (baked into image; no HF_TOKEN)"
log_info "MODEL_POOL_SIZE:    ${MODEL_POOL_SIZE}"
log_info "SPARK_TTS_DTYPE:    ${SPARK_TTS_DTYPE}"
log_info "CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-<cpu>}"

# ------------------------------------------------------------------------------
# 5. CUDA compute gate (fail closed)
# ------------------------------------------------------------------------------
log_step "Step 5: CUDA compute gate"
if [[ -n "${TTS_FORCE_CPU:-}" ]]; then
    log_warn "TTS_FORCE_CPU=1 — skipping CUDA compute gate. Expect high TTS latency."
elif [[ "${TTS_SKIP_CUDA_PROBE:-}" == "1" ]]; then
    log_warn "TTS_SKIP_CUDA_PROBE=1 — skipping CUDA compute gate (unsafe; TTS may crash)."
else
    if [[ ${#GPU_RUN_ARGS[@]} -lt 1 ]]; then
        abort_tts_cuda_environment "No --gpus flag planned and TTS_FORCE_CPU is not set."
    fi
    run_cuda_compute_probe
fi

# ------------------------------------------------------------------------------
# 6. Pull image
# ------------------------------------------------------------------------------
log_step "Step 6: Pulling ${IMAGE_TAG}"
${SUDO_CMD} docker pull "${IMAGE_TAG}"

# ------------------------------------------------------------------------------
# 7. Replace any previous TTS container
# ------------------------------------------------------------------------------
log_step "Step 7: Managing container state"
TTS_COMPOSE="${SCRIPT_DIR}/tts-stack/docker-compose.yml"
if [[ -f "${TTS_COMPOSE}" ]]; then
    ${SUDO_CMD} docker compose -f "${TTS_COMPOSE}" down --remove-orphans >/dev/null 2>&1 || true
fi

for name in "${CONTAINER_NAME}" spark-tts-frontend spark-tts-streaming etoil-tts cathedral-spark-tts; do
    if ${SUDO_CMD} docker ps -a --format '{{.Names}}' | grep -Eq "^${name}$"; then
        log_warn "Removing existing container '${name}'..."
        ${SUDO_CMD} docker stop "${name}" >/dev/null 2>&1 || true
        ${SUDO_CMD} docker rm "${name}" >/dev/null 2>&1 || true
    fi
done

# ------------------------------------------------------------------------------
# 8. docker run (WSL / GPU VM path)
# ------------------------------------------------------------------------------
log_step "Step 8: Starting Spark-TTS frontend"
${SUDO_CMD} docker run -d \
    --name "${CONTAINER_NAME}" \
    --restart unless-stopped \
    "${GPU_RUN_ARGS[@]}" \
    -p "${HOST_PORT}:${APP_PORT}" \
    -e CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES}" \
    -e NVIDIA_VISIBLE_DEVICES="${NVIDIA_VISIBLE}" \
    -e NVIDIA_DRIVER_CAPABILITIES="compute,utility" \
    -e MODEL_DIR="${MODEL_DIR}" \
    -e PORT="${APP_PORT}" \
    -e RUST_LOG="${RUST_LOG}" \
    -e MODEL_POOL_SIZE="${MODEL_POOL_SIZE}" \
    -e SPARK_TTS_DTYPE="${SPARK_TTS_DTYPE}" \
    --shm-size=4gb \
    "${IMAGE_TAG}"

log_info "Container started. Logs: ${SUDO_CMD} docker logs -f ${CONTAINER_NAME}"

# ------------------------------------------------------------------------------
# 9. Health wait
# ------------------------------------------------------------------------------
log_step "Step 9: Waiting for service health"
MAX_RETRIES="${TTS_READY_TRIES:-180}"
RETRY_COUNT=0
HEALTHY=false

echo -n "Waiting for Spark-TTS to initialize..."
while [ $RETRY_COUNT -lt $MAX_RETRIES ]; do
    if curl -s -f "http://127.0.0.1:${HOST_PORT}/" >/dev/null 2>&1 \
        || curl -s -o /dev/null -w '%{http_code}' --max-time 2 "http://127.0.0.1:${HOST_PORT}/" 2>/dev/null | grep -Eq '^[1-4][0-9][0-9]$'; then
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
    log_info "Spark-TTS frontend deployed and reachable on port ${HOST_PORT}"
    log_info "================================================================="
    echo -e "${GREEN}HTTP Endpoint:${NC}      http://localhost:${HOST_PORT}/v1/audio/speech/stream"
    echo -e "${GREEN}WebSocket Endpoint:${NC} ws://localhost:${HOST_PORT}/v1/audio/speech/stream/ws"
    echo -e "${GREEN}Container Logs:${NC}     ${SUDO_CMD} docker logs -f ${CONTAINER_NAME}"
    echo ""
    echo "Quick test:"
    echo "  curl -X POST http://localhost:${HOST_PORT}/v1/audio/speech/stream \\"
    echo "    -H 'Content-Type: application/json' \\"
    echo "    -d '{\"text\": \"Oli otya, nkulamusizza okuva e Uganda.\", \"speaker_id\": \"lug_female_4\"}' \\"
    echo "    --output output.pcm"
    echo ""
else
    echo ""
    log_error "Service did not respond within timeout period."
    log_warn "On nested Docker GPU jobs, CUDA compute often fails even when nvidia-smi works."
    echo "  ${SUDO_CMD} docker logs -f ${CONTAINER_NAME}"
    ${SUDO_CMD} docker logs --tail=80 "${CONTAINER_NAME}" || true
    exit 1
fi
