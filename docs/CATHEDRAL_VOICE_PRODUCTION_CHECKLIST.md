# Cathedral Voice — remaining ops / publisher enablement

In-repo software for G04/G05/G10 is implemented. This checklist is what remains
**outside** this monorepo (or on hardware) before reward-bearing hybrid scores.

## A. Live TDX controller (production G04)

- [ ] Run miner controller inside a TDX guest with measured workload chain
- [ ] Export real quote into `cathedral_tdx_measurement_v1` (`mrtd`, RTMRs, `debug=false`)
- [ ] Set allow-lists: `VIOLET_TDX_ALLOWED_MRTD`, optional `VIOLET_TDX_ALLOWED_RTMR0`
- [ ] Bind `challenge` / `audience=cathedral_voice_hybrid` / `hotkey` / `endpoint`
- [ ] Point `VIOLET_TDX_QUOTE_VERIFIER` at an Intel DCAP/PCS verifier (stdin = quote b64, exit 0 = ok)
- [ ] **Unset** `CATHEDRAL_TDX_SIMULATION` (simulation must never touch emissions)

## B. Receipt signing key (production G05)

- [ ] Generate Ed25519 receipt key inside the measured controller
- [ ] Miner: `VIOLET_RECEIPT_ED25519_PRIVATE_KEY=<hex>` + `VIOLET_TTS_RECEIPT_ENABLED=1` + buffer on
- [ ] Validator/publisher: `CATHEDRAL_RECEIPT_ED25519_PUBLIC_KEY=<hex>`
- [ ] Confirm HTTP TTS responses include `X-Violet-Voice-Receipt` with `status=ok`, non-empty `audio_content_hash`, and `gpu_attested=false`

## C. Cathedral publisher (out of repo)

Enable a **separate** external source (do not reuse `violet_audio` credentials):

```bash
CATHEDRAL_EXTERNAL_SCORES_INGEST_ENABLED=1
CATHEDRAL_EXTERNAL_SCORES_SOURCE=cathedral_voice_hybrid
CATHEDRAL_EXTERNAL_SCORES_MODE=blend   # or isolate until ready
# Per-source bearer + HMAC distinct from violet_audio
```

Reject rules:

1. `source != cathedral_voice_hybrid` for this lane
2. `complete != true`
3. `metadata.receipt_verified != true`
4. Missing/invalid `cathedral_voice_receipt_v1` on any positive score
5. TDX verify fail / simulated measurement when production policy forbids it
6. `gpu_attested=true` or `gpu_memory_confidential=true` → reject

## D. Testnet E2E (G10 live)

In-repo dry proof:

```bash
cd violet-subnet
pip install -e ".[dev,cathedral]"
CATHEDRAL_TDX_SIMULATION=1 python scripts/cathedral_voice_e2e_proof.py \
  --out ./data/cathedral_voice_e2e_proof.json
```

Live proof (after A–C):

- [ ] One miner with buffered receipts + live quote
- [ ] Validator posts hybrid report to publisher
- [ ] Thin relay fetches signed vector including hybrid lane
- [ ] Preserve IDs: `request_hash`, `audio_content_hash`, `mrtd`, `report.epoch`, publisher `vector_id`, chain extrinsic

## E. Explicit non-goals until A–D complete

- Do **not** connect hybrid voice scores to subnet C/W/Q weight tables in this repo
- Do **not** claim GPU attestation or confidential GPU memory
- Do **not** treat `CATHEDRAL_TDX_SIMULATION=1` proofs as production attestation
