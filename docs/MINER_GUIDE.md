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

---

### 9. Verify locally (before spending TAO on chain)

```bash
curl -fsS http://127.0.0.1:9090/health      # ASR (etoil)
curl -fsS http://127.0.0.1:8002/health      # TTS (Spark)
curl -fsS http://127.0.0.1:8091/health      # miner sidecar
curl -fsS http://127.0.0.1:8091/capacity    # GPU claim

# Qualification suite (ASR + TTS contract checks)
python scripts/run_qualification.py http://127.0.0.1:8091
```

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
| `/health` OK locally, validators can't reach you | Public IP, port **8091**, `MINER_PUBLIC_ENDPOINT`, announce |
| ASR fails | `docker compose -f violet/miner/stt-stack/docker-compose.yml logs` |
| `mount ... ./audio ... no such file or directory` | Old compose used a bind mount; pull latest `stt_install.sh` (uses `stt-audio` volume) |
| etoil `EXTERNAL_API_URL` / crash-loop on start | Set `EXTERNAL_API_URL=http://speaches:8000` in compose (fixed in latest `stt_install.sh`) |
| TTS fails / OOM on 1 GPU | Both stacks share GPU 0 — see GPU report; consider ASR-only or TTS-only host |
| `btcli` / announce errors | `BT_NETWORK`, wallet path, TAO balance, netuid **39** vs **292** |
| No emissions | Registered? Announced? GPUs in allowed tier? Passing qualification? |
