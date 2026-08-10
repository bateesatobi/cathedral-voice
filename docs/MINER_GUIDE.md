# Miner Guide — Violet (PHOSAI / Polaris)

Short guide: hardware rules, setup, registration, and how to run.

Violet supplies **ASR + TTS** to the PHOSAI Avoices platform. Miners earn emissions by staying online on allowed GPUs and serving real routed traffic.

---

## Rules (read first)

1. **Only these GPUs earn Capacity**

| GPU | Units |
|-----|-------|
| A100 40 GB | 1.0× |
| A100 80 GB | 1.6× |
| H100 80 GB | 2.4× |
| H100 NVL | 2.7× |
| H200 | 3.5× |

Anything else (RTX, L40S, …) → listed under `rejected_gpus`, earns **0**.

2. **Public endpoint required** — validators and the PHOSAI router must reach you. No rotating tunnels.
3. **Registration alone pays nothing** — you need qualification + online capacity + (later) work/quality.
4. **One earning hotkey per coldkey** — extra UIDs under the same coldkey are zeroed.
5. **Do not over-advertise concurrency** — accept only what you can serve; failures hurt Work and Quality.
6. **Speak the Avoices wire contract** — miners are drop-in replacements for the old single hosts (`/transcribe`, `/realtime/transcribe`, TTS `/v1/...`).

---

## Quick start (local / mock — no GPU)

```bash
cd violet-subnet
cp .env.example .env
./violet/miner/start.sh test
```

This builds the sample ASR/TTS + miner images, streams logs until healthy, and
runs a qualification smoke test. Use `./violet/miner/start.sh stop` to tear down.

---

## Production miner (GPU host)

### 1. Configure

```bash
cd violet-subnet
cp .env.example .env
```

Set at minimum:

```bash
VIOLET_NETUID=39           # mainnet; use 292 on testnet (or leave blank)
BT_NETWORK=finney          # or test → auto-selects netuid 292
BT_WALLET_NAME=my-coldkey
BT_WALLET_HOTKEY=my-miner

MINER_PUBLIC_ENDPOINT=https://miner.yourdomain.com
MINER_SERVICES=asr,tts
MINER_ASR_UPSTREAM=http://violet-asr:9000
MINER_TTS_UPSTREAM=http://violet-tts:8080
# Leave concurrency at 0 to auto-derive from GPUs
MINER_MAX_CONCURRENT_ASR=0
MINER_MAX_CONCURRENT_TTS=0
```

### 2. Run with Docker

```bash
docker compose -f docker/docker-compose.miner.yml up -d
```

Point `violet-asr` / `violet-tts` images at the **official** PHOSAI inference images for production (compose defaults may use samples).

### 3. Verify before registering

```bash
# Local
curl -s http://localhost:8091/health | jq
curl -s http://localhost:8091/capacity | jq
curl -s http://localhost:8091/violet/info | jq

# Qualification (cheap)
python scripts/run_qualification.py http://localhost:8091

# Full availability (30+ min) against PUBLIC URL
python scripts/run_qualification.py https://miner.yourdomain.com --full-availability
```

Confirm `capacity_units > 0` and no unexpected `rejected_gpus`.

### 4. Register on-chain

```bash
btcli subnet register \
  --netuid 39 \
  --wallet.name my-coldkey \
  --wallet.hotkey my-miner
# testnet: --netuid 292 and BT_NETWORK=test
```

### 5. Announce endpoint

Miner announces on startup. Manual:

```bash
python scripts/announce_endpoint.py --dry-run
python scripts/announce_endpoint.py
python scripts/announce_endpoint.py --show
```

---

## How you get paid (miner view)

| Component | What it rewards |
|-----------|-----------------|
| **Capacity** | Accepted GPUs kept **online & healthy** (7-day mean) |
| **Work** | Real ASR/TTS jobs from the PHOSAI router (not self-reported) |
| **Quality** | Validator probes (accuracy / signal sanity) |

Launch phase is Capacity-heavy. Stay up, pass probes, serve traffic cleanly.

See [INCENTIVE.md](INCENTIVE.md) for weights and the 7-day window.

---

## Useful commands

```bash
# Logs
docker compose -f docker/docker-compose.miner.yml logs -f miner

# GPU inventory
nvidia-smi
curl -s localhost:8091/capacity | jq '.gpus,.capacity_units,.rejected_gpus'

# Public scores (any validator dashboard)
# http://<validator>:8092
```

---

## Checklist

- [ ] Allowed GPU(s) mounted; `capacity_units > 0`
- [ ] ASR/TTS upstreams healthy
- [ ] Public HTTPS/HTTP endpoint stable
- [ ] Qualification PASS (incl. availability on public URL)
- [ ] Wallet registered + endpoint announced
- [ ] Only one hotkey earning per coldkey
