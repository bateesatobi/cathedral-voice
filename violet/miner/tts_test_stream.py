"""
Test TTS streaming — direct Spark/miner upstream or Phosai API (Violet router).

Examples:
  # Phosai (Render + Violet router → GPU miner)
  PHOSAI_API_URL=https://phosai-backend-api-latest.onrender.com python tts_test_stream.py

  # Direct Spark / miner upstream
  TTS_URL=http://localhost:8002 TTS_MODE=direct python tts_test_stream.py
"""
from __future__ import annotations

import os
import sys
import wave

import requests

TTS_MIN_BYTES = int(os.environ.get("TTS_MIN_AUDIO_BYTES", "512"))
PHOSAI_DEFAULT = "https://phosai-backend-api-latest.onrender.com"
TTS_MODE = os.environ.get("TTS_MODE", "phosai").strip().lower()  # phosai | direct
BASE_URL = (
    os.environ.get("TTS_URL")
    or os.environ.get("PHOSAI_API_URL")
    or (PHOSAI_DEFAULT if TTS_MODE == "phosai" else "http://localhost:8002")
).rstrip("/")
OUTPUT_DIR = os.environ.get("TTS_OUTPUT_DIR", ".")


def ensure_output_dir() -> None:
    os.makedirs(OUTPUT_DIR, exist_ok=True)


def parse_pcm_meta(headers) -> dict:
    normalized = {str(k).lower(): v for k, v in (headers or {}).items()}

    def _int(*keys: str, default: int) -> int:
        for key in keys:
            if key not in normalized:
                continue
            try:
                return int(normalized[key])
            except (TypeError, ValueError):
                continue
        return default

    sample_rate = _int("x-audio-sample-rate", "x-sample-rate", default=16000)
    channels = _int("x-audio-channels", "x-channels", default=1)
    width = _int("x-audio-sample-width", default=0)
    bit_depth = _int("x-bit-depth", "x-audio-bit-depth", default=0)
    if width > 0:
        bit_depth = width * 8 if width <= 4 else width
    elif bit_depth <= 0:
        bit_depth = 16

    return {
        "sample_rate": sample_rate,
        "channels": channels,
        "bit_depth": bit_depth,
    }


def tts_endpoint() -> str:
    if TTS_MODE == "phosai":
        return f"{BASE_URL}/api/tts/stream"
    return f"{BASE_URL}/v1/audio/speech/stream"


def tts_payload(text: str, speaker_id: str) -> dict:
    if TTS_MODE == "phosai":
        return {
            "text": text,
            "speaker_name": speaker_id,
            "temperature": 0.7,
        }
    # Miner API: text + speaker_id (miner remaps aliases before proxying to Spark).
    return {
        "text": text,
        "speaker_id": speaker_id,
        "temperature": 0.7,
    }


def check_health() -> bool:
    if TTS_MODE == "phosai":
        try:
            r = requests.get(f"{BASE_URL}/violet/chain", timeout=30)
            r.raise_for_status()
            data = r.json()
            print(f"✅ Phosai router: {data.get('healthy_miners', 0)} healthy / "
                  f"{data.get('discovered_miners', 0)} discovered")
            return True
        except Exception as exc:
            print(f"❌ Phosai at {BASE_URL} not reachable: {exc}")
            return False

    try:
        r = requests.get(f"{BASE_URL}/health", timeout=10)
        print(f"✅ Server health: {r.text}")
        return True
    except Exception as exc:
        print(f"❌ Server at {BASE_URL} not reachable: {exc}")
        return False


def validate_pcm(pcm_data: bytes, content_type: str) -> None:
    if len(pcm_data) < TTS_MIN_BYTES:
        preview = pcm_data[:200].decode("utf-8", errors="replace")
        raise ValueError(
            f"response too small ({len(pcm_data)} bytes); expected ≥ {TTS_MIN_BYTES}. "
            f"Preview: {preview[:160]}"
        )
    if pcm_data.startswith(b"Error:") or pcm_data.startswith(b"<!DOCTYPE") or b"<html" in pcm_data[:200].lower():
        preview = pcm_data[:200].decode("utf-8", errors="replace")
        raise ValueError(f"server returned an error page, not PCM: {preview[:160]}")
    if "text/html" in (content_type or "").lower():
        raise ValueError(f"unexpected content-type {content_type!r} (expected audio/pcm)")


def test_tts(text: str, speaker_id: str, filename: str) -> bool:
    """Generate speech and save as WAV."""
    print(f'\n🎙️ Generating: "{text[:60]}..." (speaker: {speaker_id})')

    out_path = os.path.join(OUTPUT_DIR, filename)
    url = tts_endpoint()

    try:
        resp = requests.post(
            url,
            json=tts_payload(text, speaker_id),
            timeout=180,
        )
        resp.raise_for_status()
        pcm_data = resp.content
        validate_pcm(pcm_data, resp.headers.get("Content-Type", ""))
        meta = parse_pcm_meta(resp.headers)
        sample_rate = meta["sample_rate"]
        channels = meta["channels"]
        bit_depth = meta["bit_depth"]
        sampwidth = max(1, bit_depth // 8)

        print(f"  ✅ Received {len(pcm_data)} bytes @ {sample_rate} Hz "
              f"({channels}ch, {bit_depth}-bit)")
        print(f"  📡 POST {url}")

        with wave.open(out_path, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(sampwidth)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_data)

        duration = len(pcm_data) / (sample_rate * sampwidth * channels)
        print(f"  📁 Saved to {out_path} ({duration:.1f}s)")
        return True

    except Exception as exc:
        print(f"  ❌ Error: {exc}")
        if hasattr(exc, "response") and exc.response is not None:
            print(f"     Body: {exc.response.text[:300]}")
        return False


def main() -> None:
    print(f"Mode: {TTS_MODE}  Base: {BASE_URL}")
    ensure_output_dir()
    if not check_health():
        sys.exit(1)

    lug_text = (
        "Mu muddo, yasanga ekika ky'ebisolo ekiyitibwa fox, nga kirabirira "
        "omwana waakyo Cub. Amina yeewuunya okulaba ebisolo bino, naye yatya okubyekwatako."
    )
    eng_text = (
        "Hello world, this is a test of speech synthesis through the Phosai API."
    )

    tests = [
        (lug_text, "lug_female_4", "lug_female_4_story.wav"),
        (lug_text, "lug_male_1", "lug_male_1_story.wav"),
        (eng_text, "eng_female_1", "eng_female_1_hello.wav"),
        (eng_text, "eng_male_1", "eng_male_1_hello.wav"),
        (
            "How you dey? I dey fine o. Make we go market today.",
            "pcm_female_1",
            "pcm_female_1_pidgin.wav",
        ),
        (
            "Apwoyo matek pi kony mamegi. An atye maber.",
            "ach_female_2",
            "ach_female_2_acholi.wav",
        ),
    ]

    passed = 0
    for text, speaker, fname in tests:
        if test_tts(text, speaker, fname):
            passed += 1

    print(f"\n{'=' * 50}")
    print(f"Results: {passed}/{len(tests)} tests passed")
    if passed < len(tests):
        sys.exit(1)


if __name__ == "__main__":
    main()
