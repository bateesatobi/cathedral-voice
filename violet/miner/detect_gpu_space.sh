#!/usr/bin/env bash
#
# detect_gpu_space.sh — Classify GPU host before cathedral-voice install.
#
# Distinguishes bare GPU VMs (full CUDA) from nested GPU "spaces" that only
# expose nvidia-smi (NVML) but block CUDA compute (cuCtxCreate 999).
#
# Usage:
#   ./violet/miner/detect_gpu_space.sh
#   ./violet/miner/detect_gpu_space.sh --json
#   ./violet/miner/detect_gpu_space.sh --quiet    # print class only; exit code
#   ./violet/miner/detect_gpu_space.sh --recommend-deploy
#
# Exit codes:
#   0  bare_gpu_ok or host_socket_gpu_ok — GPU inference viable
#   1  nvml_only — H100 visible but CUDA compute blocked (move to bare VM)
#   2  shell_only_gpu — shell CUDA works, docker --gpus may need run mode
#   4  no Capacity-allowed GPU (RTX 3090–GB200 list) — install scripts refuse
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_LOG_PREFIX="detect_gpu"
# shellcheck source=install_lib.sh
source "${SCRIPT_DIR}/install_lib.sh"
# shellcheck source=gpu_env.sh
source "${SCRIPT_DIR}/gpu_env.sh"

JSON=0
QUIET=0
RECOMMEND=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --json) JSON=1 ;;
    --quiet) QUIET=1 ;;
    --recommend-deploy) RECOMMEND=1 ;;
    -h|--help)
      sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

classify_gpu_space

nested=0
is_nested_docker && nested=1

exit_code=3
case "$GPU_SPACE_CLASS" in
  bare_gpu_ok|host_socket_gpu_ok) exit_code=0 ;;
  nvml_only) exit_code=1 ;;
  shell_only_gpu) exit_code=2 ;;
  *) exit_code=3 ;;
esac

GPU_TIER_OK=1
GPU_TIER_CHECK_RC=0
if [[ "${GPU_ALLOW_UNLISTED:-0}" != "1" ]]; then
  gpu_check_allowed_tiers || GPU_TIER_CHECK_RC=$?
  if [[ "$GPU_TIER_CHECK_RC" -eq 2 ]]; then
    GPU_TIER_OK=0
    exit_code=4
  elif [[ "$GPU_TIER_CHECK_RC" -eq 3 ]]; then
    GPU_TIER_OK=0
    if [[ "$exit_code" -eq 0 ]]; then
      exit_code=4
    fi
  elif [[ "$GPU_TIER_CHECK_RC" -ne 0 ]]; then
    GPU_TIER_OK=0
    exit_code=4
  fi
fi

if (( QUIET )); then
  if [[ "$GPU_TIER_CHECK_RC" -eq 2 ]]; then
    echo "no_allowed_tier"
  else
    echo "$GPU_SPACE_CLASS"
  fi
  exit "$exit_code"
fi

if (( RECOMMEND )); then
  if [[ "$GPU_TIER_CHECK_RC" -eq 2 ]]; then
    echo "refused"
  else
    echo "$GPU_SPACE_DEPLOY"
  fi
  exit "$exit_code"
fi

if (( JSON )); then
  export GPU_SPACE_CLASS GPU_SPACE_DEPLOY
  GPU_INVENTORY_JSON=""
  if command -v python3 >/dev/null 2>&1; then
    GPU_INVENTORY_JSON="$(
      PYTHONPATH="${SCRIPT_DIR}/../..${PYTHONPATH:+:$PYTHONPATH}" \
        python3 -m violet.miner.gpu_inventory --json 2>/dev/null || true
    )"
  fi
  export GPU_INVENTORY_JSON
  python3 - <<PY
import json, os
inventory = {}
raw = os.environ.get("GPU_INVENTORY_JSON") or ""
if raw.strip():
    try:
        inventory = json.loads(raw)
    except json.JSONDecodeError:
        inventory = {}
print(json.dumps({
    "class": os.environ.get("GPU_SPACE_CLASS", ""),
    "recommended_deploy": os.environ.get("GPU_SPACE_DEPLOY", ""),
    "nested_docker": bool(int("${nested}")),
    "tier_ok": bool(int("${GPU_TIER_OK}")),
    "shell_probe_rc": int("${GPU_PROBE_SHELL_RC:-127}"),
    "docker_probe_rc": int("${GPU_PROBE_DOCKER_RC:-127}"),
    "shell_alloc_ok": "ALLOC_OK" in """${GPU_PROBE_SHELL_OUT:-}""",
    "docker_alloc_ok": "ALLOC_OK" in """${GPU_PROBE_DOCKER_OUT:-}""",
    "inventory": inventory,
}, indent=2))
PY
  exit "$exit_code"
