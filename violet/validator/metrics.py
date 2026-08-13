"""
Quality measurement for ASR and TTS responses.

Deliberately dependency-free: a validator must be cheap to run and must not need
a GPU or a model download to score the network. Everything here is either an
edit-distance computation or a signal-level check on raw PCM.

Honest limits
-------------
TDD 7 defines Quality as "ASR word error rate and TTS naturalness together with
content fidelity". WER is measured properly. Naturalness is *not* measured by a
MOS model here - doing so would require a neural predictor in every validator,
which contradicts the cheap-validator goal and would itself become the thing
miners overfit to. Instead this module measures the signal-level properties that
a broken or faked TTS response fails: audio length proportional to text, energy
distribution, silence ratio, and clipping.

Cathedral Voice Brief 1 adds ``tts_semantic_score``: when a validator-owned
trusted ASR is configured, TTS audio is back-transcribed and fused with the
waveform score (fail closed if the hypothesis is missing).
"""

from __future__ import annotations

import array
import math
import re
import struct
import unicodedata
from dataclasses import dataclass
from typing import List, Optional, Sequence

# --------------------------------------------------------------------------
# Text normalisation and WER
# --------------------------------------------------------------------------

_WHITESPACE = re.compile(r"\s+")

#: Unicode categories kept during normalisation.
#: ``L*`` letters, ``N*`` numbers, ``M*`` combining marks. The marks matter:
#: ``\w`` does not match a combining grave accent, so a regex-based stripper
#: silently deletes tone marks from Yoruba and vowel marks from Amharic -
#: mangling exactly the languages Violet exists to serve, and inflating their
#: word error rate against miners that transcribed them correctly.
_KEEP_CATEGORIES = ("L", "N", "M")
_KEEP_CHARS = frozenset("'’")


def normalize_text(text: str) -> str:
    """Casefold, strip punctuation and collapse whitespace.

    Composes to NFC first so that "ọ̀" compares equal whether a miner emitted it
    precomposed or as a base character plus combining marks.
    """
    if not text:
        return ""

    composed = unicodedata.normalize("NFC", text)
    kept = []
    for char in composed:
        if char.isspace():
            kept.append(" ")
        elif char in _KEEP_CHARS:
            kept.append(char)
        elif unicodedata.category(char)[0] in _KEEP_CATEGORIES:
            kept.append(char)
        else:
            kept.append(" ")

    lowered = "".join(kept).casefold()
    return _WHITESPACE.sub(" ", lowered).strip()


def tokenize(text: str) -> List[str]:
    normalized = normalize_text(text)
    return normalized.split() if normalized else []


def levenshtein(reference: Sequence[str], hypothesis: Sequence[str]) -> int:
    """Edit distance with O(min(n, m)) memory.

    Transcripts of a 170-minute video run to thousands of tokens; the full
    matrix would be tens of megabytes per comparison.
    """
    if not reference:
        return len(hypothesis)
    if not hypothesis:
        return len(reference)

    if len(hypothesis) < len(reference):
        reference, hypothesis = hypothesis, reference

    previous = list(range(len(reference) + 1))
    for j, hyp_token in enumerate(hypothesis, start=1):
        current = [j]
        for i, ref_token in enumerate(reference, start=1):
            cost = 0 if ref_token == hyp_token else 1
            current.append(
                min(
                    previous[i] + 1,       # deletion
                    current[i - 1] + 1,    # insertion
                    previous[i - 1] + cost # substitution
                )
            )
        previous = current
    return previous[-1]


def word_error_rate(reference: str, hypothesis: str) -> float:
    """WER in ``[0, 1]``, clamped.

    Clamped because insertions can push raw WER above 1, and an unclamped value
    would let one pathological response dominate a miner's rolling average.
    """
    ref_tokens = tokenize(reference)
    hyp_tokens = tokenize(hypothesis)

    if not ref_tokens:
        # No reference to score against; treat a non-empty hypothesis as
        # unmeasurable rather than perfect or failed.
        return 0.0 if not hyp_tokens else 1.0

    distance = levenshtein(ref_tokens, hyp_tokens)
    return min(1.0, distance / len(ref_tokens))


