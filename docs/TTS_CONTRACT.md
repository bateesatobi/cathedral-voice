# TTS wire contract (Spark / Violet miners)

Canonical public contract for `POST /v1/audio/speech/stream` and the miner
sidecar that fronts Spark TTS.

## Allowed upstream payload

Spark receives **exactly**:

```json
{
  "input": "<text to speak>",
  "voice": "<catalogue voice id>",
  "temperature": 0.7
}
```

Do **not** send both `text` and `input`, or both `speaker_id` and `voice`.
Spark returns HTTP 422 on duplicate fields.

## Miner-facing aliases (deprecated)

The Violet miner sidecar accepts legacy Avoices / router shapes and remaps them
via `_spark_tts_upstream_payload`:

| Caller field | Upstream field |
|--------------|----------------|
| `input` (preferred) or `text` | `input` |
| `voice` (preferred) or `speaker_id` | `voice` |
| `temperature` | `temperature` (default `0.7`) |

Default voice when omitted: `eng_female_1`.

## Validator probes

Validators may still POST the legacy miner shape `{text, speaker_id}` — the
miner remaps before proxying. Prefer `{input, voice}` in new code.

## Semantic validation (Cathedral Voice Brief 1)

Waveform sanity (`tts_quality`) alone is not sufficient for Cathedral Voice.
When a validator-controlled trusted ASR is configured
(`VALIDATOR_TRUSTED_ASR_URL`), the probe path is:

1. Private / holdout prompt → miner TTS
2. Waveform sanity on returned PCM
3. Trusted ASR (not the miner under test) back-transcribes the audio
4. WER/CER vs the prompt → `tts_semantic_score`

Missing or empty trusted ASR hypotheses **fail closed** when semantic scoring
is required. See `violet.validator.metrics.tts_semantic_score` and
`violet.validator.trusted_asr`.

Private TTS holdout prompts: `VALIDATOR_TTS_HOLDOUT_PATH` (JSON list). Do not
log full holdout prompts on miner-visible channels.
