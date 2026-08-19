#!/usr/bin/env bash
#
# stt_install.sh — Install & run cathedral-voice ASR (speaches + etoil-api).
#
# GPU rules (via gpu_env.sh / GPU_PLAN_MODE):
#   stt  (default when run alone) — ALL host GPUs for ASR; none idle
#   both — only the STT partition from the shared plan
#
# Multi-GPU: one speaches replica per GPU + nginx round-robin LB so etoil
# spreads work across every card (SPEACHES_BASE_URL → speaches-lb).
#
# Usage:
#   ./violet/miner/stt_install.sh
#   GPU_PLAN_MODE=both STT_GPU_DEVICES=0,1 ./violet/miner/stt_install.sh
#   STT_FORCE_CPU=1 ./violet/miner/stt_install.sh       # CPU Whisper (slow; nested Docker)
#   STT_SKIP_CUDA_PROBE=1 ./violet/miner/stt_install.sh # unsafe override
#
# CUDA compute is probed before pull/run (like tts_install.sh). Nested GPU jobs that
# only expose nvidia-smi are rejected so speaches does not pass /health then 500.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=gpu_env.sh
source "${SCRIPT_DIR}/gpu_env.sh"
# shellcheck source=install_lib.sh
INSTALL_LOG_PREFIX="stt_install"
source "${SCRIPT_DIR}/install_lib.sh"

PROJECT_DIR="${STT_PROJECT_DIR:-${SCRIPT_DIR}/stt-stack}"
ENV_FILE="${PROJECT_DIR}/.env"
COMPOSE_FILE="${PROJECT_DIR}/docker-compose.yml"
NGINX_CONF="${PROJECT_DIR}/nginx-speaches.conf"

# Pick up HF_TOKEN from repo .env (export on CLI still wins if already set).
load_repo_dotenv "${SCRIPT_DIR}" || true
DEFAULT_HF_TOKEN="$(default_hf_token)"
validate_hf_token_value "${DEFAULT_HF_TOKEN}" || exit 1
ETOIL_HOST_PORT="${ETOIL_HOST_PORT:-9090}"
SPEACHES_HOST_PORT_BASE="${SPEACHES_HOST_PORT_BASE:-9000}"
# One replica per GPU (full card) when count>1; set 0 for single multi-GPU container.
STT_SPEACHES_PER_GPU="${STT_SPEACHES_PER_GPU:-1}"
STT_READY_TRIES="${STT_READY_TRIES:-600}"  # ~20 min for first HF pull
STT_STRICT="${STT_STRICT:-1}"
STT_FORCE_CPU="${STT_FORCE_CPU:-0}"
STT_SKIP_CUDA_PROBE="${STT_SKIP_CUDA_PROBE:-0}"
STT_CPU_THREADS="${STT_CPU_THREADS:-4}"
GPU_PLAN_MODE="${GPU_PLAN_MODE:-stt}"
STT_GPU_RUN_ARGS=()

log()  { echo -e "\033[1;32m[stt_install]\033[0m $*"; }
warn() { echo -e "\033[1;33m[stt_install warn]\033[0m $*"; }
err()  { echo -e "\033[1;31m[stt_install error]\033[0m $*" >&2; }

require_sudo() {
  if [[ ${EUID} -ne 0 ]]; then
    SUDO="sudo"
  else
    SUDO=""
  fi
}

