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
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=gpu_env.sh
source "${SCRIPT_DIR}/gpu_env.sh"

PROJECT_DIR="${STT_PROJECT_DIR:-${SCRIPT_DIR}/stt-stack}"
ENV_FILE="${PROJECT_DIR}/.env"
COMPOSE_FILE="${PROJECT_DIR}/docker-compose.yml"
NGINX_CONF="${PROJECT_DIR}/nginx-speaches.conf"

DEFAULT_HF_TOKEN="${HF_TOKEN:-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}"
ETOIL_HOST_PORT="${ETOIL_HOST_PORT:-9090}"
SPEACHES_HOST_PORT_BASE="${SPEACHES_HOST_PORT_BASE:-9000}"
# One replica per GPU (full card) when count>1; set 0 for single multi-GPU container.
STT_SPEACHES_PER_GPU="${STT_SPEACHES_PER_GPU:-1}"
STT_READY_TRIES="${STT_READY_TRIES:-600}"  # ~20 min for first HF pull
GPU_PLAN_MODE="${GPU_PLAN_MODE:-stt}"

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

wait_http() {
  local name="$1" url="$2" tries="${3:-90}"
  log "Waiting for ${name} (${url}) up to $((tries * 2))s..."
  local i
  for ((i = 1; i <= tries; i++)); do
    if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then
      log "${name} is up"
      return 0
    fi
    if [[ "$name" == "etoil-api" ]] && docker ps -a --format '{{.Names}} {{.Status}}' 2>/dev/null \
      | grep -E '^cathedral-etoil-api ' | grep -qiE 'restarting|exited'; then
      err "etoil-api container is crash-looping (not a slow model pull)."
      docker logs cathedral-etoil-api --tail=30 2>/dev/null || true
      return 1
    fi
    if (( i % 15 == 0 )); then
      log "  still waiting (${i}/${tries}) — model download may be in progress"
    fi
    sleep 2
  done
  err "${name} did not become ready: ${url}"
  docker compose -f "${COMPOSE_FILE}" logs --tail=80 || true
  return 1
}

