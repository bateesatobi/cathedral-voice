"""Spark TTS upstream payload contract."""

from __future__ import annotations

import json

from violet.miner.server import (
    _remap_tts_ws_control_frame,
    _spark_tts_http_upstream_payload,
    _spark_tts_ws_upstream_payload,
)


class TestSparkTtsHttpUpstreamPayload:
    def test_legacy_text_speaker_id(self):
        out = _spark_tts_http_upstream_payload(
            {"text": "Legacy prompt", "speaker_id": "lug_female_1"}
        )
        assert out == {
            "text": "Legacy prompt",
            "speaker_id": "lug_female_1",
            "temperature": 0.7,
        }
        assert "input" not in out
        assert "voice" not in out

    def test_input_voice_aliases_remapped_to_http(self):
        out = _spark_tts_http_upstream_payload(
            {"input": "Hello world", "voice": "eng_male_1", "temperature": 0.5}
        )
        assert out == {
            "text": "Hello world",
            "speaker_id": "eng_male_1",
            "temperature": 0.5,
        }
        assert "input" not in out
        assert "voice" not in out

    def test_prefers_text_over_input_when_both_present(self):
        out = _spark_tts_http_upstream_payload(
            {"text": "from-text", "input": "from-input", "voice": "eng_female_1"}
        )
        assert out["text"] == "from-text"
        assert "input" not in out

    def test_prefers_speaker_id_over_voice_when_both_present(self):
        out = _spark_tts_http_upstream_payload(
            {
                "input": "hi",
                "speaker_id": "eng_male_1",
                "voice": "eng_female_1",
            }
        )
        assert out["speaker_id"] == "eng_male_1"
        assert "voice" not in out

    def test_default_voice_and_temperature(self):
        out = _spark_tts_http_upstream_payload({"text": "hi"})
        assert out["speaker_id"] == "eng_female_1"
        assert out["temperature"] == 0.7

    def test_strips_whitespace(self):
        out = _spark_tts_http_upstream_payload(
            {"text": "  padded  ", "speaker_id": "  eng_female_1  "}
        )
        assert out["text"] == "padded"
        assert out["speaker_id"] == "eng_female_1"

    def test_invalid_temperature_falls_back(self):
        out = _spark_tts_http_upstream_payload({"text": "hi", "temperature": "nope"})
        assert out["temperature"] == 0.7

    def test_never_emits_dual_naming_schemes(self):
        for raw in (
            {"text": "a", "speaker_id": "v"},
            {"input": "a", "voice": "v"},
            {"text": "a", "input": "b", "speaker_id": "v", "voice": "w"},
        ):
            out = _spark_tts_http_upstream_payload(raw)
            assert "text" in out and "speaker_id" in out
            assert "input" not in out
            assert "voice" not in out


class TestSparkTtsWsUpstreamPayload:
    def test_ws_native_input_voice(self):
        out = _spark_tts_ws_upstream_payload(
            {"input": "Hello world", "voice": "eng_male_1", "temperature": 0.5}
        )
        assert out == {
            "input": "Hello world",
            "voice": "eng_male_1",
            "temperature": 0.5,
        }
        assert "text" not in out
        assert "speaker_id" not in out

    def test_legacy_text_speaker_id_remapped_for_ws(self):
        out = _spark_tts_ws_upstream_payload(
            {"text": "Legacy prompt", "speaker_id": "lug_female_1"}
        )
        assert out["input"] == "Legacy prompt"
        assert out["voice"] == "lug_female_1"
        assert set(out.keys()) == {"input", "voice", "temperature"}

    def test_ws_control_frame_remaps_legacy(self):
        out = json.loads(
            _remap_tts_ws_control_frame(
                json.dumps({"text": "hi", "speaker_id": "eng_female_1", "type": "synth"})
            )
        )
        assert out["input"] == "hi"
        assert out["voice"] == "eng_female_1"
        assert out["type"] == "synth"
        assert "text" not in out
        assert "speaker_id" not in out

    def test_ws_eos_unchanged(self):
        raw = json.dumps({"type": "eos"})
        assert _remap_tts_ws_control_frame(raw) == raw
