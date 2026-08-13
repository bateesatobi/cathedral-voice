# Cathedral Voice — production environment cutover

Copy-paste env blocks for a production-operable **testnet** lane. Hybrid scores
still do **not** change Violet C/W/Q weight tables until a separate explicit
decision after live E2E proof.

Publisher admit PR (must be merged + deployed first):
https://github.com/cathedralai/cathedral-validator/pull/118

Related: [CATHEDRAL_VOICE_PRODUCTION_CHECKLIST.md](./CATHEDRAL_VOICE_PRODUCTION_CHECKLIST.md),
[CATHEDRAL_VOICE_RECEIPT_v1.md](./CATHEDRAL_VOICE_RECEIPT_v1.md),
[CATHEDRAL_EXTERNAL_SCORES.md](./CATHEDRAL_EXTERNAL_SCORES.md).

---

## 0. Cathedral publisher (ops)

```bash
CATHEDRAL_EXTERNAL_SCORES_INGEST_ENABLED=1
CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_VOICE_HYBRID=<hybrid_token>
CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET_CATHEDRAL_VOICE_HYBRID=<hybrid_hmac>
CATHEDRAL_EXTERNAL_SCORES_ENABLED=1
CATHEDRAL_EXTERNAL_SCORES_SOURCE=cathedral_voice_hybrid
CATHEDRAL_EXTERNAL_SCORES_MODE=blend
CATHEDRAL_EXTERNAL_SCORES_FRACTION=0.1
# Leave UNSET in production:
# CATHEDRAL_VOICE_HYBRID_ALLOW_SIMULATION
```

Smoke: signed `POST /v1/external-scores/violet` with `source=cathedral_voice_hybrid`
→ HTTP **202**.

---

## 1. Miner (measured controller)

Generate keys inside the TDX guest:

```bash
python scripts/cathedral_voice_receipt_keygen.py --write-env ./.env.receipt
```

```bash
# Production — never set CATHEDRAL_TDX_SIMULATION
VIOLET_TTS_RECEIPT_ENABLED=1
VIOLET_TTS_RECEIPT_BUFFER=1
VIOLET_RECEIPT_ED25519_PRIVATE_KEY=<hex from keygen>
VIOLET_REQUIRE_IMAGE_DIGESTS=1

# Live measurement (JSON string or path contents loaded into env by ops)
VIOLET_TDX_MEASUREMENT=<cathedral_tdx_measurement_v1 json>
VIOLET_TDX_CHALLENGE=<bound challenge>
VIOLET_TDX_AUDIENCE=cathedral_voice_hybrid
VIOLET_TDX_EXPECTED_HOTKEY=<miner hotkey>
VIOLET_TDX_EXPECTED_ENDPOINT=<public miner endpoint>
VIOLET_PUBLIC_ENDPOINT=<same as expected endpoint>
VIOLET_TDX_ALLOWED_MRTD=<comma-separated allowed mrtd hex>
# VIOLET_TDX_ALLOWED_RTMR0=<optional>
VIOLET_TDX_QUOTE_VERIFIER=/usr/local/bin/violet-tdx-quote-verify
```

Confirm TTS responses include `X-Violet-Voice-Receipt` with `status=ok`,
non-empty `audio_content_hash`, and `gpu_attested=false`.

---

## 2. Validator (semantic + hybrid publish)

```bash
# Trusted ASR (required when hybrid / semantic gate is production-on)
VALIDATOR_TRUSTED_ASR_URL=https://<trusted-asr>/transcribe
VALIDATOR_TRUSTED_ASR_TOKEN=<optional>
VALIDATOR_TTS_SEMANTIC_REQUIRED=1
VALIDATOR_TTS_HOLDOUT_PATH=/var/lib/violet/tts_holdout.jsonl

# Hybrid publish (does not alter C/W/Q tables)
CATHEDRAL_EXTERNAL_SCORES_ENABLED=1
CATHEDRAL_EXTERNAL_SCORES_DRY_RUN=0
CATHEDRAL_PUBLISHER_URL=https://<publisher-origin>
CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_VOICE_HYBRID=<hybrid_token>
CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET_CATHEDRAL_VOICE_HYBRID=<hybrid_hmac>
# Optional shared violet_audio credentials remain separate:
# CATHEDRAL_EXTERNAL_SCORES_TOKEN=<violet_audio token>
# CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET=<violet_audio hmac>

CATHEDRAL_HYBRID_SCORES_ENABLED=1
CATHEDRAL_HYBRID_ONLY=0
CATHEDRAL_HYBRID_REQUIRE_TDX=1
CATHEDRAL_RECEIPT_ED25519_PUBLIC_KEY=<hex matching miner private>

# Production — never set
# CATHEDRAL_TDX_SIMULATION=1

VIOLET_TDX_QUOTE_VERIFIER=/usr/local/bin/violet-tdx-quote-verify
VIOLET_TDX_ALLOWED_MRTD=<same allow-list as miner>
VIOLET_TDX_AUDIENCE=cathedral_voice_hybrid
VIOLET_REQUIRE_IMAGE_DIGESTS=1
```

---

## 3. Preflight + live E2E

```bash
# Fail closed on dangerous config
python scripts/cathedral_voice_production_preflight.py --role miner
python scripts/cathedral_voice_production_preflight.py --role validator

# Dry (CI only)
CATHEDRAL_TDX_SIMULATION=1 python scripts/cathedral_voice_e2e_proof.py

# Live (requires Phase 2 keys + measurement + publisher deploy)
python scripts/cathedral_voice_e2e_proof.py --live \
  --measurement-file /path/to/measurement.json \
  --out ./data/cathedral_voice_e2e_live.json
```

Record from a successful live run: `request_hash`, `audio_content_hash`, `mrtd`,
`report.epoch`, publisher `vector_id` / response body, chain extrinsic (thin relay).
