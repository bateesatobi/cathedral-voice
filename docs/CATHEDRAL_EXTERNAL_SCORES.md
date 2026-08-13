# Cathedral SN39 external scores (cathedral-voice / violet_audio)

Cathedral thin validators **do not** accept score POSTs. They only:

1. `GET {publisher}/v1/validator/weights/next`
2. Verify the Ed25519-signed vector
3. `set_weights` on netuid **39**

Violet / **cathedral-voice** is an *external scorer*. It posts reports to the
Cathedral **publisher**, which blends them into that signed vector.

```txt
cathedral-voice (this repo)
    → POST /v1/external-scores/violet   (source=violet_audio, complete=true)
    → Cathedral publisher blends + signs
    → cathedral-validator fetches feed
    → set_weights on SN39
```

Audit reference: [cathedralai/cathedral-validator](https://github.com/cathedralai/cathedral-validator)
`docs/VIOLET_EXTERNAL_SCORES.md`.

## Critical contract rules

| Rule | Why |
|------|-----|
| `source: "violet_audio"` | Legacy blended external source |
| `source: "cathedral_voice_hybrid"` | Receipt-gated hybrid (see [CATHEDRAL_VOICE_RECEIPT_v1.md](./CATHEDRAL_VOICE_RECEIPT_v1.md)); **not** connected to subnet C/W/Q weights here |
| **`complete: true`** | Incomplete reports are stored but **never blended** |
| Honest GPU flags | Reports/receipts must set `gpu_attested=false`, `gpu_memory_confidential=false` |
| Scores in `[0, 1]` | Publisher L1-normalizes |
| Monotonic `epoch` | Older / conflicting epochs rejected |
| Fresh `generated_at` | Default max age ~1 hour |
| Registered SN39 hotkeys | Unregistered filtered when `REQUIRE_REGISTERED=1` |
| Bearer token (+ optional HMAC) | Publisher ingest auth |

**Do not** POST to the thin validator process. **Do not** use the public edge
URL if it 404s `route_not_served_by_cathedral_edge` — ask Cathedral ops for the
direct publisher / submit origin (`CATHEDRAL_PUBLISHER_URL`).

## Enable in the validator

```bash
# .env
CATHEDRAL_EXTERNAL_SCORES_ENABLED=true
CATHEDRAL_PUBLISHER_URL=https://<publisher-origin-that-serves-ingest>
CATHEDRAL_EXTERNAL_SCORES_TOKEN=<from Cathedral ops>
# optional:
CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET=<if required>
CATHEDRAL_EXTERNAL_SCORES_NETUID=39
CATHEDRAL_EXTERNAL_SCORES_DRY_RUN=false

# Hybrid intake (receipt-gated; does not change C/W/Q weights):
# CATHEDRAL_HYBRID_SCORES_ENABLED=true
# CATHEDRAL_HYBRID_ONLY=false          # true = skip violet_audio; fail closed without receipts
# CATHEDRAL_RECEIPT_HMAC_SECRET=<receipt verify secret>
# CATHEDRAL_RECEIPT_ED25519_PUBLIC_KEY=<hex>
# CATHEDRAL_HYBRID_REQUIRE_TDX=true    # true when live TDX quotes are required
# Dedicated publisher auth for hybrid (required after cathedral-validator#118):
# CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_VOICE_HYBRID=<hybrid_token>
# CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET_CATHEDRAL_VOICE_HYBRID=<hybrid_hmac>

# Optional: only feed Cathedral SN39; skip Violet-subnet set_weights
# CATHEDRAL_SKIP_LOCAL_WEIGHTS=true
```

Each weight round posts a complete `violet_audio` snapshot built from miner
`final` scores (max-normalized to 0..1).

## One-shot / ops script

```bash
python scripts/post_cathedral_scores.py --dry-run \
  --score 5Fhotkey...=0.91 --score 5Ghotkey...=0.44

python scripts/post_cathedral_scores.py --print-only \
  --from-dashboard http://127.0.0.1:8092/api/scores

python scripts/post_cathedral_scores.py \
  --from-dashboard http://127.0.0.1:8092/api/scores
```

Expect HTTP **202** (idempotent retries may also return 202 with
`"idempotent": true`).

## Publisher-side (Cathedral ops) checklist

```bash
CATHEDRAL_EXTERNAL_SCORES_INGEST_ENABLED=1
CATHEDRAL_EXTERNAL_SCORES_ENABLED=1
CATHEDRAL_EXTERNAL_SCORES_SOURCE=violet_audio
CATHEDRAL_EXTERNAL_SCORES_MODE=blend
CATHEDRAL_EXTERNAL_SCORES_FRACTION=0.10
CATHEDRAL_EXTERNAL_SCORES_TOKEN=<shared with us>
```

Without ingest enabled → `404 external_scores_ingest_not_enabled`.
Without token while blending → `503 external_scores_token_required_while_blending`.

## cathedral-voice naming

This subnet (`violet-subnet`) **is** the cathedral-voice scorer for SN39 audio
quality/work. Publish or mirror the GitHub repo as `cathedralai/cathedral-voice`
when the org repo is created; keep package import path `violet.*` for
compatibility, or add a thin `cathedral_voice` alias later.
