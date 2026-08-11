"""
Test script for the hybrid Rust+vLLM Spark TTS service.
Tests multiple speakers including Luganda male/female voices.
"""
import requests
import wave
import sys
import os

BASE_URL = os.environ.get("TTS_URL", "http://localhost:8002")

def test_tts(text: str, speaker_id: str, filename: str):
    """Generate speech and save as WAV file."""
    print(f"\n🎙️ Generating: \"{text[:60]}...\" (speaker: {speaker_id})")
    
    try:
        resp = requests.post(
            f"{BASE_URL}/v1/audio/speech/stream",
            json={
                "text": text,
                "speaker_id": speaker_id,
                "temperature": 0.7,
            },
            timeout=120,
        )
        resp.raise_for_status()
        pcm_data = resp.content
        print(f"  ✅ Received {len(pcm_data)} bytes of PCM audio")
        
        # Save as WAV
        with wave.open(filename, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(pcm_data)
        
        duration = len(pcm_data) / (16000 * 2)
        print(f"  📁 Saved to {filename} ({duration:.1f}s)")
        return True
        
    except Exception as e:
        print(f"  ❌ Error: {e}")
        return False

def main():
    # Test health
    try:
        r = requests.get(f"{BASE_URL}/health", timeout=5)
        print(f"✅ Server health: {r.text}")
    except:
        print(f"❌ Server at {BASE_URL} not reachable")
        sys.exit(1)

    # Luganda text
    lug_text = "Mu muddo, yasanga ekika ky'ebisolo ekiyitibwa fox, nga kirabirira omwana waakyo Cub. Amina yeewuunya okulaba ebisolo bino, naye yatya okubyekwatako."
    
    # English text
    eng_text = "Hello world, this is a test of the hybrid Rust and vLLM speech synthesis system."

    tests = [
        # Luganda speakers
        (lug_text, "lug_female_4", "lug_female_4_story.wav"),
        (lug_text, "lug_male_1", "lug_male_1_story.wav"),
        # English speakers
        (eng_text, "eng_female_1", "eng_female_1_hello.wav"),
        (eng_text, "eng_male_1", "eng_male_1_hello.wav"),
        # Nigerian Pidgin
        ("How you dey? I dey fine o. Make we go market today.", "pcm_female_1", "pcm_female_1_pidgin.wav"),
        # Acholi
        ("Apwoyo matek pi kony mamegi. An atye maber.", "ach_female_2", "ach_female_2_acholi.wav"),
    ]

    passed = 0
    for text, speaker, fname in tests:
        if test_tts(text, speaker, fname):
            passed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed}/{len(tests)} tests passed")

if __name__ == "__main__":
    main()