abort_stt_cuda_environment() {
  local detail="${1:-CUDA compute probe failed}"
  err "${detail}"
  echo
  echo "STT install stopped — this host cannot run speaches Whisper on GPU."
  echo "  nvidia-smi can list the GPU while Docker cannot allocate CUDA"
  echo "  (cudaErrorDevicesUnavailable / DevicesUnavailable on /transcribe)."
  echo
  echo "Typical cause: nested Docker (/.dockerenv) — inner containers get NVML only."
  echo
  echo "What to do:"
  echo "  • Run STT on a bare GPU VM or WSL2 where Docker is the host"
  echo "  • CPU smoke/dev only: STT_FORCE_CPU=1 ./violet/miner/stt_install.sh"
  echo "  • ASR-only elsewhere: MINER_SERVICES=asr ./violet/miner/start.sh prod --no-follow"
  echo
  exit 1
}

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
  local out rc=0 probe_py probe_image cuda_dev
  probe_image="${STT_CUDA_PROBE_IMAGE:-python:3.11-slim-bookworm}"
  probe_py="$(cuda_compute_probe_py)"
  cuda_dev="${STT_GPU_DEVICES:-0}"
  cuda_dev="${cuda_dev%%,*}"

  log "CUDA compute gate: context + 4KiB alloc inside Docker (not nvidia-smi)."
  if ! $SUDO docker pull "${probe_image}" >/dev/null; then
    abort_stt_cuda_environment "Could not pull CUDA probe image ${probe_image}."
  fi

  set +e
  out="$(
    $SUDO docker run --rm \
      "${STT_GPU_RUN_ARGS[@]}" \
      -e NVIDIA_DRIVER_CAPABILITIES=compute,utility \
      -e CUDA_VISIBLE_DEVICES="${cuda_dev}" \
      -e NVIDIA_VISIBLE_DEVICES="${cuda_dev}" \
      --entrypoint python3 \
      "${probe_image}" \
      -c "${probe_py}" 2>&1
  )"
  rc=$?
  set -e

  echo "${out}" | while IFS= read -r line; do
    [[ -n "$line" ]] && log "  probe: ${line}"
  done

  if [[ "$rc" -eq 0 ]] && echo "${out}" | grep -q 'ALLOC_OK'; then
    log "CUDA compute gate passed — speaches may use GPU."
    return 0
  fi
  abort_stt_cuda_environment "Docker CUDA alloc failed (probe exit ${rc}). nvidia-smi is not sufficient."
}

speaches_image() {
  if [[ "${STT_FORCE_CPU}" == "1" ]]; then
    echo "ghcr.io/speaches-ai/speaches:latest-cpu"
  else
    echo "ghcr.io/speaches-ai/speaches:latest-cuda"
  fi
}

speaches_whisper_env_block() {
  if [[ "${STT_FORCE_CPU}" == "1" ]]; then
    cat <<EOF
      WHISPER__INFERENCE_DEVICE: cpu
      WHISPER__CPU_THREADS: "${STT_CPU_THREADS}"
EOF
  fi
}

speaches_gpu_env_block() {
  local devices="$1"
  if [[ "${STT_FORCE_CPU}" == "1" || -z "$devices" ]]; then
    return 0
  fi
  cat <<EOF
      NVIDIA_VISIBLE_DEVICES: "${devices}"
      CUDA_VISIBLE_DEVICES: "$(gpu_index_list "$(gpu_device_count "$devices")")"
EOF
}

speaches_gpu_deploy_block() {
  local device_yaml="$1"
  if [[ "${STT_FORCE_CPU}" == "1" ]]; then
    return 0
  fi
  cat <<EOF
    deploy:
      resources:
        reservations:
          devices:
${device_yaml}
EOF
}

wait_http() {
  local name="$1" url="$2" tries="${3:-90}"
  local cname=""
  [[ "$name" == "etoil-api" ]] && cname="cathedral-etoil-api"
  if ! wait_http_ok "$name" "$url" "$tries" "$cname"; then
    docker compose -f "${COMPOSE_FILE}" logs --tail=80 || true
    return 1
  fi
}

smoke_etoil() {
  local base="http://127.0.0.1:${ETOIL_HOST_PORT}"
  local ok=1
  log "Contract smoke (miner-facing): GET ${base}/health + POST /transcribe"
  if ! curl -fsS --max-time 10 "${base}/health" >/dev/null; then
    err "GET /health failed"
    ok=0
  fi

  local tmp wav code
  tmp="$(mktemp -d)"
  wav="${tmp}/tone.wav"
  if make_smoke_wav "$wav"; then
    # etoil-api exposes /transcribe (validator/miner contract), not OpenAI paths.
    code="$(curl -sS -o /tmp/stt_smoke_body.json -w '%{http_code}' --max-time 300 \
      -F "file=@${wav};type=audio/wav" \
      -F "language=eng" \
      -F "response_format=json" \
      "${base}/transcribe" 2>/dev/null || echo "000")"
    if [[ "$code" == "200" ]]; then
      log "Contract smoke: POST /transcribe → 200"
    else
      warn "Transcription smoke returned HTTP ${code}"
      [[ -f /tmp/stt_smoke_body.json ]] && head -c 400 /tmp/stt_smoke_body.json || true
      echo
      ok=0
    fi
  else
    warn "Could not synthesize WAV; skipped transcription smoke"
    ok=0
  fi
  rm -rf "$tmp"
  if [[ "$ok" -ne 1 ]]; then
    if [[ "${STT_STRICT}" == "1" ]]; then
      err "STT_STRICT=1 — install smoke failed"
      return 1
    fi
    return 1
  fi
  return 0
}

