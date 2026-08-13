# Cathedral Voice — publisher deploy (PR #118)

Upstream admit path for `cathedral_voice_hybrid`:
https://github.com/cathedralai/cathedral-validator/pull/118

This repo cannot merge that PR (requires `cathedralai` write access). Track merge
state, then deploy the env below on the publisher host.

## Merge gate

```bash
gh pr view 118 --repo cathedralai/cathedral-validator --json state,mergedAt,url
```

Expected after merge: `state=MERGED`. Until then, production hybrid POSTs may still
fail with `invalid_source_for_violet_endpoint`.

## Publisher env (production)

```bash
CATHEDRAL_EXTERNAL_SCORES_INGEST_ENABLED=1
CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_VOICE_HYBRID=<hybrid_token>
CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET_CATHEDRAL_VOICE_HYBRID=<hybrid_hmac>
CATHEDRAL_EXTERNAL_SCORES_ENABLED=1
CATHEDRAL_EXTERNAL_SCORES_SOURCE=cathedral_voice_hybrid
CATHEDRAL_EXTERNAL_SCORES_MODE=blend
CATHEDRAL_EXTERNAL_SCORES_FRACTION=0.1
```

Leave unset:

```bash
# CATHEDRAL_VOICE_HYBRID_ALLOW_SIMULATION
```

Do **not** reuse `violet_audio` bearer/HMAC for this source.

## Smoke

Post one complete hybrid report signed with the dedicated HMAC. Expect HTTP **202**.

Validator-side credentials must match:

```bash
CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_VOICE_HYBRID=<same token>
CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET_CATHEDRAL_VOICE_HYBRID=<same hmac>
```

See [CATHEDRAL_VOICE_PRODUCTION_ENV.md](./CATHEDRAL_VOICE_PRODUCTION_ENV.md).
