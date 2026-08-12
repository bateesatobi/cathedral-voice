# Rewards & Incentive — Violet (PHOSAI / Polaris)

Straight summary of how miners get paid.

```text
Final Score = w_c · Capacity + w_w · Work + w_q · Quality
```

Each of C / W / Q is scaled to `[0, 1]` across the active set, then weighted.

---

## Phase weights

| Phase | Capacity | Work | Quality | When |
|-------|----------|------|---------|------|
| **Launch** | 75% | 12.5% | 12.5% | Bootstrapping |
| **Growth** | 55% | 27.5% | 17.5% | ~20k req/day |
| **Mature** | 40% | 45% | 15% | ~150k req/day |

Set with `VIOLET_PHASE=launch|growth|mature`.

---

## Capacity (C)

Mean **online healthy** capacity units over **7 days**.

```text
units = Σ (GPU tier multiplier)
```

| GPU | Multiplier |
|-----|------------|
| A100 40 GB | 1.0 |
| A100 80 GB | 1.6 |
| H100 80 GB | 2.4 |
| H100 NVL | 2.7 |
| H200 | 3.5 |

- More GPUs → more C (if accepted + healthy).
- Offline half the week → ~half the credit.
- Availability &lt; 50% → extra decay.
- Misreported hardware → penalty.

---

## Work (W)

```text
W ≈ (log1p(requests) + log1p(stream_minutes)) × latency_multiplier
```

- Only **PHOSAI router** signed reports count (miners cannot self-report).
- Faster first-byte latency → higher multiplier (full credit ≤ ~200 ms p95).
- Log scale: 10× traffic helps, but does not wipe everyone else out.

---

## Quality (Q)

```text
Q ≈ mean_probe_quality × success_rate × availability
```

- **ASR:** WER against the **Sunbird/SALT** multispeaker test corpus (standard
  build: `scripts/build_salt_evalset.py`). See [EVALSET.md](./EVALSET.md).
- **TTS:** length / energy / silence / clipping sanity (not MOS).
- Flaky miners get cut even if occasional answers look good.
- Block-seeded rotation + holdout mitigate public-corpus overfitting.

---

## 7-day window

Rolling lookback (not a calendar week reset):

- Yesterday’s burst is diluted by the other six days.
- Scores are smoothed with the previous score so rankings move gradually.
- Validator stores history in `VALIDATOR_DB_PATH` (SQLite); must survive restarts.
- About every **150 blocks**, validators compute finals and **`set_weights`** on chain.

---

## Anti-gaming (short)

| Rule | Effect |
|------|--------|
| Multi-UID | Only best hotkey per coldkey scores |
| Repeat multi-UID | Exclusion → blacklist |
| Shared endpoint | Score split across hotkeys on same host |
| Probe rotation | Block-seeded corpus; hard to overfit |
| Capacity claims | Cross-checked vs VRAM + concurrency |

---

## Example intuition

| Miner | Hardware | Traffic | Likely lead |
|-------|----------|---------|-------------|
| A | 1× H100 (2.4 u) | 10 jobs | Higher **Work** |
| B | 1× H200 (3.5 u) | 2 jobs | Higher **Capacity** |

At **launch**, B often still wins overall because C is 75%. As the network moves to **mature**, A’s traffic matters more.

Simulate:

```bash
cd violet-subnet
python scripts/simulate_scoring.py --sybil
```

---

## Where scores live

| Place | Role |
|-------|------|
| Validator SQLite | Rolling evidence + score snapshots |
| Bittensor chain | Published weights → emissions |
| PHOSAI receipts DB | Router work accounting (input to W) |
| PHOSAI admin APIs | Operator visibility (miners, invocations) |

Emissions follow **on-chain weights**, not the admin UI alone.
