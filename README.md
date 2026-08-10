# cathedral-voice

**cathedral-voice** is the speech lane for Cathedral on Bittensor **SN39**: miners serve real-time **ASR** and **TTS**, and validators measure that work so it can enter Cathedral’s SN39 weight vector.

### What it does

1. **Miners** expose a public HTTP sidecar (`/health`, `/transcribe`, streaming ASR/TTS, …) backed by GPU inference.
2. **Validators** discover miners, probe health/quality, score **Capacity / Work / Quality** over a rolling window, and publish `violet_audio` score reports to the Cathedral publisher.
3. **Optionally**, the same validator process runs the **Cathedral thin relay**: fetch the signed SN39 weight feed, verify it, and `set_weights` on netuid **39** — so voice scores and Cathedral’s other lanes share one on-chain vector without a second conflicting writer.

```text
Miners (ASR/TTS) ──► cathedral-voice validator ──► publisher (violet_audio)
                                              └──► thin relay ──► SN39 set_weights
```

| Component | Responsibility |
|-----------|----------------|
| **Miner** | Serve ASR/TTS APIs; announce a public endpoint; report GPUs |
| **Validator** | Probe, score, post `violet_audio`; optional SN39 thin `set_weights` |
| **Publisher** (Cathedral) | Blend voice scores into the signed SN39 feed |
| **Router** (optional) | Product backends can load-balance traffic across voice miners |

---

## Docs

| Doc | Contents |
|-----|----------|
| [docs/MINER_GUIDE.md](docs/MINER_GUIDE.md) | Hardware rules, register, run miner |
| [docs/VALIDATOR_GUIDE.md](docs/VALIDATOR_GUIDE.md) | Validator details, dry-run, evalset |
| [docs/UNIFIED_SN39_VALIDATOR.md](docs/UNIFIED_SN39_VALIDATOR.md) | Voice + Cathedral thin SN39 in one process |
| [docs/CATHEDRAL_EXTERNAL_SCORES.md](docs/CATHEDRAL_EXTERNAL_SCORES.md) | `POST /v1/external-scores/violet` contract |
| [docs/INCENTIVE.md](docs/INCENTIVE.md) | Capacity / Work / Quality |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How pieces fit |
| [integration/asrapi/INTEGRATION.md](integration/asrapi/INTEGRATION.md) | Wire router into ASRAPI |

---

## Miner — quick start

```bash
cd cathedral-voice
cp .env.example .env

./violet/miner/start.sh test                 # local samples, no chain
./violet/miner/start.sh prod --gpu           # production + GPUs

./violet/miner/start.sh status|logs|stop
```

See [MINER_GUIDE.md](docs/MINER_GUIDE.md). Netuids: mainnet **39** (`BT_NETWORK=finney`), testnet **292** (`BT_NETWORK=test`).

---

## Validator — how to run

The validator does **voice work** (probe + score + optional Cathedral score POST) and can also do **Cathedral thin SN39 work** (fetch signed feed → verify → `set_weights` on netuid 39).

### 1. Install

```bash
cd cathedral-voice
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[chain,cathedral,dev]"
cp .env.example .env
```

### 2. Minimum `.env`

```bash
BT_NETWORK=finney                 # → netuid 39 (use test → 292)
BT_WALLET_NAME=my-coldkey
BT_WALLET_HOTKEY=my-validator

VIOLET_PHASE=launch
VIOLET_SCORE_WINDOW_DAYS=7
VALIDATOR_DB_PATH=./data/validator.sqlite3   # persistent disk
VALIDATOR_EVALSET_PATH=/path/to/private/evalset
VALIDATOR_DASHBOARD_PORT=8092
VALIDATOR_DRY_RUN=true                       # start here

# Organic work from PHOSAI / ASRAPI (optional but needed for Work score)
VIOLET_WORK_REPORT_URL=https://api.voices.phosaico.com/violet/work-report
VIOLET_WORK_REPORT_TOKEN=
```

### 3. Local / offline (no chain)

Points at a miner HTTP endpoint (default host `:8091`):

```bash
./violet/validator/start.sh test
./violet/validator/start.sh test --miner http://127.0.0.1:8091

# or process:
python -m violet.validator.run --dry-run
```

### 4. On-chain production (dry-run first)

```bash
# Edit .env: wallet, evalset, VALIDATOR_DRY_RUN=true
./violet/validator/start.sh prod

# Process form:
python -m violet.validator.run --dry-run
# when scores look sane:
# VALIDATOR_DRY_RUN=false
python -m violet.validator.run
```

Dashboard: [http://127.0.0.1:8092](http://127.0.0.1:8092) (`/api/overview`, `/api/scores`)

```bash
./violet/validator/start.sh status
./violet/validator/start.sh logs
./violet/validator/start.sh stop
```

### 5. Unified SN39 mode (voice + Cathedral thin)

Use this when one process should **score voice miners** and **write the blended SN39 weight vector** (Cathedral compute/SAT miners + voice miners).

```bash
# Voice → publisher (violet_audio)
CATHEDRAL_EXTERNAL_SCORES_ENABLED=true
CATHEDRAL_PUBLISHER_URL=https://api.cathedral.computer
CATHEDRAL_EXTERNAL_SCORES_TOKEN=<from Cathedral ops>
CATHEDRAL_EXTERNAL_SCORES_NETUID=39

# Thin → SN39 (sole chain writer when broadcasting)
CATHEDRAL_THIN_ENABLED=true
CATHEDRAL_THIN_BROADCAST=false    # dry verify/map first
CATHEDRAL_THIN_DRY_RUN=true
CATHEDRAL_THIN_INTERVAL_S=1500

# Then go live:
# CATHEDRAL_THIN_BROADCAST=true
# CATHEDRAL_THIN_DRY_RUN=false
# (forces CATHEDRAL_SKIP_LOCAL_WEIGHTS — do not dual-write weights)
```

Manual score POST (ops):

```bash
python scripts/post_cathedral_scores.py --dry-run \
  --from-dashboard http://127.0.0.1:8092/api/scores

cathedral-voice-scores --print-only --score 5Fhotkey...=0.9
```

Full trust model: [UNIFIED_SN39_VALIDATOR.md](docs/UNIFIED_SN39_VALIDATOR.md).

### Validator loops (what runs)

| Loop | Cadence | Purpose |
|------|---------|---------|
| Discovery | ~5 min | Metagraph + miner endpoints |
| Health | ~60 s | `GET /health` |
| Evaluation | ~5 min | Qualification + quality probes |
| Work | ~5 min | PHOSAI signed work report |
| Weights | ~150 blocks | Score window → local weights and/or Cathedral POST |
| Thin SN39 | ~1500 s | Fetch signed feed → verify → `set_weights` (optional) |

---

## Rewards (voice scoring)

```text
Score = Capacity (online GPUs) + Work (organic traffic) + Quality (probes)
```

7-day rolling window. Those scores feed Cathedral as `violet_audio`. Details: [INCENTIVE.md](docs/INCENTIVE.md).

```bash
python scripts/simulate_scoring.py --sybil
```

---

## Layout

```
violet/           miner, validator, router, chain, cathedral (scores + thin relay)
docker/           compose + sample ASR/TTS images
scripts/          qualify, announce, cathedral score poster
docs/             guides
```

Python 3.10+. Install with `pip install -e ".[chain,cathedral]"`.
