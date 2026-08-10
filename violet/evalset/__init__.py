"""
The evaluation corpus validators score miners against.

TDD 12 names "reliance on a relatively small initial set of evaluation
utterances" as a known limitation, and TDD 9.3 calls for "regular rotation and
expansion of the evaluation set". Two things follow, and both are implemented
here:

* the corpus is **loaded from disk**, not hard-coded, so it can be rotated
  without shipping a new validator release;
* each evaluation round draws a **rotating subset**, seeded by block height, so
  a miner cannot learn which utterances are scored by observing past rounds.

A small built-in set ships so a validator works out of the box. Operators
running real evaluation should point ``VALIDATOR_EVALSET_PATH`` at a private,
periodically refreshed corpus - a public corpus is, by construction, a corpus
that can be overfitted.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import struct
import wave
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger("violet.evalset")

BUILTIN_MANIFEST = Path(__file__).parent / "manifest.json"


@dataclass
class AsrItem:
    """One ASR evaluation utterance."""

    id: str
    language: str
    reference: str
    #: Path to an audio file, relative to the manifest. When absent, audio is
    #: synthesised deterministically from ``id`` (see ``audio_bytes``).
    audio_path: Optional[str] = None
    duration_s: float = 3.0
    _root: Optional[Path] = field(default=None, repr=False)

    def audio_bytes(self) -> bytes:
        if self.audio_path and self._root:
            candidate = (self._root / self.audio_path).resolve()
            if candidate.is_file():
                return candidate.read_bytes()
            logger.warning(
                "eval item %s references missing audio %s; falling back to "
                "synthetic audio, which cannot measure real WER",
                self.id,
                candidate,
            )
        return synthetic_wav(self.id, self.duration_s)


@dataclass
class TtsItem:
    """One TTS evaluation prompt."""

    id: str
    language: str
    text: str
    speaker_id: str


@dataclass
class EvalSet:
    """A loaded corpus."""

    asr: List[AsrItem] = field(default_factory=list)
    tts: List[TtsItem] = field(default_factory=list)
    name: str = "builtin"
    #: Set when every ASR item falls back to synthetic audio, which means WER
    #: is not measuring transcription accuracy. Surfaced loudly by the validator.
    synthetic_only: bool = True

    def rotate_asr(self, seed: int, count: int) -> List[AsrItem]:
        return _rotate(self.asr, seed, count)

    def rotate_tts(self, seed: int, count: int) -> List[TtsItem]:
        return _rotate(self.tts, seed, count)


def _rotate(items: Sequence, seed: int, count: int) -> List:
    """Deterministic pseudo-random subset.

    Deterministic so every validator in a round scores the same utterances -
    otherwise honest validators would diverge from consensus purely through
    sampling noise, and TDD 5 penalises divergence.
    """
    if not items:
        return []
    count = max(1, min(count, len(items)))
    digest = hashlib.sha256(str(seed).encode()).digest()
    offset = struct.unpack("<I", digest[:4])[0] % len(items)
    ordered = list(items[offset:]) + list(items[:offset])
    stride = max(1, len(items) // count)
    return ordered[::stride][:count]


def synthetic_wav(seed_text: str, duration_s: float = 3.0, sample_rate: int = 16000) -> bytes:
    """Deterministic speech-shaped WAV, derived from ``seed_text``.

    Not speech, and it cannot measure transcription accuracy. It exists so the
    availability, latency, streaming and resource-accuracy tests can run against
    a validator that has no licensed audio corpus on disk.
    """
    digest = hashlib.sha256(seed_text.encode()).digest()
    fundamental = 90.0 + (digest[0] / 255.0) * 60.0
    formant = 400.0 + (digest[1] / 255.0) * 900.0

    frames = int(sample_rate * duration_s)
    samples = []
    for index in range(frames):
        t = index / sample_rate
        # Sum of a fundamental and a formant, amplitude-modulated at a
        # syllable-like 4 Hz so energy is not uniform.
        envelope = 0.5 + 0.4 * math.sin(2 * math.pi * 4.0 * t)
        value = (
            0.6 * math.sin(2 * math.pi * fundamental * t)
            + 0.3 * math.sin(2 * math.pi * formant * t)
        ) * envelope
        samples.append(int(max(-1.0, min(1.0, value)) * 24000))

    buffer = BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return buffer.getvalue()


def load_evalset(path: Optional[str] = None) -> EvalSet:
    """Load a corpus from a manifest, falling back to the built-in one."""
    manifest_path = Path(path).expanduser() if path else BUILTIN_MANIFEST
    if manifest_path.is_dir():
        manifest_path = manifest_path / "manifest.json"

    if not manifest_path.is_file():
        logger.warning(
            "evaluation manifest %s not found; using the built-in set", manifest_path
        )
        manifest_path = BUILTIN_MANIFEST

    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("could not read %s (%s); using the built-in set", manifest_path, exc)
        payload = json.loads(BUILTIN_MANIFEST.read_text(encoding="utf-8"))
        manifest_path = BUILTIN_MANIFEST

    root = manifest_path.parent
    asr_items: List[AsrItem] = []
    for raw in payload.get("asr", []):
        asr_items.append(
            AsrItem(
                id=str(raw["id"]),
                language=str(raw.get("language", "eng")),
                reference=str(raw.get("reference", "")),
                audio_path=raw.get("audio_path"),
                duration_s=float(raw.get("duration_s", 3.0)),
                _root=root,
            )
        )

    tts_items = [
        TtsItem(
            id=str(raw["id"]),
            language=str(raw.get("language", "en")),
            text=str(raw["text"]),
            speaker_id=str(raw.get("speaker_id", "eng_female_1")),
        )
        for raw in payload.get("tts", [])
    ]

    has_real_audio = any(
        item.audio_path and (root / item.audio_path).is_file() for item in asr_items
    )
    evalset = EvalSet(
        asr=asr_items,
        tts=tts_items,
        name=str(payload.get("name", manifest_path.parent.name)),
        synthetic_only=not has_real_audio,
    )

    if evalset.synthetic_only:
        logger.warning(
            "evaluation set '%s' has no real audio on disk. ASR word error rate "
            "cannot be measured, so the Quality component will be derived from "
            "availability and latency alone. Point VALIDATOR_EVALSET_PATH at a "
            "corpus with audio before treating Quality scores as meaningful.",
            evalset.name,
        )

    logger.info(
        "loaded evaluation set '%s': %d ASR items, %d TTS items",
        evalset.name,
        len(evalset.asr),
        len(evalset.tts),
    )
    return evalset


def language_coverage(evalset: EvalSet) -> Dict[str, int]:
    """Item count per language, for the dashboard and for corpus-gap reporting."""
    counts: Dict[str, int] = {}
    for item in evalset.asr:
        counts[item.language] = counts.get(item.language, 0) + 1
    for item in evalset.tts:
        counts[item.language] = counts.get(item.language, 0) + 1
    return dict(sorted(counts.items()))
