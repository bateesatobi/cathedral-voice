# Security Model

What this implementation defends against, how, and — equally important — what it
does not.

## Controls in place

| Control | Implementation | Where |
|---|---|---|
| WebSocket auth | Same bearer token as HTTP (`Authorization` header or `?token=`) | `miner/server.py` |
| Upload / WS limits | `MINER_MAX_*` env vars; idle timeout on streams | `miner/server.py`, `config.py` |
| Upstream isolation | ASR/TTS bound to `127.0.0.1` in install scripts | `miner/stt_install.sh`, `miner/tts_install.sh` |
| HTTPS in prod | `start.sh` rejects `http://` unless `MINER_ALLOW_HTTP=1` | `miner/start.sh` |
| Endpoint ↔ hotkey binding | Signed `/violet/identity/challenge` | `identity.py`, `validator/probes.py` |
| Official images | `violet/releases/manifest.json` checked at qualification | `validator/qualification.py` |
| Work report integrity | HMAC includes `mean_latency_ms`; deterministic `report_id` | `validator/work.py`, `router/receipts.py` |
| Qualification freshness | Stale qual (>24h) does not earn emissions | `validator/run.py` |
| Progressive streaming | ASR/TTS streams must emit ≥2 growing chunks | `validator/probes.py`, `qualification.py` |
| Router hotkey binding | `/health` `x-violet-hotkey` must match metagraph | `router/registry.py` |
| Endpoint stability | Health probes every 60 s; decay then removal from routing | `validator/evaluator.py`, `router/registry.py` |
| Evaluation integrity | Probes indistinguishable from production traffic; block-seeded rotating corpus; 7-day window | `validator/probes.py`, `evalset/` |
| Access control | Router routes only to qualified, currently-healthy miners | `router/registry.py` |
| Work integrity | HMAC-signed reports, verified before ingestion, deduplicated on report ID | `validator/work.py` |
| Sybil resistance | Multi-UID collapse per coldkey; endpoint-collision division | `validator/antigaming.py` |
| Emission concentration | 25% cap per miner on the weight vector | `chain/weights.py` |
| Availability manipulation | Durable rolling window; a validator restart does not reset history | `validator/store.py` |

## Not defended

These are real gaps. Several are named in TDD 9.3 and 12; the rest follow from
the same design choices.

### No cryptographic hardware attestation

A miner reports its GPUs via `nvidia-smi`. Nothing proves the reported hardware
exists. What exists instead is a consistency argument: reported VRAM must match
the claimed model within 12%, the multiplier must match the tier, and a miner
claiming ≥2 capacity units must absorb a 4-way concurrent burst.

A determined adversary can satisfy all three with less hardware than claimed —
by fronting a smaller GPU with aggressive batching, for instance. The roadmap
answer is TEE attestation or challenge-response VRAM proofs.

**Consequence at launch:** capacity is 75% of the score, and capacity is the
least verifiable component. This is the largest single exposure in v1.4.

### Coldkey-splitting Sybils

The multi-UID rule collapses hotkeys under one coldkey. An adversary funding
several *unrelated* coldkeys defeats it, as TDD 9.2 acknowledges.

Endpoint-collision detection catches the naive version, where the coldkeys point
at one machine. It does not catch an adversary who genuinely distributes across
hosts — at which point they are, arguably, several real operators.

### The Work component trusts one reporter

Work counters come from the Avoices router, signed with a shared secret. Whoever
holds that secret can mint arbitrary work for any hotkey.

Bounded by the launch weighting (12.5%) and by the requirement that a miner also
pass qualification and hold real capacity. The mitigation as the weight grows to
45% at maturity is multiple independent reporters cross-checked against each
other. **That is not implemented.** Do not advance to the mature phase without
it.

### Evaluation corpus overfitting

Block-seeded rotation stops a miner learning the order of scored utterances. It
does not stop a miner overfitting the corpus itself if the corpus is fixed and
obtainable.

The **standard** ASR corpus is [Sunbird/salt](https://huggingface.co/datasets/Sunbird/salt)
multispeaker **test** audio (see [EVALSET.md](./EVALSET.md)). That set is public,
so operators must:

* keep the builder **holdout** offline;
* rotate `--seed` / refresh holdout on a schedule;
* preferably mix in private Avoices clips over time.

The built-in package corpus remains a synthetic bootstrap only (no real WER).

### Validator collusion

Inherent to any stake-weighted system. Nothing here detects it. The dashboard
publishes each validator's scores, which makes correlated behaviour observable
to anyone who compares dashboards — but observation is not prevention.

### Network-level attacks on miners

An attacker who degrades an honest miner's connectivity degrades its
availability score. The 7-day window and the 5% failure tolerance limit the
damage from short attacks; a sustained one will cost the miner real emissions
through no fault of its own.

## Operational security

**Wallet handling.** The miner sidecar signs with its hotkey. Mount the wallet
read-only (`docker-compose.miner.yml` does). The coldkey should not be on the
miner host at all.

**Miner access token.** `MINER_ACCESS_TOKEN` gates production traffic.
Deliberately **not** applied to `/health`, `/capacity` or `/violet/info` — if a
miner could reject unknown callers on those, it could dodge evaluation.

**Work report secret.** `VIOLET_WORK_REPORT_SIGNING_KEY` (router) and
`VIOLET_WORK_REPORT_TOKEN` (validator) may be set to the same value, but note
what that means: anyone who can *read* a report can also *forge* one. Use
distinct values — bearer token for transport auth, HMAC secret for integrity —
where you can.

**Receipts durability.** `VIOLET_RECEIPTS_DB_PATH` must be on a persistent
volume. On Render that means a mounted disk. Ephemeral storage loses the work
history miners are paid on at every deploy.

**Dashboard exposure.** The validator dashboard is intentionally public and
read-only. It exposes miner scores and enforcement actions, not keys and not
Avoices user data. The work-report endpoint is different — it reveals traffic
volume, so keep the bearer token on it.

## Reporting

Security issues in this implementation should go to the subnet owner privately
before public disclosure.
