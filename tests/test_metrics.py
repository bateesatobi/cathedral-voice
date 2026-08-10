"""Quality metrics: WER, text normalisation, PCM analysis, TTS scoring."""

from __future__ import annotations

import math
import struct

import pytest

from violet.validator.metrics import (
    analyze_pcm,
    asr_quality,
    character_error_rate,
    levenshtein,
    normalize_text,
    tokenize,
    tts_quality,
    wav_to_pcm,
    word_error_rate,
)


def make_pcm(seconds: float, *, rate: int = 24000, amplitude: float = 0.5, silent: bool = False) -> bytes:
    frames = int(rate * seconds)
    if silent:
        return b"\x00\x00" * frames
    samples = [
        int(math.sin(2 * math.pi * 220 * i / rate) * amplitude * 32767)
        for i in range(frames)
    ]
    return struct.pack(f"<{len(samples)}h", *samples)


class TestNormalization:
    def test_strips_punctuation_and_case(self):
        assert normalize_text("Hello, World!") == "hello world"

    def test_collapses_whitespace(self):
        assert normalize_text("a   b\n\tc") == "a b c"

    def test_keeps_apostrophes(self):
        # "don't" and "dont" are different words; splitting on the apostrophe
        # would inflate WER on ordinary English.
        assert normalize_text("don't") == "don't"

    def test_preserves_diacritics(self):
        # Yoruba and Amharic carry meaning in diacritics; stripping them would
        # penalise exactly the languages the subnet exists to serve.
        assert normalize_text("Kaabọ̀ sí Avoices") == "kaabọ̀ sí avoices"

    def test_empty(self):
        assert normalize_text("") == ""
        assert tokenize("") == []


class TestLevenshtein:
    def test_identical(self):
        assert levenshtein(["a", "b"], ["a", "b"]) == 0

    def test_substitution(self):
        assert levenshtein(["a", "b"], ["a", "c"]) == 1

    def test_insertion_and_deletion(self):
        assert levenshtein(["a"], ["a", "b"]) == 1
        assert levenshtein(["a", "b"], ["a"]) == 1

    def test_empty_sides(self):
        assert levenshtein([], ["a", "b"]) == 2
        assert levenshtein(["a", "b"], []) == 2

    def test_symmetric(self):
        left, right = ["the", "quick", "fox"], ["a", "quick", "brown", "fox"]
        assert levenshtein(left, right) == levenshtein(right, left)


class TestWER:
    def test_perfect(self):
        assert word_error_rate("the quick brown fox", "the quick brown fox") == 0.0

    def test_one_substitution_in_four(self):
        assert word_error_rate("the quick brown fox", "the quick brown dog") == pytest.approx(0.25)

    def test_ignores_case_and_punctuation(self):
        assert word_error_rate("Hello, world.", "hello world") == 0.0

    def test_clamped_at_one(self):
        # Unclamped, heavy insertion produces WER > 1, which would let one bad
        # response dominate a rolling average.
        assert word_error_rate("a", "a b c d e f") == 1.0

    def test_empty_hypothesis(self):
        assert word_error_rate("hello world", "") == 1.0

    def test_empty_reference_unmeasurable(self):
        assert word_error_rate("", "") == 0.0
        assert word_error_rate("", "something") == 1.0


class TestCER:
    def test_agglutinative_near_miss_scores_better_than_wer(self):
        # One morpheme wrong in a long Luganda word: WER calls it a total miss,
        # CER sees it as nearly right. asr_quality blends the two.
        reference = "wasuze otya nno ssebo"
        hypothesis = "wasuze otya nno sebo"
        assert word_error_rate(reference, hypothesis) == pytest.approx(0.25)
        assert character_error_rate(reference, hypothesis) < 0.1
        assert asr_quality(reference, hypothesis) > 0.8


class TestAudioAnalysis:
    def test_detects_tone(self):
        stats = analyze_pcm(make_pcm(1.0))
        assert stats is not None
        assert stats.duration_s == pytest.approx(1.0, abs=0.01)
        assert stats.rms > 0.1
        assert not stats.is_silent

    def test_detects_silence(self):
        stats = analyze_pcm(make_pcm(1.0, silent=True))
        assert stats is not None
        assert stats.is_silent
        assert stats.silence_ratio == pytest.approx(1.0)

    def test_rejects_empty(self):
        assert analyze_pcm(b"") is None
        assert analyze_pcm(b"\x00") is None

    def test_odd_length_is_truncated_not_crashed(self):
        stats = analyze_pcm(make_pcm(0.1) + b"\x00")
        assert stats is not None

    def test_wav_header_is_stripped(self):
        import io
        import wave

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(make_pcm(0.5, rate=16000))

        pcm, rate, width, channels = wav_to_pcm(buffer.getvalue())
        assert rate == 16000
        assert width == 2
        assert channels == 1
        assert not pcm.startswith(b"RIFF")

    def test_raw_pcm_passes_through(self):
        raw = make_pcm(0.1)
        pcm, rate, _, _ = wav_to_pcm(raw)
        assert pcm == raw
        assert rate == 24000


class TestTTSQuality:
    def test_plausible_speech_scores_high(self):
        text = "Violet supplies decentralized speech inference."
        audio = make_pcm(len(text) * 0.06)
        score, note = tts_quality(audio, text)
        assert score > 0.8, note

    def test_empty_response_scores_zero(self):
        score, note = tts_quality(b"", "hello")
        assert score == 0.0
        assert "empty" in note

    def test_silence_scores_zero(self):
        # The obvious way to fake TTS: return the right number of bytes, all zero.
        score, note = tts_quality(make_pcm(2.0, silent=True), "a" * 30)
        assert score == 0.0
        assert "silent" in note

    def test_truncated_stub_is_penalised(self):
        # 50 ms of audio for a long sentence is not synthesis.
        score, note = tts_quality(make_pcm(0.05), "a" * 200)
        assert score < 0.3, note

    def test_padded_audio_is_penalised(self):
        # Ten seconds for three characters: padding to look busy.
        score, _ = tts_quality(make_pcm(10.0), "abc")
        assert score < 0.5

    def test_clipping_is_penalised(self):
        frames = 24000
        clipped = struct.pack(f"<{frames}h", *([32767] * frames))
        score, note = tts_quality(clipped, "a" * 20)
        assert score < 1.0
        assert "clipping" in note or "long" in note
