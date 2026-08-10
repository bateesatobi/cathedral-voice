# Validator Guide — Violet (PHOSAI / Polaris)

Short guide: role, setup, and how to run a validator for the Violet subnet.

Validators measure miners and set on-chain weights so PHOSAI / Polaris emissions go to useful ASR/TTS capacity.

---

## What you do

| Loop | Interval | Purpose |
|------|----------|---------|
| Discovery | ~5 min | Read metagraph / commitments |
| Health | ~60 s | Probe `/health` |
| Evaluation | ~5 min | Qualify + quality/latency probes |
| Work ingest | ~5 min | Pull signed work reports from PHOSAI |
| Weights | ~150 blocks | Score 7-day window → `set_weights` |

No GPU required. You need a private eval corpus with real audio for meaningful WER.

---

## Rules

1. Use a **private** `VALIDATOR_EVALSET_PATH` — the built-in set cannot measure real WER.
2. Keep `VALIDATOR_DB_PATH` on **persistent disk** — losing it resets the 7-day window (unfair + gameable).
3. Start in **dry run** until scores look sane.
4. Work reports come from PHOSAI (`VIOLET_WORK_REPORT_URL`); verify HMAC; reports are deduped by ID.
5. Multi-UID: only the top hotkey per coldkey keeps score; repeats → exclusion / blacklist.

---

## Quick start

```bash
cd violet-subnet
cp .env.example .env

# Offline (no chain) — after a miner is up on :8091
./violet/validator/start.sh test

# On-chain
./violet/validator/start.sh prod
```

Or manually:

```bash
pip install -e ".[chain,dev]"
cp .env.example .env
```

Minimum `.env`:

```bash
VIOLET_NETUID=39           # mainnet; use 292 on testnet (or leave blank)
BT_NETWORK=finney
BT_WALLET_NAME=my-coldkey
BT_WALLET_HOTKEY=my-validator

VIOLET_PHASE=launch
VIOLET_SCORE_WINDOW_DAYS=7
VALIDATOR_DB_PATH=./data/validator.sqlite3
VALIDATOR_EVALSET_PATH=/path/to/private/evalset
VALIDATOR_DASHBOARD_PORT=8092
VALIDATOR_DRY_RUN=true

# PHOSAI signed work feed
VIOLET_WORK_REPORT_URL=https://api.voices.phosaico.com/violet/work-report
VIOLET_WORK_REPORT_TOKEN=
VIOLET_WORK_REPORT_SIGNER=
```

### Run

```bash
# Process
python -m violet.validator.run

# Or Docker
docker compose -f docker/docker-compose.validator.yml up -d
```

Dashboard: `http://<host>:8092`  
Useful: `/api/scores`, per-miner detail, qualification notes.

When ready to publish weights:

```bash
VALIDATOR_DRY_RUN=false
```

---

## Scoring (what you compute)

```text
Final = w_c·C + w_w·W + w_q·Q     (each normalized across miners)
```

| | Launch | Growth | Mature |
|--|--------|--------|--------|
| Capacity C | 75% | 55% | 40% |
| Work W | 12.5% | 27.5% | 45% |
| Quality Q | 12.5% | 17.5% | 15% |

- **C** — mean online capacity units (GPU tier × count while healthy)
- **W** — log(requests) + log(stream minutes) × latency factor (from PHOSAI router only)
- **Q** — probe quality × success × availability

Stored locally in SQLite (`observations`, `capacity_samples`, `work_credits`, `scores`).  
Published on-chain via `set_weights`.

Details: [INCENTIVE.md](INCENTIVE.md).

---

## Example: qualify one miner

```bash
python scripts/run_qualification.py https://miner.example.com
python scripts/run_qualification.py https://miner.example.com --full-availability
```

---

## Checklist

- [ ] Wallet funded / registered as validator
- [ ] Private evalset with real audio
- [ ] Persistent `VALIDATOR_DB_PATH`
- [ ] Dry-run verified on dashboard
- [ ] Work report URL + signing secrets set
- [ ] Dry-run off only when ready to set weights
