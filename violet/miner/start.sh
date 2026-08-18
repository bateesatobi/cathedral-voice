#!/usr/bin/env bash
# Start the cathedral-voice miner (real ASR + TTS + sidecar).
#
# Usage:
#   ./violet/miner/start.sh test|prod [--gpu] [--no-follow] [--check-public]
#   ./violet/miner/start.sh stop|stop-all|status|logs
#   ./violet/miner/bootstrap.sh test|prod   # fail-closed checklist wrapper
#
# Flow:
#   0. detect_gpu_space.sh (via bootstrap or MINER_INFERENCE_DEPLOY=auto)
#   1. ensure .env + public endpoint (validated; reject paste garbage)
#   2. plan GPUs from MINER_SERVICES (solo → ALL GPUs; both → full partition)
#   3. stt_install.sh → etoil-api :9090 (+ speaches + LB when multi-GPU)
#   4. tts_install.sh → Spark-TTS :8002 (spark-tts-frontend)
#   5. auto-tune MINER_MAX_CONCURRENT_* from GPU counts (if unset/0)
#   6. start miner sidecar → proxies to those upstreams
#   7. contract smoke (+ optional firewall / announce / public-reachability hints)
#
# Upstream defaults:
#   MINER_ASR_UPSTREAM=http://host.docker.internal:9090   # etoil-api
#   MINER_TTS_UPSTREAM=http://host.docker.internal:8002   # Spark-TTS
#
# SKIP_INFERENCE_INSTALL=1 to only start the sidecar (ASR/TTS already running).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# shellcheck source=install_lib.sh
INSTALL_LOG_PREFIX="start"
source "$ROOT/violet/miner/install_lib.sh"

COMPOSE_BASE=(docker compose -f docker/docker-compose.miner.yml)
MODE="${1:-test}"
GPU=0
FOLLOW=1
SKIP_ENDPOINT_PROMPT="${SKIP_ENDPOINT_PROMPT:-0}"
CHECK_PUBLIC="${CHECK_PUBLIC:-0}"

# Service listen ports (overridden by resolve_service_ports).
ASR_PORT="${ASR_PORT:-}"
TTS_PORT="${TTS_PORT:-}"
MINER_PORT="${MINER_PORT:-}"

usage() {
  sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'
}

need_docker() {
  require_docker_usable || exit 1
}

ensure_env() {
  if [[ ! -f .env ]]; then
    echo "==> creating .env from .env.example"
    cp .env.example .env
  fi
  # Load .env defaults, but keep any vars already set on the command line
  # (e.g. MINER_SERVICES=asr must not be overwritten by MINER_SERVICES=asr,tts).
  local _preserve=(
    MINER_SERVICES
    MINER_PUBLIC_ENDPOINT
    MINER_ASR_UPSTREAM
    MINER_TTS_UPSTREAM
    MINER_PORT
    ASR_PORT
    TTS_PORT
    SKIP_INFERENCE_INSTALL
    SKIP_ENDPOINT_PROMPT
    MINER_MAX_CONCURRENT_ASR
    MINER_MAX_CONCURRENT_TTS
    STT_GPU_DEVICES
    TTS_GPU_DEVICES
    GPU_PLAN_MODE
  )
  local _saved=()
  local key
  for key in "${_preserve[@]}"; do
    if [[ -n "${!key+x}" ]]; then
      _saved+=("$key=${!key}")
    fi
  done
  # shellcheck disable=SC1091
  set -a
  [[ -f .env ]] && source .env
  set +a
  for key in "${_saved[@]}"; do
    export "${key?}"
  done
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

endpoint_port() {
  local ep="${1:-}" rest port
  [[ -z "$ep" ]] && return 1
  rest="${ep#http://}"
  rest="${rest#https://}"
  rest="${rest%%/*}"
  if [[ "$rest" =~ :([0-9]+)$ ]]; then
    port="${BASH_REMATCH[1]}"
    [[ "$port" =~ ^[0-9]+$ ]] && { echo "$port"; return 0; }
  fi
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
  local path="${2:-/health}"
  # Spark-TTS returns 404 on /health but is still serving; treat any HTTP
  # response (not connection failure) as reachable.
  local code
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 2 \
    "http://127.0.0.1:${port}${path}" 2>/dev/null || echo "000")"
  [[ "$code" != "000" && "$code" != "" ]]
}

