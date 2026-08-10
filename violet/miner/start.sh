#!/usr/bin/env bash
# Start the Violet miner stack (ASR + TTS + miner sidecar).
#
# Usage (from repo root or anywhere):
#   ./violet/miner/start.sh test      # local samples, --no-chain (default)
#   ./violet/miner/start.sh prod      # chain-ready + wallet mount
#   ./violet/miner/start.sh prod --gpu
#   ./violet/miner/start.sh stop
#   ./violet/miner/start.sh logs
#   ./violet/miner/start.sh status
#
# Flow (test/prod):
#   1. ensure .env
#   2. auto-detect ASR / TTS / miner ports (or keep env overrides)
#   3. prompt for public IP → builds MINER_PUBLIC_ENDPOINT
#   4. build images + start containers
#   5. stream logs until healthy, qualification smoke, follow logs
#
# Ports:
#   ASR and TTS listen on different ports (defaults 9000 / 8080). The script
#   detects whatever is already healthy on the host, else uses defaults / next
#   free ports, then wires MINER_ASR_UPSTREAM / MINER_TTS_UPSTREAM.
#   Validators dial the miner sidecar only (MINER_PUBLIC_ENDPOINT = public IP
#   + miner port). The miner proxies to ASR/TTS on the detected ports.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

COMPOSE_BASE=(docker compose -f docker/docker-compose.miner.yml)
MODE="${1:-test}"
GPU=0
FOLLOW=1
SKIP_ENDPOINT_PROMPT="${SKIP_ENDPOINT_PROMPT:-0}"

# Service listen ports (overridden by resolve_service_ports).
ASR_PORT="${ASR_PORT:-}"
TTS_PORT="${TTS_PORT:-}"
MINER_PORT="${MINER_PORT:-}"

usage() {
  sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'
}

need_docker() {
  command -v docker >/dev/null 2>&1 || {
    echo "docker is required" >&2
    exit 1
  }
  docker compose version >/dev/null 2>&1 || {
    echo "docker compose plugin is required" >&2
    exit 1
  }
}

ensure_env() {
  if [[ ! -f .env ]]; then
    echo "==> creating .env from .env.example"
    cp .env.example .env
  fi
  # shellcheck disable=SC1091
  set -a
  # Prefer repo .env without clobbering explicit exports from the caller.
  [[ -f .env ]] && source .env
  set +a
}

detect_public_ip() {
  local ip=""
  local url
  for url in \
    "https://api.ipify.org" \
    "https://ifconfig.me/ip" \
    "https://icanhazip.com"; do
    ip="$(curl -fsS --max-time 4 "$url" 2>/dev/null | tr -d '[:space:]' || true)"
    if [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || [[ "$ip" =~ : ]]; then
      echo "$ip"
      return 0
    fi
  done
  return 1
}

is_local_endpoint() {
  local ep="${1:-}"
  [[ -z "$ep" ]] && return 0
  case "$ep" in
    *127.0.0.1*|*localhost*|*miner.example.com*|*0.0.0.0*) return 0 ;;
    *) return 1 ;;
  esac
}

upsert_env_var() {
  local key="$1" value="$2" file="${3:-.env}"
  local tmp
  tmp="$(mktemp)"
  if [[ -f "$file" ]] && grep -qE "^[# ]*${key}=" "$file"; then
    # Replace first assignment (commented or not).
    awk -v k="$key" -v v="$value" '
      BEGIN { done=0 }
      {
        if (!done && $0 ~ ("^[# ]*" k "=")) {
          print k "=" v
          done=1
        } else {
          print
        }
      }
      END { if (!done) print k "=" v }
    ' "$file" >"$tmp"
    mv "$tmp" "$file"
  else
    printf '\n%s=%s\n' "$key" "$value" >>"$file"
    rm -f "$tmp"
  fi
}

port_is_listening() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 && return 0
  fi
  if command -v nc >/dev/null 2>&1; then
    nc -z 127.0.0.1 "$port" >/dev/null 2>&1 && return 0
  fi
  return 1
}

port_health_ok() {
  local port="$1"
  curl -fsS --max-time 2 "http://127.0.0.1:${port}/health" >/dev/null 2>&1
}

# Pick first healthy /health among candidates; else first free; else first candidate.
pick_port() {
  local preferred="$1"
  shift
  local candidates=("$@")
  local p

  if [[ -n "$preferred" ]]; then
    echo "$preferred"
    return 0
  fi

  for p in "${candidates[@]}"; do
    if port_health_ok "$p"; then
      echo "$p"
      return 0
    fi
  done

  for p in "${candidates[@]}"; do
    if ! port_is_listening "$p"; then
      echo "$p"
      return 0
    fi
  done

  echo "${candidates[0]}"
}

