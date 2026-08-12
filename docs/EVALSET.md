# Evaluation corpus standard (cathedral-voice / Violet)

How validators measure **Quality (Q)** for ASR/TTS miners.

This document is the operator standard that matches the security / incentive
review: real WER needs real audio; a public corpus can be overfitted; rotate.

---

## Why the built-in set is not enough

`violet/evalset/manifest.json` (``violet-builtin-v1``) ships **without audio
files**. The validator synthesises tones so health/latency probes still run, but
**WER is not measurable**. Production Quality scoring requires a corpus with
``audio_path`` files on disk.

---

## Standard corpus: Sunbird / SALT

**Dataset:** [Sunbird/salt](https://huggingface.co/datasets/Sunbird/salt)  
**Use for ASR:** ``multispeaker-{lang}`` **test** split only  
**Languages (aligned with Avoices East Africa):** `eng`, `lug`, `ach`, `lgg`, `teo`, `nyn`

| Piece | Role |
|-------|------|
| Scored set | `data/evalset/salt/manifest.json` + `audio/` → real WER |
| Holdout | `data/evalset/salt-holdout/` → keep offline; rotate in later |
| TTS prompts | Shipped in the same manifest (miner synthesises; no SALT studio audio required for v1) |

SALT multispeaker audio is **natural / field** speech — appropriate for ASR
probes that should look like product traffic.

---

## Build (one-time per validator host)

```bash
cd violet-subnet
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[eval,chain,dev]"

python scripts/build_salt_evalset.py \
  --out ./data/evalset/salt \
  --per-lang 20 \
  --seed 39 \
  --holdout-fraction 0.2
```

Then:

```bash
export VALIDATOR_EVALSET_PATH=./data/evalset/salt
# or absolute: /var/violet/evalset/salt
```

Rebuild with a new ``--seed`` when you rotate the public slice (TDD 9.3).

---

## Anti-overfitting (from the security review)

SALT is **public**. Miners can download it. Controls:

1. **Holdout** — builder writes a private holdout manifest; do not publish it.
2. **Block-seeded rotation** — each eval round scores a rotating subset
   (`violet.evalset.rotate_asr`), so order is not learnable from one round.
3. **Periodic rebuild** — change `--seed` / refresh holdout on a schedule.
4. **Optional private clips** — add Avoices-owned WAVs to the same manifest
   format when you have them; never rely on SALT alone forever.

See also [SECURITY.md](./SECURITY.md) (“Evaluation corpus overfitting”).

---

## Manifest format

```json
{
  "name": "violet-salt-v1",
  "asr": [
    {
      "id": "salt-lug-00012",
      "language": "lug",
      "reference": "…",
      "audio_path": "audio/lug/salt-lug-00012.wav",
      "duration_s": 4.2
    }
  ],
  "tts": [
    {
      "id": "tts-lug-001",
      "language": "lug",
      "text": "…",
      "speaker_id": "lug_female_1"
    }
  ]
}
```

Loader: `violet.evalset.load_evalset` (env ``VALIDATOR_EVALSET_PATH``).

---

## Checklist

- [ ] `.[eval]` installed; `build_salt_evalset.py` succeeded
- [ ] `VALIDATOR_EVALSET_PATH` points at the salt directory (has `manifest.json` + `audio/`)
- [ ] Dashboard / logs: evalset is **not** `synthetic_only`
- [ ] Holdout directory is **not** world-readable / not in the public image
- [ ] Dry-run validator against a known-good miner; WER fields populate on ASR probes