tts_ready_ok() {
  local port="${1:-${TTS_PORT:-8002}}"
  # Prefer a real speech POST; fall back to "any HTTP response on the port".
  local code
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 \
    -H 'Content-Type: application/json' \
    -d '{"text":"ok","speaker_id":"eng_female_1","temperature":0.7}' \
    "http://127.0.0.1:${port}/v1/audio/speech/stream" 2>/dev/null || echo "000")"
  if [[ "$code" == "200" ]]; then
    return 0
  fi
  port_health_ok "$port" "/"
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

# ASR = etoil-api (:9090), TTS = Spark (:8002), miner sidecar (:8091).
resolve_service_ports() {
  local mode="$1"
  local asr_default=9090 tts_default=8002 miner_default=8091
  local miner_from_docker="" configured_miner_port="" endpoint_configured_port=""

  compose_files_for "$mode"
  configured_miner_port="${MINER_PORT:-}"
  endpoint_configured_port="$(endpoint_port "${MINER_PUBLIC_ENDPOINT:-}" || true)"
  if [[ -n "$endpoint_configured_port" ]]; then
    if [[ -z "$configured_miner_port" ]]; then
      configured_miner_port="$endpoint_configured_port"
    elif [[ "$configured_miner_port" != "$endpoint_configured_port" ]]; then
      echo "==> MINER_PUBLIC_ENDPOINT port ${endpoint_configured_port} overrides MINER_PORT=${configured_miner_port}"
      configured_miner_port="$endpoint_configured_port"
    fi
  fi
  miner_from_docker="$(docker_published_port violet-miner "${MINER_PORT:-$miner_default}" || true)"

  # Prefer already-healthy real services on the host.
  if port_health_ok "${ASR_PORT:-$asr_default}"; then
    ASR_PORT="${ASR_PORT:-$asr_default}"
  else
    ASR_PORT="$(pick_port "${ASR_PORT:-}" "$asr_default" 9090 9091 9100)"
  fi
  # Spark-TTS may 404 on /health — use speech readiness when possible.
  if tts_ready_ok "${TTS_PORT:-$tts_default}" || port_health_ok "${TTS_PORT:-$tts_default}"; then
    TTS_PORT="${TTS_PORT:-$tts_default}"
  else
    TTS_PORT="$(pick_port "${TTS_PORT:-}" "$tts_default" 8002 8003 8080)"
  fi
  # If the operator set MINER_PORT in .env / environment, keep it exactly.
  # Only auto-pick when no explicit port was configured.
  if [[ -n "$configured_miner_port" ]]; then
    MINER_PORT="$configured_miner_port"
  else
    MINER_PORT="$(pick_port "${miner_from_docker:-}" \
      "$miner_default" 8092 8093 8191)"
  fi

  # Sidecar in Docker reaches host services via host.docker.internal.
  # Process-mode / host networking can use 127.0.0.1 instead.
  local asr_host tts_host
  asr_host="${MINER_ASR_HOST:-host.docker.internal}"
  tts_host="${MINER_TTS_HOST:-host.docker.internal}"

  MINER_ASR_UPSTREAM="${MINER_ASR_UPSTREAM:-http://${asr_host}:${ASR_PORT}}"
  MINER_TTS_UPSTREAM="${MINER_TTS_UPSTREAM:-http://${tts_host}:${TTS_PORT}}"

  # Refresh stale mock / old compose defaults.
  case "$MINER_ASR_UPSTREAM" in
    *violet-asr*|*sample-asr*)
      MINER_ASR_UPSTREAM="http://${asr_host}:${ASR_PORT}"
      ;;
  esac
  case "$MINER_TTS_UPSTREAM" in
    *violet-tts*|*sample-tts*)
      MINER_TTS_UPSTREAM="http://${tts_host}:${TTS_PORT}"
      ;;
  esac

  export ASR_PORT TTS_PORT MINER_PORT
  export MINER_ASR_UPSTREAM MINER_TTS_UPSTREAM
  export ETOIL_HOST_PORT="$ASR_PORT"
  export TTS_HOST_PORT="$TTS_PORT"

  upsert_env_var "ASR_PORT" "$ASR_PORT" .env
  upsert_env_var "TTS_PORT" "$TTS_PORT" .env
  upsert_env_var "MINER_PORT" "$MINER_PORT" .env
  upsert_env_var "MINER_ASR_UPSTREAM" "$MINER_ASR_UPSTREAM" .env
  upsert_env_var "MINER_TTS_UPSTREAM" "$MINER_TTS_UPSTREAM" .env

  echo "==> service ports"
  echo "    ASR (etoil-api) : ${ASR_PORT}  → ${MINER_ASR_UPSTREAM}"
  echo "    TTS (Spark)     : ${TTS_PORT}  → ${MINER_TTS_UPSTREAM}"
  echo "    miner sidecar   : ${MINER_PORT}  (public announce)"
}

