"""
End-to-end tests of the miner sidecar against the sample inference containers.

These cover the contract that matters most: a miner must be a drop-in
replacement for the servers Avoices calls today. If any of these break, the
smart router cannot be enabled without changing product code.
"""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest
import websockets

from violet.constants import TARGET_FIRST_BYTE_MS
from violet.protocol import (
    HEADER_MINER_HOTKEY,
    HEADER_SAMPLE_RATE,
    PATH_ASR_STREAM_WS,
    PATH_ASR_TRANSCRIBE,
    PATH_TTS_STREAM,
    PATH_TTS_STREAM_WS,
    PATH_TTS_VOICES,
    HealthReport,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def session():
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=60)
    ) as client:
        yield client


class TestControlPlane:
    async def test_health_returns_valid_json(self, session, miner):
        async with session.get(f"{miner.url}/health") as response:
            assert response.status == 200
            payload = await response.json()

        report = HealthReport.from_dict(payload)
        assert report.status == "ok"
        assert set(report.services) == {"asr", "tts"}
        assert report.upstreams == {"asr": True, "tts": True}
        assert report.capacity is not None

    async def test_health_carries_identity_header(self, session, miner):
        async with session.get(f"{miner.url}/health") as response:
            assert response.headers[HEADER_MINER_HOTKEY].startswith("5Test")

    async def test_capacity_endpoint(self, session, miner):
        async with session.get(f"{miner.url}/capacity") as response:
            payload = await response.json()
        assert payload["max_concurrent_asr"] == 4
        assert "capacity_units" in payload
        assert "load_factor" in payload

    async def test_info_reports_warnings(self, session, miner):
        async with session.get(f"{miner.url}/violet/info") as response:
            payload = await response.json()
        assert payload["services"] == ["asr", "tts"]
        # No GPU in CI: the miner must say so rather than silently claiming none.
        assert any("GPU" in warning or "gpu" in warning for warning in payload["warnings"])


class TestASR:
    async def test_batch_transcription_matches_asrapi_contract(
        self, session, miner, sample_wav
    ):
        form = aiohttp.FormData()
        form.add_field("file", sample_wav, filename="a.wav", content_type="audio/wav")
        form.add_field("language", "eng")
        form.add_field("response_format", "json")

        async with session.post(f"{miner.url}{PATH_ASR_TRANSCRIBE}", data=form) as response:
            assert response.status == 200
            payload = await response.json()

        # ASRAPI/utils/utils.py accepts either a flat "text" or "segments".
        assert "text" in payload
        assert isinstance(payload["segments"], list)
        assert all({"text", "start", "end"} <= set(seg) for seg in payload["segments"])

    async def test_text_response_format(self, session, miner, sample_wav):
        form = aiohttp.FormData()
        form.add_field("file", sample_wav, filename="a.wav", content_type="audio/wav")
        form.add_field("language", "eng")
        form.add_field("response_format", "text")

        async with session.post(f"{miner.url}{PATH_ASR_TRANSCRIBE}", data=form) as response:
            assert response.status == 200
            assert (await response.text()).strip()

    async def test_streaming_emits_progressive_partials(self, miner, sample_wav):
        url = miner.url.replace("http://", "ws://") + PATH_ASR_STREAM_WS + "?language=eng"
        pcm = sample_wav[44:]

        partials = []
        async with websockets.connect(url, max_size=None) as ws:
            for offset in range(0, len(pcm), 16000):
                await ws.send(pcm[offset : offset + 16000])
                message = await asyncio.wait_for(ws.recv(), timeout=5)
                partials.append(json.loads(message)["text"])

        assert len(partials) >= 2
        # Progressive means the transcript grows, not that it is re-sent whole.
        assert len(partials[-1]) > len(partials[0])


