#!/usr/bin/env bash
# Shared GPU planning for cathedral-voice miner install scripts.
#
# Modes (plan_gpu_devices <mode>):
#   stt  — STT alone → ALL accepted host GPUs (none of those left idle)
#   tts  — TTS alone → ALL accepted host GPUs
#   both — STT + TTS on same host → partition accepted cards
#
# Unlisted SKUs (3080, T4, …) cannot run install/start unless GPU_ALLOW_UNLISTED=1.
# Mixed hosts: only Capacity-allowed indices are planned.
#
# Overrides (comma-separated): STT_GPU_DEVICES / TTS_GPU_DEVICES

# Repo root when this file lives at violet/miner/gpu_env.sh
_violet_repo_root() {
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  cd "${here}/../.." && pwd
}

# Physical GPU count only. Do not use `nvidia-smi -L` — MIG slices add extra lines.
detect_gpu_count() {
  if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo 0
    return 0
  fi
  nvidia-smi --query-gpu=index --format=csv,noheader 2>/dev/null \
    | grep -cE '^[0-9]+' || true
}

# Classify each card with the same rules as Capacity scoring (Python).
# Falls back to a raw nvidia-smi listing if the package import fails.
print_gpu_inventory() {
  local root
  root="$(_violet_repo_root)"
  if command -v python3 >/dev/null 2>&1; then
    if PYTHONPATH="${root}${PYTHONPATH:+:$PYTHONPATH}" \
      python3 -m violet.miner.gpu_inventory --human 2>/dev/null; then
      return 0
    fi
  fi
  if command -v nvidia-smi >/dev/null 2>&1; then
    echo "GPUs (raw nvidia-smi; Capacity classifier unavailable):"
    nvidia-smi --query-gpu=index,name,memory.total,uuid,compute_cap,driver_version \
      --format=csv 2>/dev/null || nvidia-smi -L 2>/dev/null || true
    return 0
  fi
  echo "GPUs: nvidia-smi not found"
  return 0
}

export_gpu_inventory_env() {
  local root
  root="$(_violet_repo_root)"
  command -v python3 >/dev/null 2>&1 || return 1
  PYTHONPATH="${root}${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m violet.miner.gpu_inventory --env
}

load_gpu_inventory_env() {
  local raw
  raw="$(export_gpu_inventory_env)" || return 1
  eval "$raw"
}

gpu_check_allowed_tiers() {
  local root rc
  root="$(_violet_repo_root)"
  command -v python3 >/dev/null 2>&1 || return 1
  PYTHONPATH="${root}${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m violet.miner.gpu_inventory --check
}

# Fail closed: no Capacity-allowed GPU → miner scripts must not proceed.
require_allowed_gpus() {
  local root rc
  if [[ "${GPU_ALLOW_UNLISTED:-0}" == "1" ]]; then
    echo "[gpu] GPU_ALLOW_UNLISTED=1 — skipping Capacity tier gate (not for mining)." >&2
    return 0
  fi
  root="$(_violet_repo_root)"
  if ! command -v python3 >/dev/null 2>&1; then
    echo "[gpu] python3 is required to classify GPUs before install." >&2
    return 1
  fi
  if ! PYTHONPATH="${root}${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m violet.miner.gpu_inventory --check; then
    echo "[gpu] REFUSED: this host has no GPU on the subnet allowlist." >&2
    print_gpu_inventory >&2 || true
    echo "[gpu] Install/start scripts will not run on unlisted cards." >&2
    echo "[gpu] Allowed tiers:" >&2
    PYTHONPATH="${root}${PYTHONPATH:+:$PYTHONPATH}" \
      python3 -m violet.miner.gpu_inventory --list-tiers >&2 || true
    echo "[gpu] Local CPU smoke only: GPU_ALLOW_UNLISTED=1 (earns no Capacity)." >&2
    return 1
  fi
  load_gpu_inventory_env || return 1
  if [[ -n "${GPU_REJECTED_INDICES:-}" ]]; then
    echo "[gpu] Unlisted GPU indices will not be used: ${GPU_REJECTED_INDICES}" >&2
  fi
  return 0
}