services_want_asr() {
  local s="${MINER_SERVICES:-asr,tts}"
  [[ ",${s}," == *",asr,"* ]]
}

services_want_tts() {
  local s="${MINER_SERVICES:-asr,tts}"
  [[ ",${s}," == *",tts,"* ]]
}

gpu_plan_mode_for_services() {
  local asr=0 tts=0
  services_want_asr && asr=1
  services_want_tts && tts=1
  if [[ $asr -eq 1 && $tts -eq 1 ]]; then
    echo both
  elif [[ $asr -eq 1 ]]; then
    echo stt
  elif [[ $tts -eq 1 ]]; then
    echo tts
  else
    echo both
  fi
}

apply_concurrency_defaults() {
  local miner_dir="$ROOT/violet/miner"
  # shellcheck source=gpu_env.sh
  source "${miner_dir}/gpu_env.sh"
  local asr_c tts_c
  read -r asr_c tts_c <<<"$(suggest_concurrency)"
  if [[ "${MINER_MAX_CONCURRENT_ASR:-0}" == "0" ]]; then
    MINER_MAX_CONCURRENT_ASR="$asr_c"
    upsert_env_var "MINER_MAX_CONCURRENT_ASR" "$asr_c" .env
  fi
  if [[ "${MINER_MAX_CONCURRENT_TTS:-0}" == "0" ]]; then
    MINER_MAX_CONCURRENT_TTS="$tts_c"
    upsert_env_var "MINER_MAX_CONCURRENT_TTS" "$tts_c" .env
  fi
  export MINER_MAX_CONCURRENT_ASR MINER_MAX_CONCURRENT_TTS
  echo "==> concurrency ASR=${MINER_MAX_CONCURRENT_ASR} TTS=${MINER_MAX_CONCURRENT_TTS} (from GPU plan; override in .env)"
}

open_miner_firewall() {
  local port="${MINER_PORT:-8091}"
  if [[ "${OPEN_FIREWALL:-1}" != "1" ]]; then
    return 0
  fi
  if command -v ufw >/dev/null 2>&1 && sudo -n ufw status 2>/dev/null | grep -qi "Status: active"; then
    echo "==> ufw allow ${port}/tcp"
    sudo -n ufw allow "${port}/tcp" || true
  elif command -v firewall-cmd >/dev/null 2>&1 && sudo -n firewall-cmd --state 2>/dev/null | grep -qi running; then
    echo "==> firewalld allow ${port}/tcp"
    sudo -n firewall-cmd --permanent --add-port="${port}/tcp" || true
    sudo -n firewall-cmd --reload || true
  else
    echo "==> ensure cloud SG / firewall allows public TCP ${port}"
  fi
}