install_docker() {
  if command -v docker &>/dev/null; then
    log "Docker already installed: $(docker --version)"
  else
    log "Installing Docker Engine..."
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
    $SUDO sh /tmp/get-docker.sh
    rm -f /tmp/get-docker.sh
    $SUDO usermod -aG docker "${USER}" || true
    warn "Added ${USER} to docker group — re-login or 'newgrp docker' if needed."
  fi
  if ! docker compose version &>/dev/null; then
    err "docker compose plugin missing"
    exit 1
  fi
}

write_env_file() {
  mkdir -p "${PROJECT_DIR}"
  cat > "${ENV_FILE}" <<EOF
HF_TOKEN=${DEFAULT_HF_TOKEN}
ETOIL_HOST_PORT=${ETOIL_HOST_PORT}
STT_GPU_DEVICES=${STT_GPU_DEVICES:-}
STT_FORCE_CPU=${STT_FORCE_CPU}
GPU_PLAN_MODE=${GPU_PLAN_MODE}
EOF
  chmod 600 "${ENV_FILE}"
  log "Wrote ${ENV_FILE} (mode 600)"
}

# etoil-api reads EXTERNAL_API_URL at import time (OpenAI client → speaches /v1).
# SPEACHES_BASE_URL is used for realtime WebSocket proxy.
etoil_environment_block() {
  local upstream="$1"
  cat <<EOF
      EXTERNAL_API_URL: ${upstream}
      SPEACHES_BASE_URL: ${upstream}
      SPEACHES_API_KEY: empty
      SPEACHES_TRANSCRIPTION_MODEL: Achuka/etoil-whisper-stt
      SPEACHES_OPEN_TIMEOUT: "60"
      PYTHONUNBUFFERED: "1"
EOF
}

write_compose_single_speaches() {
  local devices="$1"
  local device_yaml img
  device_yaml="$(gpu_compose_device_ids_yaml "$devices")"
  img="$(speaches_image)"

  cat > "${COMPOSE_FILE}" <<EOF
# Generated by stt_install.sh — do not edit by hand.
name: cathedral-voice-stt

services:
  speaches:
    image: ${img}
    container_name: cathedral-speaches
    restart: unless-stopped
    networks: [stt-net]
    volumes:
      - hf-hub-cache:/home/ubuntu/.cache/huggingface/hub
    environment:
      HF_TOKEN: \${HF_TOKEN}
$(speaches_gpu_env_block "$devices")
$(speaches_whisper_env_block)
    ports:
      - "127.0.0.1:${SPEACHES_HOST_PORT_BASE}:8000"
$(speaches_gpu_deploy_block "${device_yaml}")
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 120s

  etoil-api:
    image: simonallanachuka/etoil-api:latest
    container_name: cathedral-etoil-api
    restart: unless-stopped
    networks: [stt-net]
    ports:
      # 0.0.0.0 bind — sidecar in Docker reaches ASR via host.docker.internal
      - "\${ETOIL_HOST_PORT:-9090}:8000"
    volumes:
      - stt-audio:/app/audio
    environment:
$(etoil_environment_block "http://speaches:8000")
    depends_on:
      - speaches
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 60s

networks:
  stt-net:
    driver: bridge

volumes:
  hf-hub-cache:
  stt-audio:
EOF
}

