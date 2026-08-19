# Miner scripts — cathedral-voice

Entry point for GPU miners on Bittensor **SN39** (mainnet **39** / testnet **292**).

**Full runbook:** [docs/MINER_GUIDE.md](../../docs/MINER_GUIDE.md)  
**Repo overview:** [README.md](../../README.md)

---

## Start here (3 commands)

Run from the **repo root** after clone:

```bash
# 1. Classify the GPU host (do this before any install)
./violet/miner/detect_gpu_space.sh

# 2. Configure env + install inference + start sidecar
cp .env.example .env   # edit wallet, HF_TOKEN, public IP
./violet/miner/bootstrap.sh prod --gpu --no-follow

# 3. Verify before registering on chain
./violet/miner/smoke_contract.sh
python scripts/run_qualification.py http://127.0.0.1:${MINER_PORT:-8091}
```

Then register, announce, and verify from the public internet — see [MINER_GUIDE § Steps 10–12](../../docs/MINER_GUIDE.md#step-10-register-on-the-subnet).

---

## Scripts in this folder

| Script | Purpose |
|--------|---------|
| [detect_gpu_space.sh](./detect_gpu_space.sh) | Bare GPU vs NVML-only nested host — run **first**; prints Capacity inventory |
| [bootstrap.sh](./bootstrap.sh) | Recommended bring-up: GPU check → STT/TTS → sidecar → smoke |
| [start.sh](./start.sh) | Start/stop/status/logs; `stop-all` tears down inference too |
| [stt_install.sh](./stt_install.sh) | ASR stack (speaches + etoil-api → `:9090`) |
| [tts_install.sh](./tts_install.sh) | TTS stack (Spark → `:8002`); CUDA gate on GPU hosts |
| [inference_run_install.sh](./inference_run_install.sh) | Single-container `docker run` mode (GPU tenants) |
| [smoke_contract.sh](./smoke_contract.sh) | Validator-facing HTTP/WS contract on miner port |
| [fetch_access_token.sh](./fetch_access_token.sh) | Optional prod auth token from Avoices backend |

---

## Host types

| `detect_gpu_space.sh` result | What to do |
|------------------------------|------------|
| `bare_gpu_ok` | `./violet/miner/bootstrap.sh prod --gpu` (default compose) |
| `host_socket_gpu_ok` | `MINER_INFERENCE_DEPLOY=run ./violet/miner/bootstrap.sh prod --gpu` |
| `nvml_only` | **No GPU inference** on this offer — use a bare GPU VM, or `MINER_SERVICES=asr` + `STT_FORCE_CPU=1` for dev only |
| `no_gpu` | Fix drivers / pick another machine |

Details: [MINER_GUIDE § GPU space detection](../../docs/MINER_GUIDE.md#gpu-space-detection-run-this-first).

---

## Ports (typical)

| Port | Service | Public? |
|------|---------|---------|
| **8091** | Miner sidecar | **Yes** — announce this |
| 9090 | ASR (etoil-api) | No — localhost only |
| 8002 | TTS (Spark) | No — localhost only |

---

## Common ops

```bash
./violet/miner/start.sh status
./violet/miner/start.sh logs
./violet/miner/start.sh stop-all
python scripts/announce_endpoint.py          # after IP change
```
