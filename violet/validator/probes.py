"""
Measurement of a single miner.

Every probe returns a :class:`ProbeResult` rather than raising, because a
validator sweep must complete even when half the network is down: an exception
escaping one probe would abort the round and skew scores for everyone.

Probes deliberately look like production traffic - same paths, same payload
shapes, no marker header the miner could branch on. TDD 9.2 requires evaluation
queries to be hard to distinguish from real ones; a miner that could detect
probes could serve them from a fast path and everything else badly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp
import websockets

from ..constants import MIN_TTS_AUDIO_BYTES
from ..evalset import AsrItem, TtsItem
from ..protocol import (
    DEFAULT_CHANNELS,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_SAMPLE_WIDTH,
    HEADER_CHANNELS,
    HEADER_MINER_HOTKEY,
    HEADER_SAMPLE_RATE,
    HEADER_SAMPLE_WIDTH,
    PATH_ASR_STREAM_WS,
    PATH_ASR_TRANSCRIBE,
    PATH_CAPACITY,
    PATH_HEALTH,
    PATH_IDENTITY_CHALLENGE,
    PATH_INFO,
    PATH_TTS_STREAM,
    PATH_TTS_STREAM_WS,
    CapacityReport,
    HealthReport,
)
from ..identity import (
    challenge_is_fresh,
    challenge_message,
    new_nonce,
    verify_hotkey_signature,
)
from .metrics import asr_quality, tts_quality, word_error_rate

logger = logging.getLogger("violet.validator.probes")


@dataclass
class ProbeResult:
    """Outcome of one measurement."""

    ok: bool
    kind: str
    latency_ms: float = 0.0
    #: Time to the first byte or first partial. This, not total latency, is the
    #: number the 200 ms target in TDD 1 refers to.
    first_byte_ms: Optional[float] = None
    quality: Optional[float] = None
    wer: Optional[float] = None
    detail: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def failure(cls, kind: str, detail: str, latency_ms: float = 0.0) -> "ProbeResult":
        return cls(ok=False, kind=kind, latency_ms=latency_ms, detail=detail)


def _ws_url(endpoint: str, path: str) -> str:
    base = endpoint.rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):] + path
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):] + path
    return base + path


class MinerProbe:
    """Probes one miner endpoint."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        endpoint: str,
        *,
        hotkey: str = "",
        access_token: str = "",
    ):
        self.session = session
        self.endpoint = endpoint.rstrip("/")
        self.hotkey = hotkey
        self._headers = (
            {"Authorization": f"Bearer {access_token}"} if access_token else {}
        )

    def url(self, path: str) -> str:
        return f"{self.endpoint}{path}"

    # -- control plane -----------------------------------------------------

    async def health(self, timeout_s: float = 5.0) -> ProbeResult:
        """``GET /health`` must return valid JSON within a strict timeout."""
        started = time.perf_counter()
        try:
            async with self.session.get(
                self.url(PATH_HEALTH), timeout=aiohttp.ClientTimeout(total=timeout_s)
            ) as response:
                body = await response.read()
                elapsed = (time.perf_counter() - started) * 1000.0

                if response.status != 200:
                    return ProbeResult.failure(
                        "health", f"HTTP {response.status}", elapsed
                    )
                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    return ProbeResult.failure(
                        "health", "response body is not valid JSON", elapsed
                    )
                if not isinstance(payload, dict) or "status" not in payload:
                    return ProbeResult.failure(
                        "health", "JSON is missing the required 'status' field", elapsed
                    )

                header_hotkey = response.headers.get(HEADER_MINER_HOTKEY)
                if self.hotkey and header_hotkey and header_hotkey != self.hotkey:
                    return ProbeResult.failure(
                        "health",
                        f"hotkey header mismatch (got {header_hotkey[:12]}…)",
                        elapsed,
                    )

                report = HealthReport.from_dict(payload)
                # "degraded" is a real state, not a failure: one of two services
                # is down and the other should keep serving.
                ok = report.status in {"ok", "degraded"}
                return ProbeResult(
                    ok=ok,
                    kind="health",
                    latency_ms=elapsed,
                    detail=report.status,
                    payload={"health": report},
                )
        except asyncio.TimeoutError:
            return ProbeResult.failure(
                "health", f"timed out after {timeout_s:.1f}s", timeout_s * 1000.0
            )
        except aiohttp.ClientError as exc:
            return ProbeResult.failure(
                "health", f"unreachable: {exc}", (time.perf_counter() - started) * 1000.0
            )

    async def capacity(self, timeout_s: float = 5.0) -> Optional[CapacityReport]:
        try:
            async with self.session.get(
                self.url(PATH_CAPACITY), timeout=aiohttp.ClientTimeout(total=timeout_s)
            ) as response:
                if response.status != 200:
                    return None
                return CapacityReport.from_dict(await response.json())
        except (asyncio.TimeoutError, aiohttp.ClientError, json.JSONDecodeError):
            return None

    async def info(self, timeout_s: float = 5.0) -> Optional[Dict[str, object]]:
        """Fetch ``/violet/info`` for declared image digests."""
        try:
            async with self.session.get(
                self.url(PATH_INFO), timeout=aiohttp.ClientTimeout(total=timeout_s)
            ) as response:
                if response.status != 200:
                    return None
                payload = await response.json()
                return payload if isinstance(payload, dict) else None
        except (asyncio.TimeoutError, aiohttp.ClientError, json.JSONDecodeError):
            return None

    async def verify_identity(self, *, timeout_s: float = 5.0) -> ProbeResult:
        """Confirm the endpoint is bound to the expected on-chain hotkey."""
        if not self.hotkey:
            return ProbeResult(
                ok=True,
                kind="identity",
                detail="no expected hotkey configured",
                payload={"skipped": True},
            )

        nonce = new_nonce()
        started = time.perf_counter()
        try:
            async with self.session.get(
                self.url(PATH_IDENTITY_CHALLENGE),
                params={"nonce": nonce},
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as response:
                elapsed = (time.perf_counter() - started) * 1000.0
                if response.status == 503:
                    return ProbeResult(
                        ok=True,
                        kind="identity",
                        detail="identity signing unavailable on miner",
                        latency_ms=elapsed,
                        payload={"skipped": True},
                    )
                if response.status != 200:
                    body = await response.text()
                    return ProbeResult.failure(
                        "identity",
                        f"HTTP {response.status}: {body[:120]!r}",
                        elapsed,
                    )
                payload = await response.json()
        except asyncio.TimeoutError:
            return ProbeResult.failure(
                "identity", f"timed out after {timeout_s:.1f}s", timeout_s * 1000.0
            )
        except aiohttp.ClientError as exc:
            return ProbeResult.failure("identity", f"request failed: {exc}")

        elapsed = (time.perf_counter() - started) * 1000.0
        hotkey = str(payload.get("hotkey", "") or "")
        signature = str(payload.get("signature", "") or "")
        issued_at = float(payload.get("issued_at", 0.0) or 0.0)
        if hotkey != self.hotkey:
            return ProbeResult.failure(
                "identity",
                f"response hotkey {hotkey[:12]}… does not match expected",
                elapsed,
            )
        if str(payload.get("nonce", "")) != nonce:
            return ProbeResult.failure("identity", "nonce mismatch in response", elapsed)
        if not challenge_is_fresh(issued_at):
            return ProbeResult.failure("identity", "challenge response expired", elapsed)

        message = challenge_message(hotkey, nonce, issued_at)
        if not verify_hotkey_signature(hotkey, message, signature):
            return ProbeResult.failure(
                "identity", "signature verification failed", elapsed
            )
        return ProbeResult(
            ok=True,
            kind="identity",
            latency_ms=elapsed,
            detail="hotkey signature verified",
        )

    # -- ASR ---------------------------------------------------------------

    async def asr_batch(self, item: AsrItem, timeout_s: float = 30.0) -> ProbeResult:
        """Transcribe a known sample and score the result."""
        audio = item.audio_bytes()
        form = aiohttp.FormData()
        form.add_field("file", audio, filename=f"{item.id}.wav", content_type="audio/wav")
        form.add_field("language", item.language)
        form.add_field("response_format", "json")

        started = time.perf_counter()
        try:
            async with self.session.post(
                self.url(PATH_ASR_TRANSCRIBE),
                data=form,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as response:
                body = await response.read()
                elapsed = (time.perf_counter() - started) * 1000.0

                if response.status == 503:
                    # At capacity is not a quality failure; the miner correctly
                    # shed load. Reported so the caller can skip rather than
                    # penalise.
                    return ProbeResult(
                        ok=False, kind="asr", latency_ms=elapsed, detail="at capacity",
                        payload={"at_capacity": True},
                    )
                if response.status != 200:
                    return ProbeResult.failure(
                        "asr", f"HTTP {response.status}: {body[:120]!r}", elapsed
                    )

                hypothesis = _extract_transcript(body)
                if hypothesis is None:
                    return ProbeResult.failure(
                        "asr", "response contained no transcript", elapsed
                    )
        except asyncio.TimeoutError:
            return ProbeResult.failure(
                "asr", f"timed out after {timeout_s:.0f}s", timeout_s * 1000.0
            )
        except aiohttp.ClientError as exc:
            return ProbeResult.failure("asr", f"request failed: {exc}")

        wer = word_error_rate(item.reference, hypothesis)
        quality = asr_quality(item.reference, hypothesis)
        return ProbeResult(
            ok=True,
            kind="asr",
            latency_ms=elapsed,
            first_byte_ms=elapsed,
            quality=quality,
            wer=wer,
            detail=f"wer={wer:.3f}",
            payload={"hypothesis": hypothesis, "item_id": item.id},
        )

    async def asr_stream(
        self, item: AsrItem, *, first_chunk_timeout_s: float = 3.0, total_timeout_s: float = 30.0
    ) -> ProbeResult:
        """Verify partial transcripts arrive progressively over WebSocket."""
        url = _ws_url(self.endpoint, f"{PATH_ASR_STREAM_WS}?language={item.language}")
        audio = item.audio_bytes()
        pcm = audio[44:] if audio[:4] == b"RIFF" else audio
        # ~0.5 s bursts at 16 kHz 16-bit mono, as a live capture would arrive.
        burst = 16000
        chunks = [pcm[i : i + burst] for i in range(0, len(pcm), burst)] or [b"\x00" * burst]

        started = time.perf_counter()
        partials: List[str] = []
        first_partial_ms: Optional[float] = None

        try:
            async with websockets.connect(
                url, max_size=4 * 1024 * 1024, open_timeout=10
            ) as ws:
                async def send_audio() -> None:
                    for chunk in chunks:
                        await ws.send(chunk)
                        await asyncio.sleep(0.05)

                sender = asyncio.create_task(send_audio())
                try:
                    deadline = time.perf_counter() + total_timeout_s
                    while time.perf_counter() < deadline:
                        remaining = deadline - time.perf_counter()
                        budget = first_chunk_timeout_s if not partials else remaining
                        message = await asyncio.wait_for(
                            ws.recv(), timeout=max(0.1, min(budget, remaining))
                        )
                        if isinstance(message, (bytes, bytearray)):
                            continue
                        try:
                            data = json.loads(message)
                        except json.JSONDecodeError:
                            continue
                        text = (data.get("text") or "").strip()
                        if text:
                            if first_partial_ms is None:
                                first_partial_ms = (time.perf_counter() - started) * 1000.0
                            partials.append(text)
                        if data.get("is_final") or data.get("type") == "final":
                            break
                        if sender.done() and len(partials) >= 2:
                            break
                finally:
                    sender.cancel()
                    await asyncio.gather(sender, return_exceptions=True)
        except asyncio.TimeoutError:
            elapsed = (time.perf_counter() - started) * 1000.0
            if not partials:
                return ProbeResult.failure(
                    "asr_stream",
                    f"no partial transcript within {first_chunk_timeout_s:.1f}s",
                    elapsed,
                )
        except Exception as exc:
            return ProbeResult.failure("asr_stream", f"websocket failed: {exc}")

        if not partials:
            return ProbeResult.failure("asr_stream", "no transcripts received")

        elapsed = (time.perf_counter() - started) * 1000.0
        # Progressive output means the transcript grew over time. A miner that
        # buffers the whole utterance and emits once is functional but is not
        # streaming, and TDD 8 makes streaming the preferred interaction model.
        progressive = len(partials) > 1 and len(partials[-1]) > len(partials[0])
        quality = asr_quality(item.reference, partials[-1])

        if not progressive or len(partials) < 2:
            return ProbeResult(
                ok=False,
                kind="asr_stream",
                latency_ms=elapsed,
                first_byte_ms=first_partial_ms,
                quality=quality,
                wer=word_error_rate(item.reference, partials[-1]),
                detail=f"non-progressive stream ({len(partials)} partials)",
                payload={"partials": len(partials), "progressive": progressive},
            )

        return ProbeResult(
            ok=True,
            kind="asr_stream",
            latency_ms=elapsed,
            first_byte_ms=first_partial_ms,
            quality=quality,
            wer=word_error_rate(item.reference, partials[-1]),
            detail="progressive" if progressive else "single-shot",
            payload={"partials": len(partials), "progressive": progressive},
        )

    # -- TTS ---------------------------------------------------------------

    async def tts_batch(self, item: TtsItem, timeout_s: float = 30.0) -> ProbeResult:
        """Synthesize a fixed prompt and check the audio matches the text."""
        payload = {"text": item.text, "speaker_id": item.speaker_id, "temperature": 0.7}
        started = time.perf_counter()
        first_byte_ms: Optional[float] = None
        audio = bytearray()

        try:
            async with self.session.post(
                self.url(PATH_TTS_STREAM),
                json=payload,
                headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as response:
                if response.status == 503:
                    return ProbeResult(
                        ok=False, kind="tts",
                        latency_ms=(time.perf_counter() - started) * 1000.0,
                        detail="at capacity", payload={"at_capacity": True},
                    )
                if response.status != 200:
                    body = await response.read()
                    return ProbeResult.failure(
                        "tts",
                        f"HTTP {response.status}: {body[:120]!r}",
                        (time.perf_counter() - started) * 1000.0,
                    )

                sample_rate = _header_int(response.headers, HEADER_SAMPLE_RATE, DEFAULT_SAMPLE_RATE)
                channels = _header_int(response.headers, HEADER_CHANNELS, DEFAULT_CHANNELS)
                width = _header_int(response.headers, HEADER_SAMPLE_WIDTH, DEFAULT_SAMPLE_WIDTH)

                async for chunk in response.content.iter_chunked(8192):
                    if first_byte_ms is None and chunk:
                        first_byte_ms = (time.perf_counter() - started) * 1000.0
                    audio.extend(chunk)
        except asyncio.TimeoutError:
            return ProbeResult.failure(
                "tts", f"timed out after {timeout_s:.0f}s", timeout_s * 1000.0
            )
        except aiohttp.ClientError as exc:
            return ProbeResult.failure("tts", f"request failed: {exc}")

        elapsed = (time.perf_counter() - started) * 1000.0
        if len(audio) < MIN_TTS_AUDIO_BYTES:
            return ProbeResult.failure(
                "tts", f"only {len(audio)} bytes of audio returned", elapsed
            )

        quality, note = tts_quality(
            bytes(audio), item.text,
            sample_rate=sample_rate, sample_width=width, channels=channels,
        )
        return ProbeResult(
            ok=quality > 0.0,
            kind="tts",
            latency_ms=elapsed,
            first_byte_ms=first_byte_ms,
            quality=quality,
            detail=note,
            payload={"bytes": len(audio), "item_id": item.id, "sample_rate": sample_rate},
        )

    async def tts_stream(
        self, item: TtsItem, *, first_chunk_timeout_s: float = 3.0, total_timeout_s: float = 30.0
    ) -> ProbeResult:
        """Verify audio frames begin arriving before synthesis completes."""
        url = _ws_url(self.endpoint, PATH_TTS_STREAM_WS)
        started = time.perf_counter()
        first_frame_ms: Optional[float] = None
        audio = bytearray()
        frame_count = 0

        try:
            async with websockets.connect(
                url, max_size=4 * 1024 * 1024, open_timeout=10
            ) as ws:
                await ws.send(
                    json.dumps(
                        {"text": item.text, "speaker_id": item.speaker_id, "temperature": 0.7}
                    )
                )
                deadline = time.perf_counter() + total_timeout_s
                while time.perf_counter() < deadline:
                    budget = first_chunk_timeout_s if first_frame_ms is None else (
                        deadline - time.perf_counter()
                    )
                    message = await asyncio.wait_for(ws.recv(), timeout=max(0.1, budget))
                    if isinstance(message, (bytes, bytearray)):
                        if first_frame_ms is None:
                            first_frame_ms = (time.perf_counter() - started) * 1000.0
                        frame_count += 1
                        audio.extend(message)
                        continue
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError:
                        continue
                    if data.get("type") in {"end", "done", "final"}:
                        break
                    if data.get("type") == "error":
                        return ProbeResult.failure(
                            "tts_stream", f"miner reported: {data.get('message')}"
                        )
        except asyncio.TimeoutError:
            if first_frame_ms is None:
                return ProbeResult.failure(
                    "tts_stream",
                    f"no audio frame within {first_chunk_timeout_s:.1f}s",
                    (time.perf_counter() - started) * 1000.0,
                )
        except Exception as exc:
            return ProbeResult.failure("tts_stream", f"websocket failed: {exc}")

        elapsed = (time.perf_counter() - started) * 1000.0
        if len(audio) < MIN_TTS_AUDIO_BYTES:
            return ProbeResult.failure(
                "tts_stream", f"only {len(audio)} bytes streamed", elapsed
            )

        quality, note = tts_quality(bytes(audio), item.text)
        progressive = frame_count >= 2
        if not progressive:
            return ProbeResult(
                ok=False,
                kind="tts_stream",
                latency_ms=elapsed,
                first_byte_ms=first_frame_ms,
                quality=quality,
                detail=f"single-frame stream ({frame_count} frames)",
                payload={"bytes": len(audio), "frames": frame_count, "progressive": False},
            )
        return ProbeResult(
            ok=quality > 0.0,
            kind="tts_stream",
            latency_ms=elapsed,
            first_byte_ms=first_frame_ms,
            quality=quality,
            detail=note,
            payload={"bytes": len(audio), "frames": frame_count, "progressive": True},
        )

    # -- capacity cross-check ---------------------------------------------

    async def throughput(self, item: TtsItem, *, parallel: int = 4) -> ProbeResult:
        """Fire concurrent requests to observe real parallelism.

        This is the observation half of the resource-accuracy check (TDD 4.4,
        9.2): a miner claiming eight H200s should absorb concurrent load without
        its per-request latency collapsing. One that claims large capacity and
        serialises everything is misreporting.
        """
        started = time.perf_counter()
        results = await asyncio.gather(
            *(self.tts_batch(item, timeout_s=45.0) for _ in range(parallel)),
            return_exceptions=True,
        )
        elapsed = (time.perf_counter() - started) * 1000.0

        successes = [
            r for r in results if isinstance(r, ProbeResult) and r.ok
        ]
        at_capacity = sum(
            1 for r in results
            if isinstance(r, ProbeResult) and r.payload.get("at_capacity")
        )
        if not successes:
            detail = "all concurrent requests rejected" if at_capacity else "all failed"
            return ProbeResult.failure("throughput", detail, elapsed)

        mean_latency = sum(r.latency_ms for r in successes) / len(successes)
        return ProbeResult(
            ok=True,
            kind="throughput",
            latency_ms=elapsed,
            first_byte_ms=min(
                (r.first_byte_ms for r in successes if r.first_byte_ms is not None),
                default=None,
            ),
            detail=f"{len(successes)}/{parallel} concurrent, mean {mean_latency:.0f}ms",
            payload={
                "parallel": parallel,
                "succeeded": len(successes),
                "at_capacity": at_capacity,
                "mean_latency_ms": mean_latency,
                "wall_ms": elapsed,
            },
        )


def _header_int(headers, key: str, default: int) -> int:
    try:
        return int(headers.get(key, default))
    except (TypeError, ValueError):
        return default


def _extract_transcript(body: bytes) -> Optional[str]:
    """Pull the transcript out of any of the shapes miners legitimately return.

    ``ASRAPI/utils/utils.py`` already tolerates a flat ``{"text": ...}``, a
    ``{"segments": [...]}`` list, and a bare list of segments, so the validator
    must accept the same range rather than failing miners on formatting.
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        text = body.decode("utf-8", "replace").strip()
        return text or None

    if isinstance(payload, str):
        return payload.strip() or None

    if isinstance(payload, dict):
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
        segments = payload.get("segments")
    elif isinstance(payload, list):
        segments = payload
    else:
        return None

    if isinstance(segments, list):
        parts = [
            str(segment.get("text", "")).strip()
            for segment in segments
            if isinstance(segment, dict)
        ]
        joined = " ".join(part for part in parts if part)
        return joined or None

    return None