smoke_etoil() {
  local base="http://127.0.0.1:${ETOIL_HOST_PORT}"
  log "Contract smoke: GET ${base}/health"
  curl -fsS --max-time 10 "${base}/health" >/dev/null

  # Prefer OpenAI-compatible transcription if exposed; else accept health-only.
  local tmp wav
  tmp="$(mktemp -d)"
  wav="${tmp}/tone.wav"
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$wav" <<'PY' 2>/dev/null || true
import sys, wave, math, struct
path = sys.argv[1]
rate, n = 16000, 16000
with wave.open(path, "w") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
    for i in range(n):
        v = int(8000 * math.sin(2 * math.pi * 440 * i / rate))
        w.writeframes(struct.pack("<h", v))
PY
  fi
  if [[ -f "$wav" ]]; then
    local code
    code="$(curl -sS -o /tmp/stt_smoke_body.json -w '%{http_code}' --max-time 120 \
      -F "file=@${wav};type=audio/wav" \
      -F "model=Achuka/etoil-whisper-stt" \
      "${base}/v1/audio/transcriptions" 2>/dev/null || echo "000")"
    if [[ "$code" == "200" ]]; then
      log "Contract smoke: POST /v1/audio/transcriptions → 200"
    else
      # Alternate Avoices-shaped path used by some etoil builds.
      code="$(curl -sS -o /tmp/stt_smoke_body.json -w '%{http_code}' --max-time 120 \
        -F "file=@${wav};type=audio/wav" \
        "${base}/transcribe" 2>/dev/null || echo "000")"
      if [[ "$code" == "200" ]]; then
        log "Contract smoke: POST /transcribe → 200"
      else
        warn "Transcription smoke returned HTTP ${code} (health OK; check model/logs if scoring fails)"
        [[ -f /tmp/stt_smoke_body.json ]] && head -c 400 /tmp/stt_smoke_body.json || true
        echo
      fi
    fi
  else
    warn "Could not synthesize WAV; skipped transcription smoke"
  fi
  rm -rf "$tmp"
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
STT_GPU_DEVICES=${STT_GPU_DEVICES}
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
  local device_yaml
  device_yaml="$(gpu_compose_device_ids_yaml "$devices")"

  cat > "${COMPOSE_FILE}" <<EOF
# Generated by stt_install.sh — do not edit by hand.
name: cathedral-voice-stt

services:
  speaches:
    image: ghcr.io/speaches-ai/speaches:latest-cuda
    container_name: cathedral-speaches
    restart: unless-stopped
    networks: [stt-net]
    volumes:
      - hf-hub-cache:/home/ubuntu/.cache/huggingface/hub
    environment:
      HF_TOKEN: \${HF_TOKEN}
      NVIDIA_VISIBLE_DEVICES: "${devices}"
      CUDA_VISIBLE_DEVICES: "$(gpu_index_list "$(gpu_device_count "$devices")")"
    ports:
      - "${SPEACHES_HOST_PORT_BASE}:8000"
    deploy:
      resources:
        reservations:
          devices:
${device_yaml}
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
  local id idx=0 port
  local yaml_services="" depends_list="" upstream_servers=""

  IFS=',' read -r -a arr <<<"$devices"
  for id in "${arr[@]}"; do
    id="$(echo "$id" | tr -d '[:space:]')"
    [[ -z "$id" ]] && continue
    port=$((SPEACHES_HOST_PORT_BASE + idx))
    yaml_services+="
  speaches-${idx}:
    image: ghcr.io/speaches-ai/speaches:latest-cuda
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
      - \"${port}:8000\"
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
  local devices="${STT_GPU_DEVICES}"
  if [[ -z "$devices" ]]; then
    err "No GPUs assigned to STT (STT_GPU_DEVICES empty). Install NVIDIA drivers or set STT_GPU_DEVICES."
    exit 1
  fi
  local n
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
  if [[ -f /.dockerenv ]] && [[ -z "${STT_ALLOW_NESTED_DOCKER:-}" ]]; then
    warn "Shell appears to be inside a container (/.dockerenv)."
    warn "Run stt_install on the GPU host VM, not inside a dev container."
    warn "Bind mounts to ./audio fail when the Docker daemon is on the host."
    warn "This script now uses a named volume (stt-audio) to avoid that."
    warn "Set STT_ALLOW_NESTED_DOCKER=1 to silence this warning."
  fi
}

start_stack() {
  log "Pulling images..."
  docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" pull
  log "Starting STT stack (idempotent up -d)..."
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
}

main() {
  require_sudo
  plan_gpu_devices "${GPU_PLAN_MODE}"
  check_disk_gb 40 "${PROJECT_DIR}" || warn "Low disk — HF model pulls may fail"
  install_docker
  install_nvidia_toolkit_if_needed "$SUDO"
  if command -v nvidia-smi &>/dev/null; then
    log "GPUs: $(nvidia-smi --query-gpu=name --format=csv,noheader | paste -sd'; -')"
  else
    warn "nvidia-smi not found — speaches needs an NVIDIA GPU + driver."
  fi
  write_env_file
  warn_docker_in_docker
  write_compose_file
  start_stack
  wait_http "etoil-api" "http://127.0.0.1:${ETOIL_HOST_PORT}/health" "${STT_READY_TRIES}"
  smoke_etoil || true

  log "Done."
  echo "  ASR API (etoil) : http://127.0.0.1:${ETOIL_HOST_PORT}"
  echo "  Miner upstream  : MINER_ASR_UPSTREAM=http://127.0.0.1:${ETOIL_HOST_PORT}"
  echo "  STT GPUs        : ${STT_GPU_DEVICES}"
  echo "  Logs            : docker compose -f ${COMPOSE_FILE} logs -f"
  echo "  Stop            : docker compose -f ${COMPOSE_FILE} down"
}

main "$@"
