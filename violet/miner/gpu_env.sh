#!/usr/bin/env bash
# Shared GPU planning for cathedral-voice miner install scripts.
#
# Modes (plan_gpu_devices <mode>):
#   stt  — STT alone → ALL host GPUs (none left idle)
#   tts  — TTS alone → ALL host GPUs
#   both — STT + TTS on same host → partition so every GPU is assigned
#          N=1 → share GPU 0
#          N>=2 → first half STT, second half TTS (covers all indices)
#
# Overrides (comma-separated): STT_GPU_DEVICES / TTS_GPU_DEVICES

detect_gpu_count() {
  if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo 0
    return 0
  fi
  nvidia-smi -L 2>/dev/null | wc -l | tr -d ' '
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
  local n
  n="$(detect_gpu_count)"
  GPU_COUNT="$n"
  export GPU_COUNT

  if [[ "${GPU_PLAN_LOCKED:-0}" == "1" ]] \
    && [[ -n "${STT_GPU_DEVICES:-}${TTS_GPU_DEVICES:-}" ]]; then
    export STT_GPU_DEVICES TTS_GPU_DEVICES
    return 0
  fi

  if [[ -n "${STT_GPU_DEVICES_OVERRIDE:-}" || -n "${TTS_GPU_DEVICES_OVERRIDE:-}" ]]; then
    case "$mode" in
      stt)
        STT_GPU_DEVICES="${STT_GPU_DEVICES_OVERRIDE:-${NVIDIA_VISIBLE_DEVICES:-$(gpu_index_list "$n")}}"
        TTS_GPU_DEVICES=""
        ;;
      tts)
        TTS_GPU_DEVICES="${TTS_GPU_DEVICES_OVERRIDE:-${NVIDIA_VISIBLE_DEVICES:-$(gpu_index_list "$n")}}"
        STT_GPU_DEVICES=""
        ;;
      *)
        STT_GPU_DEVICES="${STT_GPU_DEVICES_OVERRIDE:-}"
        TTS_GPU_DEVICES="${TTS_GPU_DEVICES_OVERRIDE:-}"
        if [[ -z "$STT_GPU_DEVICES" && "$n" -ge 1 ]]; then
          STT_GPU_DEVICES="$(gpu_index_list "$n")"
        fi
        if [[ -z "$TTS_GPU_DEVICES" && "$n" -ge 1 ]]; then
          TTS_GPU_DEVICES="$(gpu_index_list "$n")"
        fi
        ;;
    esac
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
      # Solo STT: every GPU works ASR — none idle.
      STT_GPU_DEVICES="$(gpu_index_list "$n")"
      TTS_GPU_DEVICES=""
      ;;
    tts)
      TTS_GPU_DEVICES="$(gpu_index_list "$n")"
      STT_GPU_DEVICES=""
      ;;
    both|*)
      if (( n == 1 )); then
        STT_GPU_DEVICES="0"
        TTS_GPU_DEVICES="0"
      else
        local mid=$(( (n + 1) / 2 ))  # STT gets ceil(N/2), TTS the rest
        if (( mid < 1 )); then mid=1; fi
        if (( mid >= n )); then mid=$((n - 1)); fi
        STT_GPU_DEVICES="$(gpu_index_list "$n" 0 $((mid - 1)))"
        TTS_GPU_DEVICES="$(gpu_index_list "$n" "$mid" $((n - 1)))"
        assert_no_idle_gpus "$n" "$STT_GPU_DEVICES" "$TTS_GPU_DEVICES" \
          || { echo "internal GPU plan left a card idle" >&2; exit 1; }
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
