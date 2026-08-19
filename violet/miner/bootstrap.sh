#!/usr/bin/env bash
#
# bootstrap.sh — Fail-closed miner bring-up checklist for cathedral-voice.
#
# Runs: GPU plan → STT/TTS (per MINER_SERVICES) → sidecar → contract smoke →
# admission checklist (wallet, public port hint, announce dry-run) → optional
# qualification.
#
# Usage (from repo root):
#   ./violet/miner/bootstrap.sh test
#   ./violet/miner/bootstrap.sh prod --no-follow
#   BOOTSTRAP_QUALIFY=1 ./violet/miner/bootstrap.sh prod --gpu
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "$ROOT"

MODE="${1:-test}"
shift || true

# shellcheck source=gpu_env.sh
source "${SCRIPT_DIR}/gpu_env.sh"
# shellcheck source=install_lib.sh
INSTALL_LOG_PREFIX="bootstrap"
source "${SCRIPT_DIR}/install_lib.sh"

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

print_provide_checklist() {
  local port="${MINER_PORT:-8091}"
  local ep="${MINER_PUBLIC_ENDPOINT:-http://127.0.0.1:${port}}"
  echo
  echo "============================================================"
  echo " What this miner MUST provide (validators + router)"
  echo "============================================================"
  echo " Public endpoint : ${ep}"
  echo " Open TCP        : ${port}  (only this port is announced)"
  echo
  echo " API surface on :${port}"
  echo "   GET  /health"
  echo "   GET  /capacity          ← GPU / capacity_units"
  echo "   GET  /violet/info       ← hotkey, uid, services"
  echo "   POST /transcribe        ← batch ASR"
  echo "   WS   /realtime/transcribe"
  echo "   POST /v1/audio/speech/stream"
  echo "   GET  /v1/voices         (optional)"
  echo "   WS   /v1/audio/speech/stream/ws"
  echo
  echo " On-chain (required for rewards)"
  echo "   1. BT_WALLET_NAME / BT_WALLET_HOTKEY = wallet *names* (not secrets)"
  echo "   2. btcli subnet register  → assigns UID"
  echo "   3. python scripts/announce_endpoint.py  → publishes ${ep}"
  echo "   4. start.sh prod --gpu     → hotkey appears in /health"
  echo
  echo " Verify"
  echo "   curl -fsS ${ep}/health"
  echo "   curl -fsS ${ep}/capacity"
  echo "   python scripts/run_qualification.py ${ep} --services ${MINER_SERVICES:-asr,tts}"
  echo "============================================================"
}

