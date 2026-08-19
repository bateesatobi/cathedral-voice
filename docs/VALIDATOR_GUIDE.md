# Validator Guide — cathedral-voice

Complete step-by-step guide: install, register, run, and verify a **cathedral-voice** validator on **testnet** or **mainnet**.

Validators probe miners, score **Capacity / Work / Quality**, and (optionally) post voice scores to Cathedral or run the thin SN39 relay.

**No GPU required** on the validator machine.

---

## Networks

| | **Mainnet** | **Testnet** |
|--|-------------|-------------|
| Bittensor network | `finney` | `test` |
| Subnet netuid | **39** | **292** |
| `.env` | `BT_NETWORK=finney` | `BT_NETWORK=test` |
| Override (optional) | `VIOLET_NETUID=39` | `VIOLET_NETUID=292` |

---

## What you need before you start

| Requirement | Why |
|-------------|-----|
| **Linux or macOS** server | Validator runs in Docker or Python process |
| **Docker + Docker Compose** | Recommended path (`start.sh`) |
| **Python 3.10+** | Process mode, scripts, dry-run |
| **Persistent disk** | `VALIDATOR_DB_PATH` holds the 7-day score window |
| **SALT evalset** | Real audio for WER — build with `scripts/build_salt_evalset.py` (see [EVALSET.md](./EVALSET.md)) |
| **TAO** in coldkey wallet | Validator registration + weight transactions |

Optional for **Work** scoring: PHOSAI work-report URL + HMAC secrets from ops.

---

## Validator setup — numbered steps

### 1. Install system prerequisites

```bash
# Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"
# newgrp docker

docker compose version
```

---

### 2. Clone the repository

```bash
git clone https://github.com/bateesatobi/cathedral-voice.git
cd cathedral-voice
```

---

### 3. Create a Python environment and install dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate

pip install -U pip wheel
pip install -e ".[chain,cathedral,dev,eval]"
```

| Extra | Purpose |
|-------|---------|
| `chain` | Bittensor SDK + metagraph / weights |
| `cathedral` | SN39 thin relay crypto |
| `dev` | pytest, httpx, qualification tooling |
| `eval` | Build Sunbird/SALT Quality corpus (`datasets`, etc.) |

There is no separate `requirements.txt` — **`pyproject.toml`** is the source of truth.

---

### 4. Install Bittensor CLI (`btcli`)

Verify after step 3:

```bash
btcli --version
```

If missing:

```bash
pip install "bittensor>=11.0,<12"
```

---

### 5. Create a Bittensor wallet

```bash
btcli wallet new_coldkey --wallet.name my-val-coldkey
btcli wallet new_hotkey --wallet.name my-val-coldkey --wallet.hotkey my-validator
```

Fund the coldkey. Validators typically **stake** TAO on the subnet after registration (see subnet docs / `btcli stake`).

---

### 6. Prepare the standard SALT evalset (required for real WER)

Production Quality scoring needs **real audio**. The built-in corpus is synthetic
and cannot measure WER.

**Standard:** build from [Sunbird/salt](https://huggingface.co/datasets/Sunbird/salt)
multispeaker **test** audio (East African langs used by Avoices).

```bash
pip install -e ".[eval]"
python scripts/build_salt_evalset.py --out ./data/evalset/salt --per-lang 20 --seed 39
```

```bash
VALIDATOR_EVALSET_PATH=./data/evalset/salt
```

Details, holdout / anti-overfit rules, and manifest format: **[EVALSET.md](./EVALSET.md)**.

---

### 7. Configure `.env`

```bash
cp .env.example .env
mkdir -p ./data
```

**Testnet example:**

```bash
BT_NETWORK=test
BT_WALLET_NAME=my-val-coldkey
BT_WALLET_HOTKEY=my-validator

VIOLET_PHASE=launch
VIOLET_SCORE_WINDOW_DAYS=7
VALIDATOR_DB_PATH=./data/validator.sqlite3
VALIDATOR_EVALSET_PATH=./data/evalset/salt
VALIDATOR_DASHBOARD_PORT=8092
VALIDATOR_DRY_RUN=true          # start here — no on-chain weights yet

# Optional: organic Work from PHOSAI router
VIOLET_WORK_REPORT_URL=https://phosai-backend-api-latest.onrender.com/api/admin/violet/work-report
VIOLET_WORK_REPORT_TOKEN=
VIOLET_WORK_REPORT_SIGNER=
```

**Mainnet example:** same, with `BT_NETWORK=finney` (netuid **39** auto-selected).

---

### 8. Test locally against a miner (no chain)

Use this before registering or setting weights. You need a running miner URL (your own or a test miner).

**Docker (recommended):**

```bash
./violet/validator/start.sh test --miner http://127.0.0.1:8091 --no-follow
```

**Process mode:**

```bash
source .venv/bin/activate
export VIOLET_STATIC_MINERS=http://127.0.0.1:8091
export VALIDATOR_DRY_RUN=true
python -m violet.validator.run --dry-run
```

Open dashboard:

```text
http://127.0.0.1:8092
```

Check `/api/overview`, `/api/scores`, per-miner probe results.

Qualify a miner manually:

```bash
python scripts/run_qualification.py http://127.0.0.1:8091
python scripts/run_qualification.py http://MINER_PUBLIC_IP:8091 --services asr,tts
```

---

### 9. Register as a validator on the subnet

**Testnet (netuid 292):**

```bash
btcli subnet register --netuid 292 \
  --wallet.name my-val-coldkey \
  --wallet.hotkey my-validator \
  --subtensor.network test
