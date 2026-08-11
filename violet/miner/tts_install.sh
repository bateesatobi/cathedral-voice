#!/usr/bin/env bash
#
# tts_install.sh — Install & run cathedral-voice TTS (Spark-TTS frontend).
#
# GPU rules (via gpu_env.sh / GPU_PLAN_MODE):
#   tts  (default when run alone) — ALL host GPUs for TTS; none idle
#   both — only the TTS partition; share GPU 0 when N=1
#
# MODEL_POOL_SIZE defaults to assigned GPU count so every card holds work.
# When sharing a single GPU with STT (plan=both, N=1), VRAM util is lowered.
#
# Usage:
#   ./violet/miner/tts_install.sh
#   GPU_PLAN_MODE=both TTS_GPU_DEVICES=2,3 ./violet/miner/tts_install.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=gpu_env.sh
source "${SCRIPT_DIR}/gpu_env.sh"
# shellcheck source=install_lib.sh
INSTALL_LOG_PREFIX="tts_install"
source "${SCRIPT_DIR}/install_lib.sh"

DEFAULT_HF_TOKEN="$(default_hf_token)"
IMAGE_TAG="${TTS_IMAGE:-simonallanachuka/spark-tts-frontend:latest}"
CONTAINER_NAME="${TTS_CONTAINER_NAME:-cathedral-spark-tts}"
TTS_HOST_PORT="${TTS_HOST_PORT:-8002}"
MODEL_NAME="${MODEL_NAME:-phosai/phosai_tts_v1}"
TOKENIZER_REPO="${TOKENIZER_REPO:-phosai/phosai_tts_v1}"
# Base Spark assets the frontend always expects under /app/models/Spark-TTS-0.5B
SPARK_BASE_REPO="${SPARK_BASE_REPO:-SparkAudio/Spark-TTS-0.5B}"
# Empty → auto: f16 when sharing one GPU with STT, else f32
SPARK_TTS_DTYPE="${SPARK_TTS_DTYPE:-}"
# Prefer a named Docker volume so nested Docker (shell in container, daemon on
# host) does not fail with "no such file or directory" on bind mounts.
MODELS_VOLUME="${TTS_MODELS_VOLUME:-cathedral-tts-models}"
MODELS_DIR="${TTS_MODELS_DIR:-}"  # optional host bind; empty → use MODELS_VOLUME
TTS_READY_TRIES="${TTS_READY_TRIES:-900}"  # ~30 min first pull
TTS_STRICT="${TTS_STRICT:-1}"
GPU_PLAN_MODE="${GPU_PLAN_MODE:-tts}"
SHM_SIZE="${TTS_SHM_SIZE:-4gb}"
SEED_IMAGE="${TTS_SEED_IMAGE:-python:3.11-slim}"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[tts_install]${NC} $*"; }
warn() { echo -e "${YELLOW}[tts_install warn]${NC} $*"; }
err()  { echo -e "${RED}[tts_install error]${NC} $*" >&2; }

wait_http() {
  local name="$1" url="$2" tries="${3:-90}"
  # Optional 4th arg: "any" = any HTTP response counts (Spark has no /health).
  local mode="${4:-ok}"
  if [[ "$mode" == "any" ]]; then
    wait_http_any "$name" "$url" "$tries" "${CONTAINER_NAME}"
  else
    wait_http_ok "$name" "$url" "$tries" "${CONTAINER_NAME}"
  fi
}

smoke_tts() {
  local base="http://127.0.0.1:${TTS_HOST_PORT}"
  local ok=1
  log "Contract smoke (miner-facing): POST /v1/audio/speech/stream (do not require /health)"

  local code
  code="$(curl -sS -o /tmp/tts_smoke.pcm -w '%{http_code}' --max-time 180 \
    -H 'Content-Type: application/json' \
    -d '{"text":"Hello cathedral voice smoke test.","speaker_id":"eng_female_1","temperature":0.7}' \
    "${base}/v1/audio/speech/stream" 2>/dev/null || echo "000")"
  if [[ "$code" == "200" ]]; then
    local bytes
    bytes="$(wc -c </tmp/tts_smoke.pcm | tr -d ' ')"
    log "Contract smoke: POST /v1/audio/speech/stream → 200 (${bytes} bytes)"
  else
    warn "Speech stream smoke → HTTP ${code}"
    ok=0
  fi

  code="$(curl -sS -o /tmp/tts_voices.json -w '%{http_code}' --max-time 30 \
    "${base}/v1/audio/voices" 2>/dev/null || echo "000")"
  if [[ "$code" == "200" ]]; then
    log "Optional: GET /v1/audio/voices → 200"
  else
    log "Optional: GET /v1/audio/voices → HTTP ${code} (OK if this image has no /voices)"
  fi

  if [[ "$ok" -ne 1 ]]; then
    if [[ "${TTS_STRICT}" == "1" ]]; then
      err "TTS_STRICT=1 — speech stream smoke failed"
      return 1
    fi
    return 1
  fi
  return 0
}