docker_published_port() {
  # Best-effort: host port mapped to a container's internal port.
  local service="$1" internal="$2"
  local line
  line="$("${COMPOSE[@]}" port "$service" "$internal" 2>/dev/null | head -n1 || true)"
  if [[ "$line" =~ :([0-9]+)$ ]]; then
    echo "${BASH_REMATCH[1]}"
    return 0
  fi
  return 1
}

# ASR / TTS / miner listen on different ports. Detect running services, else
# defaults, then wire upstream URLs the miner sidecar will use.
resolve_service_ports() {
  local mode="$1"
  local asr_default=9000 tts_default=8080 miner_default=8091
  local asr_from_docker="" tts_from_docker="" miner_from_docker=""

  compose_files_for "$mode"

  # Prefer ports already published by a previous compose run.
  asr_from_docker="$(docker_published_port violet-asr "${ASR_PORT:-$asr_default}" || true)"
  tts_from_docker="$(docker_published_port violet-tts "${TTS_PORT:-$tts_default}" || true)"
  miner_from_docker="$(docker_published_port violet-miner "${MINER_PORT:-$miner_default}" || true)"

  # Env / .env wins; else docker published; else probe health / free ports.
  ASR_PORT="$(pick_port "${ASR_PORT:-${MOCK_ASR_PORT:-$asr_from_docker}}" \
    "$asr_default" 9001 9002 9100)"
  TTS_PORT="$(pick_port "${TTS_PORT:-${MOCK_TTS_PORT:-$tts_from_docker}}" \
    "$tts_default" 8081 8082 8180)"
  MINER_PORT="$(pick_port "${MINER_PORT:-$miner_from_docker}" \
    "$miner_default" 8092 8093 8191)"

  # Host publish aliases used by compose test overlay.
  MOCK_ASR_PORT="$ASR_PORT"
  MOCK_TTS_PORT="$TTS_PORT"

  # Inside the compose network the miner reaches ASR/TTS by service name.
  # If a healthy ASR/TTS is already on the host and not our container ports
  # path, still prefer docker DNS for the stack we are about to start.
  MINER_ASR_UPSTREAM="${MINER_ASR_UPSTREAM:-http://violet-asr:${ASR_PORT}}"
  MINER_TTS_UPSTREAM="${MINER_TTS_UPSTREAM:-http://violet-tts:${TTS_PORT}}"

  # If upstream was a stale hardcoded default, refresh to detected ports.
  case "$MINER_ASR_UPSTREAM" in
    http://violet-asr:*|http://127.0.0.1:*|http://localhost:*)
      MINER_ASR_UPSTREAM="http://violet-asr:${ASR_PORT}"
      ;;
  esac
  case "$MINER_TTS_UPSTREAM" in
    http://violet-tts:*|http://127.0.0.1:*|http://localhost:*)
      MINER_TTS_UPSTREAM="http://violet-tts:${TTS_PORT}"
      ;;
  esac

  export ASR_PORT TTS_PORT MINER_PORT MOCK_ASR_PORT MOCK_TTS_PORT
  export MINER_ASR_UPSTREAM MINER_TTS_UPSTREAM

  upsert_env_var "ASR_PORT" "$ASR_PORT" .env
  upsert_env_var "TTS_PORT" "$TTS_PORT" .env
  upsert_env_var "MINER_PORT" "$MINER_PORT" .env
  upsert_env_var "MOCK_ASR_PORT" "$ASR_PORT" .env
  upsert_env_var "MOCK_TTS_PORT" "$TTS_PORT" .env
  upsert_env_var "MINER_ASR_UPSTREAM" "$MINER_ASR_UPSTREAM" .env
  upsert_env_var "MINER_TTS_UPSTREAM" "$MINER_TTS_UPSTREAM" .env

  echo "==> service ports"
  echo "    ASR   : ${ASR_PORT}  (upstream ${MINER_ASR_UPSTREAM})"
  echo "    TTS   : ${TTS_PORT}  (upstream ${MINER_TTS_UPSTREAM})"
  echo "    miner : ${MINER_PORT}  (public announce uses this port)"
}

