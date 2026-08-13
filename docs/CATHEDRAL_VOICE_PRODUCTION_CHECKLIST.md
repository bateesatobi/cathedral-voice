# Cathedral Voice — remaining ops / publisher enablement

In-repo software for G04/G05/G10 is implemented. This checklist is what remains
**outside** this monorepo (or on hardware) before reward-bearing hybrid scores.

Cutover env reference: [CATHEDRAL_VOICE_PRODUCTION_ENV.md](./CATHEDRAL_VOICE_PRODUCTION_ENV.md)  
Publisher PR deploy: [CATHEDRAL_VOICE_PUBLISHER_DEPLOY.md](./CATHEDRAL_VOICE_PUBLISHER_DEPLOY.md)

```bash
python scripts/cathedral_voice_production_preflight.py --role both
python scripts/cathedral_voice_receipt_keygen.py --write-env ./.env.receipt
# Install scripts/violet-tdx-quote-verify as VIOLET_TDX_QUOTE_VERIFIER
```

## A. Live TDX controller (production G04)

- [ ] Run miner controller inside a TDX guest with measured workload chain
- [ ] Export real quote into `cathedral_tdx_measurement_v1` (`mrtd`, RTMRs, `debug=false`)
- [ ] Set allow-lists: `VIOLET_TDX_ALLOWED_MRTD`, optional `VIOLET_TDX_ALLOWED_RTMR0`
- [ ] Bind `challenge` / `audience=cathedral_voice_hybrid` / `hotkey` / `endpoint`
- [ ] Install `scripts/violet-tdx-quote-verify` and point `VIOLET_TDX_QUOTE_VERIFIER` at it; set `VIOLET_TDX_DCAP_VERIFIER` to Intel DCAP/PCS
- [ ] **Unset** `CATHEDRAL_TDX_SIMULATION` (simulation must never touch emissions)

## B. Receipt signing key (production G05)

- [ ] Generate Ed25519 receipt key inside the measured controller (`scripts/cathedral_voice_receipt_keygen.py`)
- [ ] Miner: `VIOLET_RECEIPT_ED25519_PRIVATE_KEY=<hex>` + `VIOLET_TTS_RECEIPT_ENABLED=1` + buffer on
- [ ] Validator/publisher: `CATHEDRAL_RECEIPT_ED25519_PUBLIC_KEY=<hex>`
- [ ] Confirm HTTP TTS responses include `X-Violet-Voice-Receipt` with `status=ok`, non-empty `audio_content_hash`, and `gpu_attested=false`

## C. Cathedral publisher (`cathedralai/cathedral-validator`)

PR: https://github.com/cathedralai/cathedral-validator/pull/118 (needs `cathedralai` merge)

Publisher admit path for `cathedral_voice_hybrid` (allowlist + receipt/HMAC gates):

```bash
CATHEDRAL_EXTERNAL_SCORES_INGEST_ENABLED=1
CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_VOICE_HYBRID=<hybrid_token>
CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET_CATHEDRAL_VOICE_HYBRID=<hybrid_hmac>

# Composition (blend only; FRACTION required — no external_primary / no confidential_primary)
CATHEDRAL_EXTERNAL_SCORES_ENABLED=1
CATHEDRAL_EXTERNAL_SCORES_SOURCE=cathedral_voice_hybrid
CATHEDRAL_EXTERNAL_SCORES_MODE=blend
CATHEDRAL_EXTERNAL_SCORES_FRACTION=0.1

# CI only — leave unset in production
# CATHEDRAL_VOICE_HYBRID_ALLOW_SIMULATION=1
```

Reject rules (enforced by publisher):

1. `source` not in allowlist / not `cathedral_voice_hybrid` for this lane
2. `complete != true`
3. `metadata.receipt_verified != true`
4. Missing/invalid `cathedral_voice_receipt_v1` on any positive score
5. TDX verify fail / simulated measurement unless `CATHEDRAL_VOICE_HYBRID_ALLOW_SIMULATION=1`
6. `gpu_attested=true` or `gpu_memory_confidential=true` → reject
7. Missing dedicated HMAC secret → 503; bad signature → 401

## D. Testnet E2E (G10 live)

In-repo dry proof:

```bash
cd violet-subnet
pip install -e ".[dev,cathedral]"
CATHEDRAL_TDX_SIMULATION=1 python scripts/cathedral_voice_e2e_proof.py \
  --out ./data/cathedral_voice_e2e_proof.json
```

Live proof (after A–C):

```bash
python scripts/cathedral_voice_live_e2e_record.py \
  --measurement-file /var/lib/violet/tdx_measurement.json \
  --out ./data/cathedral_voice_live_evidence.json \
  --vector-id <from-thin-relay> \
  --chain-extrinsic <extrinsic-hash>
```

- [ ] One miner with buffered receipts + live quote
- [ ] Validator posts hybrid report to publisher
- [ ] Thin relay fetches signed vector including hybrid lane
- [ ] Preserve IDs: `request_hash`, `audio_content_hash`, `mrtd`, `report.epoch`, publisher `vector_id`, chain extrinsic

## E. Explicit non-goals until A–D complete

- Do **not** connect hybrid voice scores to subnet C/W/Q weight tables in this repo
- Do **not** claim GPU attestation or confidential GPU memory
- Do **not** treat `CATHEDRAL_TDX_SIMULATION=1` proofs as production attestation
