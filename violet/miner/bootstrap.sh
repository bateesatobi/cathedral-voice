#!/usr/bin/env bash
#
# bootstrap.sh — Fail-closed miner bring-up checklist for cathedral-voice.
#
# Runs: GPU plan → STT/TTS (per MINER_SERVICES) → sidecar → contract smoke →
# optional firewall open → wallet/announce hints.
#
# Usage (from repo root):
#   ./violet/miner/bootstrap.sh test
#   ./violet/miner/bootstrap.sh prod --no-follow
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "$ROOT"

MODE="${1:-test}"
shift || true

# shellcheck source=gpu_env.sh
source "${SCRIPT_DIR}/gpu_env.sh"

log()  { echo -e "\033[1;32m[bootstrap]\033[0m $*"; }
warn() { echo -e "\033[1;33m[bootstrap warn]\033[0m $*"; }
err()  { echo -e "\033[1;31m[bootstrap error]\033[0m $*" >&2; }

open_miner_firewall() {
  local port="${MINER_PORT:-8091}"
  if command -v ufw >/dev/null 2>&1 && sudo ufw status 2>/dev/null | grep -qi "Status: active"; then
    log "Opening ufw TCP ${port}"
    sudo ufw allow "${port}/tcp" || warn "ufw allow failed"
  elif command -v firewall-cmd >/dev/null 2>&1 && sudo firewall-cmd --state 2>/dev/null | grep -qi running; then
    log "Opening firewalld TCP ${port}"
    sudo firewall-cmd --permanent --add-port="${port}/tcp" || true
    sudo firewall-cmd --reload || true
  else
    warn "No active ufw/firewalld — ensure cloud security group allows TCP ${port}"
  fi
}

check_wallet() {
  local name="${BT_WALLET_NAME:-default}"
  local hotkey="${BT_WALLET_HOTKEY:-default}"
  local path="${BT_WALLET_PATH:-$HOME/.bittensor/wallets}"
  if [[ -d "${path}/${name}/hotkeys" ]]; then
    if [[ -f "${path}/${name}/hotkeys/${hotkey}" ]] || [[ -f "${path}/${name}/hotkeys/${hotkey}.json" ]]; then
      log "Wallet hotkey found: ${name}/${hotkey}"
      return 0
    fi
  fi
  warn "Wallet ${name}/${hotkey} not found under ${path}"
  warn "Register: btcli subnet register --netuid \${VIOLET_NETUID} --wallet.name ${name} --wallet.hotkey ${hotkey}"
  return 1
}

hint_announce() {
  if [[ "${MODE}" == "prod" || "${MODE}" == "production" ]]; then
    log "After registration, announce:"
    echo "  python scripts/announce_endpoint.py --dry-run"
    echo "  python scripts/announce_endpoint.py"
  fi
}

export SKIP_ENDPOINT_PROMPT="${SKIP_ENDPOINT_PROMPT:-0}"
# bootstrap always runs full start; stop-all available via start.sh stop-all
log "Starting miner (${MODE}) with full GPU utilization..."
"${SCRIPT_DIR}/start.sh" "${MODE}" "$@"

# shellcheck disable=SC1091
[[ -f .env ]] && set -a && source .env && set +a

open_miner_firewall
check_wallet || true
hint_announce

ASR_PORT="${ASR_PORT:-9090}" TTS_PORT="${TTS_PORT:-8002}" MINER_PORT="${MINER_PORT:-8091}" \
  MINER_SERVICES="${MINER_SERVICES:-asr,tts}" \
  "${SCRIPT_DIR}/smoke_contract.sh" || {
    err "Contract smoke failed — miner may not qualify"
    exit 1
  }

log "Bootstrap complete."