fi

echo "============================================================"
echo " cathedral-voice GPU space detection"
echo "============================================================"
echo
echo "Environment"
if (( nested )); then
  echo "  Shell context     : nested container (/.dockerenv present)"
  echo "  Docker socket     : $( [[ -S /var/run/docker.sock ]] && echo present || echo missing )"
else
  echo "  Shell context     : bare host (no /.dockerenv)"
fi
if command -v nvidia-smi >/dev/null 2>&1; then
  echo "  nvidia-smi        : $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo unavailable)"
else
  echo "  nvidia-smi        : not installed"
fi
echo
echo "Capacity inventory (physical GPUs, accepted tiers only earn units)"
print_gpu_inventory | sed 's/^/  /'
echo
echo "CUDA compute probes (NVML/nvidia-smi is NOT sufficient)"
echo "  Shell namespace   : rc=${GPU_PROBE_SHELL_RC:-?} $(gpu_probe_has_alloc_ok "${GPU_PROBE_SHELL_OUT:-}" && echo ALLOC_OK || echo FAIL)"
if [[ -n "${GPU_PROBE_SHELL_OUT:-}" ]]; then
  echo "${GPU_PROBE_SHELL_OUT}" | sed 's/^/    /'
fi
echo "  docker run --gpus : rc=${GPU_PROBE_DOCKER_RC:-?} $(gpu_probe_has_alloc_ok "${GPU_PROBE_DOCKER_OUT:-}" && echo ALLOC_OK || echo FAIL)"
if [[ -n "${GPU_PROBE_DOCKER_OUT:-}" ]]; then
  echo "${GPU_PROBE_DOCKER_OUT}" | sed 's/^/    /'
fi
echo
echo "Classification      : ${GPU_SPACE_CLASS}"
echo "Recommended deploy  : MINER_INFERENCE_DEPLOY=${GPU_SPACE_DEPLOY}"
echo "Capacity tier gate  : $( [[ "${GPU_TIER_OK}" == "1" ]] && echo pass || echo FAIL )"
echo

if [[ "${GPU_TIER_CHECK_RC:-0}" -eq 2 ]]; then
  echo "Verdict: REFUSED — GPU(s) present but none are on the subnet allowlist."
  echo "  Miner install/start scripts will not run on this host."
  echo "  python3 -m violet.miner.gpu_inventory --list-tiers"
  echo "  Dev-only bypass: GPU_ALLOW_UNLISTED=1 (earns no Capacity)."
  echo "============================================================"
  exit 4
fi

case "$GPU_SPACE_CLASS" in
  bare_gpu_ok)
    echo "Verdict: OK — bare GPU VM. Use standard install:"
    echo "  ./violet/miner/stt_install.sh"
    echo "  ./violet/miner/tts_install.sh"
    echo "  ./violet/miner/start.sh prod"
    ;;
  host_socket_gpu_ok)
    echo "Verdict: OK — GPU tenant with working Docker GPU alloc (host socket)."
    echo "  Use single-container run mode (no compose bridge):"
    echo "  MINER_INFERENCE_DEPLOY=run ./violet/miner/start.sh prod"
    echo "  Or: ./violet/miner/inference_run_install.sh"
    ;;
  shell_only_gpu)
    echo "Verdict: PARTIAL — CUDA works in this shell but not via docker run --gpus."
    echo "  Try run mode; if inference still fails, use a bare GPU VM."
    echo "  MINER_INFERENCE_DEPLOY=run ./violet/miner/inference_run_install.sh"
    ;;
  nvml_only)
    echo "Verdict: NVML-ONLY — GPU is listed but CUDA compute is BLOCKED."
    echo "  This is common on nested GPU rental jobs (cuCtxCreate 999)."
    echo "  You cannot run GPU ASR/TTS here. Options:"
    echo "    1. Move to a bare GPU VM / WSL2 (Docker is the host OS service)"
    echo "    2. Dev smoke only: STT_FORCE_CPU=1 ./violet/miner/stt_install.sh"
    echo "    3. ASR-only on another host: MINER_SERVICES=asr"
    ;;
  *)
    echo "Verdict: NO GPU detected for inference."
    echo "  Install NVIDIA drivers + nvidia-container-toolkit, or pick a GPU offer."
    ;;
esac
echo "============================================================"
exit "$exit_code"
