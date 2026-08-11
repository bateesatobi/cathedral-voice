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

---

## What you need before you start

| Requirement | Why |
|-------------|-----|
| **NVIDIA GPU** (A100 / H100 / H200 class) | Real ASR/TTS inference |
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

```bash
cp .env.example .env
```

**Testnet example:**

```bash
BT_NETWORK=test
BT_WALLET_NAME=my-coldkey
BT_WALLET_HOTKEY=my-miner

MINER_SERVICES=asr,tts
MINER_PORT=8091
ASR_PORT=9090
TTS_PORT=8002
MINER_ASR_UPSTREAM=http://host.docker.internal:9090
MINER_TTS_UPSTREAM=http://host.docker.internal:8002
# Set after step 9, or let start.sh prompt:
# MINER_PUBLIC_ENDPOINT=http://YOUR_PUBLIC_IP:8091

# Hugging Face token for model pulls (stt_install / tts_install).
# Prefer your own token; rotate if a shared default was ever committed.
# HF_TOKEN=hf_...
```

**Mainnet example:** same as above, but:

```bash
BT_NETWORK=finney
# netuid 39 is chosen automatically
```

Edit with your wallet names and (later) your public endpoint.

---

### 7. Install GPU inference (ASR + TTS)

These scripts use **every GPU** on the host (split when both ASR and TTS run together).

```bash
# ASR: speaches + etoil-api → :9090
./violet/miner/stt_install.sh

# TTS: Spark-TTS → :8002
./violet/miner/tts_install.sh
```

Or let the next step install them automatically if ports are down.

Optional TTS stream smoke test:

```bash
TTS_URL=http://127.0.0.1:8002 python violet/miner/tts_test_stream.py
```

---

### 8. Start the miner (sidecar + inference)

**Recommended** (firewall hint + contract smoke + announce hints):

```bash
./violet/miner/bootstrap.sh prod --no-follow
```

**Or** directly:

```bash
./violet/miner/start.sh prod --gpu --no-follow
```

When prompted, enter your **public IP or DNS**. Port **8091** is appended automatically.

The sidecar proxies:

- ASR → etoil-api (`MINER_ASR_UPSTREAM`, default `:9090`)
- TTS → Spark (`MINER_TTS_UPSTREAM`, default `:8002`)

**Open TCP 8091** in your cloud security group / firewall so validators can reach you.  
On home/office routers (Keenetic, etc.), add an explicit port-forward — see [Network / NAT](#network--nat-keenetic-and-similar-routers) above.

`bootstrap.sh` ends with an **admission checklist** (local smoke, wallet, public-port hint, announce dry-run). Optional qualification:

```bash
BOOTSTRAP_QUALIFY=1 ./violet/miner/bootstrap.sh prod --gpu --no-follow
```

---

### 9. Verify locally (before spending TAO on chain)

```bash
# Full validator-facing contract on :8091 (+ local upstreams)
./violet/miner/smoke_contract.sh

# Spark often 404s on /health — speech stream is the TTS readiness check
curl -fsS http://127.0.0.1:9090/health
curl -sS -o /dev/null -w '%{http_code}\n' -H 'Content-Type: application/json' \
  -d '{"text":"hi","speaker_id":"eng_female_1","temperature":0.7}' \
  http://127.0.0.1:8002/v1/audio/speech/stream

curl -fsS http://127.0.0.1:8091/health | python3 -m json.tool | head -40
curl -fsS http://127.0.0.1:8091/capacity | python3 -m json.tool | head -40
curl -fsS http://127.0.0.1:8091/violet/info | python3 -m json.tool | head -40

# Qualification suite (ASR + TTS contract checks)
python scripts/run_qualification.py http://127.0.0.1:8091 --services asr,tts
```

Expect `capacity_units > 0` (e.g. H100 → 2.4) and qualification **All tests passed**.  
Fix any failures before registering.

---

### 10. Register on the subnet

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
btcli wallet overview --wallet.name my-coldkey --subtensor.network finney   # or test
```

---

### 11. Announce your public endpoint on chain

Validators discover you from the **commitment**, not from Docker ports alone.

Ensure `.env` has the real public URL:

```bash
MINER_PUBLIC_ENDPOINT=http://YOUR_PUBLIC_IP:8091
```

Dry-run (no transaction):

```bash
python scripts/announce_endpoint.py --dry-run
```

Publish:

```bash
python scripts/announce_endpoint.py
```

Inspect all announcements on the subnet:

```bash
python scripts/announce_endpoint.py --show
```

The miner process also re-announces when your endpoint or GPU inventory changes.

---

### 12. Verify from outside the host

From another machine (or phone off Wi‑Fi):

```bash
curl -fsS http://YOUR_PUBLIC_IP:8091/health
curl -fsS http://YOUR_PUBLIC_IP:8091/capacity
```

If this fails, fix firewall / security group before expecting validator probes.

---

### 13. Day-2 operations

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
| Solo TTS | `tts_install.sh` alone | All → TTS |
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
| `mount ... ./audio ... no such file or directory` | Old compose used a bind mount; pull latest `stt_install.sh` (uses `stt-audio` volume) |
| etoil `EXTERNAL_API_URL` / crash-loop on start | Set `EXTERNAL_API_URL=http://speaches:8000` in compose (fixed in latest `stt_install.sh`) |
| TTS fails / OOM on 1 GPU | Both stacks share GPU 0 — see GPU report; consider ASR-only or TTS-only host |
| TTS crash-loop / corrupt tokenizer | Set `HF_TOKEN` and re-run `tts_install.sh` (seeds via huggingface_hub) |
| `btcli` / announce errors | `BT_NETWORK`, wallet path, TAO balance, netuid **39** vs **292** |
| No emissions | Registered? Announced? GPUs in allowed tier? Passing qualification? |
