# Architecture — Violet (PHOSAI / Polaris)

ASR/TTS for the PHOSAI Avoices product; Polaris/Violet miners and validators
supply the inference layer. Product billing and public APIs stay on Avoices —
Violet only replaces the inference hop.

## Layers

```
Avoices frontend (asrvoices) — PHOSAI
        │
        ▼
Avoices backend (ASRAPI)
  ├─ identity, billing, product storage, public API
  └─ Violet smart router
        │  discovery ▲          traffic ▼
        │            │
        │      ┌─────┴──────────────────────────┐
        │      │      Bittensor metagraph       │
        │      │  commitments, axons, weights   │
        │      └─────┬──────────────────────────┘
        │            ▲ weights           ▲ announcements
        │      ┌─────┴──────┐            │
        │      │ Validators │─ probes ──▶│
        │      └────────────┘            │
        ▼                                │
  Miner pool (Polaris / Violet)
    ┌──────────────────────────────────┐
    │  miner sidecar  (this repo)      │
    │      ├─▶ official ASR container  │
    │      └─▶ official TTS container  │
    └──────────────────────────────────┘
```

## Why the miner is a sidecar

The miner process performs no inference. It fronts the official ASR and TTS
Docker images running on the same host and adds three things they do not
provide:

1. **Admission control.** The sidecar refuses at its concurrency limit instead
   of queueing. Queueing hides saturation: the request eventually succeeds,
   slowly, and the miner keeps attracting traffic it cannot serve. A 503 lets
   the router shed to another miner while the first byte is still in budget.
2. **Health aggregation.** One `/health` describing both upstreams, the GPU
   inventory and current load — the single endpoint validators and the router
   both poll.
3. **Identity.** Every response carries the serving hotkey, so the router can
   attribute work without remembering which URL it dialled.

Everything else is passed through byte-for-byte. TDD 9.2 requires the serving
interface to be unmodified, and a sidecar that rewrote payloads would be
modifying it.

This also means the inference images stay swappable. Point
`MINER_ASR_UPSTREAM` at the sample container, at the official image, or at an
existing GPU box, and the rest of the subnet cannot tell the difference.

## Discovery: two paths, one reader

A miner publishes its address in either or both of:

- **An on-chain commitment** — the full payload: endpoint URL, services, GPU
  inventory, image digests. Carries a DNS hostname, so it supports TLS.
- **The axon record** — IP and port only, cheaper to publish.

`ChainClient.announcements()` merges both, commitment first. Validators and the
router call the same method, so they can never disagree about who exists.

Chose commitments over a central registry deliberately: a registry would put a
trusted third party between miners and validators, and would have to be run,
secured and paid for. A commitment is signed by the hotkey by construction and
readable by anyone.

GPU claims in the announcement are **not** trusted. They are cross-checked
against `/capacity` and against observed concurrency before they earn anything.

## Evaluation

Loops at different cadences, running concurrently:

| Loop | Period | Does |
|---|---|---|
| Discovery | 5 min | Reads the metagraph, rebuilds the miner set |
| Health | 60 s | Probes `/health`; feeds availability history and the online-capacity series |
| Evaluation | 5 min | Full qualification plus quality/latency probes |
| Work | 5 min | Pulls signed work counters from PHOSAI / Avoices |
| Weights | ~150 blocks | Scores the window, applies anti-gaming, submits |

Probes look exactly like production traffic: same paths, same payloads, no
marker header.

## Rotation and validator consensus

Each round draws a rotating subset of the evaluation corpus, seeded on **block
height**. Two consequences, both intended:

- A miner cannot learn which utterances are scored by observing past rounds.
- Every validator in a round draws the *same* utterances, so honest validators
  do not diverge from consensus through sampling noise — which TDD 5 penalises.

## Routing

Selection scores each healthy candidate on headroom, latency and on-chain
incentive, then samples probabilistically among the top three rather than always
taking the maximum.

Streaming sessions are **sticky**: ASR decoder state lives on the miner, so
moving mid-stream restarts the transcript. Only a hard failure re-selects.

Failover applies **before the first byte only**. Once audio has reached the
client, switching miners would splice two different voices into one utterance,
so a mid-stream failure is surfaced rather than papered over.

Legacy single-host fallback is optional (`VIOLET_FALLBACK_*` /
`VIOLET_ALLOW_LEGACY_FALLBACK`). PHOSAI can require miner-only failover.

## Work accounting

The Work score must count real traffic. Two sources exist:

- Ask the miner how much it did — self-reported and trivially inflated. **Not
  used.**
- Ask the party that dispatched the traffic — the PHOSAI router — for signed,
  aggregated counters per hotkey. **Used.**

The router writes a receipt per completed request, aggregates them, and signs
the aggregate with a shared secret. Validators verify before ingesting and
deduplicate on report ID so a retry or restart cannot pay twice.

## Data that must survive restarts

| Store | Holds | If lost |
|---|---|---|
| `VALIDATOR_DB_PATH` | 7-day rolling window, qualifications, strikes | Every miner's history resets — unfair to steady operators, and exploitable |
| `VIOLET_RECEIPTS_DB_PATH` | Work receipts | Miners lose credit for work they performed |

Both are SQLite in WAL mode. A validator should be one process and one file.

## Component dependencies

The chain layer is an **optional** install extra. The router embeds in the
Avoices backend and the sample containers run without `bittensor`, so neither
drags the full SDK into a production web service that does not need it.