resolve_inference_deploy() {
  local mode="${MINER_INFERENCE_DEPLOY:-auto}"
  case "$mode" in
    compose|run|cpu)
      echo "$mode"
      return 0
      ;;
    auto)
      classify_gpu_space
      case "$GPU_SPACE_DEPLOY" in
        compose|run|cpu_fallback)
          if [[ "$GPU_SPACE_DEPLOY" == cpu_fallback ]]; then
            echo "cpu"
          else
            echo "$GPU_SPACE_DEPLOY"
          fi
          ;;
        *)
          echo "compose"
          ;;
      esac
      ;;
    *)
      echo "WARN: unknown MINER_INFERENCE_DEPLOY=${mode}; using compose" >&2
      echo "compose"
      ;;
  esac
}

install_inference_stt() {
  local miner_dir="$1" plan_mode="$2" deploy="$3"
  if [[ "$deploy" == "run" ]]; then
    GPU_PLAN_MODE="$plan_mode" GPU_PLAN_LOCKED=1 \
      ETOIL_HOST_PORT="${ASR_PORT}" \
      STT_GPU_DEVICES="${STT_GPU_DEVICES:-}" \
      "${miner_dir}/inference_run_install.sh" stt
  elif [[ "$deploy" == "cpu" ]]; then
    STT_FORCE_CPU=1 GPU_PLAN_MODE="$plan_mode" GPU_PLAN_LOCKED=1 \
      ETOIL_HOST_PORT="${ASR_PORT}" \
      "${miner_dir}/stt_install.sh"
  else
    GPU_PLAN_MODE="$plan_mode" GPU_PLAN_LOCKED=1 \
      ETOIL_HOST_PORT="${ASR_PORT}" \
      STT_GPU_DEVICES="${STT_GPU_DEVICES}" \
      TTS_GPU_DEVICES="${TTS_GPU_DEVICES}" \
      "${miner_dir}/stt_install.sh"
  fi
}

install_inference_tts() {
  local miner_dir="$1" plan_mode="$2" deploy="$3"
  if [[ "$deploy" == "run" ]]; then
    GPU_PLAN_MODE="$plan_mode" GPU_PLAN_LOCKED=1 \
      TTS_HOST_PORT="${TTS_PORT}" \
      TTS_GPU_DEVICES="${TTS_GPU_DEVICES:-}" \
      "${miner_dir}/inference_run_install.sh" tts
  else
    if ! GPU_PLAN_MODE="$plan_mode" GPU_PLAN_LOCKED=1 \
      TTS_HOST_PORT="${TTS_PORT}" \
      HOST_PORT="${TTS_PORT}" \
      STT_GPU_DEVICES="${STT_GPU_DEVICES}" \
      TTS_GPU_DEVICES="${TTS_GPU_DEVICES}" \
      CUDA_VISIBLE_DEVICES="${TTS_GPU_DEVICES:-${CUDA_VISIBLE_DEVICES:-0}}" \
      "${miner_dir}/tts_install.sh"; then
      return 1
    fi
  fi
}

