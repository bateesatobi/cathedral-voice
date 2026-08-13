"""Validator-owned trusted ASR for TTS semantic back-transcription.

The miner under test must never be the ASR used to score its own TTS output.
Configure a separate endpoint with ``VALIDATOR_TRUSTED_ASR_URL`` (same
``POST /transcribe`` contract as Violet miners).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import aiohttp

from ..evalset import TtsItem
from ..protocol import PATH_ASR_TRANSCRIBE

logger = logging.getLogger("violet.validator.trusted_asr")


@dataclass
class TrustedAsrConfig:
    """How the validator reaches its reference ASR."""

    url: str = ""
    token: str = ""
    timeout_s: float = 30.0
    #: When true, missing/empty hypotheses zero TTS quality.
    semantic_required: bool = False
    language: str = "eng"

    @property
    def enabled(self) -> bool:
        return bool(self.url.strip())

    @property
    def require_hypothesis(self) -> bool:
        if not self.enabled:
            return False
        return self.semantic_required


def trusted_asr_config_from_env() -> TrustedAsrConfig:
    def _bool(name: str, default: bool = False) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() in ("1", "true", "yes", "on")

    def _float(name: str, default: float) -> float:
        try:
            return float(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return default

    url = (os.getenv("VALIDATOR_TRUSTED_ASR_URL") or "").strip().rstrip("/")
    # Fail closed by default whenever a trusted ASR URL is configured.
    semantic_default = bool(url)
    return TrustedAsrConfig(
        url=url,
        token=(os.getenv("VALIDATOR_TRUSTED_ASR_TOKEN") or "").strip(),
        timeout_s=_float("VALIDATOR_TRUSTED_ASR_TIMEOUT_S", 30.0),
        semantic_required=_bool("VALIDATOR_TTS_SEMANTIC_REQUIRED", semantic_default),
        language=(os.getenv("VALIDATOR_TRUSTED_ASR_LANGUAGE") or "eng").strip() or "eng",
    )


def pcm_to_wav(
    pcm: bytes,
    *,
    sample_rate: int = 24000,
    sample_width: int = 2,
    channels: int = 1,
) -> bytes:
    """Wrap raw PCM in a minimal WAV container for multipart ASR upload."""
    import io
    import wave

    if pcm[:4] == b"RIFF":
        return pcm
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sample_width)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


async def transcribe_trusted(
    session: aiohttp.ClientSession,
    config: TrustedAsrConfig,
    audio: bytes,
    *,
    language: Optional[str] = None,
    filename: str = "tts_probe.wav",
    sample_rate: int = 24000,
    sample_width: int = 2,
    channels: int = 1,
) -> tuple[Optional[str], str]:
    """POST audio to the trusted ASR. Returns ``(hypothesis, detail)``.

    On any transport/parse failure returns ``(None, reason)`` so callers can
    fail closed.
    """
    if not config.enabled:
        return None, "trusted ASR not configured"

    wav = pcm_to_wav(
        audio, sample_rate=sample_rate, sample_width=sample_width, channels=channels
    )
    form = aiohttp.FormData()
    form.add_field("file", wav, filename=filename, content_type="audio/wav")
    form.add_field("language", language or config.language)
    form.add_field("response_format", "json")

    headers: Dict[str, str] = {}
    if config.token:
        headers["Authorization"] = f"Bearer {config.token}"

    url = config.url.rstrip("/") + PATH_ASR_TRANSCRIBE
    try:
        async with session.post(
            url,
            data=form,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=config.timeout_s),
        ) as response:
            body = await response.read()
            if response.status != 200:
                return None, f"trusted ASR HTTP {response.status}: {body[:120]!r}"
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception:
                text = body.decode("utf-8", errors="replace").strip()
                return (text or None), ("ok" if text else "empty trusted ASR body")
            if isinstance(data, dict):
                hyp = (
                    data.get("text")
                    or data.get("transcript")
                    or data.get("transcription")
                    or ""
                )
            else:
                hyp = str(data)
            hyp = (hyp or "").strip()
            if not hyp:
                return None, "empty trusted ASR hypothesis"
            return hyp, "ok"
    except Exception as exc:
        return None, f"trusted ASR request failed: {exc}"


def load_tts_holdout(path: str | Path) -> List[TtsItem]:
    """Load private TTS prompts from JSON (list or ``{tts: [...]}``).

    Never log full prompt text from this file on miner-visible channels.
    """
    root = Path(path)
    if not root.is_file():
        raise FileNotFoundError(f"TTS holdout not found: {root}")
    raw = json.loads(root.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        items = raw.get("tts") or raw.get("prompts") or []
    else:
        items = raw
    out: List[TtsItem] = []
    for i, entry in enumerate(items):
        if not isinstance(entry, dict):
            continue
        text = (entry.get("text") or entry.get("input") or "").strip()
        if not text:
            continue
        out.append(
            TtsItem(
                id=str(entry.get("id") or f"holdout-{i}"),
                language=str(entry.get("language") or "eng"),
                text=text,
                speaker_id=str(
                    entry.get("speaker_id") or entry.get("voice") or "eng_female_1"
                ),
            )
        )
    return out


def holdout_path_from_env() -> str:
    return (os.getenv("VALIDATOR_TTS_HOLDOUT_PATH") or "").strip()


def rotate_holdout(items: Sequence[TtsItem], seed: int, count: int) -> List[TtsItem]:
    """Deterministic rotating subset (same idea as evalset.rotate_*)."""
    if not items or count <= 0:
        return []
    n = len(items)
    start = abs(int(seed)) % n
    return [items[(start + i) % n] for i in range(min(count, n))]


def redact_prompt(text: str, *, keep: int = 12) -> str:
    """Safe snippet for logs — never the full private holdout prompt."""
    t = (text or "").strip()
    if len(t) <= keep:
        return "<redacted>"
    return t[:keep] + "…"
