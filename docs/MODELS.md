# Model licensing and official images

Violet miners run **official ASR/TTS containers** behind the sidecar. Operators
must comply with each upstream model's license and terms.

## Official stack (v1.4)

| Service | Image | License / terms |
|---------|-------|-----------------|
| ASR (etoil-api) | `simonallanachuka/etoil-api` | Follow upstream repo terms |
| ASR (speaches) | `ghcr.io/speaches-ai/speaches:latest-cuda` | Apache-2.0 (check pinned tag) |
| TTS (Spark) | `ghcr.io/cathedral-voice/spark-tts` | Follow Cathedral Voice release notes |

## Operator obligations

1. Set `MINER_ASR_IMAGE` and `MINER_TTS_IMAGE` to the digest or tag you run.
2. Do not substitute unreviewed weights inside official images without updating
   your on-chain announcement.
3. Validators compare declared digests against `violet/releases/manifest.json`.
   Populate `allowed_digests` when pinning a release.

## HF access

Speech models may require a Hugging Face token (`HF_TOKEN`). Use a **scoped,
rotatable** token — never commit tokens to git.