```

**Mainnet (netuid 39):**

```bash
btcli subnet register --netuid 39 \
  --wallet.name my-val-coldkey \
  --wallet.hotkey my-validator \
  --subtensor.network finney
```

Stake if required by subnet policy (consult current Cathedral / SN39 validator requirements).

---

### 10. Run the validator on-chain (dry-run first)

Keep `VALIDATOR_DRY_RUN=true` until dashboard scores look sane.

**Docker:**

```bash
./violet/validator/start.sh prod --no-follow
```

**Process mode:**

```bash
source .venv/bin/activate
python -m violet.validator.run --dry-run
```

Dashboard: `http://127.0.0.1:8092`

---

### 11. Go live (publish weights)

When dry-run scores are stable:

```bash
# In .env
VALIDATOR_DRY_RUN=false
```

Restart:

```bash
./violet/validator/start.sh stop
./violet/validator/start.sh prod --no-follow
```

Or process mode without `--dry-run`:

```bash
python -m violet.validator.run
```

---

### 12. Optional — Cathedral voice scores + thin SN39 relay

For one process that **scores voice miners** and participates in **Cathedral SN39** weights, see [UNIFIED_SN39_VALIDATOR.md](./UNIFIED_SN39_VALIDATOR.md).

Summary flags in `.env`:

```bash
CATHEDRAL_EXTERNAL_SCORES_ENABLED=true
CATHEDRAL_PUBLISHER_URL=https://api.cathedral.computer
CATHEDRAL_EXTERNAL_SCORES_TOKEN=<from Cathedral ops>

CATHEDRAL_THIN_ENABLED=true
CATHEDRAL_THIN_BROADCAST=false    # verify first
CATHEDRAL_THIN_DRY_RUN=true
```

Go live only after dry verification:

```bash
CATHEDRAL_THIN_BROADCAST=true
CATHEDRAL_THIN_DRY_RUN=false
```

---

### 13. Day-2 operations

```bash
./violet/validator/start.sh status
./violet/validator/start.sh logs
./violet/validator/start.sh stop
```

**Back up** `VALIDATOR_DB_PATH` — deleting it resets the 7-day window.

---

## What the validator runs

| Loop | Interval | Purpose |
|------|----------|---------|
| Discovery | ~5 min | Metagraph + miner endpoint commitments |
| Health | ~60 s | `GET /health` |
| Evaluation | ~5 min | Qualification + quality probes |
| Work | ~5 min | Signed PHOSAI work report |
| Weights | ~150 blocks | 7-day window → scores → `set_weights` |
| Thin SN39 | ~1500 s | Optional Cathedral signed feed → `set_weights` |

---

## Scoring (reference)

```text
Final = w_c·C + w_w·W + w_q·Q
```

| Phase | Capacity | Work | Quality |
|-------|----------|------|---------|
| Launch | 75% | 12.5% | 12.5% |
| Growth | 55% | 27.5% | 17.5% |
| Mature | 40% | 45% | 15% |

Details: [INCENTIVE.md](./INCENTIVE.md)

---

## Rules

1. Use the **SALT standard evalset** (`VALIDATOR_EVALSET_PATH` → real audio). Built-in tones cannot measure WER — see [EVALSET.md](./EVALSET.md).
2. Keep the **holdout** offline; rotate seed/holdout periodically (public SALT can be overfitted).
3. Keep `VALIDATOR_DB_PATH` on **persistent disk**.
4. Always **dry-run** before `VALIDATOR_DRY_RUN=false`.
5. Work reports are **HMAC-signed** and **deduped** — miners cannot self-report Work.
6. One earning hotkey per coldkey; sybil UIDs are zeroed.

---

## Troubleshooting

| Symptom | Check |
|---------|--------|
| No miners in dashboard | `BT_NETWORK` / netuid, metagraph sync, miner announcements |
| All probes fail | Miner public `:8091`, firewall, `MINER_PUBLIC_ENDPOINT` on chain |
| Scores flat / reset | `VALIDATOR_DB_PATH` persistence |
| Work always zero | `VIOLET_WORK_REPORT_URL` + token/signer |
| Weights not on chain | `VALIDATOR_DRY_RUN=false`, wallet funded, registration |