install_docker() {
  if command -v docker &>/dev/null; then
    log "Docker already installed: $(docker --version)"
    return 0
  fi
  log "Installing Docker..."
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  sudo sh /tmp/get-docker.sh
  rm -f /tmp/get-docker.sh
  sudo usermod -aG docker "${USER}" || true
}

# The spark-tts image downloads tokenizer.json with bare curl (often without
# auth / LFS handling). That writes HTML/empty files → crash loop. Pre-seed the
# models volume with huggingface_hub instead.
seed_tts_models() {
  local models_mount="$1"
  local seed_src="${models_mount%%:*}"
  log "Pre-seeding TTS models into volume (Spark base + ${MODEL_NAME})..."
  log "  This can take several minutes on first run."

  sudo docker pull "${SEED_IMAGE}" >/dev/null

  sudo docker run --rm \
    -e HF_TOKEN="${DEFAULT_HF_TOKEN}" \
    -e HUGGING_FACE_HUB_TOKEN="${DEFAULT_HF_TOKEN}" \
    -e SPARK_BASE_REPO="${SPARK_BASE_REPO}" \
    -e MODEL_NAME="${MODEL_NAME}" \
    -v "${seed_src}:/models" \
    "${SEED_IMAGE}" \
    bash -c '
set -euo pipefail
pip install -q "huggingface_hub>=0.23"
python - <<'"'"'PY'"'"'
import os, shutil
from pathlib import Path
from huggingface_hub import snapshot_download, hf_hub_download

token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
base_repo = os.environ["SPARK_BASE_REPO"]
model_name = os.environ["MODEL_NAME"]
root = Path("/models")
spark_dir = root / "Spark-TTS-0.5B"
spark_dir.mkdir(parents=True, exist_ok=True)

# Drop corrupt leftover tokenizer from failed container curls.
for bad in (spark_dir / "tokenizer.json", spark_dir / "LLM" / "tokenizer.json"):
    if bad.exists():
        try:
            text = bad.read_text(errors="ignore")[:64].lstrip()
            if not text.startswith("{"):
                print(f"removing corrupt {bad}")
                bad.unlink()
        except Exception:
            bad.unlink(missing_ok=True)

print(f"snapshot_download {base_repo} → {spark_dir}")
snapshot_download(
    repo_id=base_repo,
    local_dir=str(spark_dir),
    token=token,
)

# Frontend expects tokenizer.json at model root (not only under LLM/).
candidates = [
    spark_dir / "tokenizer.json",
    spark_dir / "LLM" / "tokenizer.json",
]
src = next((p for p in candidates if p.exists() and p.stat().st_size > 1000), None)
if src is None:
    # Fallback: fetch LLM tokenizer explicitly from known layouts.
    for repo, path in (
        (base_repo, "LLM/tokenizer.json"),
        (base_repo, "tokenizer.json"),
        ("unsloth/Spark-TTS-0.5B", "LLM/tokenizer.json"),
    ):
        try:
            downloaded = hf_hub_download(
                repo_id=repo, filename=path, token=token, local_dir=str(spark_dir)
            )
            src = Path(downloaded)
            if src.exists() and src.stat().st_size > 1000:
                break
        except Exception as exc:
            print(f"  skip {repo}/{path}: {exc}")
            src = None
    if src is None:
        raise SystemExit("could not obtain a valid tokenizer.json")

dest = spark_dir / "tokenizer.json"
if src.resolve() != dest.resolve():
    shutil.copy2(src, dest)
text = dest.read_text(errors="ignore")[:1]
if text != "{":
    raise SystemExit(f"tokenizer.json still invalid at {dest}")
print(f"OK tokenizer.json ({dest.stat().st_size} bytes)")

# Crane/Candle looks for model.safetensors at the Spark-TTS-0.5B ROOT,
# not under LLM/. Official HF layout keeps weights in LLM/ — promote them.
llm_dir = spark_dir / "LLM"
for name in (
    "model.safetensors",
    "model.safetensors.index.json",
    "config.json",
    "generation_config.json",
):
    src_w = llm_dir / name
    dst_w = spark_dir / name
    if src_w.is_file() and (not dst_w.exists() or dst_w.stat().st_size < src_w.stat().st_size):
        shutil.copy2(src_w, dst_w)
        print(f"promote {src_w.relative_to(spark_dir)} → {dst_w.name}")
for src_w in llm_dir.glob("model-*.safetensors"):
    dst_w = spark_dir / src_w.name
    if not dst_w.exists():
        shutil.copy2(src_w, dst_w)
        print(f"promote {src_w.name}")

# Also pull the serving model weights when different from the Spark base,
# then overlay LLM weights into both LLM/ and model root (what Crane loads).
if model_name and model_name not in (base_repo, "Spark-TTS-0.5B", "unsloth/Spark-TTS-0.5B"):
    out = root / model_name.replace("/", "__")
    print(f"snapshot_download {model_name} → {out}")
    snapshot_download(
        repo_id=model_name,
        local_dir=str(out),
        token=token,
    )
    llm_dst = spark_dir / "LLM"
    llm_dst.mkdir(parents=True, exist_ok=True)
    for src_dir in (out / "LLM", out):
        if not src_dir.is_dir():
            continue
        for name in (
            "model.safetensors",
            "model.safetensors.index.json",
            "config.json",
            "generation_config.json",
        ):
            src_f = src_dir / name
            if src_f.is_file() and src_f.stat().st_size > 100:
                for dst in (llm_dst / name, spark_dir / name):
                    shutil.copy2(src_f, dst)
                    print(f"overlay {src_f} → {dst}")
        for src_f in src_dir.glob("model-*.safetensors"):
            for dst in (llm_dst / src_f.name, spark_dir / src_f.name):
                shutil.copy2(src_f, dst)
                print(f"overlay {src_f.name}")

# Crane requires model.safetensors or index at the model root.
root_weight = spark_dir / "model.safetensors"
root_index = spark_dir / "model.safetensors.index.json"
if not root_weight.exists() and not root_index.exists():
    raise SystemExit(
        f"no model.safetensors at {spark_dir} (Crane needs it at ROOT, not only under LLM/)"
    )

# Reject git-lfs pointer stubs (tiny files that look downloaded but are not).
for weight in list(spark_dir.glob("model*.safetensors")) + list(llm_dir.glob("model*.safetensors")):
    size = weight.stat().st_size
    head = weight.read_bytes()[:80]
    if size < 1_000_000 or head.startswith(b"version https://git-lfs.github.com"):
        raise SystemExit(
            f"corrupt/incomplete weight {weight} ({size} bytes) — re-seed with network"
        )
    print(f"OK weight {weight.relative_to(spark_dir)} ({size} bytes)")
print("seed complete")
PY
'
}

