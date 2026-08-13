"""TTS semantic scoring + trusted ASR fail-closed behaviour."""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from violet.evalset import TtsItem
from violet.validator.metrics import tts_quality, tts_semantic_score
from violet.validator.trusted_asr import (
    TrustedAsrConfig,
    load_tts_holdout,
    pcm_to_wav,
    redact_prompt,
    rotate_holdout,
    transcribe_trusted,
)


def make_pcm(seconds: float, *, rate: int = 24000, amplitude: float = 0.5) -> bytes:
    frames = int(rate * seconds)
    samples = [
        int(
            math.sin(2 * math.pi * 220 * i / rate)
            * amplitude
            * (0.35 + 0.65 * abs(math.sin(2 * math.pi * 5 * i / rate)))
            * 32767
        )
        for i in range(frames)
    ]
    return struct.pack(f"<{len(samples)}h", *samples)


class TestTtsSemanticScore:
    def test_exact_match_high(self):
        wave = 0.9
        score, note = tts_semantic_score("hello world", "hello world", wave)
        assert score > 0.85
        assert "wer=0.000" in note

    def test_mismatch_low(self):
        score, _ = tts_semantic_score(
            "the quick brown fox", "totally different words here", 1.0
        )
        assert score < 0.4

    def test_fail_closed_missing_hypothesis(self):
        score, note = tts_semantic_score("hello", None, 1.0, require_hypothesis=True)
        assert score == 0.0
        assert "fail-closed" in note

    def test_fail_closed_empty_hypothesis(self):
        score, note = tts_semantic_score("hello", "  ", 1.0, require_hypothesis=True)
        assert score == 0.0
        assert "fail-closed" in note

    def test_waveform_only_when_not_required(self):
        score, note = tts_semantic_score("hello", None, 0.7, require_hypothesis=False)
        assert score == 0.7
        assert "waveform only" in note

    def test_fuses_waveform_and_semantic(self):
        # Perfect transcript but weak waveform still pulls score down.
        high, _ = tts_semantic_score("hello world", "hello world", 1.0, waveform_weight=0.35)
        low, _ = tts_semantic_score("hello world", "hello world", 0.2, waveform_weight=0.35)
        assert high > low


class TestHoldoutLoader:
    def test_load_list_and_rotate(self, tmp_path: Path):
        path = tmp_path / "holdout.json"
        path.write_text(
            json.dumps(
                [
                    {"id": "a", "text": "one", "speaker_id": "eng_female_1"},
                    {"id": "b", "input": "two", "voice": "eng_male_1", "language": "eng"},
                ]
            ),
            encoding="utf-8",
        )
        items = load_tts_holdout(path)
        assert len(items) == 2
        assert items[0].text == "one"
        assert items[1].text == "two"
        assert items[1].speaker_id == "eng_male_1"
        rotated = rotate_holdout(items, seed=1, count=1)
        assert len(rotated) == 1

    def test_redact_prompt(self):
        assert redact_prompt("short") == "<redacted>"
        assert redact_prompt("this is a long private holdout prompt").endswith("…")


@pytest.mark.asyncio
async def test_transcribe_trusted_parses_json():
    config = TrustedAsrConfig(url="http://trusted-asr.local", semantic_required=True)
    session = MagicMock()

    response = AsyncMock()
    response.status = 200
    response.read = AsyncMock(return_value=b'{"text": "hello world"}')

    post_cm = MagicMock()
    post_cm.__aenter__ = AsyncMock(return_value=response)
    post_cm.__aexit__ = AsyncMock(return_value=None)
    session.post = MagicMock(return_value=post_cm)

    hyp, detail = await transcribe_trusted(
        session, config, make_pcm(0.5), language="eng"
    )
    assert hyp == "hello world"
    assert detail == "ok"
    # Ensure we wrapped PCM as WAV for multipart.
    assert session.post.called


@pytest.mark.asyncio
async def test_transcribe_trusted_fail_closed_on_http_error():
    config = TrustedAsrConfig(url="http://trusted-asr.local", semantic_required=True)
    session = MagicMock()
    response = AsyncMock()
    response.status = 500
    response.read = AsyncMock(return_value=b"boom")
    post_cm = MagicMock()
    post_cm.__aenter__ = AsyncMock(return_value=response)
    post_cm.__aexit__ = AsyncMock(return_value=None)
    session.post = MagicMock(return_value=post_cm)

    hyp, detail = await transcribe_trusted(session, config, make_pcm(0.3))
    assert hyp is None
    assert "HTTP 500" in detail


@pytest.mark.asyncio
async def test_score_tts_audio_integration_mocked_asr():
    """Miner audio + mocked trusted ASR → semantic quality with WER."""
    from violet.validator.probes import MinerProbe

    pcm = make_pcm(1.2)
    wave, _ = tts_quality(pcm, "hello world")
    assert wave > 0.0

    config = TrustedAsrConfig(
        url="http://trusted-asr.local",
        semantic_required=True,
        language="eng",
    )
    session = MagicMock()
    response = AsyncMock()
    response.status = 200
    response.read = AsyncMock(return_value=b'{"text": "hello world"}')
    post_cm = MagicMock()
    post_cm.__aenter__ = AsyncMock(return_value=response)
    post_cm.__aexit__ = AsyncMock(return_value=None)
    session.post = MagicMock(return_value=post_cm)

    probe = MinerProbe(session, "http://miner.local", trusted_asr=config)
    item = TtsItem(id="t1", language="eng", text="hello world", speaker_id="eng_female_1")
    result = await probe._score_tts_audio(
        item,
        pcm,
        kind="tts",
        latency_ms=10.0,
        first_byte_ms=5.0,
    )
    assert result.ok
    assert result.quality and result.quality > 0.7
    assert result.wer == 0.0
    assert result.payload["semantic"] is True


@pytest.mark.asyncio
async def test_score_tts_audio_fail_closed_when_asr_missing():
    from violet.validator.probes import MinerProbe

    pcm = make_pcm(1.0)
    config = TrustedAsrConfig(url="http://trusted-asr.local", semantic_required=True)
    session = MagicMock()
    response = AsyncMock()
    response.status = 503
    response.read = AsyncMock(return_value=b"down")
    post_cm = MagicMock()
    post_cm.__aenter__ = AsyncMock(return_value=response)
    post_cm.__aexit__ = AsyncMock(return_value=None)
    session.post = MagicMock(return_value=post_cm)

    probe = MinerProbe(session, "http://miner.local", trusted_asr=config)
    item = TtsItem(id="t1", language="eng", text="hello world", speaker_id="eng_female_1")
    result = await probe._score_tts_audio(
        item, pcm, kind="tts", latency_ms=10.0, first_byte_ms=5.0
    )
    assert not result.ok
    assert result.quality == 0.0
    assert "fail-closed" in result.detail


def test_pcm_to_wav_roundtrip_header():
    pcm = make_pcm(0.1)
    wav = pcm_to_wav(pcm, sample_rate=24000)
    assert wav[:4] == b"RIFF"
    assert pcm_to_wav(wav)[:4] == b"RIFF"
