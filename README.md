# cathedral-voice

**cathedral-voice** is the speech lane for Cathedral on Bittensor **SN39**: miners serve **ASR** and **TTS** over HTTP; validators score **Capacity / Work / Quality** and publish `violet_audio` to Cathedral’s SN39 weight feed.

| Network | `BT_NETWORK` | Netuid |
|---------|--------------|--------|
| Mainnet | `finney` | **39** |
| Testnet | `test` | **292** |

```text
Miners (ASR/TTS) ──► cathedral-voice validator ──► Cathedral publisher (violet_audio)
                                              └──► thin SN39 relay (optional set_weights)
```

---

## Who should read what

| Role | Start here | Full runbook |
|------|------------|--------------|
| **Miner operator** | [violet/miner/README.md](violet/miner/README.md) | [docs/MINER_GUIDE.md](docs/MINER_GUIDE.md) |
| **Validator operator** | § [Validator quick start](#validator-quick-start) below | [docs/VALIDATOR_GUIDE.md](docs/VALIDATOR_GUIDE.md) |
| **Integrator / router** | [integration/asrapi/INTEGRATION.md](integration/asrapi/INTEGRATION.md) | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |

---

## Miner quick start

**Requirements:** NVIDIA GPU (A100 / H100 / H200 class), Ubuntu 22.04+, Docker, NVIDIA Container Toolkit, public TCP port for the sidecar, TAO for registration.

> **Important:** Run on a **bare GPU VM or WSL2** where Docker is the host OS service. Nested GPU rental jobs often show `nvidia-smi` but block CUDA compute — see [GPU detection](#gpu-host-detection) below.

### Path A — recommended (one script)

```bash
git clone https://github.com/bateesatobi/cathedral-voice.git
cd cathedral-voice

python3 -m venv .venv && source .venv/bin/activate
pip install -U pip wheel && pip install -e ".[chain]"

cp .env.example .env
# Edit: BT_NETWORK, BT_WALLET_*, HF_TOKEN, MINER_PUBLIC_ENDPOINT

./violet/miner/detect_gpu_space.sh
./violet/miner/bootstrap.sh prod --gpu --no-follow
./violet/miner/smoke_contract.sh
python scripts/run_qualification.py http://127.0.0.1:${MINER_PORT:-8091}
```

### Path B — after local verify: chain

```bash
# Testnet
btcli subnet register --netuid 292 --wallet.name … --wallet.hotkey … --subtensor.network test

# Mainnet
btcli subnet register --netuid 39 --wallet.name … --wallet.hotkey … --subtensor.network finney

python scripts/announce_endpoint.py --dry-run
python scripts/announce_endpoint.py

# From another network (not the miner host):
curl -fsS http://YOUR_PUBLIC_IP:${MINER_PORT:-8091}/health
```

Full numbered steps, NAT/firewall, troubleshooting: **[docs/MINER_GUIDE.md](docs/MINER_GUIDE.md)**.

Miner script reference: **[violet/miner/README.md](violet/miner/README.md)**.

---

## GPU host detection

Before installing ASR/TTS, classify the machine:

```bash
./violet/miner/detect_gpu_space.sh
```

| Result | Meaning |
|--------|---------|
| `bare_gpu_ok` | Production-ready — use default bootstrap |
| `host_socket_gpu_ok` | GPU tenant with working Docker GPU — set `MINER_INFERENCE_DEPLOY=run` |
| `nvml_only` | H100 visible but CUDA blocked — **move to a bare GPU VM** |
| `no_gpu` | Fix drivers or change host |

Set `MINER_INFERENCE_DEPLOY=auto` in `.env` (default) to pick compose / run / cpu automatically.

---

## Miner architecture (at a glance)

Validators dial **one** public URL (`MINER_PUBLIC_ENDPOINT`, default port **8091**). The sidecar proxies to local inference:

| Port | Service | Image / stack |
|------|---------|---------------|
| **8091** | Miner sidecar (public) | `docker compose` — [docker/](docker/) |
| 9090 | ASR | speaches + etoil-api — `stt_install.sh` |
| 8002 | TTS | Spark — `tts_install.sh` |

`MINER_SERVICES=asr,tts` (both) · `asr` only on hosts that cannot run TTS CUDA.

---

## Validator quick start

```bash
git clone https://github.com/bateesatobi/cathedral-voice.git && cd cathedral-voice
python3 -m venv .venv && source .venv/bin/activate
pip install -U pip wheel && pip install -e ".[chain,cathedral,dev]"

cp .env.example .env
# Build SALT evalset — see docs/EVALSET.md

./violet/validator/start.sh test --miner http://MINER_IP:8091
```

Full steps 1–13: **[docs/VALIDATOR_GUIDE.md](docs/VALIDATOR_GUIDE.md)** · corpus: **[docs/EVALSET.md](docs/EVALSET.md)**.

Optional unified SN39 (voice scores + Cathedral thin relay): **[docs/UNIFIED_SN39_VALIDATOR.md](docs/UNIFIED_SN39_VALIDATOR.md)**.

---

## Install dependencies

There is no `requirements.txt`. Use `pyproject.toml`:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -U pip wheel

pip install -e ".[chain]"                 # miner (register, announce)
pip install -e ".[chain,cathedral,dev]" # validator
```

---

## Documentation index

| Doc | Contents |
|-----|----------|
| [docs/MINER_GUIDE.md](docs/MINER_GUIDE.md) | **Miner runbook** — prerequisites, 12 steps, GPU modes, ops, troubleshooting |
| [violet/miner/README.md](violet/miner/README.md) | Miner scripts cheat sheet |
| [docs/VALIDATOR_GUIDE.md](docs/VALIDATOR_GUIDE.md) | Validator runbook |
| [docs/EVALSET.md](docs/EVALSET.md) | SALT quality corpus (WER) |
| [docs/MINER_GPU_BOOTSTRAP_REPORT.md](docs/MINER_GPU_BOOTSTRAP_REPORT.md) | Multi-GPU planning |
| [docs/INCENTIVE.md](docs/INCENTIVE.md) | Capacity / Work / Quality scoring |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System overview |
| [docs/UNIFIED_SN39_VALIDATOR.md](docs/UNIFIED_SN39_VALIDATOR.md) | Voice + Cathedral SN39 in one process |
| [docs/CATHEDRAL_EXTERNAL_SCORES.md](docs/CATHEDRAL_EXTERNAL_SCORES.md) | Publisher score contract |
| [integration/asrapi/INTEGRATION.md](integration/asrapi/INTEGRATION.md) | Router / ASRAPI wiring |
| [docker/README.md](docker/README.md) | Sidecar compose files |

---

## Rewards (voice scoring)

```text
Score = Capacity (online GPUs) + Work (organic traffic) + Quality (probes)
```

7-day rolling window → Cathedral `violet_audio`. Details: [docs/INCENTIVE.md](docs/INCENTIVE.md).

```bash
python scripts/simulate_scoring.py --sybil
```

---

## Repository layout

```text
violet/miner/     install scripts, sidecar, detect_gpu_space, bootstrap
violet/validator/ validator process
violet/router/    optional load balancer for product traffic
docker/           miner sidecar compose (ASR/TTS run on host via install scripts)
scripts/          announce, qualify, cathedral score poster
docs/             runbooks and architecture
```

Python **3.10+**.
