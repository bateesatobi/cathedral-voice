#!/usr/bin/env bash
#
# install_lib.sh — Shared primitives for cathedral-voice miner install scripts.
#
# Sourced by: stt_install.sh, tts_install.sh, start.sh, bootstrap.sh
#
# Provides:
#   is_nested_docker / warn_nested_docker
#   require_docker_usable
#   default_hf_token
#   wait_http_ok / wait_http_any / wait_speech_ready
#   make_smoke_wav
#   seed_wallet_volume / assert_wallet_keyfile
#   validate_public_endpoint_input / normalize_public_endpoint
#   print_public_reachability_hint
#
# shellcheck shell=bash

# Avoid double-source.
if [[ -n "${VIOLET_INSTALL_LIB_LOADED:-}" ]]; then
  return 0 2>/dev/null || true
fi
VIOLET_INSTALL_LIB_LOADED=1

# --------------------------------------------------------------------------
# Logging (prefix override via INSTALL_LOG_PREFIX)
# --------------------------------------------------------------------------

_install_log()  { echo -e "\033[1;32m[${INSTALL_LOG_PREFIX:-install}]\033[0m $*"; }
_install_warn() { echo -e "\033[1;33m[${INSTALL_LOG_PREFIX:-install} warn]\033[0m $*"; }
_install_err()  { echo -e "\033[1;31m[${INSTALL_LOG_PREFIX:-install} error]\033[0m $*" >&2; }

# --------------------------------------------------------------------------
# Docker / DinD
# --------------------------------------------------------------------------

is_nested_docker() {
  [[ -f /.dockerenv ]]
}

warn_nested_docker() {
  local allow_var="${1:-STT_ALLOW_NESTED_DOCKER}"
  if is_nested_docker && [[ -z "${!allow_var:-}" ]]; then
    _install_warn "Shell appears to be inside a container (/.dockerenv)."
    _install_warn "Prefer named Docker volumes — bind mounts often fail when the"
    _install_warn "Docker daemon runs on the host and cannot see shell paths."
    _install_warn "Set ${allow_var}=1 to silence this warning."
  fi
}

require_docker_usable() {
  if ! command -v docker >/dev/null 2>&1; then
    _install_err "docker is required"
    return 1
  fi
  if ! docker compose version >/dev/null 2>&1; then
    _install_err "docker compose plugin is required"
    return 1
  fi
  if ! docker info >/dev/null 2>&1; then
    _install_err "cannot talk to Docker daemon (docker info failed)"
    return 1
  fi
  return 0
}

# Prefer named volume under DinD; allow explicit bind via BIND_DIR.
# Prints the docker -v fragment value: "name:/path" or "/host:/path"
prefer_named_volume() {
  local named="$1" container_path="$2" bind_dir="${3:-}"
  if [[ -n "$bind_dir" ]]; then
    mkdir -p "$bind_dir" 2>/dev/null || true
    echo "${bind_dir}:${container_path}"
  else
    echo "${named}:${container_path}"
  fi
}

# --------------------------------------------------------------------------
# HuggingFace token (set HF_TOKEN in .env — never commit real tokens)
# --------------------------------------------------------------------------

default_hf_token() {
  if [[ -n "${HF_TOKEN:-}" ]]; then
    echo "${HF_TOKEN}"
    return 0
  fi
  if [[ -n "${HUGGING_FACE_HUB_TOKEN:-}" ]]; then
    echo "${HUGGING_FACE_HUB_TOKEN}"
    return 0
  fi
  _install_warn "HF_TOKEN unset — private HF pulls may fail. Set HF_TOKEN in .env (see .env.example)."
  echo ""
}

# Detect Git-LFS pointer / HTML login pages left by unauthenticated curl.
hf_file_looks_corrupt() {
  local f="$1"
  [[ ! -f "$f" ]] && return 0
  local sz head
  sz="$(wc -c <"$f" | tr -d ' ')"
  (( sz < 200 )) && return 0
  head="$(head -c 64 "$f" 2>/dev/null || true)"
  [[ "$head" == *"version https://git-lfs"* ]] && return 0
  [[ "$head" == *"<!DOCTYPE"* || "$head" == *"<html"* ]] && return 0
  return 1
}

