"""
Drop-in Violet integration for the Avoices backend (ASRAPI).

Copy this file to ``ASRAPI/utils/violet_integration.py`` and wire it as
described in ``INTEGRATION.md``. It is written to be added to a running
production service safely:

* **Off by default.** Nothing happens until ``VIOLET_ROUTER_ENABLED=true``.
* **Never worse than today.** Every path falls back to the existing single-host
  ASR/TTS servers, so a subnet-wide outage degrades to current behaviour rather
  than an incident.
* **No product logic changes.** The helpers return exactly the shapes the
  existing call sites already handle.

The module keeps one process-wide router. Creating one per request would
rediscover the metagraph on every call.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, AsyncIterator, Dict, Optional, Tuple

logger = logging.getLogger("violet.integration")

_router = None
_router_lock: Optional[asyncio.Lock] = None
_init_failed = False


def _lock() -> asyncio.Lock:
    global _router_lock
    if _router_lock is None:
        _router_lock = asyncio.Lock()
    return _router_lock


def enabled() -> bool:
    return os.getenv("VIOLET_ROUTER_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }


async def get_router():
    """Return the shared router, or ``None`` when disabled or unavailable.

    A failure to initialise is remembered so a broken chain endpoint does not
    make every request pay a connection timeout - Avoices keeps serving through
    the legacy path instead.
    """
    global _router, _init_failed

    if not enabled() or _init_failed:
        return None
    if _router is not None:
        return _router

    async with _lock():
        if _router is not None:
            return _router
        try:
            from violet.chain import ChainClient
            from violet.config import load_config
            from violet.router import VioletRouter

            config = load_config()
            chain = None
            if config.chain.netuid:
                # Read-only: the backend never signs anything on chain.
                config.chain.signing_enabled = False
                chain = await ChainClient(config.chain).connect()

            router = VioletRouter(config.router, chain=chain)
            await router.start()
            _router = router
            logger.info("Violet router initialised: %s", router.status())
        except Exception as exc:
            _init_failed = True
            logger.error(
                "Violet router failed to initialise (%s); Avoices will continue "
                "using the legacy ASR/TTS endpoints",
                exc,
            )
            return None

    return _router


async def shutdown_router() -> None:
    """Call from the FastAPI lifespan shutdown."""
    global _router
    if _router is not None:
        await _router.stop()
        _router = None


# --------------------------------------------------------------------------
# ASR
# --------------------------------------------------------------------------


async def transcribe_via_violet(
    audio_path: str,
    language: str,
    response_format: str = "json",
    *,
    audio_seconds: float = 0.0,
) -> Optional[Any]:
    """Transcribe through the subnet.

    Returns the parsed body in whatever shape the caller asked for, or ``None``
    when the subnet could not serve it - in which case the caller should use its
    existing code path unchanged.
    """
    router = await get_router()
    if router is None:
        return None

    try:
        with open(audio_path, "rb") as handle:
            audio = handle.read()

        response = await router.transcribe(
            audio,
            filename=os.path.basename(audio_path),
            language=language,
            response_format=response_format,
            audio_seconds=audio_seconds,
        )
        if response.status != 200:
            logger.warning("Violet transcription returned HTTP %s", response.status)
            return None

        if response_format in {"text", "srt", "vtt"}:
            return response.text
        return response.json()
    except Exception as exc:
        logger.warning("Violet transcription failed (%s); falling back", exc)
        return None


async def asr_stream_url(session_id: str, language: str = "eng") -> Optional[Tuple[str, Any]]:
    """WebSocket URL for a realtime ASR session, plus the miner handle.

    Sticky for the life of ``session_id``; pass the handle back to
    :func:`finish_stream` when the session ends so the work is credited.
    """
    router = await get_router()
    if router is None:
        return None
    url, miner, _ = router.stream_target("asr", session_id, language=language)
    return (url, miner) if url else None


# --------------------------------------------------------------------------
# TTS
# --------------------------------------------------------------------------


async def synthesize_via_violet(
    text: str, speaker_id: str, *, temperature: float = 0.7
) -> Optional[Tuple[bytes, Dict[str, int]]]:
    """Synthesize through the subnet.

    Returns ``(pcm, meta)`` where ``meta`` carries the sample rate, channel
    count and sample width - the same tuple shape
    ``utils/tts_synthesis.py`` already threads through.
    """
    router = await get_router()
    if router is None:
        return None

    try:
        response = await router.synthesize(
            text, speaker_id=speaker_id, temperature=temperature
        )
        if response.status != 200 or not response.body:
            logger.warning("Violet synthesis returned HTTP %s", response.status)
            return None

        meta = {
            "sample_rate": int(response.headers.get("x-audio-sample-rate", 24000)),
            "channels": int(response.headers.get("x-audio-channels", 1)),
            "sample_width": int(response.headers.get("x-audio-sample-width", 2)),
        }
        return response.body, meta
    except Exception as exc:
        logger.warning("Violet synthesis failed (%s); falling back", exc)
        return None


async def synthesize_stream_via_violet(
    text: str, speaker_id: str, *, temperature: float = 0.7
) -> Optional[AsyncIterator[bytes]]:
    """Streaming synthesis generator, or ``None`` when unavailable."""
    router = await get_router()
    if router is None:
        return None

    async def generate() -> AsyncIterator[bytes]:
        async for headers, chunk in router.synthesize_stream(
            text, speaker_id=speaker_id, temperature=temperature
        ):
            if chunk:
                yield chunk

    return generate()


async def tts_stream_url(session_id: str) -> Optional[Tuple[str, Any]]:
    router = await get_router()
    if router is None:
        return None
    url, miner, _ = router.stream_target("tts", session_id)
    return (url, miner) if url else None


def finish_stream(
    session_id: str, miner: Any, *, service: str, seconds: float, ok: bool = True
) -> None:
    """Release a sticky session and credit the work it did."""
    if _router is None or miner is None:
        return
    _router.end_stream(session_id, miner, service=service, seconds=seconds, ok=ok)


# --------------------------------------------------------------------------
# Admin / validator surface
# --------------------------------------------------------------------------


async def router_status() -> Dict[str, object]:
    """Live subnet status for the Avoices admin console."""
    router = await get_router()
    if router is None:
        return {"enabled": enabled(), "available": False}
    status = router.status()
    status["available"] = True
    return status


async def build_work_report(since_seconds: float = 7 * 86400) -> Dict[str, object]:
    """Signed work counters for validators (TDD 7, Work score).

    Serve this from ``GET /violet/work-report``. Validators verify the signature
    with the shared secret before ingesting, so the endpoint may be public - but
    keep the bearer token on it anyway, since the counters reveal Avoices
    traffic volume.
    """
    router = await get_router()
    if router is None:
        return {"entries": [], "error": "router unavailable"}
    return router.work_report(time.time() - since_seconds)