def character_error_rate(reference: str, hypothesis: str) -> float:
    """CER, used as a secondary signal for agglutinative languages.

    Luganda and Kinyarwanda pack morphemes into single orthographic words, so a
    one-morpheme error costs a whole word under WER. CER is the fairer read for
    those languages, and the two are combined in ``asr_quality``.
    """
    ref = normalize_text(reference).replace(" ", "")
    hyp = normalize_text(hypothesis).replace(" ", "")
    if not ref:
        return 0.0 if not hyp else 1.0
    return min(1.0, levenshtein(list(ref), list(hyp)) / len(ref))


def asr_quality(reference: str, hypothesis: str) -> float:
    """Quality in ``[0, 1]`` from a reference/hypothesis pair.

    Weighted toward WER, with CER softening the penalty on agglutinative
    languages where a near-miss is scored as a total miss by WER alone.
    """
    wer = word_error_rate(reference, hypothesis)
    cer = character_error_rate(reference, hypothesis)
    combined = 0.7 * wer + 0.3 * cer
    return max(0.0, 1.0 - combined)


def tts_semantic_score(
    reference_text: str,
    hypothesis_text: Optional[str],
    waveform_score: float,
    *,
    require_hypothesis: bool = True,
    waveform_weight: float = 0.35,
) -> tuple[float, str]:
    """Combine waveform sanity with trusted-ASR back-transcription fidelity.

    Cathedral Voice Brief 1: waveform checks alone do not prove the miner spoke
    the prompt. When ``require_hypothesis`` is true (fail-closed mode), a
    missing or empty hypothesis zeros the score even if the waveform looks fine.
    """
    wave = max(0.0, min(1.0, float(waveform_score)))
    if require_hypothesis and not (hypothesis_text or "").strip():
        return 0.0, "semantic fail-closed: missing trusted ASR hypothesis"

    if not (hypothesis_text or "").strip():
        return wave, "waveform only (no hypothesis)"

    semantic = asr_quality(reference_text, hypothesis_text or "")
    wer = word_error_rate(reference_text, hypothesis_text or "")
    cer = character_error_rate(reference_text, hypothesis_text or "")
    w = max(0.0, min(1.0, float(waveform_weight)))
    fused = max(0.0, min(1.0, w * wave + (1.0 - w) * semantic))
    note = f"semantic wer={wer:.3f} cer={cer:.3f}; wave={wave:.3f}"
    return fused, note


# --------------------------------------------------------------------------
# Audio analysis
# --------------------------------------------------------------------------


@dataclass
class AudioStats:
    """Signal-level properties of a PCM buffer."""

    duration_s: float
    rms: float
    peak: float
    silence_ratio: float
    clipping_ratio: float
    sample_count: int

    @property
    def is_silent(self) -> bool:
        return self.rms < 0.005 or self.silence_ratio > 0.95


def analyze_pcm(
    payload: bytes,
    *,
    sample_rate: int = 24000,
    sample_width: int = 2,
    channels: int = 1,
) -> Optional[AudioStats]:
    """Compute signal statistics for a raw PCM buffer.

    Returns ``None`` when the payload is too short or the width unsupported,
    which the caller treats as a failed response rather than silent audio.
    """
    if sample_width != 2 or not payload:
        return None

    usable = len(payload) - (len(payload) % 2)
    if usable < 2:
        return None

    samples = array.array("h")
    samples.frombytes(payload[:usable])
    if not samples:
        return None

    frames = len(samples) // max(1, channels)
    duration_s = frames / float(sample_rate) if sample_rate else 0.0

    total_square = 0.0
    peak = 0
    clipped = 0
    # Window the signal to measure silence the way a listener perceives it -
    # a 20 ms window that is near-zero is a pause, an individual zero sample is
    # just a zero crossing.
    window = max(1, int(sample_rate * 0.02))
    silent_windows = 0
    window_count = 0
    window_square = 0.0
    window_index = 0

    for sample in samples:
        magnitude = abs(sample)
        total_square += float(sample) * float(sample)
        if magnitude > peak:
            peak = magnitude
        if magnitude >= 32700:
            clipped += 1

        window_square += float(sample) * float(sample)
        window_index += 1
        if window_index >= window:
            window_count += 1
            if math.sqrt(window_square / window_index) / 32768.0 < 0.01:
                silent_windows += 1
            window_square = 0.0
            window_index = 0

    if window_index:
        window_count += 1
        if math.sqrt(window_square / window_index) / 32768.0 < 0.01:
            silent_windows += 1

    rms = math.sqrt(total_square / len(samples)) / 32768.0
    return AudioStats(
        duration_s=duration_s,
        rms=rms,
        peak=peak / 32768.0,
        silence_ratio=(silent_windows / window_count) if window_count else 1.0,
        clipping_ratio=clipped / len(samples),
        sample_count=len(samples),
    )