# Generic HF snapshot into a named volume or bind (DinD-safe via docker run).
# Args: REPO LOCAL_DIR_IN_VOLUME [VOLUME_OR_BIND_SRC] [SEED_IMAGE]
seed_hf_repo() {
  local repo="$1" dest_rel="$2" mount_src="${3:-}" seed_image="${4:-python:3.11-slim}"
  local token
  token="$(default_hf_token)"
  if [[ -z "$mount_src" ]]; then
    _install_err "seed_hf_repo: mount source required"
    return 1
  fi
  _install_log "HF snapshot ${repo} → ${mount_src}:${dest_rel}"
  docker pull "$seed_image" >/dev/null
  docker run --rm \
    -e HF_TOKEN="${token}" \
    -e HUGGING_FACE_HUB_TOKEN="${token}" \
    -e HF_REPO="${repo}" \
    -e HF_DEST="/models/${dest_rel}" \
    -v "${mount_src}:/models" \
    "$seed_image" \
    bash -c 'pip install -q "huggingface_hub>=0.23" && python - <<"PY"
import os
from pathlib import Path
from huggingface_hub import snapshot_download
dest = Path(os.environ["HF_DEST"])
dest.mkdir(parents=True, exist_ok=True)
snapshot_download(repo_id=os.environ["HF_REPO"], local_dir=str(dest),
                  token=os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN"))
print(f"OK {dest}")
PY'
}

# --------------------------------------------------------------------------
# HTTP waits
# --------------------------------------------------------------------------

# wait_http_ok NAME URL [TRIES] [CONTAINER_NAME_FOR_CRASH_DETECT]
wait_http_ok() {
  local name="$1" url="$2" tries="${3:-90}" cname="${4:-}"
  local i restart_hits=0
  _install_log "Waiting for ${name} (${url}) up to $((tries * 2))s..."
  for ((i = 1; i <= tries; i++)); do
    if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then
      _install_log "${name} is up"
      return 0
    fi
    if [[ -n "$cname" ]] && docker ps -a --format '{{.Names}} {{.Status}}' 2>/dev/null \
      | grep -E "^${cname} " | grep -qiE 'restarting|exited'; then
      restart_hits=$((restart_hits + 1))
      if (( restart_hits >= 3 )); then
        _install_err "${cname} is crash-looping (not a slow model pull)."
        docker logs --tail=60 "$cname" 2>/dev/null || true
        return 1
      fi
    else
      restart_hits=0
    fi
    if (( i % 20 == 0 )); then
      _install_log "  still waiting (${i}/${tries})"
      [[ -n "$cname" ]] && docker logs --tail=10 "$cname" 2>/dev/null || true
    fi
    sleep 2
  done
  _install_err "${name} did not become ready: ${url}"
  return 1
}

# Any HTTP response (including 404) means the process is accepting connections.
wait_http_any() {
  local name="$1" url="$2" tries="${3:-90}" cname="${4:-}"
  local i restart_hits=0 code
  _install_log "Waiting for ${name} (${url}) up to $((tries * 2))s (any HTTP)..."
  for ((i = 1; i <= tries; i++)); do
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 "$url" 2>/dev/null || echo "000")"
    if [[ "$code" != "000" && -n "$code" ]]; then
      _install_log "${name} is up (HTTP ${code})"
      return 0
    fi
    if [[ -n "$cname" ]] && docker ps -a --format '{{.Names}} {{.Status}}' 2>/dev/null \
      | grep -E "^${cname} " | grep -qiE 'restarting|exited'; then
      restart_hits=$((restart_hits + 1))
      if (( restart_hits >= 3 )); then
        _install_err "${cname} is crash-looping (not a slow model pull)."
        docker logs --tail=60 "$cname" 2>/dev/null || true
        return 1
      fi
    else
      restart_hits=0
    fi
    if (( i % 20 == 0 )); then
      _install_log "  still waiting (${i}/${tries})"
      [[ -n "$cname" ]] && docker logs --tail=10 "$cname" 2>/dev/null || true
    fi
    sleep 2
  done
  _install_err "${name} did not become ready: ${url}"
  return 1
}

