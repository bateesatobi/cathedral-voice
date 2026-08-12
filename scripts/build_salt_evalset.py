#!/usr/bin/env python3
"""
Build the standard Violet ASR evalset from Hugging Face ``Sunbird/salt``.

This is the **canonical Quality corpus** for cathedral-voice validators:

* ASR uses ``multispeaker-{lang}`` **test** split (natural speech → real WER)
* Languages match Avoices / Violet East-Africa coverage
* Block-seeded rotation still happens at probe time (see ``violet.evalset``)
* A separate holdout slice is written for operators to keep offline/private

Usage::

    pip install -e ".[eval]"
    python scripts/build_salt_evalset.py --out ./data/evalset/salt

Then::

    export VALIDATOR_EVALSET_PATH=./data/evalset/salt

Attribution: Sunbird AI SALT — https://huggingface.co/datasets/Sunbird/salt
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("build_salt_evalset")

# Multispeaker ASR subsets available on Sunbird/salt (ISO-ish codes we use).
SALT_MULTISPEAKER_LANGS = ("eng", "lug", "ach", "lgg", "teo", "nyn")

# TTS prompts aligned to Violet speakers (studio-quality text; audio is miner-side).
DEFAULT_TTS: List[Dict[str, str]] = [
    {
        "id": "tts-eng-001",
        "language": "en",
        "text": "Violet supplies decentralized speech inference for the Avoices platform.",
        "speaker_id": "eng_female_1",
    },
    {
        "id": "tts-eng-002",
        "language": "en",
        "text": "Please synthesize this sentence clearly and at a natural pace.",
        "speaker_id": "eng_male_1",
    },
    {
        "id": "tts-lug-001",
        "language": "lug",
        "text": "Tukwaniriza ku mukutu gwa Avoices, tusanyuse nnyo okukulaba.",
        "speaker_id": "lug_female_1",
    },
    {
        "id": "tts-ach-001",
        "language": "ach",
        "text": "Itye nining? Wan waco ni apwoyo matek pi tic maber.",
        "speaker_id": "ach_female_1",
    },
    {
        "id": "tts-nyn-001",
        "language": "nyn",
        "text": "Agandi, ndi kurungi webale munonga okujaho.",
        "speaker_id": "nyn_female_248",
    },
    {
        "id": "tts-teo-001",
        "language": "teo",
        "text": "Yoga noi, eong ajokus akwap kere.",
        "speaker_id": "teo_female_241",
    },
]


def _pick_text(row: Dict[str, Any]) -> str:
    for key in ("text", "transcription", "sentence", "utterance", "transcript"):
        val = row.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    # Nested translations
    for key in ("translations", "translation"):
        val = row.get(key)
        if isinstance(val, dict):
            for sub in val.values():
                if isinstance(sub, str) and sub.strip():
                    return sub.strip()
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _audio_array(row: Dict[str, Any]) -> Tuple[Any, int]:
    audio = row.get("audio")
    if isinstance(audio, dict):
        arr = audio.get("array")
        sr = int(audio.get("sampling_rate") or audio.get("sample_rate") or 16000)
        if arr is not None:
            return arr, sr
    sr = int(row.get("sample_rate") or row.get("sampling_rate") or 16000)
    if row.get("audio") is not None and not isinstance(row.get("audio"), dict):
        return row["audio"], sr
    raise ValueError("row has no usable audio array")


def _write_wav_mono16(path: Path, samples, sample_rate: int) -> float:
    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    arr = np.asarray(samples, dtype=np.float64)
    if arr.ndim > 1:
        arr = arr.mean(axis=-1)
    # Resample lightly if needed (naive; prefer original 16k)
    if sample_rate != 16000 and sample_rate > 0:
        duration = len(arr) / float(sample_rate)
        target_len = max(1, int(round(duration * 16000)))
        x_old = np.linspace(0.0, 1.0, num=len(arr), endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=target_len, endpoint=False)
        arr = np.interp(x_new, x_old, arr)
        sample_rate = 16000
    peak = float(np.max(np.abs(arr))) if len(arr) else 1.0
    if peak < 1e-9:
        peak = 1.0
    pcm = (arr / peak * 0.9 * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())
    return len(pcm) / float(sample_rate)


def _load_split(config_name: str, split: str):
    from datasets import load_dataset

    return load_dataset("Sunbird/salt", config_name, split=split)


def build_asr_items(
    *,
    langs: Sequence[str],
    per_lang: int,
    seed: int,
    out_audio: Path,
    holdout_fraction: float,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    scored: List[Dict[str, Any]] = []
    holdout: List[Dict[str, Any]] = []
    rng = random.Random(seed)

    for lang in langs:
        config = f"multispeaker-{lang}"
        logger.info("Loading %s …", config)
        try:
            ds = _load_split(config, "test")
        except Exception as exc:
            logger.warning("Skipping %s (%s)", config, exc)
            continue

        indices = list(range(len(ds)))
        rng.shuffle(indices)
        take = indices[: max(per_lang * 2, per_lang)]  # extra for holdout split
        selected = take[:per_lang]
        hold_n = max(0, int(round(per_lang * holdout_fraction)))
        hold_idx = set(selected[:hold_n]) if hold_n else set()
        score_idx = [i for i in selected if i not in hold_idx]
        # If holdout ate everything, keep at least half for scoring
        if not score_idx and selected:
            mid = max(1, len(selected) // 2)
            score_idx = selected[mid:]
            hold_idx = set(selected[:mid])

        for rank, di in enumerate(selected):
            row = ds[int(di)]
            if hasattr(row, "keys"):
                row = dict(row)
            text = _pick_text(row)
            if not text:
                logger.debug("No text for %s[%s]; skip", lang, di)
                continue
            try:
                samples, sr = _audio_array(row)
            except Exception as exc:
                logger.debug("No audio for %s[%s]: %s", lang, di, exc)
                continue

            item_id = f"salt-{lang}-{di:05d}"
            rel = f"audio/{lang}/{item_id}.wav"
            abs_path = out_audio.parent / rel
            try:
                duration_s = _write_wav_mono16(abs_path, samples, sr)
            except Exception as exc:
                logger.warning("Failed to write %s: %s", abs_path, exc)
                continue

            entry = {
                "id": item_id,
                "language": lang,
                "reference": text,
                "audio_path": rel,
                "duration_s": round(float(duration_s), 3),
                "source": "Sunbird/salt",
                "subset": config,
                "split": "test",
            }
            if di in hold_idx:
                holdout.append(entry)
            else:
                scored.append(entry)

        logger.info(
            "%s: scored=%d holdout=%d",
            lang,
            sum(1 for e in scored if e["language"] == lang),
            sum(1 for e in holdout if e["language"] == lang),
        )

    return scored, holdout


def write_manifest(
    path: Path,
    *,
    name: str,
    asr: List[Dict[str, Any]],
    tts: List[Dict[str, Any]],
    notes: List[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": name,
        "notes": notes,
        "asr": asr,
        "tts": tts,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info("Wrote %s (%d ASR, %d TTS)", path, len(asr), len(tts))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("./data/evalset/salt"),
        help="Output directory (manifest.json + audio/)",
    )
    parser.add_argument(
        "--langs",
        default=",".join(SALT_MULTISPEAKER_LANGS),
        help="Comma-separated language codes",
    )
    parser.add_argument("--per-lang", type=int, default=20, help="Clips per language (test split)")
    parser.add_argument("--seed", type=int, default=39, help="Deterministic sample seed")
    parser.add_argument(
        "--holdout-fraction",
        type=float,
        default=0.2,
        help="Fraction of selected clips kept offline as holdout (anti-overfit)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        import datasets  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        logger.error('Missing deps. Install with: pip install -e ".[eval]"')
        return 1

    langs = [x.strip() for x in args.langs.split(",") if x.strip()]
    out: Path = args.out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    asr, holdout = build_asr_items(
        langs=langs,
        per_lang=max(1, args.per_lang),
        seed=args.seed,
        out_audio=out / "audio",
        holdout_fraction=max(0.0, min(0.5, args.holdout_fraction)),
    )
    if not asr:
        logger.error("No ASR items built — check network / Hugging Face access")
        return 2

    notes = [
        "Standard Violet Quality corpus built from Sunbird/salt multispeaker test audio.",
        "Real audio enables WER scoring. Block-seeded rotation still applies at probe time.",
        "Do NOT publish the holdout manifest. Keep holdout offline and rotate periodically (TDD 9.3).",
        "Attribution: Sunbird AI SALT — https://huggingface.co/datasets/Sunbird/salt",
        f"Builder seed={args.seed} per_lang={args.per_lang} langs={','.join(langs)}",
    ]
    write_manifest(
        out / "manifest.json",
        name="violet-salt-v1",
        asr=asr,
        tts=DEFAULT_TTS,
        notes=notes,
    )
    if holdout:
        hold_dir = out.parent / "salt-holdout"
        hold_dir.mkdir(parents=True, exist_ok=True)
        fixed = []
        for item in holdout:
            fixed.append({**item, "audio_path": f"../salt/{item['audio_path']}"})
        write_manifest(
            hold_dir / "manifest.json",
            name="violet-salt-holdout-v1",
            asr=fixed,
            tts=[],
            notes=[
                "PRIVATE holdout — do not ship in the public validator image.",
                "Audio files live under ../salt/audio/ (relative paths in this manifest).",
                "Use for periodic rotation against overfitting the public SALT test set.",
            ],
        )

    print(
        f"\nStandard evalset ready.\n"
        f"  export VALIDATOR_EVALSET_PATH={out}\n"
        f"  ASR items: {len(asr)}  holdout: {len(holdout)}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