# Admission gate printed after local smoke — the single remaining manual step
# is usually router/cloud port-forward (scripts cannot open Keenetic WAN ports).
admission_checklist() {
  local port="${MINER_PORT:-8091}"
  local ep="${MINER_PUBLIC_ENDPOINT:-http://127.0.0.1:${port}}"
  local name="${BT_WALLET_NAME:-default}"
  local hotkey="${BT_WALLET_HOTKEY:-default}"
  local fail=0

  echo
  echo "============================================================"
  echo " Admission checklist (go-live)"
  echo "============================================================"

  echo " [1] Local contract smoke"
  if curl -fsS --max-time 5 "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
    echo "     OK  http://127.0.0.1:${port}/health"
  else
    echo "     FAIL local /health"
    fail=1
  fi

  echo " [2] Wallet names + keyfile"
  if check_wallet; then
    if [[ "${MODE}" == "prod" || "${MODE}" == "production" ]]; then
      if assert_wallet_keyfile; then
        echo "     OK  volume keyfile for ${name}/${hotkey}"
      else
        echo "     FAIL wallet volume unreadable"
        fail=1
      fi
    else
      echo "     OK  host wallet ${name}/${hotkey}"
    fi
  else
    if [[ "${MODE}" == "prod" || "${MODE}" == "production" ]]; then
      fail=1
    fi
  fi

  echo " [3] Public TCP ${port} (manual — NAT hairpin often fails from this host)"
  echo "     Announce URL: ${ep}"
  echo "     Open WAN:${port} → this VM LAN:${port} on your router/SG"
  echo "     If :80 shows KeeneticOS / router UI, do NOT put the miner on 80."
  echo "     Verify from a second network:"
  echo "       curl -fsS ${ep}/health"
  echo "       nc -vz <public-ip> ${port}"
  print_public_reachability_hint "$port" "$ep"

  echo " [4] Miner access token (production traffic auth)"
  if [[ -n "${MINER_ACCESS_TOKEN:-}" ]]; then
    echo "     OK  MINER_ACCESS_TOKEN is set"
  elif [[ "${FETCH_MINER_TOKEN:-0}" == "1" ]]; then
    if [[ -x "${SCRIPT_DIR}/fetch_access_token.sh" ]]; then
      if VIOLET_TOKEN_API_URL="${VIOLET_TOKEN_API_URL:-https://api.phosaico.com}" \
        "${SCRIPT_DIR}/fetch_access_token.sh" "${MODE}" --env-file "${ROOT}/.env"; then
        echo "     OK  fetched MINER_ACCESS_TOKEN"
      else
        echo "     FAIL token fetch — set VIOLET_MINER_TOKEN_MASTER_KEY on ASRAPI first"
        fail=1
      fi
    else
      echo "     FAIL fetch_access_token.sh missing"
      fail=1
    fi
  else
    echo "     skip — run: ./violet/miner/fetch_access_token.sh ${MODE} --write-env"
  fi

  echo " [5] Announce dry-run (prod)"
  if [[ "${MODE}" == "prod" || "${MODE}" == "production" ]]; then
    if [[ -f scripts/announce_endpoint.py ]] && command -v python3 >/dev/null 2>&1; then
      if python3 scripts/announce_endpoint.py --dry-run 2>&1 | tail -n 20; then
        echo "     OK  announce dry-run"
      else
        warn "announce dry-run failed (register wallet / fix .env first)"
      fi
    else
      warn "scripts/announce_endpoint.py or python3 missing — skip dry-run"
    fi
  else
    echo "     skip (mode=${MODE})"
  fi

  echo " [6] Qualification (optional: BOOTSTRAP_QUALIFY=1)"
  if [[ "${BOOTSTRAP_QUALIFY:-0}" == "1" ]]; then
    if [[ -f scripts/run_qualification.py ]] && command -v python3 >/dev/null 2>&1; then
      python3 scripts/run_qualification.py "http://127.0.0.1:${port}" \
        --services "${MINER_SERVICES:-asr,tts}" || fail=1
    else
      warn "qualification script/python missing"
    fi
  else
    echo "     skip — run: python scripts/run_qualification.py http://127.0.0.1:${port} --services ${MINER_SERVICES:-asr,tts}"
  fi

  echo "============================================================"
  if [[ "$fail" -ne 0 ]]; then
    err "Admission checklist failed — fix items above before expecting rewards"
    return 1
  fi
  log "Admission checklist passed (public port-forward still operator responsibility)"
  return 0
}

export SKIP_ENDPOINT_PROMPT="${SKIP_ENDPOINT_PROMPT:-0}"
# bootstrap always runs full start; stop-all available via start.sh stop-all
# shellcheck disable=SC1091
[[ -f .env ]] && set -a && source .env && set +a

if [[ -x "${SCRIPT_DIR}/detect_gpu_space.sh" ]]; then
  log "GPU space preflight"
  if ! "${SCRIPT_DIR}/detect_gpu_space.sh"; then
    warn "GPU space check reported issues — see above before expecting GPU inference"
  fi
  echo
fi

if [[ ",${MINER_SERVICES:-asr,tts}," == *",tts,"* ]]; then
  log "TTS requires a bare GPU VM or WSL (Docker as the host)."
  log "Nested GPU jobs often pass nvidia-smi and fail CUDA alloc — tts_install will abort."
  log "ASR-only on this host: MINER_SERVICES=asr ./violet/miner/bootstrap.sh ${MODE}"
fi
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

print_provide_checklist
admission_checklist || exit 1
log "Bootstrap complete."