# Spark / TTS readiness: POST speech stream must return 200.
wait_speech_ready() {
  local port="$1" tries="${2:-90}" cname="${3:-}"
  local i code
  _install_log "Waiting for TTS speech readiness on :${port}..."
  for ((i = 1; i <= tries; i++)); do
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 \
      -H 'Content-Type: application/json' \
      -d '{"text":"ok","speaker_id":"eng_female_1","temperature":0.7}' \
      "http://127.0.0.1:${port}/v1/audio/speech/stream" 2>/dev/null || echo "000")"
    if [[ "$code" == "200" ]]; then
      _install_log "tts speech ready on :${port}"
      return 0
    fi
    if [[ -n "$cname" ]] && docker ps -a --format '{{.Names}} {{.Status}}' 2>/dev/null \
      | grep -E "^${cname} " | grep -qiE 'restarting|exited'; then
      _install_err "${cname} is crash-looping while waiting for speech"
      docker logs --tail=40 "$cname" 2>/dev/null || true
      return 1
    fi
    if (( i % 30 == 0 )); then
      _install_log "  still waiting speech (${i}/${tries}) last HTTP=${code}"
    fi
    sleep 2
  done
  _install_err "TTS speech not ready on :${port}"
  return 1
}

# --------------------------------------------------------------------------
# Audio fixture
# --------------------------------------------------------------------------

make_smoke_wav() {
  local wav="$1"
  if ! command -v python3 >/dev/null 2>&1; then
    return 1
  fi
  python3 - "$wav" <<'PY'
import sys, wave, math, struct
path = sys.argv[1]
rate, n = 16000, 16000
with wave.open(path, "w") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
    for i in range(n):
        v = int(8000 * math.sin(2 * math.pi * 440 * i / rate))
        w.writeframes(struct.pack("<h", v))
PY
}

# --------------------------------------------------------------------------
# Wallet volume (DinD-safe tar pipe + chown for miner uid 10001)
# --------------------------------------------------------------------------

seed_wallet_volume() {
  local src="${BT_WALLET_DIR:-${HOME}/.bittensor}"
  local vol="${WALLET_VOLUME_NAME:-violet-bittensor-wallets}"
  local name="${BT_WALLET_NAME:-default}"
  local hotkey="${BT_WALLET_HOTKEY:-default}"
  local uid="${MINER_CONTAINER_UID:-10001}"

  if [[ "${BT_WALLET_BIND:-0}" == "1" ]]; then
    _install_log "BT_WALLET_BIND=1 — using host bind ${src}"
    return 0
  fi

  docker volume create "$vol" >/dev/null 2>&1 || true

  if [[ ! -d "$src" ]]; then
    _install_warn "no wallet dir at ${src} — volume ${vol} left empty"
    _install_warn "create with: btcli wallet new_coldkey / new_hotkey"
    return 1
  fi

  _install_log "seeding wallet volume ${vol} from ${src} (tar pipe; DinD-safe)"
  if ! tar -C "$src" -cf - . \
    | docker run --rm -i \
        -v "${vol}:/dst" \
        alpine:3.20 \
        sh -c "mkdir -p /dst && tar -C /dst -xf - && chown -R ${uid}:${uid} /dst && chmod -R u+rwX,go-rwx /dst && ls -la /dst/wallets"
  then
    _install_err "wallet seed failed"
    return 1
  fi

  if [[ -e "${src}/wallets/${name}/hotkeys/${hotkey}" \
     || -e "${src}/wallets/${name}/hotkeys/${hotkey}.json" ]]; then
    _install_log "found hotkey file for ${name}/${hotkey} in source tree"
  else
    _install_warn "${src}/wallets/${name}/hotkeys/${hotkey} not found"
    _install_warn ".env has BT_WALLET_NAME=${name} BT_WALLET_HOTKEY=${hotkey}"
    return 1
  fi
  return 0
}

