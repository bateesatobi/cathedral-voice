#!/usr/bin/env bash
# Start the Violet validator (scoring dashboard + probe loops).
#
# Usage:
#   ./violet/validator/start.sh test              # offline local validator (default)
#   ./violet/validator/start.sh test --miner http://127.0.0.1:8091
#   ./violet/validator/start.sh prod              # on-chain validator (needs wallet/.env)
#   ./violet/validator/start.sh stop
#   ./violet/validator/start.sh logs
#   ./violet/validator/start.sh status
#
# Flow (test/prod):
#   1. ensure .env
#   2. build image
#   3. start container
#   4. stream logs until healthy
#   5. print dashboard URL and follow logs

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

MODE="${1:-test}"
FOLLOW=1
MINER_URL="${VIOLET_STATIC_MINERS:-http://host.docker.internal:8091}"

usage() {
  sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
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
  set -a
  [[ -f .env ]] && # shellcheck disable=SC1091
  source .env
  set +a
}

compose_for() {
  local mode="$1"
  case "$mode" in
    test|local)
      COMPOSE=(docker compose -f docker/docker-compose.validator.test.yml)
      ;;
    prod|production)
      COMPOSE=(docker compose -f docker/docker-compose.validator.yml)
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

start_stack() {
  local mode="$1"
  need_docker
  ensure_env
  compose_for "$mode"

  export VALIDATOR_DASHBOARD_PORT="${VALIDATOR_DASHBOARD_PORT:-8092}"
  export VIOLET_STATIC_MINERS="$MINER_URL"

  if [[ "$mode" == "prod" || "$mode" == "production" ]]; then
    if [[ -z "${BT_WALLET_NAME:-}" ]]; then
      echo "WARN: BT_WALLET_NAME unset — set wallet fields in .env for chain mode" >&2
    fi
    mkdir -p "${VIOLET_EVALSET_DIR:-./evalset}"
  fi

  echo "==> building validator image ($mode)"
  "${COMPOSE[@]}" build

  echo "==> starting validator"
  "${COMPOSE[@]}" up -d --remove-orphans

  echo "==> streaming startup logs (20s)"
  "${COMPOSE[@]}" logs -f --tail=20 &
  local log_pid=$!
  trap 'kill '"$log_pid"' 2>/dev/null || true' EXIT
  sleep 20
  kill "$log_pid" 2>/dev/null || true
  wait "$log_pid" 2>/dev/null || true
  trap - EXIT

  echo "==> recent logs"
  "${COMPOSE[@]}" logs --tail=40 || true

  "${COMPOSE[@]}" up -d --wait || true
  wait_http "validator" "http://127.0.0.1:${VALIDATOR_DASHBOARD_PORT}/health" 60

  echo "==> validator /health"
  curl -fsS "http://127.0.0.1:${VALIDATOR_DASHBOARD_PORT}/health"
  echo

  echo
  echo "============================================================"
  echo " Validator READY"
  echo "   dashboard : http://127.0.0.1:${VALIDATOR_DASHBOARD_PORT}/health"
  echo "   mode      : $mode"
  if [[ "$mode" == "test" || "$mode" == "local" ]]; then
    echo "   miner     : $MINER_URL"
  else
    echo "   netuid    : mainnet 39 / testnet 292 (from BT_NETWORK)"
    echo "   dry_run   : ${VALIDATOR_DRY_RUN:-unset}"
  fi
  echo "   stop      : ./violet/validator/start.sh stop"
  echo "============================================================"
  echo

  if [[ "$FOLLOW" -eq 1 ]]; then
    echo "==> following logs (Ctrl+C to detach; container keeps running)"
    "${COMPOSE[@]}" logs -f
  fi
}

stop_stack() {
  need_docker
  docker compose -f docker/docker-compose.validator.test.yml down --remove-orphans 2>/dev/null || true
  docker compose -f docker/docker-compose.validator.yml down --remove-orphans 2>/dev/null || true
  echo "==> validator stack stopped"
}

status_stack() {
  need_docker
  docker compose -f docker/docker-compose.validator.test.yml ps 2>/dev/null || true
  docker compose -f docker/docker-compose.validator.yml ps 2>/dev/null || true
  curl -fsS "http://127.0.0.1:${VALIDATOR_DASHBOARD_PORT:-8092}/health" 2>/dev/null && echo || \
    echo "validator not reachable"
}

logs_stack() {
  need_docker
  compose_for "${2:-test}"
  "${COMPOSE[@]}" logs -f --tail=100
}

if [[ $# -gt 0 ]]; then
  MODE="$1"
  shift
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --miner)
      shift
      MINER_URL="${1:?--miner needs a URL}"
      ;;
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
