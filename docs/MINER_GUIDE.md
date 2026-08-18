# Miner Guide — cathedral-voice

Complete step-by-step guide: install, register, run, and verify a **cathedral-voice** miner on **testnet** or **mainnet**.

---

## Networks

| | **Mainnet** | **Testnet** |
|--|-------------|-------------|
| Bittensor network | `finney` | `test` |
| Subnet netuid | **39** | **292** |
| `.env` | `BT_NETWORK=finney` | `BT_NETWORK=test` |
| Override (optional) | `VIOLET_NETUID=39` | `VIOLET_NETUID=292` |

Leave `VIOLET_NETUID` blank — the repo picks **39** or **292** from `BT_NETWORK`.

---

## What validators and the router consume

You announce **one** public URL (`MINER_PUBLIC_ENDPOINT`, port **8091**).  
Internal ASR (`:9090`) and TTS (`:8002`) stay private; the sidecar proxies them.

| Method | Path | Required | Used for |
|--------|------|----------|----------|
| GET | `/health` | yes | Reachability, services, upstreams, capacity snapshot |
| GET | `/capacity` | yes | GPU inventory → Capacity (C) score |
| GET | `/violet/info` | yes | Hotkey, uid, services, warnings |
| POST | `/transcribe` | yes (if ASR) | Batch ASR probes + product traffic |
| WS | `/realtime/transcribe` | yes (if ASR) | Streaming ASR probes |
| POST | `/v1/audio/speech/stream` | yes (if TTS) | TTS probes + product traffic |
| GET | `/v1/voices` | optional | Voice catalogue |
| WS | `/v1/audio/speech/stream/ws` | optional | Streaming TTS |

**On-chain (no emissions without these):**

1. Register → chain assigns **UID** to your hotkey  
2. Announce `MINER_PUBLIC_ENDPOINT` (commitment)  
3. Keep TCP **8091** reachable from the public internet  
4. Run with wallet loaded (`start.sh prod`) so `/health` shows your hotkey  

`.env` uses wallet **names** only (`BT_WALLET_NAME` / `BT_WALLET_HOTKEY`) — never coldkey/hotkey secrets.

Capture check:

```bash
./violet/miner/smoke_contract.sh
python scripts/run_qualification.py http://127.0.0.1:8091 --services asr,tts
```

### Miner access token (production auth)

Avoices issues `MINER_ACCESS_TOKEN` after your hotkey proves registration on chain.
Validators still reach `/health` without a token; product traffic from the router
must present it when the miner enforces auth.