# Fail-fast: keyfile must exist in the named volume (readable by uid 10001).
assert_wallet_keyfile() {
  local vol="${WALLET_VOLUME_NAME:-violet-bittensor-wallets}"
  local name="${BT_WALLET_NAME:-default}"
  local hotkey="${BT_WALLET_HOTKEY:-default}"
  local uid="${MINER_CONTAINER_UID:-10001}"
  local path="wallets/${name}/hotkeys/${hotkey}"

  if [[ "${BT_WALLET_BIND:-0}" == "1" ]]; then
    local src="${BT_WALLET_DIR:-${HOME}/.bittensor}"
    if [[ -e "${src}/${path}" || -e "${src}/${path}.json" ]]; then
      return 0
    fi
    _install_err "wallet keyfile missing: ${src}/${path}"
    return 1
  fi

  if ! docker volume inspect "$vol" >/dev/null 2>&1; then
    _install_err "wallet volume ${vol} does not exist — run seed_wallet_volume first"
    return 1
  fi

  if docker run --rm -u "${uid}:${uid}" -v "${vol}:/w:ro" alpine:3.20 \
    sh -c "test -r /w/${path} || test -r /w/${path}.json"
  then
    _install_log "wallet keyfile readable in volume: ${path}"
    return 0
  fi

  _install_err "wallet keyfile not readable as uid ${uid}: /home/violet/.bittensor/${path}"
  _install_err "Fix:"
  _install_err "  1. btcli wallet list  # confirm BT_WALLET_NAME / BT_WALLET_HOTKEY"
  _install_err "  2. tar -C ~/.bittensor -cf - . | docker run --rm -i -v ${vol}:/dst alpine:3.20 sh -c 'tar -C /dst -xf - && chown -R ${uid}:${uid} /dst'"
  return 1
}

# --------------------------------------------------------------------------
# Public endpoint validation
# --------------------------------------------------------------------------

# Reject pasted shell commands / garbage. Returns 0 if input looks like IP/DNS/URL.
validate_public_endpoint_input() {
  local raw="$1"
  if [[ -z "$raw" ]]; then
    _install_err "public IP/host is empty"
    return 1
  fi
  if [[ "$raw" == *" "* ]] || [[ "$raw" == *$'\t'* ]] || [[ "$raw" == *$'\n'* ]]; then
    _install_err "public IP/host contains whitespace (looks like pasted commands): '$raw'"
    return 1
  fi
  # Reject common paste-garbage tokens / shell metacharacters.
  if printf '%s' "$raw" | grep -Eq 'curl|python|wget|[;|&<>]'; then
    _install_err "public IP/host looks like a shell command: '$raw'"
    return 1
  fi
  # Disallow multiple http schemes (e.g. http://a http://b mashed together).
  local count
  count="$(grep -o 'https\?://' <<<"$raw" | wc -l | tr -d ' ')"
  if (( count > 1 )); then
    _install_err "public IP/host contains multiple URLs: '$raw'"
    return 1
  fi
  return 0
}

# Normalize bare IP / host:port / URL → http(s)://...
# Args: INPUT MINER_PORT
normalize_public_endpoint() {
  local host_or_url="$1" port="${2:-8091}" answer
  case "$host_or_url" in
    http://*|https://*)
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
      answer="http://${host_or_url}"
      ;;
    *)
      answer="http://${host_or_url}:${port}"
      ;;
  esac
  echo "$answer"
}

print_public_reachability_hint() {
  local port="${1:-${MINER_PORT:-8091}}"
  local ep="${2:-${MINER_PUBLIC_ENDPOINT:-}}"
  echo
  _install_warn "Public reachability is NOT verified from this host (NAT hairpin often fails)."
  _install_warn "If validators cannot dial you:"
  echo "  • Cloud SG / firewall: allow inbound TCP ${port}"
  echo "  • Home/office router (Keenetic etc.): forward WAN:${port} → VM_LAN_IP:${port}"
  echo "  • Do NOT put the miner on :80/:443 if those serve the router admin UI"
  if [[ -n "$ep" ]]; then
    echo "  • From another network:  curl -fsS ${ep}/health"
    echo "  • From another network:  nc -vz <host> ${port}"
  fi
  echo
}