class TestTTS:
    async def test_synthesis_returns_pcm_with_framing_headers(self, session, miner):
        payload = {"input": "Violet subnet test.", "voice": "eng_female_1"}
        async with session.post(f"{miner.url}{PATH_TTS_STREAM}", json=payload) as response:
            assert response.status == 200
            # The framing headers must survive the proxy, or the caller cannot
            # wrap the PCM.
            assert response.headers[HEADER_SAMPLE_RATE] == "24000"
            audio = await response.read()

        assert len(audio) > 512

    async def test_first_byte_is_within_target(self, session, miner):
        import time

        payload = {"input": "Latency probe.", "voice": "eng_female_1"}
        started = time.perf_counter()
        first_byte_ms = None

        async with session.post(f"{miner.url}{PATH_TTS_STREAM}", json=payload) as response:
            async for chunk in response.content.iter_chunked(1024):
                if chunk:
                    first_byte_ms = (time.perf_counter() - started) * 1000
                    break

        assert first_byte_ms is not None
        # Generous versus the 200 ms production target: CI is not a GPU host.
        assert first_byte_ms < TARGET_FIRST_BYTE_MS * 10

    async def test_empty_text_is_rejected(self, session, miner):
        async with session.post(
            f"{miner.url}{PATH_TTS_STREAM}", json={"input": "  ", "voice": "x"}
        ) as response:
            assert response.status == 400

    async def test_malformed_body_is_rejected(self, session, miner):
        async with session.post(
            f"{miner.url}{PATH_TTS_STREAM}", data=b"not json",
            headers={"Content-Type": "application/json"},
        ) as response:
            assert response.status == 400

    async def test_voices_catalogue(self, session, miner):
        async with session.get(f"{miner.url}{PATH_TTS_VOICES}") as response:
            assert response.status == 200
            payload = await response.json()
        assert payload["voices"]
        assert {"id", "language"} <= set(payload["voices"][0])

    async def test_streaming_synthesis(self, miner):
        url = miner.url.replace("http://", "ws://") + PATH_TTS_STREAM_WS

        audio = bytearray()
        async with websockets.connect(url, max_size=None) as ws:
            await ws.send(json.dumps({"input": "Streaming test.", "voice": "eng_male_1"}))
            while True:
                message = await asyncio.wait_for(ws.recv(), timeout=15)
                if isinstance(message, (bytes, bytearray)):
                    audio.extend(message)
                elif json.loads(message).get("type") == "end":
                    break

        assert len(audio) > 512

    async def test_streaming_accepts_legacy_aliases(self, miner):
        """Legacy {text, speaker_id} is remapped on the WS bridge."""
        url = miner.url.replace("http://", "ws://") + PATH_TTS_STREAM_WS
        audio = bytearray()
        async with websockets.connect(url, max_size=None) as ws:
            await ws.send(
                json.dumps({"text": "Legacy streaming.", "speaker_id": "eng_female_1"})
            )
            while True:
                message = await asyncio.wait_for(ws.recv(), timeout=15)
                if isinstance(message, (bytes, bytearray)):
                    audio.extend(message)
                elif json.loads(message).get("type") == "end":
                    break
        assert len(audio) > 512


class TestAdmissionControl:
    async def test_saturation_returns_503_rather_than_queueing(self, session, miner):
        """A saturated miner must shed load so the router can route elsewhere.

        Queueing would hide saturation: the request eventually succeeds, slowly,
        and the miner keeps attracting traffic it cannot serve.
        """
        from violet.miner.upstream import AtCapacity, Slots

        slots = Slots(1, "tts")
        async with slots.acquire():
            with pytest.raises(AtCapacity) as excinfo:
                async with slots.acquire():
                    pass
        assert excinfo.value.status == 503

    async def test_slot_is_released_after_use(self):
        from violet.miner.upstream import Slots

        slots = Slots(1, "asr")
        async with slots.acquire():
            assert slots.active == 1
        assert slots.active == 0

    async def test_slot_released_on_exception(self):
        from violet.miner.upstream import Slots

        slots = Slots(1, "asr")
        with pytest.raises(ValueError):
            async with slots.acquire():
                raise ValueError("boom")
        assert slots.active == 0

    async def test_zero_limit_means_unbounded(self):
        from violet.miner.upstream import Slots

        slots = Slots(0, "asr")
        async with slots.acquire():
            async with slots.acquire():
                assert slots.active == 2


class TestValidatorAgainstMiner:
    """The validator's probes, run against a real miner."""

    async def test_all_six_qualification_tests(self, session, miner):
        import time

        from violet.evalset import load_evalset
        from violet.validator.probes import MinerProbe
        from violet.validator.qualification import AvailabilitySample, run_qualification

        evalset = load_evalset()
        probe = MinerProbe(
            session,
            miner.url,
            hotkey="5TestHotkeyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        )

        now = time.time()
        availability = [
            AvailabilitySample(at=now - 1800 + index * 60, ok=True) for index in range(31)
        ]

        result = await run_qualification(
            probe, evalset, services=["asr", "tts"],
            availability=availability, availability_window_s=1800, seed=7,
        )

        assert result.passed, result.summary()
        assert len(result.outcomes) == 8

    async def test_availability_fails_without_observation(self, session, miner):
        """A brand-new miner must not be admitted before it has been watched."""
        from violet.evalset import load_evalset
        from violet.validator.probes import MinerProbe
        from violet.validator.qualification import TEST_AVAILABILITY, run_qualification

        result = await run_qualification(
            MinerProbe(session, miner.url), load_evalset(),
            services=["asr", "tts"], availability=[], availability_window_s=1800, seed=1,
        )
        outcome = next(o for o in result.outcomes if o.name == TEST_AVAILABILITY)
        assert not outcome.passed
        assert not result.passed

    async def test_unreachable_miner_fails_cleanly(self, session):
        """A dead endpoint must produce a report, not an exception."""
        from violet.evalset import load_evalset
        from violet.validator.probes import MinerProbe
        from violet.validator.qualification import run_qualification

        result = await run_qualification(
            MinerProbe(session, "http://127.0.0.1:1"), load_evalset(),
            services=["asr", "tts"], availability=[], availability_window_s=60, seed=1,
        )
        assert not result.passed
        assert len(result.outcomes) == 8
        assert all("unreachable" in o.detail for o in result.outcomes[1:])
