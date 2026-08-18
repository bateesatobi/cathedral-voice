# Miner Guide — cathedral-voice

Step-by-step guide to install, run, register, and verify a **cathedral-voice** miner on **testnet** or **mainnet**.

| | |
|--|--|
| **Repo home** | [README.md](../README.md) |
| **Script cheat sheet** | [violet/miner/README.md](../violet/miner/README.md) |
| **Mainnet** | `BT_NETWORK=finney` → netuid **39** |
| **Testnet** | `BT_NETWORK=test` → netuid **292** |

Leave `VIOLET_NETUID` blank — the repo picks **39** or **292** from `BT_NETWORK`.

---

## Table of contents

1. [Choose your path](#choose-your-path)
2. [Prerequisites checklist](#prerequisites-checklist)
3. [GPU space detection](#gpu-space-detection-run-this-first)
4. [Step-by-step setup](#step-by-step-setup)
   - [Step 1 — System prerequisites](#step-1--system-prerequisites)
   - [Step 2 — Clone the repository](#step-2--clone-the-repository)
   - [Step 3 — Python environment](#step-3--python-environment)
   - [Step 4 — Bittensor CLI](#step-4--bittensor-cli)
   - [Step 5 — Wallet](#step-5--wallet)
   - [Step 6 — Configure `.env`](#step-6--configure-env)
   - [Step 7 — Install inference (ASR + TTS)](#step-7--install-inference-asr--tts)
   - [Step 8 — Start the miner](#step-8--start-the-miner)
   - [Step 9 — Verify locally](#step-9--verify-locally)
   - [Step 10 — Register on the subnet](#step-10--register-on-the-subnet)
   - [Step 11 — Announce your endpoint](#step-11--announce-your-endpoint)
   - [Step 12 — Verify from the public internet](#step-12--verify-from-the-public-internet)
5. [Reference](#reference)
   - [API contract](#api-contract)
   - [GPU deploy modes](#gpu-deploy-modes)
   - [Access token (production)](#access-token-production)
   - [GPU planning](#gpu-planning)
   - [Capacity rules](#capacity-rules)
   - [Day-2 operations](#day-2-operations)
   - [Troubleshooting](#troubleshooting)

---

## Choose your path

| Path | When | Command |
|------|------|---------|
| **A — Bootstrap (recommended)** | Bare GPU VM; first-time setup | `./violet/miner/bootstrap.sh prod --gpu --no-follow` |
| **B — Manual inference first** | You want STT/TTS up before sidecar | Steps 7 → 8 with `SKIP_INFERENCE_INSTALL=1` on second start |
| **C — ASR only** | TTS CUDA gate fails (nested GPU job) | `MINER_SERVICES=asr ./violet/miner/bootstrap.sh prod --gpu` |
| **D — Dev / NVML-only host** | CUDA blocked; smoke test only | `STT_FORCE_CPU=1` or `MINER_INFERENCE_DEPLOY=cpu` — **not for production mining** |

**Always run first:**

```bash
./violet/miner/detect_gpu_space.sh
```

---

## Prerequisites checklist

| Requirement | Why |
|-------------|-----|
| **NVIDIA GPU** (A100 / H100 / H200 class) | Real ASR/TTS inference |
| **Bare GPU VM or WSL2** | Docker must be the **host** service — nested GPU jobs often block CUDA |
| **Ubuntu 22.04+** | Install scripts target Debian/Ubuntu |
| **Docker + Compose plugin** | Sidecar + inference containers |
| **NVIDIA driver + Container Toolkit** | GPU inside Docker (`nvidia-ctk`) |
| **Public IP or DNS** | Validators dial port **8091** (or your `MINER_PORT`) |
| **Python 3.10+** | Announce, qualification |
| **TAO** in coldkey | Subnet registration fee |
| **`HF_TOKEN`** in `.env` | STT model pull (TTS image has models baked in) |

**Ports**

| Port | Service | Exposed publicly? |
|------|---------|-------------------|
| **8091** | Miner sidecar | **Yes** — set `MINER_PUBLIC_ENDPOINT` to this |
| 9090 | ASR (etoil-api) | No — localhost / sidecar proxy only |
| 8002 | TTS (Spark) | No — localhost / sidecar proxy only |

**Network / NAT:** Forward **WAN:8091 → miner LAN IP:8091**. Validators do **not** use ports 22, 80, or 443. Verify from **outside** your LAN:

```bash
curl -fsS http://YOUR_PUBLIC_IP:8091/health
```

---

## GPU space detection (run this first)

```bash
./violet/miner/detect_gpu_space.sh
```

| Class | Meaning | Action |
|-------|---------|--------|
| `bare_gpu_ok` | Full CUDA on bare VM | Default bootstrap / `MINER_INFERENCE_DEPLOY=compose` |
| `host_socket_gpu_ok` | GPU tenant, Docker `--gpus` works | `MINER_INFERENCE_DEPLOY=run` |
| `shell_only_gpu` | Shell CUDA only | Try `run` mode; may still fail |
| `nvml_only` | `nvidia-smi` OK, `cuCtxCreate` 999 | **Use a bare GPU VM** — no GPU inference here |
| `no_gpu` | No GPU | Fix drivers or change host |

Set in `.env`:

```bash
MINER_INFERENCE_DEPLOY=auto   # default — auto-picks compose | run | cpu
```

See also [violet/miner/inference_run_install.sh](../violet/miner/inference_run_install.sh) for single-container `docker run` mode.

> **TTS note:** `tts_install.sh` runs a CUDA compute gate (`ALLOC_OK` required). Nested GPU spaces fail here by design — use Path C (ASR only) or a bare VM.

---

## Step-by-step setup

### Step 1 — System prerequisites

```bash
# Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"
# log out and back in, or: newgrp docker

# GPU driver — use cloud image or: sudo ubuntu-drivers install && sudo reboot
nvidia-smi

# NVIDIA Container Toolkit
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

docker run --rm --gpus all nvidia/cuda:12.0.0-base-ubuntu22.04 nvidia-smi
```

STT/TTS install scripts can install the toolkit if missing, but doing it here avoids first-boot surprises.

---

### Step 2 — Clone the repository

```bash
git clone https://github.com/bateesatobi/cathedral-voice.git
cd cathedral-voice
```

---

### Step 3 — Python environment

No `requirements.txt` — install from **`pyproject.toml`**:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip wheel
pip install -e ".[chain]"
```

Optional: `pip install -e ".[chain,dev]"` for qualification helpers.

---

### Step 4 — Bittensor CLI

```bash
btcli --version
```

If missing: `pip install "bittensor>=11.0,<12"`

---

### Step 5 — Wallet

```bash
btcli wallet new_coldkey --wallet.name my-coldkey
btcli wallet new_hotkey --wallet.name my-coldkey --wallet.hotkey my-miner
btcli wallet list
```

Fund the coldkey with TAO. **One earning hotkey per coldkey** on this subnet.

---

### Step 6 — Configure `.env`

```bash
cp .env.example .env
```

Minimum keys:

```bash
BT_NETWORK=finney              # or test
BT_WALLET_NAME=my-coldkey
BT_WALLET_HOTKEY=my-miner

MINER_SERVICES=asr,tts         # or asr only if TTS CUDA gate fails
MINER_INFERENCE_DEPLOY=auto

MINER_PORT=8091
MINER_PUBLIC_ENDPOINT=http://YOUR_PUBLIC_IP:8091
MINER_ALLOW_HTTP=1             # if prod uses http:// not https://

ASR_PORT=9090
TTS_PORT=8002
MINER_ASR_UPSTREAM=http://host.docker.internal:9090
MINER_TTS_UPSTREAM=http://host.docker.internal:8002

HF_TOKEN=hf_...                # plain ASCII; required for STT
```

- `MINER_PUBLIC_ENDPOINT` is what gets **announced on chain** — must match your public port-forward.
- If the public port is not 8091, set **both** `MINER_PORT` and `MINER_PUBLIC_ENDPOINT` to that port.
- Never put wallet secrets in `.env` — only wallet **names**.

---

### Step 7 — Install inference (ASR + TTS)

**Option 1 — let bootstrap install (Step 8):** skip this step; bootstrap calls STT/TTS when ports are down.

**Option 2 — manual install:**

```bash
source .venv/bin/activate
export HF_TOKEN="$(grep '^HF_TOKEN=' .env | cut -d= -f2-)"

./violet/miner/detect_gpu_space.sh

# ASR → :9090
./violet/miner/stt_install.sh
# or CPU dev: STT_FORCE_CPU=1 ./violet/miner/stt_install.sh

# TTS → :8002 (CUDA gate must pass on GPU hosts)
./violet/miner/tts_install.sh
```

Success checks:

- STT: `Contract smoke: POST /transcribe → 200`
- TTS: CUDA gate `ALLOC_OK`, then `http://127.0.0.1:8002/` responds

**Run mode** (GPU tenants): `MINER_INFERENCE_DEPLOY=run ./violet/miner/inference_run_install.sh`

---

### Step 8 — Start the miner

Recommended — installs inference if needed, starts sidecar, runs smoke:

```bash
./violet/miner/detect_gpu_space.sh
MINER_SERVICES=asr,tts ./violet/miner/bootstrap.sh prod --gpu --no-follow
```

Inference already running:

```bash
MINER_SERVICES=asr,tts SKIP_INFERENCE_INSTALL=1 ./violet/miner/start.sh prod --gpu --no-follow
```

When prompted, enter your **public IP or DNS** (or set `MINER_PUBLIC_ENDPOINT` in `.env` and use `SKIP_ENDPOINT_PROMPT=1`).

Optional qualification during bootstrap:

```bash
BOOTSTRAP_QUALIFY=1 ./violet/miner/bootstrap.sh prod --gpu --no-follow
```

Optional prod access token (after register):

```bash
FETCH_MINER_TOKEN=1 ./violet/miner/bootstrap.sh prod --gpu --no-follow
```

---

### Step 9 — Verify locally

Run **before** spending TAO on registration:

```bash
./violet/miner/smoke_contract.sh

curl -fsS http://127.0.0.1:9090/health
curl -sS -o /dev/null -w '%{http_code}\n' -H 'Content-Type: application/json' \
  -d '{"text":"hi","speaker_id":"eng_female_1","temperature":0.7}' \
  http://127.0.0.1:8002/v1/audio/speech/stream

curl -fsS "http://127.0.0.1:${MINER_PORT:-8091}/health" | python3 -m json.tool | head -40
curl -fsS "http://127.0.0.1:${MINER_PORT:-8091}/capacity" | python3 -m json.tool | head -40

python scripts/run_qualification.py "http://127.0.0.1:${MINER_PORT:-8091}" --services asr,tts
```

Expect `capacity_units > 0` and qualification **All tests passed**.

---

### Step 10 — Register on the subnet

Only after Step 9 passes.

**Testnet (292):**

```bash
btcli subnet register --netuid 292 \
  --wallet.name my-coldkey --wallet.hotkey my-miner \
  --subtensor.network test
```

**Mainnet (39):**

```bash
btcli subnet register --netuid 39 \
  --wallet.name my-coldkey --wallet.hotkey my-miner \
  --subtensor.network finney
```

```bash
btcli wallet overview --wallet.name my-coldkey --subtensor.network finney
```

---

### Step 11 — Announce your endpoint

Validators use the **on-chain commitment**, not Docker ports alone.

```bash
python scripts/announce_endpoint.py --dry-run
python scripts/announce_endpoint.py
```

One-time override:

```bash
python scripts/announce_endpoint.py --endpoint http://YOUR_PUBLIC_IP:8091
```

Inspect subnet announcements:

```bash
python scripts/announce_endpoint.py --show
```

---

### Step 12 — Verify from the public internet

From another machine or phone (off your Wi‑Fi):

```bash
curl -fsS http://YOUR_PUBLIC_IP:8091/health
curl -fsS http://YOUR_PUBLIC_IP:8091/capacity
```

If this fails, fix firewall / security group / router port-forward before expecting validator probes.

---

## Reference

### API contract

You announce **one** URL (`MINER_PUBLIC_ENDPOINT`). The sidecar proxies internal ASR/TTS.

| Method | Path | Required | Purpose |
|--------|------|----------|---------|
| GET | `/health` | yes | Reachability, services, upstreams |
| GET | `/capacity` | yes | GPU inventory → Capacity score |
| GET | `/violet/info` | yes | Hotkey, uid, services |
| POST | `/transcribe` | if ASR | Batch ASR |
| WS | `/realtime/transcribe` | if ASR | Streaming ASR |
| POST | `/v1/audio/speech/stream` | if TTS | TTS |
| GET | `/v1/voices` | optional | Voice catalogue |

**On-chain requirements for emissions:**

1. Registered UID for your hotkey  
2. Announced `MINER_PUBLIC_ENDPOINT`  
3. Public TCP **8091** (or your `MINER_PORT`) reachable  
4. Sidecar running with wallet loaded (`/health` shows hotkey)

---

### GPU deploy modes

| `MINER_INFERENCE_DEPLOY` | Installs via | Use when |
|--------------------------|--------------|----------|
| `auto` | bootstrap picks | Default |
| `compose` | `stt_install.sh` + compose | Bare GPU VM |
| `run` | `inference_run_install.sh` | GPU tenant, host docker.sock |
| `cpu` | `STT_FORCE_CPU=1` | NVML-only dev smoke |

---

### Access token (production)

Product traffic from the Avoices router may require `MINER_ACCESS_TOKEN`. Validators reach `/health` without it.

```bash
./violet/miner/fetch_access_token.sh test --write-env
./violet/miner/start.sh test --no-follow
```

Backend ops: set `VIOLET_MINER_TOKEN_MASTER_KEY` on the ASRAPI Render service.

---

### GPU planning

| Mode | When | GPU assignment |
|------|------|----------------|
| Solo ASR | `stt_install.sh` alone | All GPUs → STT |
| Solo TTS | `tts_install.sh` alone | All GPUs → TTS |
| Both | default `start.sh` | Split across STT/TTS |

Details: [MINER_GPU_BOOTSTRAP_REPORT.md](./MINER_GPU_BOOTSTRAP_REPORT.md)

---

### Capacity rules

1. Only **A100 40/80, H100 80, H100 NVL, H200** count toward Capacity.  
2. **One earning hotkey per coldkey** on this subnet.  
3. Do not over-advertise concurrency — sidecar enforces `MINER_MAX_CONCURRENT_*` (auto-tuned when `0`).

---

### Day-2 operations

```bash
./violet/miner/start.sh status
./violet/miner/start.sh logs
./violet/miner/start.sh stop          # sidecar only
./violet/miner/start.sh stop-all      # sidecar + ASR + TTS

python scripts/announce_endpoint.py   # after IP change
```

---

### Troubleshooting

| Symptom | Fix |
|---------|-----|
| Validators can't reach you | Public IP, `MINER_PUBLIC_ENDPOINT`, port-forward **8091**, run `announce_endpoint.py` |
| Port 80 shows router admin | Do not use port 80 for miner; forward **8091** |
| `./violet/miner/detect_gpu_space.sh` → `nvml_only` | Bare GPU VM required for H100 GPU inference |
| ASR `/transcribe` 500, `DevicesUnavailable` | Same — nested host; try `STT_FORCE_CPU=1` for dev only |
| TTS install stops at CUDA gate | `MINER_SERVICES=asr` or move to bare VM |
| HF / `UnicodeEncodeError` on STT | Fix `HF_TOKEN` in `.env` (plain ASCII `hf_...`) |
| TTS OOM on 1 GPU | ASR+TTS share GPU 0 — use split hosts or `MINER_SERVICES=asr` |
| No emissions | Registered? Announced? Allowed GPU tier? Qualification passing? |
| `btcli` / announce errors | Check `BT_NETWORK`, wallet path, TAO, netuid **39** vs **292** |

Logs:

```bash
docker compose -f violet/miner/stt-stack/docker-compose.yml logs
docker logs -f spark-tts-frontend
docker logs -f violet-miner-violet-miner-1
```

---

**Next:** [README.md](../README.md) · [Validator guide](./VALIDATOR_GUIDE.md) · [Incentive model](./INCENTIVE.md)