gpu_csv_slice() {
  local csv="$1" start="${2:-0}" end="${3:-}"
  local -a arr=()
  local i out=()
  [[ -z "$csv" ]] && { echo ""; return 0; }
  IFS=',' read -r -a arr <<< "$csv"
  if [[ -z "$end" ]]; then
    end=$((${#arr[@]} - 1))
  fi
  if (( ${#arr[@]} < 1 || start > end )); then
    echo ""
    return 0
  fi
  for ((i = start; i <= end; i++)); do
    out+=("${arr[i]}")
  done
  local IFS=,
  echo "${out[*]}"
}

# Every accepted index must appear in STT∪TTS (rejected cards may stay idle).
assert_no_idle_accepted_gpus() {
  local accepted="$1" stt="$2" tts="$3"
  local id found
  [[ -z "$accepted" ]] && return 0
  IFS=',' read -r -a ids <<< "$accepted"
  for id in "${ids[@]}"; do
    id="$(echo "$id" | tr -d '[:space:]')"
    [[ -z "$id" ]] && continue
    found=0
    [[ ",${stt}," == *",${id},"* ]] && found=1
    [[ ",${tts}," == *",${id},"* ]] && found=1
    if [[ $found -eq 0 ]]; then
      echo "accepted GPU ${id} would be idle (STT=${stt} TTS=${tts})" >&2
      return 1
    fi
  done
  return 0
}

assert_devices_are_accepted() {
  local devices="$1" label="${2:-GPU}"
  local id
  [[ -z "$devices" ]] && return 0
  [[ "${GPU_ALLOW_UNLISTED:-0}" == "1" ]] && return 0
  IFS=',' read -r -a ids <<< "$devices"
  for id in "${ids[@]}"; do
    id="$(echo "$id" | tr -d '[:space:]')"
    [[ -z "$id" ]] && continue
    if [[ ",${GPU_ACCEPTED_INDICES}," != *",${id},"* ]]; then
      echo "${label} index ${id} is not on the Capacity allowlist (accepted=${GPU_ACCEPTED_INDICES:-none})" >&2
      return 1
    fi
  done
  return 0
}

gpu_index_list() {
  local n="$1" start="${2:-0}" end="${3:-}"
  local i out=()
  if [[ -z "$end" ]]; then
    end=$((n - 1))
  fi
  if (( n < 1 || start > end )); then
    echo ""
    return 0
  fi
  for ((i = start; i <= end; i++)); do
    out+=("$i")
  done
  local IFS=,
  echo "${out[*]}"
}

gpu_device_count() {
  local devices="$1"
  if [[ -z "$devices" ]]; then
    echo 0
    return 0
  fi
  awk -F',' '{print NF}' <<<"$devices"
}

# Verify every index 0..N-1 appears in STT∪TTS when mode=both (N>=2).
assert_no_idle_gpus() {
  local n="$1" stt="$2" tts="$3"
  local i found
  (( n < 1 )) && return 0
  for ((i = 0; i < n; i++)); do
    found=0
    [[ ",${stt}," == *",${i},"* ]] && found=1
    [[ ",${tts}," == *",${i},"* ]] && found=1
    if [[ $found -eq 0 ]]; then
      echo "GPU ${i} would be idle (STT=${stt} TTS=${tts})" >&2
      return 1
    fi
  done
  return 0
}

# plan_gpu_devices [stt|tts|both]
#
# Always recomputes from mode + nvidia-smi unless the caller locks a plan:
#   GPU_PLAN_LOCKED=1 with STT_GPU_DEVICES / TTS_GPU_DEVICES already set
# (start.sh plans once, then locks before calling install scripts).
#
# Manual pin without lock:
#   STT_GPU_DEVICES_OVERRIDE=0,1 TTS_GPU_DEVICES_OVERRIDE=2,3
plan_gpu_devices() {
  local mode="${1:-both}"
  local n accepted

  require_allowed_gpus || return 1
  if ! load_gpu_inventory_env; then
    if [[ "${GPU_ALLOW_UNLISTED:-0}" == "1" ]]; then
      n="$(detect_gpu_count)"
      GPU_COUNT="$n"
      GPU_ACCEPTED_INDICES="$(gpu_index_list "$n")"
      GPU_ACCEPTED_COUNT="$n"
      export GPU_COUNT GPU_ACCEPTED_INDICES GPU_ACCEPTED_COUNT
    else
      return 1
    fi
  fi

  accepted="${GPU_ACCEPTED_INDICES:-}"
  n="$(gpu_device_count "$accepted")"
  export GPU_COUNT GPU_ACCEPTED_INDICES GPU_ACCEPTED_COUNT

  if [[ "${GPU_PLAN_LOCKED:-0}" == "1" ]] \
    && [[ -n "${STT_GPU_DEVICES:-}${TTS_GPU_DEVICES:-}" ]]; then
    assert_devices_are_accepted "${STT_GPU_DEVICES:-}" STT || return 1
    assert_devices_are_accepted "${TTS_GPU_DEVICES:-}" TTS || return 1
    export STT_GPU_DEVICES TTS_GPU_DEVICES
    return 0
  fi

  if [[ -n "${STT_GPU_DEVICES_OVERRIDE:-}" || -n "${TTS_GPU_DEVICES_OVERRIDE:-}" ]]; then
    case "$mode" in
      stt)
        STT_GPU_DEVICES="${STT_GPU_DEVICES_OVERRIDE:-${NVIDIA_VISIBLE_DEVICES:-$accepted}}"
        TTS_GPU_DEVICES=""
        ;;
      tts)
        TTS_GPU_DEVICES="${TTS_GPU_DEVICES_OVERRIDE:-${NVIDIA_VISIBLE_DEVICES:-$accepted}}"
        STT_GPU_DEVICES=""
        ;;
      *)
        STT_GPU_DEVICES="${STT_GPU_DEVICES_OVERRIDE:-}"
        TTS_GPU_DEVICES="${TTS_GPU_DEVICES_OVERRIDE:-}"
        if [[ -z "$STT_GPU_DEVICES" && "$n" -ge 1 ]]; then
          STT_GPU_DEVICES="$accepted"
        fi
        if [[ -z "$TTS_GPU_DEVICES" && "$n" -ge 1 ]]; then
          TTS_GPU_DEVICES="$accepted"
        fi
        ;;
    esac
    assert_devices_are_accepted "${STT_GPU_DEVICES:-}" STT || return 1
    assert_devices_are_accepted "${TTS_GPU_DEVICES:-}" TTS || return 1
    export STT_GPU_DEVICES TTS_GPU_DEVICES
    return 0
  fi

  if (( n < 1 )); then
    STT_GPU_DEVICES=""
    TTS_GPU_DEVICES=""
    export STT_GPU_DEVICES TTS_GPU_DEVICES
    return 0
  fi

  case "$mode" in
    stt)
      STT_GPU_DEVICES="$accepted"
      TTS_GPU_DEVICES=""
      ;;
    tts)
      TTS_GPU_DEVICES="$accepted"
      STT_GPU_DEVICES=""
      ;;
    both|*)
      if (( n == 1 )); then
        STT_GPU_DEVICES="$accepted"
        TTS_GPU_DEVICES="$accepted"
      else
        local mid=$(( (n + 1) / 2 ))
        if (( mid < 1 )); then mid=1; fi
        if (( mid >= n )); then mid=$((n - 1)); fi
        STT_GPU_DEVICES="$(gpu_csv_slice "$accepted" 0 $((mid - 1)))"
        TTS_GPU_DEVICES="$(gpu_csv_slice "$accepted" "$mid" $((n - 1)))"
        assert_no_idle_accepted_gpus "$accepted" "$STT_GPU_DEVICES" "$TTS_GPU_DEVICES" \
          || { echo "internal GPU plan left an allowed card idle" >&2; return 1; }
      fi
      ;;
  esac
  export STT_GPU_DEVICES TTS_GPU_DEVICES
}

