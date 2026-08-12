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

**Networks:** mainnet **netuid 39** (`BT_NETWORK=finney`) · testnet **netuid 292** (`BT_NETWORK=test`)

---

## Install & run (numbered guides)

Full step-by-step runbooks (clone → Bittensor → deps → register → run → verify):

| Role | Guide | Steps |
|------|-------|-------|
| **Miner** | [docs/MINER_GUIDE.md](docs/MINER_GUIDE.md) | **1–13** — GPU host, Docker, `pip install -e ".[chain]"`, wallet, `.env`, inference, sidecar, qualify, register, announce |
| **Validator** | [docs/VALIDATOR_GUIDE.md](docs/VALIDATOR_GUIDE.md) · [docs/EVALSET.md](docs/EVALSET.md) | **1–13** — install, **SALT evalset**, dry-run, register, go live |

**Dependencies:** no `requirements.txt` — install from `pyproject.toml`:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -U pip wheel
pip install -e ".[chain]"              # miner (announce / register)
pip install -e ".[chain,cathedral,dev]" # validator
```

**Quick copy-paste (mainnet miner, after clone):**

```bash
cp .env.example .env   # set BT_NETWORK=finney, wallet names
pip install -e ".[chain]"
./violet/miner/bootstrap.sh prod --no-follow
python scripts/run_qualification.py http://127.0.0.1:8091
btcli subnet register --netuid 39 --wallet.name … --wallet.hotkey … --subtensor.network finney
python scripts/announce_endpoint.py
```

**Quick copy-paste (testnet):** same, but `BT_NETWORK=test`, `--netuid 292`, `--subtensor.network test`.

---

## Docs

| Doc | Contents |
|-----|----------|
| [docs/MINER_GUIDE.md](docs/MINER_GUIDE.md) | **Full miner runbook** (steps 1–13, testnet + mainnet) |
| [docs/VALIDATOR_GUIDE.md](docs/VALIDATOR_GUIDE.md) | **Full validator runbook** (steps 1–13, testnet + mainnet) |
| [docs/EVALSET.md](docs/EVALSET.md) | **SALT standard Quality corpus** (WER, holdout, anti-overfit) |
| [docs/UNIFIED_SN39_VALIDATOR.md](docs/UNIFIED_SN39_VALIDATOR.md) | Voice + Cathedral thin SN39 in one process |
| [docs/CATHEDRAL_EXTERNAL_SCORES.md](docs/CATHEDRAL_EXTERNAL_SCORES.md) | `POST /v1/external-scores/violet` contract |
| [docs/INCENTIVE.md](docs/INCENTIVE.md) | Capacity / Work / Quality |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How pieces fit |
| [integration/asrapi/INTEGRATION.md](integration/asrapi/INTEGRATION.md) | Wire router into ASRAPI |

---

## Miner — at a glance

See **[MINER_GUIDE.md](docs/MINER_GUIDE.md)** for all 13 steps. Summary:

1. Prerequisites (GPU, Docker, NVIDIA driver, **NVIDIA Container Toolkit**)
2. Clone repo
3. `pip install -e ".[chain]"`
4. `btcli` / Bittensor
5. Create wallet
6. Configure `.env` (`finney` → 39, `test` → 292)
7. `./violet/miner/stt_install.sh` + `tts_install.sh`
8. `./violet/miner/bootstrap.sh prod --no-follow`
9. Local verify + `run_qualification.py`
10. `btcli subnet register`
11. `python scripts/announce_endpoint.py`
12. Public `curl` check
13. Ops (`status`, `logs`, `stop-all`)

ASR upstream = **etoil-api** `:9090`. TTS upstream = **Spark** `:8002`.

---

## Validator — at a glance

See **[VALIDATOR_GUIDE.md](docs/VALIDATOR_GUIDE.md)** for all 13 steps. Summary:

1. Docker (+ Python 3.10+)
2. Clone repo
3. `pip install -e ".[chain,cathedral,dev]"`
4. `btcli`
5. Create wallet
6. SALT evalset (`scripts/build_salt_evalset.py` → `VALIDATOR_EVALSET_PATH`)
7. Configure `.env` (`VALIDATOR_DRY_RUN=true` first)
8. `./violet/validator/start.sh test --miner http://…:8091`
9. `btcli subnet register`
10. `./violet/validator/start.sh prod`
11. Set `VALIDATOR_DRY_RUN=false` when ready
12. Optional Cathedral SN39 mode
13. Ops + backup `VALIDATOR_DB_PATH`

Unified SN39 details: [UNIFIED_SN39_VALIDATOR.md](docs/UNIFIED_SN39_VALIDATOR.md).

---

## Validator — advanced (loops & SN39)

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