def wav_to_pcm(payload: bytes) -> tuple[bytes, int, int, int]:
    """Strip a RIFF header if present, returning ``(pcm, rate, width, channels)``.

    Miners may return either raw PCM or a WAV, since both appear in the current
    Avoices code paths. Handled here so callers do not each reimplement it.
    """
    if len(payload) < 44 or payload[:4] != b"RIFF" or payload[8:12] != b"WAVE":
        return payload, 24000, 2, 1

    offset = 12
    rate, width, channels = 24000, 2, 1
    while offset + 8 <= len(payload):
        chunk_id = payload[offset : offset + 4]
        (chunk_size,) = struct.unpack("<I", payload[offset + 4 : offset + 8])
        body = offset + 8
        if chunk_id == b"fmt " and body + 16 <= len(payload):
            (_fmt, channels, rate, _byte_rate, _align, bits) = struct.unpack(
                "<HHIIHH", payload[body : body + 16]
            )
            width = max(1, bits // 8)
        elif chunk_id == b"data":
            return payload[body : body + chunk_size], rate, width, channels
        offset = body + chunk_size + (chunk_size % 2)

    return payload[44:], rate, width, channels


#: Speech sits roughly in this band. Outside it, the response is either a
#: truncated stub or padded with silence to look like real output.
MIN_SECONDS_PER_CHAR = 0.015
MAX_SECONDS_PER_CHAR = 0.30


def tts_quality(
    payload: bytes,
    text: str,
    *,
    sample_rate: int = 24000,
    sample_width: int = 2,
    channels: int = 1,
) -> tuple[float, str]:
    """Score a TTS response in ``[0, 1]`` and explain the score.

    This is content fidelity and signal sanity, not naturalness - see the module
    docstring. It answers "is this plausibly speech for this text", which is the
    question that separates a working miner from a broken or gaming one.
    """
    if not payload:
        return 0.0, "empty response"

    pcm, detected_rate, detected_width, detected_channels = wav_to_pcm(payload)
    stats = analyze_pcm(
        pcm,
        sample_rate=detected_rate if payload[:4] == b"RIFF" else sample_rate,
        sample_width=detected_width if payload[:4] == b"RIFF" else sample_width,
        channels=detected_channels if payload[:4] == b"RIFF" else channels,
    )
    if stats is None:
        return 0.0, "undecodable audio"
    if stats.is_silent:
        return 0.0, f"silent audio (rms={stats.rms:.4f})"

    char_count = max(1, len(text.strip()))
    seconds_per_char = stats.duration_s / char_count

    score = 1.0
    notes: List[str] = []

    if seconds_per_char < MIN_SECONDS_PER_CHAR:
        # Far too short for the text: the miner returned a stub.
        ratio = seconds_per_char / MIN_SECONDS_PER_CHAR
        score *= max(0.0, ratio)
        notes.append(f"audio too short for text ({stats.duration_s:.2f}s)")
    elif seconds_per_char > MAX_SECONDS_PER_CHAR:
        ratio = MAX_SECONDS_PER_CHAR / seconds_per_char
        score *= max(0.2, ratio)
        notes.append(f"audio too long for text ({stats.duration_s:.2f}s)")

    if stats.silence_ratio > 0.6:
        score *= 1.0 - (stats.silence_ratio - 0.6)
        notes.append(f"mostly silence ({stats.silence_ratio:.0%})")

    if stats.clipping_ratio > 0.01:
        score *= max(0.3, 1.0 - stats.clipping_ratio * 10)
        notes.append(f"clipping ({stats.clipping_ratio:.1%})")

    if stats.rms < 0.02:
        score *= 0.6
        notes.append(f"very low level (rms={stats.rms:.3f})")

    crest = stats.peak / max(stats.rms, 1e-6)
    if crest < 1.45:
        score *= 0.25
        notes.append(f"flat tone (crest={crest:.1f})")
    elif crest < 2.0:
        score *= 0.85
        notes.append(f"limited dynamics (crest={crest:.1f})")

    score = max(0.0, min(1.0, score))
    return score, "; ".join(notes) if notes else "ok"