# One speaches per GPU + nginx LB → every card receives traffic.
write_compose_per_gpu_speaches() {
  local devices="$1"
  local id idx=0 port img
  local yaml_services="" depends_list="" upstream_servers=""
  img="$(speaches_image)"

  IFS=',' read -r -a arr <<<"$devices"
  for id in "${arr[@]}"; do
    id="$(echo "$id" | tr -d '[:space:]')"
    [[ -z "$id" ]] && continue
    port=$((SPEACHES_HOST_PORT_BASE + idx))
    yaml_services+="
  speaches-${idx}:
    image: ${img}
    container_name: cathedral-speaches-${idx}
    restart: unless-stopped
    networks: [stt-net]
    volumes:
      - hf-hub-cache:/home/ubuntu/.cache/huggingface/hub
    environment:
      HF_TOKEN: \${HF_TOKEN}
      NVIDIA_VISIBLE_DEVICES: \"${id}\"
      CUDA_VISIBLE_DEVICES: \"0\"
    ports:
      - \"127.0.0.1:${port}:8000\"
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              device_ids: [\"${id}\"]
              capabilities: [gpu]
    healthcheck:
      test: [\"CMD\", \"curl\", \"-f\", \"http://localhost:8000/health\"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 120s
"
    depends_list+="      - speaches-${idx}"$'\n'
    upstream_servers+="        server speaches-${idx}:8000;"$'\n'
    idx=$((idx + 1))
  done

  mkdir -p "${PROJECT_DIR}"
  cat > "${NGINX_CONF}" <<EOF
# Generated by stt_install.sh — round-robin across all speaches GPUs.
worker_processes auto;
error_log /var/log/nginx/error.log warn;
pid /tmp/nginx.pid;
events { worker_connections 4096; }
http {
  upstream speaches_pool {
    least_conn;
${upstream_servers}
  }
  server {
    listen 8000;
    location / {
      proxy_http_version 1.1;
      proxy_set_header Host \$host;
      proxy_set_header Connection "";
      proxy_read_timeout 600s;
      proxy_send_timeout 600s;
      proxy_pass http://speaches_pool;
    }
  }
}
EOF

  cat > "${COMPOSE_FILE}" <<EOF
# Generated by stt_install.sh — one speaches per GPU + nginx LB.
name: cathedral-voice-stt

services:
${yaml_services}
  speaches-lb:
    image: nginx:alpine
    container_name: cathedral-speaches-lb
    restart: unless-stopped
    networks: [stt-net]
    volumes:
      - ./nginx-speaches.conf:/etc/nginx/nginx.conf:ro
    depends_on:
${depends_list}

  etoil-api:
    image: simonallanachuka/etoil-api:latest
    container_name: cathedral-etoil-api
    restart: unless-stopped
    networks: [stt-net]
    ports:
      # 0.0.0.0 bind — sidecar in Docker reaches ASR via host.docker.internal
      - "\${ETOIL_HOST_PORT:-9090}:8000"
    volumes:
      - stt-audio:/app/audio
    environment:
$(etoil_environment_block "http://speaches-lb:8000")
    depends_on:
      - speaches-lb
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 60s

networks:
  stt-net:
    driver: bridge

volumes:
  hf-hub-cache:
  stt-audio:
EOF
}

write_compose_file() {
  local devices="${STT_GPU_DEVICES:-}"
  local n=0

  if [[ "${STT_FORCE_CPU}" == "1" ]]; then
    warn "STT_FORCE_CPU=1 — CPU Whisper inference (slow; not for production mining)"
    log "Mode: single speaches (CPU, latest-cpu image)"
    write_compose_single_speaches ""
    log "Wrote ${COMPOSE_FILE}"
    return 0
  fi

  if [[ -z "$devices" ]]; then
    err "No GPUs assigned to STT (STT_GPU_DEVICES empty). Install NVIDIA drivers or set STT_GPU_DEVICES."
    exit 1
  fi
  n="$(gpu_device_count "$devices")"
  log "STT GPUs: ${devices} (count=${n}, plan=${GPU_PLAN_MODE})"

  if [[ "${STT_SPEACHES_PER_GPU}" == "1" ]] && (( n > 1 )); then
    log "Mode: one speaches per GPU + nginx least_conn LB (all GPUs busy)"
    write_compose_per_gpu_speaches "$devices"
  else
    log "Mode: single speaches with all STT GPUs visible"
    write_compose_single_speaches "$devices"
  fi
  log "Wrote ${COMPOSE_FILE}"
}

warn_docker_in_docker() {
  warn_nested_docker "STT_ALLOW_NESTED_DOCKER"
}

stt_speaches_replica_count() {
  local devices="${STT_GPU_DEVICES:-}"
  local n
  n="$(gpu_device_count "$devices")"
  if [[ "${STT_SPEACHES_PER_GPU}" == "1" ]] && (( n > 1 )); then
    echo "$n"
  else
    echo 1
  fi
}

start_stack() {
  local replicas n
  replicas="$(stt_speaches_replica_count)"
  n="$replicas"

  log "Pulling images..."
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" pull

  if (( replicas > 1 )); then
    # Avoid HuggingFace file-lock fights: bootstrap model on GPU 0 first.
    log "Multi-GPU: starting speaches-0 only for model prefetch..."
    docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" up -d --remove-orphans speaches-0
    wait_http "speaches-0" "http://127.0.0.1:${SPEACHES_HOST_PORT_BASE}/health" 120
    pull_speaches_model
    warmup_speaches_model || warn "GPU warm-up incomplete on speaches-0 — remaining replicas may retry"
    log "Starting full STT stack (all speaches + nginx + etoil)..."
  else
    log "Starting STT stack (idempotent up -d)..."
  fi

  if ! docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" up -d --remove-orphans; then
    err "docker compose up failed."
    if [[ -f /.dockerenv ]]; then
      err "If you are inside a container, run ./violet/miner/stt_install.sh on the bare-metal GPU host instead."
    fi
    docker compose -f "${COMPOSE_FILE}" ps || true
    docker compose -f "${COMPOSE_FILE}" logs --tail=40 || true
    exit 1
  fi
  docker compose -f "${COMPOSE_FILE}" ps
  STT_SPEACHES_REPLICAS="$n"
  export STT_SPEACHES_REPLICAS
}

pull_speaches_model() {
  local model="${SPEACHES_TRANSCRIPTION_MODEL:-Achuka/etoil-whisper-stt}"
  local speaches_url="http://127.0.0.1:${SPEACHES_HOST_PORT_BASE}"
  local timeout_s="${STT_MODEL_PULL_TIMEOUT_S:-1800}"
  local poll_s=15 elapsed=0

  model_list_has() {
    curl -fsS --max-time 10 "${speaches_url}/v1/models" 2>/dev/null | grep -q "${model}"
  }

  log "Ensuring model '${model}' is installed in speaches..."

  if model_list_has; then
    log "Model '${model}' already listed in /v1/models"
    return 0
  fi

  # POST is synchronous in speaches — can hang 10–30+ min on first HF pull.
  # Kick off in background and poll /v1/models instead of blocking the install script.
  log "Starting model download (POST runs in background; may take 10–30 min first time)..."
  log "  HuggingFace filelock lines in docker logs are normal while one worker downloads."
  curl -sS -o /tmp/speaches_model_pull.json \
    -X POST "${speaches_url}/v1/models/${model}" \
    --max-time "${timeout_s}" &
  local pull_pid=$!

  while (( elapsed < timeout_s )); do
    if model_list_has; then
      log "Model '${model}' is listed in /v1/models"
      wait "$pull_pid" 2>/dev/null || true
      return 0
    fi
    if ! kill -0 "$pull_pid" 2>/dev/null; then
      break
    fi
    if (( elapsed > 0 && elapsed % 60 == 0 )); then
      log "  still downloading (${elapsed}s / ${timeout_s}s) — watch: docker logs -f cathedral-speaches"
    fi
    sleep "$poll_s"
    elapsed=$((elapsed + poll_s))
  done

  if model_list_has; then
    log "Model '${model}' is listed in /v1/models"
    return 0
  fi

  warn "Model '${model}' not listed after ${timeout_s}s"
  warn "Check: curl ${speaches_url}/v1/models  and  docker logs -f cathedral-speaches"
  kill "$pull_pid" 2>/dev/null || true
  return 1
}

# Listed in /v1/models ≠ weights loaded on GPU. First inference can take minutes.
warmup_speaches_model() {
  local model="${SPEACHES_TRANSCRIPTION_MODEL:-Achuka/etoil-whisper-stt}"
  local speaches_url="http://127.0.0.1:${SPEACHES_HOST_PORT_BASE}"
  local timeout_s="${STT_WARMUP_TIMEOUT_S:-600}"
  local tmp wav code

  if [[ "${STT_FORCE_CPU}" == "1" ]]; then
    log "Warming up '${model}' on CPU (first inference loads weights; up to ${timeout_s}s)..."
  else
    log "Warming up '${model}' on GPU (first inference loads weights; up to ${timeout_s}s)..."
  fi
  tmp="$(mktemp -d)"
  wav="${tmp}/tone.wav"
  if ! make_smoke_wav "$wav"; then
    warn "Could not synthesize WAV; skipped GPU warm-up"
    rm -rf "$tmp"
    return 1
  fi

  code="$(curl -sS -o /tmp/speaches_warmup.json -w '%{http_code}' --max-time "${timeout_s}" \
    -F "file=@${wav};type=audio/wav" \
    -F "model=${model}" \
    "${speaches_url}/v1/audio/transcriptions" 2>/dev/null || echo "000")"
  rm -rf "$tmp"

  if [[ "$code" == "200" ]]; then
    log "GPU warm-up OK (speaches /v1/audio/transcriptions → 200)"
    return 0
  fi

  warn "GPU warm-up returned HTTP ${code}"
  [[ -f /tmp/speaches_warmup.json ]] && head -c 400 /tmp/speaches_warmup.json || true
  echo
  return 1
}

main() {
  require_sudo
  if [[ "${STT_FORCE_CPU}" == "1" ]]; then
    warn "STT_FORCE_CPU=1 — skipping GPU plan; Whisper runs on CPU."
    export STT_GPU_DEVICES=""
  else
    plan_gpu_devices "${GPU_PLAN_MODE}"
  fi
  check_disk_gb 40 "${PROJECT_DIR}" || warn "Low disk — HF model pulls may fail"
  install_docker
  if [[ "${STT_FORCE_CPU}" != "1" ]]; then
    install_nvidia_toolkit_if_needed "$SUDO"
    if command -v nvidia-smi &>/dev/null; then
      log "GPU inventory for Capacity scoring:"
      print_gpu_inventory | while IFS= read -r line; do log "  ${line}"; done
    else
      warn "nvidia-smi not found — speaches needs an NVIDIA GPU + driver."
    fi
    if [[ -n "${STT_GPU_DEVICES:-}" ]]; then
      STT_GPU_RUN_ARGS=(--gpus "device=${STT_GPU_DEVICES}")
    else
      STT_GPU_RUN_ARGS=(--gpus all)
    fi
    if [[ "${STT_SKIP_CUDA_PROBE}" == "1" ]]; then
      warn "STT_SKIP_CUDA_PROBE=1 — skipping CUDA compute gate (unsafe; /transcribe may 500)."
    else
      run_cuda_compute_probe
    fi
  fi
  write_env_file
  warn_docker_in_docker
  write_compose_file
  start_stack
  if [[ "${STT_SPEACHES_REPLICAS:-1}" -le 1 ]]; then
    wait_http "speaches" "http://127.0.0.1:${SPEACHES_HOST_PORT_BASE}/health" 120
    pull_speaches_model || warn "Model pull incomplete — check docker logs"
    warmup_speaches_model || warn "GPU warm-up incomplete — first request may be slow"
  fi
  wait_http "etoil-api" "http://127.0.0.1:${ETOIL_HOST_PORT}/health" "${STT_READY_TRIES}"
  if ! smoke_etoil; then
    if [[ "${STT_STRICT}" == "1" ]]; then
      err "STT contract smoke failed (set STT_STRICT=0 to continue anyway)"
      exit 1
    fi
    warn "STT smoke failed (STT_STRICT=0 — continuing)"
  fi

  log "Done."
  echo "  ASR API (etoil) : http://127.0.0.1:${ETOIL_HOST_PORT}"
  echo "  Miner upstream  : MINER_ASR_UPSTREAM=http://host.docker.internal:${ETOIL_HOST_PORT}"
  if [[ "${STT_FORCE_CPU}" == "1" ]]; then
    echo "  STT mode        : CPU (STT_FORCE_CPU=1)"
  else
    echo "  STT GPUs        : ${STT_GPU_DEVICES}"
  fi
  echo "  Logs            : docker compose -f ${COMPOSE_FILE} logs -f"
  echo "  Stop            : docker compose -f ${COMPOSE_FILE} down"
}

main "$@"
