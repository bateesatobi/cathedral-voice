# Violet Subnet (PHOSAI / Polaris)

Decentralized **ASR + TTS** for the [PHOSAI Avoices](https://voices.phosaico.com) platform on Bittensor. Built for **PHOSAI** and **Polaris**.

| Role | Job |
|------|-----|
| **Miner** | GPU sidecar serving Avoices-compatible ASR/TTS APIs |
| **Validator** | Probe, score (7-day C/W/Q), set weights |
| **Router** | Inside PHOSAI ASRAPI — picks miners, failover, work receipts |

Miners speak the **same endpoints** Avoices already uses (`/transcribe`, `/realtime/transcribe`, `/v1/audio/speech/stream`, …), so the product can switch to the subnet without rewriting apps.

---

## Docs

| Doc | Contents |
|-----|----------|
| [docs/MINER_GUIDE.md](docs/MINER_GUIDE.md) | Hardware rules, register, run miner |
| [docs/VALIDATOR_GUIDE.md](docs/VALIDATOR_GUIDE.md) | Run validator, dry-run, weights |
| [docs/INCENTIVE.md](docs/INCENTIVE.md) | Rewards: Capacity / Work / Quality |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How pieces fit |
| [docs/SECURITY.md](docs/SECURITY.md) | Threat model |
| [integration/asrapi/INTEGRATION.md](integration/asrapi/INTEGRATION.md) | Wire router into ASRAPI |

---

## Miner — get started fast

```bash
cd violet-subnet
pip install -e ".[dev]"
cp .env.example .env

# Mock ASR/TTS (laptop OK)
python -m uvicorn --app-dir docker/mock-asr app:app --port 9000 &
python -m uvicorn --app-dir docker/mock-tts app:app --port 8080 &

MINER_PUBLIC_ENDPOINT=http://localhost:8091 \
MINER_ASR_UPSTREAM=http://localhost:9000 \
MINER_TTS_UPSTREAM=http://localhost:8080 \
python -m violet.miner.run --no-chain &

python scripts/run_qualification.py http://localhost:8091
```

**GPU production**

```bash
# Edit .env: VIOLET_NETUID, wallet, MINER_PUBLIC_ENDPOINT, upstreams
docker compose -f docker/docker-compose.miner.yml up -d

btcli subnet register --netuid <netuid> \
  --wallet.name <coldkey> --wallet.hotkey <miner>

python scripts/announce_endpoint.py
```

Allowed GPUs only: A100 40/80, H100 80, H100 NVL, H200. Details: [MINER_GUIDE.md](docs/MINER_GUIDE.md).

---

## Validator — get started fast

```bash
pip install -e ".[chain,dev]"
cp .env.example .env
# Set wallet, VALIDATOR_EVALSET_PATH (private real audio), VALIDATOR_DRY_RUN=true

python -m violet.validator.run
# Dashboard: http://localhost:8092
```

Or: `docker compose -f docker/docker-compose.validator.yml up -d`  
Guide: [VALIDATOR_GUIDE.md](docs/VALIDATOR_GUIDE.md).

---

## Rewards (one line)

```text
Score = Capacity (online GPUs) + Work (PHOSAI traffic) + Quality (probes)
```

7-day rolling window → validators `set_weights` on chain. Full write-up: [INCENTIVE.md](docs/INCENTIVE.md).

```bash
python scripts/simulate_scoring.py --sybil
```

---

## Customer traffic (PHOSAI)

Apps call **Avoices / ASRAPI** (e.g. realtime `WS /api/realtime-transcribe`).  
With `VIOLET_ROUTER_ENABLED=true`, ASRAPI’s smart router load-balances across miners — customers never pick miner URLs.

---

## Layout

```
violet/           miner, validator, router, chain, protocol
docker/           compose + sample ASR/TTS
scripts/          qualify, announce, simulate scoring
docs/             guides
integration/      ASRAPI drop-in
```

Python 3.10+. `bittensor>=11` for chain (optional for local mock miner/router tests).