gpu_compose_device_ids_yaml() {
  local devices="$1"
  if [[ -z "$devices" ]]; then
    echo '            - driver: nvidia'
    echo '              count: all'
    echo '              capabilities: [gpu]'
    return 0
  fi
  local ids_yaml="[" first=1 id
  IFS=',' read -r -a arr <<<"$devices"
  for id in "${arr[@]}"; do
    id="$(echo "$id" | tr -d '[:space:]')"
    [[ -z "$id" ]] && continue
    if [[ $first -eq 1 ]]; then first=0; else ids_yaml+=", "; fi
    ids_yaml+="\"${id}\""
  done
  ids_yaml+="]"
  echo '            - driver: nvidia'
  echo "              device_ids: ${ids_yaml}"
  echo '              capabilities: [gpu]'
}

# Suggest admission caps from GPU counts (sidecar concurrency).
suggest_concurrency() {
  local stt_n tts_n
  stt_n="$(gpu_device_count "${STT_GPU_DEVICES:-}")"
  tts_n="$(gpu_device_count "${TTS_GPU_DEVICES:-}")"
  (( stt_n < 1 )) && stt_n=1
  (( tts_n < 1 )) && tts_n=1
  # ~2 in-flight jobs per GPU as a safe default; operators can raise later.
  echo "$((stt_n * 2)) $((tts_n * 2))"
}

check_disk_gb() {
  local need_gb="${1:-40}" path="${2:-.}"
  local avail
  avail="$(df -BG "$path" 2>/dev/null | awk 'NR==2{gsub(/G/,"",$4); print $4}')"
  if [[ -z "$avail" ]]; then
    return 0
  fi
  if (( avail < need_gb )); then
    echo "Only ${avail}G free under ${path}; need ~${need_gb}G for models" >&2
    return 1
  fi
  return 0
}

install_nvidia_toolkit_if_needed() {
  local SUDO="${1:-sudo}"
  if command -v nvidia-ctk &>/dev/null; then
    return 0
  fi
  if ! command -v nvidia-smi &>/dev/null; then
    return 0
  fi
  echo "[gpu] Installing NVIDIA Container Toolkit..."
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | $SUDO gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | $SUDO tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
  $SUDO apt-get update -qq
  $SUDO apt-get install -y nvidia-container-toolkit
  $SUDO nvidia-ctk runtime configure --runtime=docker
  $SUDO systemctl restart docker || true
}