ensure_inference_stacks() {
  local miner_dir="$ROOT/violet/miner"
  local plan_mode deploy
  # shellcheck source=gpu_env.sh
  source "${miner_dir}/gpu_env.sh"
  plan_mode="$(gpu_plan_mode_for_services)"
  export GPU_PLAN_MODE="$plan_mode"
  plan_gpu_devices "$plan_mode"
  export GPU_PLAN_LOCKED=1
  deploy="$(resolve_inference_deploy)"
  echo "==> GPU plan mode=${plan_mode} count=${GPU_COUNT:-?} STT=[${STT_GPU_DEVICES:-}] TTS=[${TTS_GPU_DEVICES:-}]"
  echo "==> inference deploy mode=${deploy} (MINER_INFERENCE_DEPLOY=${MINER_INFERENCE_DEPLOY:-auto})"
  if [[ "${deploy}" == "cpu" ]]; then
    echo "==> NVML-only / CPU fallback — GPU inference not available on this host"
    echo "    Run ./violet/miner/detect_gpu_space.sh for details"
  fi
  if [[ "${GPU_COUNT:-0}" -ge 1 ]]; then
    assert_no_idle_gpus "${GPU_COUNT}" \
      "${STT_GPU_DEVICES:-}" "${TTS_GPU_DEVICES:-}" \
      || echo "WARN: idle GPU detected in plan" >&2
  fi

  if services_want_asr; then
    if ! port_health_ok "${ASR_PORT}"; then
      echo "==> installing / starting STT (deploy=${deploy})"
      install_inference_stt "$miner_dir" "$plan_mode" "$deploy"
    else
      echo "==> ASR already healthy on :${ASR_PORT}"
    fi
  else
    echo "==> MINER_SERVICES excludes asr — skip STT"
  fi

  if services_want_tts; then
    if [[ "${deploy}" == "cpu" ]]; then
      echo "==> skipping TTS — CPU-only host (use bare GPU VM for TTS)"
    elif ! port_health_ok "${TTS_PORT}"; then
      echo "==> installing / starting TTS (deploy=${deploy})"
      if ! install_inference_tts "$miner_dir" "$plan_mode" "$deploy"; then
        echo
        echo "==> TTS install aborted (GPU compute not available in Docker)."
        echo "    ASR-only:  MINER_SERVICES=asr $0 ${MODE:-prod} --gpu --no-follow"
        echo "    TTS needs a bare GPU VM or WSL (Docker as the host), not a nested job."
        exit 1
      fi
    else
      echo "==> TTS already healthy on :${TTS_PORT}"
    fi
  else
    echo "==> MINER_SERVICES excludes tts — skip TTS"
  fi

  apply_concurrency_defaults
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

  if ! validate_public_endpoint_input "$answer"; then
    echo "Enter only the IP (e.g. 93.120.231.186) or a URL (http://ip:8091)." >&2
    exit 1
  fi

  # Extra shape check: bare token should look like IPv4 or DNS label(s).
  host_or_url="$answer"
  local bare="$host_or_url"
  bare="${bare#http://}"
  bare="${bare#https://}"
  bare="${bare%%/*}"
  bare="${bare%%:*}"
  if [[ ! "$bare" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] \
    && [[ ! "$bare" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]; then
    echo "ERROR: public host looks invalid: '$bare'" >&2
    echo "Expected IPv4 (a.b.c.d) or DNS name (miner.example.com)." >&2
    exit 1
  fi

  answer="$(normalize_public_endpoint "$host_or_url" "$port")"

  if [[ "$mode" != "test" && "$mode" != "local" ]] && is_local_endpoint "$answer"; then
    echo "ERROR: prod MINER_PUBLIC_ENDPOINT cannot be localhost" >&2
    exit 1
  fi

  export MINER_PUBLIC_ENDPOINT="$answer"
  upsert_env_var "MINER_PUBLIC_ENDPOINT" "$answer" .env

  if [[ "$mode" == "prod" || "$mode" == "production" ]]; then
    if [[ "$answer" != https://* ]]; then
      if [[ "${MINER_ALLOW_HTTP:-0}" == "1" ]]; then
        echo "WARNING: prod endpoint uses plaintext HTTP (MINER_ALLOW_HTTP=1)" >&2
      else
        echo "ERROR: prod MINER_PUBLIC_ENDPOINT must use https:// (set MINER_ALLOW_HTTP=1 for lab only)" >&2
        exit 1
      fi
    fi
  fi

  # Re-source so callers see the canonical value (guards against .env write races).
  # shellcheck disable=SC1091
  set -a && source .env && set +a
  export MINER_PUBLIC_ENDPOINT="$answer"
  echo "==> saved MINER_PUBLIC_ENDPOINT=${MINER_PUBLIC_ENDPOINT} → .env"
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
      # Optional host bind when the Docker daemon can see the path.
      if [[ "${BT_WALLET_BIND:-0}" == "1" ]]; then
        local wdir="${BT_WALLET_DIR:-${HOME}/.bittensor}"
        mkdir -p "$wdir" 2>/dev/null || true
        cat > docker/docker-compose.miner.wallet-bind.yml <<EOF
services:
  violet-miner:
    volumes:
      - ${wdir}:/home/violet/.bittensor:ro
EOF
        COMPOSE+=(-f docker/docker-compose.miner.wallet-bind.yml)
      fi
      ;;
    *)
      echo "unknown mode: $mode" >&2
      usage
      exit 2
      ;;
  esac
  # Capacity scoring needs nvidia-smi inside the sidecar (test and prod).
  if [[ "$GPU" -eq 1 ]] \
    || { [[ "${MINER_AUTO_GPU:-1}" == "1" ]] && command -v nvidia-smi >/dev/null 2>&1; }; then
    COMPOSE+=(-f docker/docker-compose.miner.gpu.yml)
    GPU=1
  fi
}

# seed_wallet_volume / assert_wallet_keyfile come from install_lib.sh

# Thin wrapper kept for call sites that expect start.sh logging style.
wait_http() {
  local name="$1" url="$2" tries="${3:-60}"
  echo "==> waiting for $name ($url)"
  if wait_http_ok "$name" "$url" "$tries"; then
    return 0
  fi
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

  if [[ "${SKIP_INFERENCE_INSTALL:-0}" != "1" ]]; then
    ensure_inference_stacks
  else
    echo "==> SKIP_INFERENCE_INSTALL=1 — expecting ASR :${ASR_PORT} and TTS :${TTS_PORT}"
    # Still plan GPUs so concurrency defaults match the host.
    # shellcheck source=gpu_env.sh
    source "$ROOT/violet/miner/gpu_env.sh"
    plan_gpu_devices "$(gpu_plan_mode_for_services)"
    apply_concurrency_defaults
  fi

  echo "==> waiting for inference health (long window for first model pull)"
  if services_want_asr; then
    wait_http "asr(etoil)" "http://127.0.0.1:${ASR_PORT}/health" 300
  fi
  if services_want_tts; then
    # Spark frontend often has no /health — wait until speech stream answers.
    if ! wait_speech_ready "${TTS_PORT}" 450; then
      echo "ERROR: tts(spark) did not become ready on :${TTS_PORT}" >&2
      exit 1
    fi
  fi

  apply_concurrency_defaults 2>/dev/null || true
  open_miner_firewall

  if [[ "$mode" == "prod" || "$mode" == "production" ]]; then
    if ! seed_wallet_volume; then
      echo "ERROR: wallet seed failed — refusing to start prod crash-loop" >&2
      exit 1
    fi
    if ! assert_wallet_keyfile; then
      echo "ERROR: wallet keyfile missing/unreadable — fix names or re-seed, then retry" >&2
      exit 1
    fi
  fi

  echo "==> building miner sidecar ($mode)"
  "${COMPOSE[@]}" build

  echo "==> starting miner sidecar"
  "${COMPOSE[@]}" up -d --remove-orphans

  echo "==> streaming startup logs (15s)"
  "${COMPOSE[@]}" logs -f --tail=20 &
  local log_pid=$!
  trap 'kill '"$log_pid"' 2>/dev/null || true' EXIT
  sleep 15
  kill "$log_pid" 2>/dev/null || true
  wait "$log_pid" 2>/dev/null || true
  trap - EXIT

  show_boot_logs
  "${COMPOSE[@]}" up -d --wait || true

  wait_http "miner" "http://127.0.0.1:${MINER_PORT}/health" 60

  echo "==> miner /health"
  curl -fsS "http://127.0.0.1:${MINER_PORT}/health" | tee /tmp/violet-miner-health.json
  echo

  if [[ -x "$ROOT/violet/miner/smoke_contract.sh" ]] || [[ -f "$ROOT/violet/miner/smoke_contract.sh" ]]; then
    echo "==> contract smoke"
    ASR_PORT="$ASR_PORT" TTS_PORT="$TTS_PORT" MINER_PORT="$MINER_PORT" \
      MINER_SERVICES="${MINER_SERVICES:-asr,tts}" \
      bash "$ROOT/violet/miner/smoke_contract.sh" || \
      echo "    contract smoke reported issues; check logs before registering"
  fi

  if [[ -f scripts/run_qualification.py ]]; then
    echo "==> running qualification smoke"
    if command -v python3 >/dev/null 2>&1; then
      python3 scripts/run_qualification.py "http://127.0.0.1:${MINER_PORT}" \
        --services "${MINER_SERVICES:-asr,tts}" || \
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
  echo "   ASR etoil   : http://127.0.0.1:${ASR_PORT}  (${MINER_ASR_UPSTREAM})"
  echo "   TTS spark   : http://127.0.0.1:${TTS_PORT}  (${MINER_TTS_UPSTREAM})"
  echo "   mode        : $mode"
  echo "   stop        : ./violet/miner/start.sh stop"
  echo "   stop-all    : ./violet/miner/start.sh stop-all   # sidecar + ASR + TTS"
  if ! is_local_endpoint "${MINER_PUBLIC_ENDPOINT}"; then
    echo
    echo " Verify from outside this host:"
    echo "   curl -fsS ${MINER_PUBLIC_ENDPOINT}/health"
    echo " Announce (after btcli register):"
    echo "   python scripts/announce_endpoint.py --dry-run"
    echo "   python scripts/announce_endpoint.py"
  fi
  if [[ "${CHECK_PUBLIC}" == "1" ]] || [[ "$mode" == "prod" || "$mode" == "production" ]]; then
    print_public_reachability_hint "${MINER_PORT}" "${MINER_PUBLIC_ENDPOINT}"
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
  echo "==> miner sidecar stopped"
}

stop_all_stacks() {
  stop_stack
  local stt_compose="$ROOT/violet/miner/stt-stack/docker-compose.yml"
  if [[ -f "$stt_compose" ]]; then
    echo "==> stopping STT stack"
    docker compose -f "$stt_compose" down --remove-orphans 2>/dev/null || true
  fi
  local tts_compose="${ROOT}/violet/miner/tts-stack/docker-compose.yml"
  if [[ -f "$tts_compose" ]]; then
    echo "==> stopping TTS compose stack"
    docker compose -f "$tts_compose" down --remove-orphans 2>/dev/null || true
  fi
  local tts_name="${TTS_CONTAINER_NAME:-spark-tts-frontend}"
  if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -Eq "^${tts_name}\$"; then
    echo "==> stopping TTS ${tts_name}"
    docker stop "$tts_name" 2>/dev/null || true
    docker rm "$tts_name" 2>/dev/null || true
  fi
  # Legacy names from previous tts_install.sh
  if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -Eq '^etoil-tts$'; then
    echo "==> stopping legacy TTS etoil-tts"
    docker stop etoil-tts 2>/dev/null || true
    docker rm etoil-tts 2>/dev/null || true
  fi
  if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -Eq '^spark-tts-streaming$'; then
    echo "==> stopping legacy TTS spark-tts-streaming"
    docker stop spark-tts-streaming 2>/dev/null || true
    docker rm spark-tts-streaming 2>/dev/null || true
  fi
  if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -Eq '^cathedral-spark-tts$'; then
    echo "==> stopping legacy TTS cathedral-spark-tts"
    docker stop cathedral-spark-tts 2>/dev/null || true
    docker rm cathedral-spark-tts 2>/dev/null || true
  fi
  echo "==> miner + inference stacks stopped"
}

status_stack() {
  need_docker
  docker compose -f docker/docker-compose.miner.yml ps || true
  curl -fsS "http://127.0.0.1:${MINER_PORT:-8091}/health" 2>/dev/null && echo || echo "miner not reachable"
  curl -fsS "http://127.0.0.1:${ASR_PORT:-9090}/health" 2>/dev/null && echo "asr ok" || echo "asr not reachable"
  if tts_ready_ok "${TTS_PORT:-8002}"; then
    echo "tts ok"
  else
    echo "tts not reachable"
  fi
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
    --check-public) CHECK_PUBLIC=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown flag: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

case "$MODE" in
  test|local|prod|production) start_stack "$MODE" ;;
  stop|down) stop_stack ;;
  stop-all) stop_all_stacks ;;
  status) status_stack ;;
  logs) logs_stack "$@" ;;
  -h|--help|help) usage ;;
  *) echo "unknown command: $MODE" >&2; usage; exit 2 ;;
esac