# Best practice: YOU choose the public IP/DNS. The chain axon is written from
# MINER_PUBLIC_ENDPOINT on announce — it is not a source of truth for first setup.
# ASR/TTS ports are auto-resolved; you only need the public IP (or full URL).
prompt_public_endpoint() {
  local mode="$1"
  local port="${MINER_PORT:-8091}"
  local current="${MINER_PUBLIC_ENDPOINT:-}"
  local detected="" suggested_ip="" answer="" host_or_url=""

  if [[ "$SKIP_ENDPOINT_PROMPT" == "1" ]]; then
    return 0
  fi
  # Non-interactive (CI / piped): keep env as-is.
  if [[ ! -t 0 ]]; then
    echo "==> non-interactive shell; using MINER_PUBLIC_ENDPOINT=${current:-<unset>}"
    return 0
  fi

  echo
  echo "============================================================"
  echo " Public IP (validators & router dial miner on port ${port})"
  echo "------------------------------------------------------------"
  echo " Auto-detected service ports on this host:"
  echo "   ASR  : ${ASR_PORT}   ← miner proxies /transcribe … here"
  echo "   TTS  : ${TTS_PORT}   ← miner proxies /v1/audio/speech … here"
  echo "   miner: ${port}   ← THIS is what you announce publicly"
  echo
  echo " Enter your public IP (or DNS). Port ${port} is appended automatically."
  echo " Full URL also accepted (http://ip:${port} or https://miner.example.com)."
  echo " Open TCP ${port} publicly. ASR/TTS stay on the docker network unless"
  echo " you intentionally publish ${ASR_PORT}/${TTS_PORT}."
  echo "============================================================"

  detected="$(detect_public_ip || true)"
  if [[ -n "$detected" ]]; then
    suggested_ip="$detected"
    echo "==> detected egress public IP: $detected"
  fi

  # Prefer showing bare IP in the prompt; we attach the miner port ourselves.
  local current_host=""
  if [[ -n "$current" ]] && ! is_local_endpoint "$current"; then
    current_host="${current#http://}"
    current_host="${current_host#https://}"
    current_host="${current_host%%/*}"
    current_host="${current_host%%:*}"
  fi

  if [[ "$mode" == "test" || "$mode" == "local" ]]; then
    local default="${current_host:-${suggested_ip:-127.0.0.1}}"
    read -r -p "Public IP / host [${default}]: " answer
    answer="${answer:-$default}"
  else
    local default="${current_host:-${suggested_ip:-}}"
    if [[ -z "$default" ]]; then
      read -r -p "Public IP / host (required): " answer
    else
      read -r -p "Public IP / host [${default}]: " answer
      answer="${answer:-$default}"
    fi
    if [[ -z "$answer" ]] || [[ "$answer" == "127.0.0.1" ]] || [[ "$answer" == "localhost" ]]; then
      echo "ERROR: prod needs a publicly reachable IP or DNS" >&2
      exit 1
    fi
  fi

  host_or_url="$answer"
  case "$host_or_url" in
    http://*|https://*)
      # If user pasted a URL without an explicit port, attach miner port
      # (unless https default 443 / http with existing port).
      if [[ "$host_or_url" =~ ^https?://[^/:]+$ ]]; then
        if [[ "$host_or_url" == https://* ]]; then
          answer="$host_or_url"
        else
          answer="${host_or_url}:${port}"
        fi
      else
        answer="$host_or_url"
      fi
      ;;
    *:*)
      # ip:port or host:port
      answer="http://${host_or_url}"
      ;;
    *)
      answer="http://${host_or_url}:${port}"
      ;;
  esac

  if [[ "$mode" != "test" && "$mode" != "local" ]] && is_local_endpoint "$answer"; then
    echo "ERROR: prod MINER_PUBLIC_ENDPOINT cannot be localhost" >&2
    exit 1
  fi

  export MINER_PUBLIC_ENDPOINT="$answer"
  upsert_env_var "MINER_PUBLIC_ENDPOINT" "$answer" .env
  echo "==> saved MINER_PUBLIC_ENDPOINT=$answer → .env"
  echo "==> ASR :${ASR_PORT}  TTS :${TTS_PORT}  (proxied via miner, not announced)"
  echo
}

compose_files_for() {
  local mode="$1"
  COMPOSE=("${COMPOSE_BASE[@]}")
  case "$mode" in
    test|local)
      COMPOSE+=(-f docker/docker-compose.miner.test.yml)
      ;;
    prod|production)
      COMPOSE+=(-f docker/docker-compose.miner.prod.yml)
      if [[ "$GPU" -eq 1 ]]; then
        COMPOSE+=(-f docker/docker-compose.miner.gpu.yml)
      fi
      ;;
    *)
      echo "unknown mode: $mode" >&2
      usage
      exit 2
      ;;
  esac
}

wait_http() {
  local name="$1" url="$2" tries="${3:-60}"
  echo "==> waiting for $name ($url)"
  for ((i = 1; i <= tries; i++)); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      echo "    $name is up"
      return 0
    fi
    sleep 2
  done
  echo "ERROR: $name did not become ready: $url" >&2
  "${COMPOSE[@]}" ps || true
  "${COMPOSE[@]}" logs --tail=80 || true
  return 1
}

show_boot_logs() {
  echo "==> recent container logs"
  "${COMPOSE[@]}" logs --tail=40 || true
}

