# Miner Guide — cathedral-voice

Hardware rules, real ASR/TTS install, registration, and how to run.

---

## Rules

1. **Only these GPUs earn Capacity:** A100 40/80, H100 80, H100 NVL, H200.
2. **Public endpoint required** — validators must reach `MINER_PUBLIC_ENDPOINT` (miner port, default `8091`).
3. **ASR upstream is etoil-api** (`:9090`). **TTS upstream is Spark-TTS** (`:8002`).
4. Install scripts use **all GPUs on the host** (or a split when both STT and TTS run together).

---

## GPU planning

`violet/miner/gpu_env.sh` → `plan_gpu_devices <stt|tts|both>`:

| Mode | When | Assignment |
|------|------|------------|
| `stt` | `stt_install` alone / `MINER_SERVICES=asr` | **All** GPUs → ASR |
| `tts` | `tts_install` alone / `MINER_SERVICES=tts` | **All** GPUs → TTS |
| `both` | default `start.sh` (`asr,tts`) | Partition; **no idle** card |

| Host GPUs (`both`) | STT | TTS |
|--------------------|-----|-----|
| 1 | GPU 0 (shared, TTS VRAM limited) | GPU 0 |
| 2+ | first `ceil(N/2)` | remaining |

Pin manually:

```bash
STT_GPU_DEVICES_OVERRIDE=0,1 TTS_GPU_DEVICES_OVERRIDE=2,3 \
  GPU_PLAN_MODE=both ./violet/miner/stt_install.sh
```

- **STT multi-GPU:** one speaches per GPU + nginx `least_conn` LB; etoil (`:9090`) fronts the LB.
- **TTS:** `MODEL_POOL_SIZE` = assigned GPU count.
- Full write-up: [`MINER_GPU_BOOTSTRAP_REPORT.md`](./MINER_GPU_BOOTSTRAP_REPORT.md)

---

## Install inference (real services)

```bash
cd cathedral-voice

# ASR: speaches + etoil-api on :9090 (uses ALL GPUs when run alone)
./violet/miner/stt_install.sh

# TTS: Spark-TTS on :8002 (uses ALL GPUs when run alone)
./violet/miner/tts_install.sh

# Optional stream smoke test
TTS_URL=http://127.0.0.1:8002 python violet/miner/tts_test_stream.py
```

No translation-secret prompts. HF token is set by the install scripts (override with `HF_TOKEN=…`).

---

## Run the miner sidecar

```bash
cp .env.example .env
# set wallet + MINER_PUBLIC_ENDPOINT (or let start.sh prompt)

# Recommended: fail-closed checklist (firewall + smoke + announce hints)
./violet/miner/bootstrap.sh prod --no-follow

# Or
./violet/miner/start.sh prod --gpu
# start.sh installs STT/TTS if :9090 / :8002 are not healthy yet

./violet/miner/start.sh status|logs|stop|stop-all
```

If ASR/TTS are already up:

```bash
SKIP_INFERENCE_INSTALL=1 ./violet/miner/start.sh prod --gpu
```

`.env` defaults:

```bash
MINER_ASR_UPSTREAM=http://host.docker.internal:9090
MINER_TTS_UPSTREAM=http://host.docker.internal:8002
ASR_PORT=9090
TTS_PORT=8002
MINER_PORT=8091
```

---

## Register + announce

```bash
btcli subnet register --netuid 39 \
  --wallet.name <coldkey> --wallet.hotkey <miner>

python scripts/announce_endpoint.py
```

---

## Verify

```bash
curl -fsS http://127.0.0.1:9090/health    # etoil
curl -fsS http://127.0.0.1:8002/health    # spark
curl -fsS http://127.0.0.1:8091/health    # miner sidecar
curl -fsS http://127.0.0.1:8091/capacity
```
