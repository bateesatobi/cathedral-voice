# Unified cathedral-voice validator (voice + SN39 thin)

This repo's validator can run **both**:

1. **Voice / cathedral-voice work** — probe ASR/TTS miners, score C/W/Q, POST
   `violet_audio` to the Cathedral publisher.
2. **Cathedral thin work** — fetch the signed SN39 weight feed, verify Ed25519,
   map hotkeys→UIDs, `set_weights` on **netuid 39**.

```txt
                    ┌─────────────────────────────┐
  ASR/TTS miners ──►│ cathedral-voice validator   │
  (public :8091)    │  health / eval / work       │
                    │  score C/W/Q                │
                    │         │                   │
                    │         ▼                   │
                    │  POST violet_audio           │──► Cathedral publisher
                    │  (complete=true)            │      blend base + violet
                    │                             │      sign vector
                    │  GET weights/next ◄─────────┼──────┘
                    │  verify + set_weights SN39  │
                    └─────────────────────────────┘
                              │
                              ▼
                         Bittensor SN39
```

## Bittensor: “sub-subnets” / multi-mechanism

On Bittensor today:

| Pattern | What it is | Relevance |
|---------|------------|-----------|
| **Multi-netuid validator** | Same hotkey registered on several subnets; `set_weights(netuid=…)` per subnet | Possible, but Cathedral voice + thin share **SN39 (39)** |
| **Multiple incentive mechanisms** (formerly called sub-subnets) | Up to **2** `mechid`s inside one subnet; separate weight matrices + emission split | Chain-native parallel lanes |
| **Cathedral publisher lanes** | Soft lanes inside **one** signed vector (`base`, `violet_audio`, …) blended off-chain | **What SN39 uses now** |

So “one validator serving both Cathedral miners and cathedral-voice miners” on SN39 is **not** two chain mechs by default — it is **one signed vector** composed by the publisher. Our colocated validator contributes the violet lane (scores) and writes the composed vector (thin).

## Trust model (no silent failure of either population)

| Failure | Effect |
|---------|--------|
| Voice probes/score POST fail | Publisher fail-closed → **base-only** vector → Cathedral compute/SAT miners still paid |
| Violet snapshot stale / `complete!=true` | Same — **not blended**; base preserved |
| Thin feed verify fails | **No** `set_weights` this tick; voice keeps posting for next blend |
| Thin maps only registered hotkeys | Deregistered skipped; others keep mass |
| Dual local+thin `set_weights` | **Forbidden** — last write wins and can wipe the blend |

**Sole writer rule:** when `CATHEDRAL_THIN_BROADCAST=true`, the process forces `CATHEDRAL_SKIP_LOCAL_WEIGHTS` so only the **verified signed feed** hits chain.

## Enable both paths

```bash
# Voice → publisher
CATHEDRAL_EXTERNAL_SCORES_ENABLED=true
CATHEDRAL_PUBLISHER_URL=https://api.cathedral.computer   # or ingest-capable origin
CATHEDRAL_EXTERNAL_SCORES_TOKEN=<secret>
CATHEDRAL_EXTERNAL_SCORES_NETUID=39

# Thin → SN39
CATHEDRAL_THIN_ENABLED=true
CATHEDRAL_THIN_BROADCAST=true          # actually set_weights
CATHEDRAL_THIN_DRY_RUN=false
CATHEDRAL_THIN_INTERVAL_S=1500
CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY=10890a66aa752479cb3b634f366d7bd27c374324d83f88d2d6b69ab066f25e26
CATHEDRAL_WEIGHT_POLICY_KEY_ID=cathedral-weight-policy

# Preferred: official CLI if installed (full Cathedral hardening)
# CATHEDRAL_THIN_PREFER_SUBPROCESS=true
# pip install / path to cathedral-validator
```

Publisher ops must still have:

```bash
CATHEDRAL_EXTERNAL_SCORES_INGEST_ENABLED=1
CATHEDRAL_EXTERNAL_SCORES_ENABLED=1
CATHEDRAL_EXTERNAL_SCORES_SOURCE=violet_audio
CATHEDRAL_EXTERNAL_SCORES_MODE=blend
CATHEDRAL_EXTERNAL_SCORES_FRACTION=0.10
```

## Isolation

- Voice loops and thin loop are **separate asyncio tasks**.
- Exceptions in thin never cancel health/eval/work.
- Exceptions in voice never cancel thin.
- Prefer `cathedral-validator serve --once` subprocess when available; else in-process fetch/verify/map via `violet.cathedral.thin_relay`.

## Recommended rollout

1. `CATHEDRAL_THIN_ENABLED=true` + dry-run (no broadcast) — confirm verify/map logs.
2. Enable score POST dry-run, then live token.
3. `CATHEDRAL_THIN_BROADCAST=true` once feed looks right.
4. Keep a second independent `cathedral-validator` only if you accept **one** broadcaster (shared runtime lock) — prefer a single writer process.
