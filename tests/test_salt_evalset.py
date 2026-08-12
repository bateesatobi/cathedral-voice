"""Tests for the SALT evalset builder helpers (no Hugging Face download)."""

from __future__ import annotations

import json
from pathlib import Path

from violet.evalset import load_evalset


def test_load_salt_style_manifest_with_audio(tmp_path: Path):
    audio_dir = tmp_path / "audio" / "lug"
    audio_dir.mkdir(parents=True)
    wav = audio_dir / "salt-lug-00001.wav"
    # Minimal RIFF header + silence is enough for is_file() checks in loader
    wav.write_bytes(b"RIFF" + b"\x00" * 36 + b"data" + b"\x00" * 8)

    manifest = {
        "name": "violet-salt-v1",
        "asr": [
            {
                "id": "salt-lug-00001",
                "language": "lug",
                "reference": "wasuze otya",
                "audio_path": "audio/lug/salt-lug-00001.wav",
                "duration_s": 1.0,
            }
        ],
        "tts": [],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    evalset = load_evalset(str(tmp_path))
    assert evalset.name == "violet-salt-v1"
    assert evalset.synthetic_only is False
    assert len(evalset.asr) == 1
    assert evalset.asr[0].audio_bytes().startswith(b"RIFF")


def test_builder_pick_text():
    from scripts.build_salt_evalset import _pick_text

    assert _pick_text({"text": " hello "}) == "hello"
    assert _pick_text({"transcription": "yo"}) == "yo"
    assert _pick_text({}) == ""
