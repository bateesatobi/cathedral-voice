# Cathedral Voice receipt v1 (`cathedral_voice_receipt_v1`)

Hybrid miner-owned attestation for voice (TTS) work on SN39 / Violet.

**Status:** design + schema stubs in this repo. Live TDX hardware and Cathedral
publisher ingest for `cathedral_voice_hybrid` are out of band — do **not** claim
production attestation without measured controller quotes.

**Gate:** receipt-gated hybrid scores must **not** change C/W/Q weight tables
until Cathedral ops enables blend for the new source.

---

## Trust model

| Component | Role | Attestation |
|-----------|------|-------------|
| Miner controller (CPU path) | Builds receipt, hashes request/audio, signs | **TDX measured** (quote / MRs) |
| GPU synthesis path | Runs Spark / TTS | **Trusted-not-attested** — bound by content hashes + hotkey identity, not GPU TEE |
| Validator / Cathedral | Verify signature + quote policy + hashes | Fail closed when required |

GPU work is explicitly **not** attested. The receipt records
`gpu_attestation_status: "trusted_not_attested"` (or `"unattested"` /
`"unavailable"` when TDX is off). Miners must never claim GPU attestation.

```txt
Validator private prompt
    → Miner controller (TDX) records request_hash
    → GPU synthesises audio (unattested)
    → Controller hashes audio, attaches TDX measurement + hotkey signature
    → Receipt travels with score row / sidecar
    → Cathedral intake accepts only if verify_receipt() passes
```

---

## Schema fields

| Field | Type | Notes |
|-------|------|-------|
| `version` | string | Always `cathedral_voice_receipt_v1` |
| `status` | enum | `ok` \| `unavailable` |
| `miner_hotkey` | string | SS58 hotkey that will receive scores |
| `request_hash` | hex sha256 | Canonical hash of `{input, voice, temperature}` |
| `audio_content_hash` | hex sha256 | Hash of raw response body (PCM/WAV bytes) |
| `controller_measurement` | string \| null | TDX quote or MR bundle (base64/hex); null if unavailable |
| `gpu_attestation_status` | enum | `unattested` \| `trusted_not_attested` \| `unavailable` |
| `issued_at` | ISO-8601 UTC | Millisecond precision preferred |
| `signature` | string \| null | Hotkey signature over canonical payload (excluding `signature`) |
| `probe_id` | string \| optional | Validator item id / correlation (no full prompt) |

Canonical JSON for hashing/signing: UTF-8, `sort_keys=True`, separators `,` `:`,
**without** the `signature` field.

---

## Verification steps (validator / Cathedral)

1. `version == "cathedral_voice_receipt_v1"`.
2. If intake requires receipts and `status != "ok"` → **reject**.
3. Recompute `request_hash` / `audio_content_hash` when the verifier has the
   raw request/audio; mismatch → **reject**.
4. Verify hotkey `signature` over the canonical body.
5. If policy requires TDX: parse `controller_measurement`, check quote against
   allowed MRs / collateral; missing measurement → **reject**.
6. `gpu_attestation_status` must not claim a stronger status than allowed
   (`trusted_not_attested` is the production GPU label).
7. Only then accept a `cathedral_voice_hybrid` score row.

Fail closed: missing receipt, invalid signature, or failed TDX policy ⇒ score
ignored (not blended).

Implementation stub: `violet.cathedral.receipt_v1`.

---

## Miner attachment (optional)

Env:

```bash
VIOLET_TTS_RECEIPT_ENABLED=1
# When TDX quote tooling is absent, emit status=unavailable (never forge quotes)
```

HTTP: optional response header `X-Violet-Voice-Receipt: <canonical-json>` on
`POST /v1/audio/speech/stream`. Prefer short JSON; do not put private validator
prompts in the receipt.

---

## Publisher checklist (Cathedral ops — `cathedralai/cathedral-validator`)

- [x] Allow source `cathedral_voice_hybrid` (separate from `violet_audio`)
- [x] Require `metadata.receipt_verified == true` + per-row receipts on positive scores
- [x] Reject incomplete / unverified hybrid reports for blend
- [x] Dedicated bearer + mandatory HMAC; explicit `FRACTION`; block `external_primary`
- [ ] Do **not** alter Violet C/W/Q weights inside subnet code for this path
- [ ] Keep `violet_audio` ingest unchanged for backward compatibility
- [ ] Merge/deploy publisher PR and wire production secrets

See also: [CATHEDRAL_EXTERNAL_SCORES.md](./CATHEDRAL_EXTERNAL_SCORES.md),
[CATHEDRAL_VOICE_PRODUCTION_ENV.md](./CATHEDRAL_VOICE_PRODUCTION_ENV.md),
[CATHEDRAL_VOICE_PUBLISHER_DEPLOY.md](./CATHEDRAL_VOICE_PUBLISHER_DEPLOY.md),
[TTS_CONTRACT.md](./TTS_CONTRACT.md),
[CATHEDRAL_VOICE_PRODUCTION_CHECKLIST.md](./CATHEDRAL_VOICE_PRODUCTION_CHECKLIST.md).

## Ed25519 + TDX software path (this repo)

```bash
# CI / dry-run only
export CATHEDRAL_TDX_SIMULATION=1
export VIOLET_TTS_RECEIPT_ENABLED=1
export VIOLET_TTS_RECEIPT_BUFFER=1
export VIOLET_RECEIPT_ED25519_PRIVATE_KEY=<hex>

python scripts/cathedral_voice_e2e_proof.py
```

Production cutover (no simulation):

```bash
python scripts/cathedral_voice_receipt_keygen.py --write-env ./.env.receipt
python scripts/cathedral_voice_production_preflight.py --role both
python scripts/cathedral_voice_e2e_proof.py --live \
  --measurement-file /var/lib/violet/tdx_measurement.json
```

Production must use live quotes (`VIOLET_TDX_QUOTE_VERIFIER`) and must **not**
set `CATHEDRAL_TDX_SIMULATION`.
