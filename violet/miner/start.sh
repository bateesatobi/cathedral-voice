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
#   2. build images
#   3. start containers
#   4. stream logs until healthy
#   5. run qualification smoke
#   6. follow logs (Ctrl+C detaches; containers keep running)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

COMPOSE_BASE=(docker compose -f docker/docker-compose.miner.yml)
MODE="${1:-test}"
GPU=0
FOLLOW=1

usage() {
  sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
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

  export MINER_PUBLIC_ENDPOINT="${MINER_PUBLIC_ENDPOINT:-http://127.0.0.1:8091}"
  export MINER_PORT="${MINER_PORT:-8091}"

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

  wait_http "mock-asr" "http://127.0.0.1:${MOCK_ASR_PORT:-9000}/health" 30 || \
    echo "    (ASR port may be internal-only in prod; continuing)"
  wait_http "mock-tts" "http://127.0.0.1:${MOCK_TTS_PORT:-8080}/health" 30 || \
    echo "    (TTS port may be internal-only in prod; continuing)"
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
  echo "   health : http://127.0.0.1:${MINER_PORT}/health"
  echo "   mode   : $mode"
  echo "   netuid : mainnet 39 / testnet 292 (from BT_NETWORK)"
  echo "   stop   : ./violet/miner/start.sh stop"
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
