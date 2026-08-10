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
cp .env.example .env

# Local (sample ASR/TTS images, no chain): builds, starts, waits healthy, qualifies
./violet/miner/start.sh test

# Production (wallet in .env; add --gpu on NVIDIA hosts)
./violet/miner/start.sh prod
./violet/miner/start.sh prod --gpu

./violet/miner/start.sh status
./violet/miner/start.sh logs
./violet/miner/start.sh stop
```

**GPU production (manual)**

```bash
# Edit .env: wallet, MINER_PUBLIC_ENDPOINT, upstreams
# Netuids: mainnet 39 (BT_NETWORK=finney), testnet 292 (BT_NETWORK=test)
docker compose -f docker/docker-compose.miner.yml \
  -f docker/docker-compose.miner.prod.yml \
  -f docker/docker-compose.miner.gpu.yml up -d

btcli subnet register --netuid 39 \
  --wallet.name <coldkey> --wallet.hotkey <miner>

python scripts/announce_endpoint.py
```

Allowed GPUs only: A100 40/80, H100 80, H100 NVL, H200. Details: [MINER_GUIDE.md](docs/MINER_GUIDE.md).

---

## Validator — get started fast

```bash
cp .env.example .env

# Offline local validator (points at a miner on the host by default)
./violet/validator/start.sh test
./violet/validator/start.sh test --miner http://host.docker.internal:8091

# On-chain validator (wallet + evalset in .env; start dry-run)
./violet/validator/start.sh prod

./violet/validator/start.sh status
./violet/validator/start.sh stop
```

Dashboard: `http://localhost:8092`  
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
