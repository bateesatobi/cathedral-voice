# Miner GPU utilization & gap-fix report

**Date:** 2026-08-11  
**Scope:** cathedral-voice miner install/start path  
(`violet/miner/{gpu_env,stt_install,tts_install,start,bootstrap,smoke_contract}.sh`)

---

## Verdict

Hosts with **1 or N GPUs** now assign **every card** to real inference work. Solo STT or solo TTS takes **all** GPUs. Combined ASR+TTS partitions the machine so **no index is left idle**. Multi-GPU ASR load-balances across replicas. Startup gaps (toolkit, long model waits, contract smoke, firewall, stop-all, concurrency, announce hints) are closed in the scripts.

---

## How GPU assignment works

Central planner: `violet/miner/gpu_env.sh` → `plan_gpu_devices <mode>`.

| Mode | When | Assignment |
|------|------|------------|
| `stt` | `stt_install.sh` alone, or `MINER_SERVICES=asr` | **All** GPUs → STT |
| `tts` | `tts_install.sh` alone, or `MINER_SERVICES=tts` | **All** GPUs → TTS |
| `both` | Default `start.sh` / `bootstrap.sh` with `asr,tts` | Partition; **every** GPU in STT ∪ TTS |

**Both-mode partition:**

- **N = 1:** STT and TTS share GPU `0` (TTS VRAM util lowered to `0.35`).
- **N ≥ 2:** STT gets `ceil(N/2)`, TTS the rest (e.g. 4 GPUs → STT `0,1` / TTS `2,3`; 3 → STT `0,1` / TTS `2`).
- `assert_no_idle_gpus` fails the plan if any index `0..N-1` is missing.

Overrides still work via `STT_GPU_DEVICES_OVERRIDE` / `TTS_GPU_DEVICES_OVERRIDE`.
`start.sh` plans once then sets `GPU_PLAN_LOCKED=1` so install children keep that split.

---

## How all GPUs stay busy (not just “visible”)

### ASR (`stt_install.sh`)

- **1 STT GPU:** one `speaches` container.
- **2+ STT GPUs (default `STT_SPEACHES_PER_GPU=1`):** one `speaches` **per GPU** + **nginx `least_conn`** (`speaches-lb`).
- **etoil-api** (`:9090`) points at `http://speaches-lb:8000` (or single `speaches`), so traffic is spread — previously etoil only hit `speaches-0` and left other cards idle.
- Miner upstream: `MINER_ASR_UPSTREAM` → etoil-api (not raw speaches).

### TTS (`tts_install.sh`)

- `MODEL_POOL_SIZE` defaults to **number of assigned TTS GPUs**.
- All assigned device IDs are passed via `--gpus` / `NVIDIA_VISIBLE_DEVICES`.
- Solo / dedicated cards use `GPU_MEMORY_UTILIZATION=0.85`; single-GPU shared with STT uses `0.35`.

### Sidecar admission

- If `MINER_MAX_CONCURRENT_ASR/TTS` are `0`, `suggest_concurrency` sets **~2 in-flight jobs per assigned GPU** and writes them into `.env`.

---

## Gaps fixed (what / how)

| Gap | Fix |
|-----|-----|
| Solo install used half/half → idle GPUs | Modes `stt`/`tts` assign **all** GPUs |
| Multi-speaches idle behind etoil | nginx LB + `SPEACHES_BASE_URL=speaches-lb` |
| STT missing NVIDIA toolkit | `install_nvidia_toolkit_if_needed` on STT path |
| Health-only checks | Install smokes + `smoke_contract.sh` (`/health`, `/capacity`, TTS voices, transcription attempt) |
| Short waits / cold model pull | STT ~20 min, TTS ~30 min wait loops with log progress |
| Firewall / public port | Best-effort ufw/firewalld open of miner port; cloud SG reminder |
| Incomplete stop | `start.sh stop-all` tears down sidecar + STT compose + Spark container |
| No concurrency auto-tune | Derived from GPU plan when unset |
| Disk | `check_disk_gb` warning before pulls |
| Idempotent STT | `docker compose up -d --remove-orphans` |
| Wallet / announce | `bootstrap.sh` checks wallet path; prints `announce_endpoint` steps |
| 1-GPU OOM risk | Shared-GPU TTS VRAM headroom |

**Still operator-owned (scripts guide, do not silently pay):**

- `btcli subnet register`
- Paying `python scripts/announce_endpoint.py`
- Cloud security-group rules when no host firewall
- Rotating the hardcoded HF token (present for seamless installs; treat as sensitive)

---

## How to run

```bash
# Full checklist (recommended)
./violet/miner/bootstrap.sh prod --no-follow

# Or
./violet/miner/start.sh prod --no-follow

# ASR-only host → all GPUs on STT
MINER_SERVICES=asr ./violet/miner/start.sh prod --no-follow

# Tear everything down
./violet/miner/start.sh stop-all
```

Standalone:

```bash
./violet/miner/stt_install.sh   # all GPUs → ASR
./violet/miner/tts_install.sh   # all GPUs → TTS
```

---

## Files touched

| File | Role |
|------|------|
| `violet/miner/gpu_env.sh` | Detect, plan, assert no idle, compose device YAML, concurrency, toolkit, disk |
| `violet/miner/stt_install.sh` | Toolkit, per-GPU speaches + LB, long wait, smoke |
| `violet/miner/tts_install.sh` | Full-GPU pool, VRAM policy, long wait, smoke |
| `violet/miner/start.sh` | Service-aware plan, concurrency, firewall, smoke, `stop-all` |
| `violet/miner/bootstrap.sh` | Fail-closed wrapper + wallet/announce hints |
| `violet/miner/smoke_contract.sh` | Post-boot contract checks |

---

## Example layouts

```
1 GPU, asr+tts     → STT=0 TTS=0 (shared, TTS mem 0.35)
2 GPUs, asr+tts    → STT=0 TTS=1
4 GPUs, asr+tts    → STT=0,1  TTS=2,3  (+ nginx LB on STT)
4 GPUs, asr only   → STT=0,1,2,3  (4 speaches + LB)
8 GPUs, tts only   → TTS=0..7  pool=8
```
