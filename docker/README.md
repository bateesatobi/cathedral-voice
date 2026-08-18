# Docker assets

Sidecar compose only — **ASR and TTS run on the host** via [violet/miner/stt_install.sh](../violet/miner/stt_install.sh) and [tts_install.sh](../violet/miner/tts_install.sh).

| File | Purpose |
|------|---------|
| `docker-compose.miner.yml` | Miner sidecar (base) |
| `docker-compose.miner.prod.yml` | Production overrides (wallet volume, etc.) |
| `docker-compose.miner.gpu.yml` | GPU visibility for sidecar `/capacity` |
| `Dockerfile.miner` | Sidecar image build |
| `mock-*` | **pytest only** — not used in production install |

**Miner docs:** [violet/miner/README.md](../violet/miner/README.md) · [docs/MINER_GUIDE.md](../docs/MINER_GUIDE.md)