**On ASRAPI (Render backend, not the frontend):** set `VIOLET_MINER_TOKEN_MASTER_KEY`
on [phosai-backend-api-1.onrender.com](https://phosai-backend-api-1.onrender.com)
(e.g. `openssl rand -hex 32`).

**On the GPU host (after `btcli subnet register`):**

```bash
pip install -e ".[chain]"
# Default API URL is the Render backend (not https://voices.phosaico.com)
./violet/miner/fetch_access_token.sh test --write-env
./violet/miner/start.sh test --no-follow   # reload sidecar with new .env
```

Or auto-fetch during bootstrap: `FETCH_MINER_TOKEN=1 ./violet/miner/bootstrap.sh test`

---

## What you need before you start

| Requirement | Why |
|-------------|-----|
| **NVIDIA GPU** (A100 / H100 / H200 class) | Real ASR/TTS inference |
| **Bare GPU VM or WSL2** (Docker is the **host**) | TTS CUDA. Nested GPU jobs/`/.dockerenv` usually cannot allocate CUDA |
| **Ubuntu 22.04+** (or similar Linux) | Install scripts target Debian/Ubuntu |
| **Docker + Docker Compose** | Sidecar + inference containers |
| **NVIDIA driver** | GPU must show in `nvidia-smi` |
| **NVIDIA Container Toolkit** | Lets Docker containers use the GPU (`nvidia-ctk`) |
| **Public IP or DNS** | Validators dial your miner on port **8091** |
| **Python 3.10+** | Announce script, qualification, optional process-mode miner |
| **TAO** in coldkey wallet | Subnet registration fee |

**Ports on the host**

| Port | Service |
|------|---------|
| **8091** | Miner sidecar (public — announce this) |
| 9090 | ASR / etoil-api (local; proxied by sidecar) |
| 8002 | TTS / Spark (local; proxied by sidecar) |

### Network / NAT (Keenetic and similar routers)

Validators dial **only** `MINER_PUBLIC_ENDPOINT` (TCP **8091**). Opening SSH (`22`) or HTTP (`80`/`443`) is **not** enough.

- If a browser to `http://YOUR_PUBLIC_IP/` shows a **KeeneticOS** (or other) admin panel, WAN **80** is the router — do **not** put the miner on port 80.
- Forward **WAN:8091 → VM_LAN_IP:8091** on the router (and allow **8091** in any cloud security group).
- Checking from the miner host often fails due to **NAT hairpin**; verify from a second network:

```bash
curl -fsS http://YOUR_PUBLIC_IP:8091/health
nc -vz YOUR_PUBLIC_IP 8091
```

Install scripts print this as the remaining manual step after local smoke passes.

---

## GPU space detection (run this first)

Before spending time on STT/TTS install, classify the host:

```bash
./violet/miner/detect_gpu_space.sh
```

| Class | Meaning | Deploy mode |
|-------|---------|-------------|
| `bare_gpu_ok` | Bare GPU VM — full CUDA in Docker | `compose` (default) |
| `host_socket_gpu_ok` | GPU tenant with working `docker run --gpus` | `run` |
| `shell_only_gpu` | CUDA in shell only — experimental | `run` |
| `nvml_only` | `nvidia-smi` works, `cuCtxCreate` fails (999) | `cpu` fallback only |
| `no_gpu` | No usable GPU | fix drivers / pick another offer |

Set in `.env` (or export):

```bash
MINER_INFERENCE_DEPLOY=auto   # default — picks compose | run | cpu from detect
# MINER_INFERENCE_DEPLOY=compose   # docker-compose stacks (bare GPU VM)
# MINER_INFERENCE_DEPLOY=run       # single-container docker run (GPU tenants)
# MINER_INFERENCE_DEPLOY=cpu       # STT_FORCE_CPU dev smoke (not for mining)
```

**Run mode** (`inference_run_install.sh`): one `docker run` per service on a shared bridge network — no compose file. Use when the host Docker socket can allocate GPU but compose bridges misbehave.

**NVML-only hosts** (nested GPU jobs like Datura/Lium spaces): neither compose nor run mode will use the H100 for inference. Move to a **bare GPU VM** (Chutes-style: K8s on bare metal, not DinD).

---

## TTS host — read this before installing Spark

`nvidia-smi` listing an H100 is **not** enough. Spark-TTS needs Docker to **allocate CUDA** (`cuCtxCreate` / `cuMemAlloc`). Nested GPU products (Datura/Lium-style jobs, `/.dockerenv`, Docker-in-Docker) often show the GPU and then fail with `CUDA-capable device(s) is/are busy or unavailable`. Installing TTS there hangs or crashes.

| Host | TTS | ASR |
|------|-----|-----|
| Ubuntu VM / WSL2, Docker is the host | yes | yes |
| Nested GPU job / space inside a container | **no** (`tts_install` aborts) | often yes |
| Attested / confidential GPU (TEE) | avoid | avoid |

`tts_install.sh` runs a **CUDA compute gate** before pulling/starting Spark. If the gate fails, install **stops** (it will not leave a crashing TTS container).

```bash
# Must print ALLOC_OK on a TTS-capable host
./violet/miner/tts_install.sh
```

On a nested host, serve ASR only:

```bash
MINER_SERVICES=asr ./violet/miner/start.sh prod --gpu --no-follow
```

Do not use `TTS_FORCE_CPU=1` unless you accept multi-second TTS latency. `TTS_SKIP_CUDA_PROBE=1` is unsafe.

---

## Miner setup — numbered steps

### 1. Install system prerequisites

On the GPU host:

```bash
# Docker (if missing)
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"
# log out and back in, or: newgrp docker

# NVIDIA driver — use your cloud image or:
# sudo ubuntu-drivers install && sudo reboot

# Verify GPU driver
nvidia-smi

# NVIDIA Container Toolkit (required for GPU inside Docker)
# Skip if nvidia-ctk is already installed.
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# Verify toolkit
nvidia-ctk --version
docker run --rm --gpus all nvidia/cuda:12.0.0-base-ubuntu22.04 nvidia-smi
```

The STT/TTS install scripts can also install the toolkit automatically if it is missing, but installing it here avoids surprises on first boot.

---

### 2. Clone the repository

```bash
git clone https://github.com/bateesatobi/cathedral-voice.git
cd cathedral-voice
```

---

### 3. Create a Python environment and install dependencies

There is no `requirements.txt`. Dependencies live in **`pyproject.toml`**.

```bash
python3 -m venv .venv
source .venv/bin/activate

# Core + Bittensor (needed for register / announce)
pip install -U pip wheel
pip install -e ".[chain]"
```

Optional extras:

```bash
pip install -e ".[chain,dev]"    # + tests / qualification helpers
```

---

### 4. Install Bittensor CLI (`btcli`)

Usually installed with step 3 (`chain` extra). Verify:

```bash
btcli --version
```

If missing:

```bash
pip install "bittensor>=11.0,<12"
```

---

### 5. Create a Bittensor wallet

Skip if you already have a coldkey + miner hotkey.

```bash
# Coldkey (once per operator)
btcli wallet new_coldkey --wallet.name my-coldkey

# Miner hotkey (one earning hotkey per coldkey on this subnet)
btcli wallet new_hotkey --wallet.name my-coldkey --wallet.hotkey my-miner
```

Fund the coldkey with TAO (mainnet or testnet faucet for test).

List wallets:

```bash
btcli wallet list
```

---

### 6. Configure `.env`

Create the file:

```bash
cp .env.example .env
```

Set at least these keys before you run anything:

```bash
BT_NETWORK=test                 # or finney
BT_WALLET_NAME=my-coldkey
BT_WALLET_HOTKEY=my-miner

MINER_SERVICES=asr,tts          # use `asr` only if CUDA compute gate fails
MINER_PORT=8091
ASR_PORT=9090
TTS_PORT=8002

# The sidecar proxies to these local services
MINER_ASR_UPSTREAM=http://host.docker.internal:9090
MINER_TTS_UPSTREAM=http://host.docker.internal:8002

# Set this to the REAL public URL that validators should dial.
# Must match the public port you expose on the host/router.
MINER_PUBLIC_ENDPOINT=http://YOUR_PUBLIC_IP:8091

# Required for private / gated Hugging Face pulls used by STT.
# TTS image has models baked in (no HF_TOKEN for tts_install).
# Use plain ASCII only: hf_... (no smart quotes, no em-dashes, no trailing comments)
HF_TOKEN=hf_...
```

Mainnet uses:

```bash
BT_NETWORK=finney
```

Notes:

- `MINER_PORT` is the miner sidecar listen port, and `start.sh` now uses it as-is when it is set.
- `MINER_PUBLIC_ENDPOINT` is what gets announced on chain.
- If you change the public port, change **both** `MINER_PORT` and `MINER_PUBLIC_ENDPOINT`.
- If `MINER_PUBLIC_ENDPOINT` already includes a port, `start.sh` syncs `MINER_PORT` to that port.
- Keep `ASR_PORT` and `TTS_PORT` private unless you intentionally publish them.

---

### 7. Set the miner public port

Before you start the miner, choose the public port validators should dial.

Example using port `40202`:

```bash
sed -i '/^MINER_PORT=/d;/^MINER_PUBLIC_ENDPOINT=/d' .env
cat >> .env <<'EOF'
MINER_PORT=40202
MINER_PUBLIC_ENDPOINT=http://YOUR_PUBLIC_IP:40202
EOF
```

Verify:

```bash
grep -E '^(MINER_PORT|MINER_PUBLIC_ENDPOINT)=' .env
```

If you use `prod` with plain HTTP instead of HTTPS, also set:

```bash
echo 'MINER_ALLOW_HTTP=1' >> .env
```

Then open that same port publicly in your firewall / router port-forward.

---

### 8. Install GPU inference (ASR + TTS)

Run from the repo root:

```bash
cd ~/cathedral-voice
source .venv/bin/activate

# Make HF_TOKEN visible to the install scripts as well as .env
export HF_TOKEN="$(grep '^HF_TOKEN=' .env | cut -d= -f2-)"

# ASR: speaches + etoil-api -> :9090
./violet/miner/stt_install.sh

# TTS: spark-tts-frontend -> :8002 (models baked in; no HF_TOKEN)
# Aborts if Docker cannot allocate CUDA (nested GPU jobs).
./violet/miner/tts_install.sh
```

What success looks like:

- STT: `Contract smoke: POST /transcribe -> 200`
- TTS: CUDA gate prints `ALLOC_OK`, then `spark-tts-frontend` answers on `http://127.0.0.1:8002/`

If you want the sidecar to install them automatically, skip this step and use `bootstrap.sh` in step 9.

Known issues we hit in practice:

- If `stt_install.sh` shows Hugging Face / `UnicodeEncodeError` failures, your `HF_TOKEN` is missing or malformed. Re-check `.env` and re-export `HF_TOKEN`. TTS does not need `HF_TOKEN`.
- If `tts_install.sh` stops at **CUDA compute gate**, this host cannot run TTS in Docker. Use `MINER_SERVICES=asr` here, and run TTS on a bare GPU VM or WSL.
- CPU TTS (slow, not recommended):

```bash
TTS_FORCE_CPU=1 ./violet/miner/tts_install.sh
```

Optional TTS stream smoke test:

```bash
TTS_URL=http://127.0.0.1:8002 python violet/miner/tts_test_stream.py
```

---

### 9. Start the miner (sidecar + inference)

Recommended: start everything with the checklist wrapper.

```bash
MINER_SERVICES=asr,tts ./violet/miner/bootstrap.sh prod --gpu --no-follow
```

If STT/TTS are already running and healthy, start only the sidecar:

```bash
MINER_SERVICES=asr,tts SKIP_INFERENCE_INSTALL=1 ./violet/miner/start.sh prod --gpu --no-follow
```

When prompted, enter your public IP or DNS.

- If you enter only a host, `start.sh` appends `MINER_PORT`.
- If your public port is not `8091`, set `MINER_PORT` and `MINER_PUBLIC_ENDPOINT` first in `.env`.

The sidecar proxies:

- ASR → etoil-api (`MINER_ASR_UPSTREAM`, default `:9090`)
- TTS → Spark (`MINER_TTS_UPSTREAM`, default `:8002`)

Open `MINER_PORT` publicly in your cloud security group / firewall so validators can reach you.  
On home/office routers (Keenetic, etc.), add an explicit port-forward — see [Network / NAT](#network--nat-keenetic-and-similar-routers) above.

`bootstrap.sh` ends with an **admission checklist** (local smoke, wallet, public-port hint, announce dry-run). Optional qualification:

```bash
BOOTSTRAP_QUALIFY=1 ./violet/miner/bootstrap.sh prod --gpu --no-follow
```

---

### 10. Verify locally (before spending TAO on chain)

```bash
# Full validator-facing contract on the public miner port (+ local upstreams)
./violet/miner/smoke_contract.sh

# Spark often 404s on /health — speech stream is the TTS readiness check
curl -fsS http://127.0.0.1:9090/health
curl -sS -o /dev/null -w '%{http_code}\n' -H 'Content-Type: application/json' \
  -d '{"text":"hi","speaker_id":"eng_female_1","temperature":0.7}' \
  http://127.0.0.1:8002/v1/audio/speech/stream

curl -fsS "http://127.0.0.1:${MINER_PORT:-8091}/health" | python3 -m json.tool | head -40
curl -fsS "http://127.0.0.1:${MINER_PORT:-8091}/capacity" | python3 -m json.tool | head -40
curl -fsS "http://127.0.0.1:${MINER_PORT:-8091}/violet/info" | python3 -m json.tool | head -40

# Qualification suite (ASR + TTS contract checks)
python scripts/run_qualification.py "http://127.0.0.1:${MINER_PORT:-8091}" --services asr,tts
```

Expect `capacity_units > 0` (e.g. H100 → 2.4) and qualification **All tests passed**.  
Fix any failures before registering.

---

### 11. Register on the subnet

Do this only after the miner passes local smoke.

**Testnet (netuid 292):**

```bash
btcli subnet register --netuid 292 \
  --wallet.name my-coldkey \
  --wallet.hotkey my-miner \
  --subtensor.network test
```

**Mainnet (netuid 39):**

```bash
btcli subnet register --netuid 39 \
  --wallet.name my-coldkey \
  --wallet.hotkey my-miner \
  --subtensor.network finney
```

Confirm registration:

```bash
btcli wallet overview --wallet.name my-coldkey --subtensor.network test     # or finney
```

---

### 12. Announce your public endpoint on chain

Validators discover you from the **commitment**, not from Docker ports alone.

Ensure `.env` has the exact public URL and port that the internet can reach:

```bash
MINER_PORT=8091
MINER_PUBLIC_ENDPOINT=http://YOUR_PUBLIC_IP:8091
```

If you use another public port, announce that exact port instead:

```bash
MINER_PORT=9001
MINER_PUBLIC_ENDPOINT=http://YOUR_PUBLIC_IP:9001
```

Dry-run (no transaction):

```bash
python scripts/announce_endpoint.py --dry-run
```

Publish:

```bash
python scripts/announce_endpoint.py
```

You can override the endpoint one time without editing `.env`:

```bash
python scripts/announce_endpoint.py --endpoint http://YOUR_PUBLIC_IP:9001
```

Inspect all announcements on the subnet:

```bash
python scripts/announce_endpoint.py --show
```

The miner process also re-announces when your endpoint or GPU inventory changes.

---

### 13. Verify from outside the host

From another machine (or phone off Wi‑Fi):

```bash
curl -fsS http://YOUR_PUBLIC_IP:8091/health
curl -fsS http://YOUR_PUBLIC_IP:8091/capacity
```

If this fails, fix firewall / security group before expecting validator probes.

If your public port is not `8091`, replace `8091` above with your actual `MINER_PORT`.

---

### 14. Day-2 operations

```bash
./violet/miner/start.sh status          # sidecar + ASR/TTS health
./violet/miner/start.sh logs            # follow sidecar logs
./violet/miner/start.sh stop            # stop sidecar only
./violet/miner/start.sh stop-all        # sidecar + ASR + TTS

# Re-announce after IP change
python scripts/announce_endpoint.py
```

---

## GPU planning (reference)

| Mode | When | GPUs |
|------|------|------|
| Solo ASR | `stt_install.sh` alone | All → STT |
| Solo TTS | `tts_install.sh` alone | Spark TTS (`spark-tts-frontend`) |
| Both | default `start.sh` | Split; no idle card |

Details: [MINER_GPU_BOOTSTRAP_REPORT.md](./MINER_GPU_BOOTSTRAP_REPORT.md)

---

## Rules (earn Capacity)

1. Only **A100 40/80, H100 80, H100 NVL, H200** count toward Capacity.
2. **One earning hotkey per coldkey** on this subnet.
3. Do not over-advertise concurrency — sidecar enforces `MINER_MAX_CONCURRENT_*` (auto-tuned from GPU count when `0`).

---

## Troubleshooting

| Symptom | Check |
|---------|--------|
| `/health` OK locally, validators can't reach you | Public IP, port **8091**, `MINER_PUBLIC_ENDPOINT`, announce; Keenetic must forward **8091** (not just 22/80/443) |
| Port 80 shows router admin UI | Do not bind miner to 80; forward WAN:8091 → VM:8091 |
| ASR fails | `docker compose -f violet/miner/stt-stack/docker-compose.yml logs` |
| ASR `/health` OK, `/transcribe` 500, speaches log `DevicesUnavailable` | Nested Docker — no CUDA alloc. Run `./violet/miner/detect_gpu_space.sh` |
| `cuCtxCreate 999` / NVML-only H100 | Not fixable in software — bare GPU VM required for GPU inference |
| STT install stopped at CUDA compute gate | Same as TTS gate — run STT on bare GPU VM/WSL |
| `mount ... ./audio ... no such file or directory` | Old compose used a bind mount; pull latest `stt_install.sh` (uses `stt-audio` volume) |
| etoil `EXTERNAL_API_URL` / crash-loop on start | Set `EXTERNAL_API_URL=http://speaches:8000` in compose (fixed in latest `stt_install.sh`) |
| TTS fails / OOM on 1 GPU | Both stacks share GPU 0 — see GPU report; consider ASR-only or TTS-only host |
| TTS install stopped at CUDA compute gate | Nested Docker / no CUDA alloc. Use `MINER_SERVICES=asr` or a bare GPU VM/WSL |
| TTS crash-loop / DevicesUnavailable | Same as CUDA gate — do not force-install on that host |
| `btcli` / announce errors | `BT_NETWORK`, wallet path, TAO balance, netuid **39** vs **292** |
| No emissions | Registered? Announced? GPUs in allowed tier? Passing qualification? |