start_stack() {
  local mode="$1"
  need_docker
  ensure_env
  compose_files_for "$mode"
  resolve_service_ports "$mode"
  prompt_public_endpoint "$mode"
  export MINER_PUBLIC_ENDPOINT="${MINER_PUBLIC_ENDPOINT:-http://127.0.0.1:${MINER_PORT}}"

  echo "==> building miner images ($mode)"
  "${COMPOSE[@]}" build

  echo "==> starting miner stack"
  "${COMPOSE[@]}" up -d --remove-orphans

  echo "==> streaming startup logs (20s)"
  "${COMPOSE[@]}" logs -f --tail=20 &
  local log_pid=$!
  trap 'kill '"$log_pid"' 2>/dev/null || true' EXIT
  sleep 20
  kill "$log_pid" 2>/dev/null || true
  wait "$log_pid" 2>/dev/null || true
  trap - EXIT

  show_boot_logs

  # Health via published ports / compose health.
  echo "==> waiting for docker health"
  "${COMPOSE[@]}" up -d --wait || true

  wait_http "asr" "http://127.0.0.1:${ASR_PORT}/health" 30 || \
    echo "    (ASR host port may be unpublished in prod; continuing)"
  wait_http "tts" "http://127.0.0.1:${TTS_PORT}/health" 30 || \
    echo "    (TTS host port may be unpublished in prod; continuing)"
  wait_http "miner" "http://127.0.0.1:${MINER_PORT}/health" 60

  echo "==> miner /health"
  curl -fsS "http://127.0.0.1:${MINER_PORT}/health" | tee /tmp/violet-miner-health.json
  echo

  if [[ -f scripts/run_qualification.py ]]; then
    echo "==> running qualification smoke"
    if command -v python3 >/dev/null 2>&1; then
      python3 scripts/run_qualification.py "http://127.0.0.1:${MINER_PORT}" || \
        echo "    qualification reported issues (see above); miner is still serving"
    else
      echo "    python3 not found; skip qualification"
    fi
  fi

  echo
  echo "============================================================"
  echo " Miner READY"
  echo "   local miner : http://127.0.0.1:${MINER_PORT}/health"
  echo "   public      : ${MINER_PUBLIC_ENDPOINT}"
  echo "   ASR port    : ${ASR_PORT}  (upstream ${MINER_ASR_UPSTREAM})"
  echo "   TTS port    : ${TTS_PORT}  (upstream ${MINER_TTS_UPSTREAM})"
  echo "   mode        : $mode"
  echo "   netuid      : mainnet 39 / testnet 292 (from BT_NETWORK)"
  echo "   stop        : ./violet/miner/start.sh stop"
  if ! is_local_endpoint "${MINER_PUBLIC_ENDPOINT}"; then
    echo
    echo " Verify from outside this host:"
    echo "   curl -fsS ${MINER_PUBLIC_ENDPOINT}/health"
  fi
  echo "============================================================"
  echo

  if [[ "$FOLLOW" -eq 1 ]]; then
    echo "==> following logs (Ctrl+C to detach; containers keep running)"
    "${COMPOSE[@]}" logs -f
  fi
}

stop_stack() {
  need_docker
  # Tear down both test and prod project variants for this compose name.
  docker compose -f docker/docker-compose.miner.yml \
    -f docker/docker-compose.miner.test.yml down --remove-orphans 2>/dev/null || true
  docker compose -f docker/docker-compose.miner.yml \
    -f docker/docker-compose.miner.prod.yml down --remove-orphans 2>/dev/null || true
  docker compose -f docker/docker-compose.miner.yml \
    -f docker/docker-compose.miner.prod.yml \
    -f docker/docker-compose.miner.gpu.yml down --remove-orphans 2>/dev/null || true
  docker compose -f docker/docker-compose.miner.yml down --remove-orphans 2>/dev/null || true
  echo "==> miner stack stopped"
}

status_stack() {
  need_docker
  docker compose -f docker/docker-compose.miner.yml ps || true
  curl -fsS "http://127.0.0.1:${MINER_PORT:-8091}/health" 2>/dev/null && echo || echo "miner not reachable"
}

logs_stack() {
  need_docker
  compose_files_for "${2:-test}"
  # Try test overlay first, then base.
  if ! "${COMPOSE[@]}" logs -f --tail=100; then
    "${COMPOSE_BASE[@]}" logs -f --tail=100
  fi
}

# ---- argv ----
if [[ $# -gt 0 ]]; then
  MODE="$1"
  shift
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --gpu) GPU=1 ;;
    --no-follow) FOLLOW=0 ;;
    --skip-endpoint-prompt) SKIP_ENDPOINT_PROMPT=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown flag: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

case "$MODE" in
  test|local|prod|production) start_stack "$MODE" ;;
  stop|down) stop_stack ;;
  status) status_stack ;;
  logs) logs_stack "$@" ;;
  -h|--help|help) usage ;;
  *) echo "unknown command: $MODE" >&2; usage; exit 2 ;;
esac