main() {
  plan_gpu_devices "${GPU_PLAN_MODE}"
  local devices="${TTS_GPU_DEVICES}"
  local n
  n="$(gpu_device_count "$devices")"
  if [[ -z "$devices" || "$n" -lt 1 ]]; then
    err "No GPUs assigned to TTS. Install NVIDIA drivers or set TTS_GPU_DEVICES."
    exit 1
  fi

  if [[ -n "${MODELS_DIR}" ]]; then
    check_disk_gb 40 "${MODELS_DIR}" || warn "Low disk — TTS model pulls may fail"
  else
    check_disk_gb 40 / || warn "Low disk — TTS model pulls may fail"
  fi

  local pool="${MODEL_POOL_SIZE:-$n}"
  # Shared single-GPU with STT: leave headroom. Solo / dedicated cards: use more VRAM.
  local mem_util="${GPU_MEMORY_UTILIZATION:-}"
  if [[ -z "$mem_util" ]]; then
    if [[ "$GPU_PLAN_MODE" == "both" && "$n" -eq 1 && "${GPU_COUNT:-1}" -eq 1 ]]; then
      mem_util="0.35"
      warn "Single-GPU host sharing STT+TTS — GPU_MEMORY_UTILIZATION=${mem_util}"
    else
      mem_util="0.85"
    fi
  fi

  local dtype="${SPARK_TTS_DTYPE}"
  if [[ -z "$dtype" ]]; then
    if [[ "$GPU_PLAN_MODE" == "both" && "$n" -eq 1 && "${GPU_COUNT:-1}" -eq 1 ]]; then
      dtype="f16"
      warn "Single-GPU host sharing STT+TTS — SPARK_TTS_DTYPE=${dtype}"
    else
      dtype="f32"
    fi
  fi

  log "TTS GPUs: ${devices} (pool=${pool}, mem_util=${mem_util}, dtype=${dtype}, plan=${GPU_PLAN_MODE})"

  if [[ -f /.dockerenv ]] && [[ -z "${TTS_ALLOW_NESTED_DOCKER:-}" ]]; then
    warn_nested_docker "TTS_ALLOW_NESTED_DOCKER"
    warn "Using named volume '${MODELS_VOLUME}' for /app/models (avoids bind-mount DinD failures)."
  fi

  install_docker
  install_nvidia_toolkit_if_needed "sudo"

  local models_mount
  if [[ -n "${MODELS_DIR}" ]]; then
    mkdir -p "${MODELS_DIR}"
    models_mount="${MODELS_DIR}:/app/models"
    log "Models bind mount: ${MODELS_DIR}"
  else
    models_mount="${MODELS_VOLUME}:/app/models"
    log "Models named volume: ${MODELS_VOLUME}"
  fi

  # Stop crash-looping container before seeding so the volume is free.
  if sudo docker ps -a --format '{{.Names}}' | grep -Eq "^${CONTAINER_NAME}\$"; then
    warn "Stopping existing container ${CONTAINER_NAME} before model seed"
    sudo docker stop "${CONTAINER_NAME}" || true
    sudo docker rm "${CONTAINER_NAME}" || true
  fi

  seed_tts_models "${models_mount}"

  log "Pulling ${IMAGE_TAG}..."
  sudo docker pull "${IMAGE_TAG}"

  local cuda_inside
  # Inside the container Docker already remaps --gpus device=… to ordinals
  # 0..N-1. Force those remapped indices (not host physical IDs).
  cuda_inside="$(gpu_index_list "$n")"

  log "Starting ${CONTAINER_NAME} on host port ${TTS_HOST_PORT}..."
  # Do not also set NVIDIA_VISIBLE_DEVICES to host IDs — that can make Candle
  # pick a non-existent CudaDevice ordinal when combined with --gpus device=.
  sudo docker run -d \
    --name "${CONTAINER_NAME}" \
    --restart unless-stopped \
    --gpus "device=${devices}" \
    -p "${TTS_HOST_PORT}:8002" \
    -e HF_TOKEN="${DEFAULT_HF_TOKEN}" \
    -e HUGGING_FACE_HUB_TOKEN="${DEFAULT_HF_TOKEN}" \
    -e MODEL_NAME="${MODEL_NAME}" \
    -e TOKENIZER_REPO="${TOKENIZER_REPO}" \
    -e CUDA_VISIBLE_DEVICES="${cuda_inside}" \
    -e MODEL_POOL_SIZE="${pool}" \
    -e SPARK_TTS_DTYPE="${dtype}" \
    -e GPU_MEMORY_UTILIZATION="${mem_util}" \
    -e PORT=8002 \
    -v "${models_mount}" \
    -v cathedral_spark_hf_cache:/root/.cache/huggingface \
    --shm-size="${SHM_SIZE}" \
    "${IMAGE_TAG}"

  # This image often 404s on /health; treat any HTTP response as "process is up",
  # then require speech stream (never curl -f /health as readiness).
  wait_http "spark-tts" "http://127.0.0.1:${TTS_HOST_PORT}/health" "${TTS_READY_TRIES}" "any" \
    || {
      err "TTS failed to become healthy. Diagnostics:"
      sudo docker logs --tail=80 "${CONTAINER_NAME}" 2>/dev/null || true
      command -v nvidia-smi >/dev/null && nvidia-smi || true
      warn "If CUDA OOM: stop speaches temporarily, start TTS alone, or set SPARK_TTS_DTYPE=f16"
      warn "  docker stop cathedral-speaches cathedral-etoil-api"
      warn "  GPU_PLAN_MODE=tts SPARK_TTS_DTYPE=f16 ./violet/miner/tts_install.sh"
      exit 1
    }
  wait_speech_ready "${TTS_HOST_PORT}" 90 "${CONTAINER_NAME}" \
    || warn "speech readiness soft-fail before contract smoke"
  if ! smoke_tts; then
    if [[ "${TTS_STRICT}" == "1" ]]; then
      err "TTS contract smoke failed (set TTS_STRICT=0 to continue anyway)"
      exit 1
    fi
    warn "TTS smoke failed (TTS_STRICT=0 — continuing)"
  fi

  log "Done."
  echo "  TTS API         : http://127.0.0.1:${TTS_HOST_PORT}"
  echo "  Miner upstream  : MINER_TTS_UPSTREAM=http://127.0.0.1:${TTS_HOST_PORT}"
  echo "  TTS GPUs        : ${TTS_GPU_DEVICES}"
  echo "  Ready check     : curl speech stream ( /health may 404 on this image )"
  echo "  Stream test     : TTS_URL=http://127.0.0.1:${TTS_HOST_PORT} python ${SCRIPT_DIR}/tts_test_stream.py"
  echo "  Logs            : sudo docker logs -f ${CONTAINER_NAME}"
}

main "$@"
