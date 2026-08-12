"""
The smart router: the Avoices backend's entry point into the Violet miner pool.

Design constraint that shapes everything here: enabling the router must not be
able to make Avoices less available than it is today. So every call falls back
to the legacy single-host endpoint when no miner can serve it, the router can be
disabled with one environment variable, and no product logic changes - the
methods return exactly what the existing ASRAPI call sites already expect.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from urllib.parse import quote

import aiohttp

from ..chain import ChainClient
from ..config import RouterConfig
from ..constants import SERVICE_ASR, SERVICE_TTS
from ..protocol import (
    DEFAULT_SAMPLE_RATE,
    DEFAULT_SAMPLE_WIDTH,
    HEADER_SAMPLE_RATE,
    HEADER_SAMPLE_WIDTH,
    PATH_ASR_STREAM_WS,
    PATH_ASR_TRANSCRIBE,
    PATH_TTS_CLONE,
    PATH_TTS_STREAM,
    PATH_TTS_STREAM_WS,
    PATH_TTS_VOICES,
)
from .receipts import Receipt, ReceiptLedger
from .registry import MinerEndpoint, MinerRegistry
from .selector import StickySessions, select

logger = logging.getLogger("violet.router")


class NoMinerAvailable(RuntimeError):
    """No miner could serve the request and no fallback was configured."""


@dataclass
class RoutedResponse:
    """A completed non-streaming call."""

    status: int
    body: bytes
    headers: Dict[str, str]
    hotkey: str
    latency_ms: float
    #: True when served by the legacy fallback rather than a miner.
    fallback: bool = False

    def json(self) -> Any:
        return json.loads(self.body)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class VioletRouter:
    """Routes ASR and TTS work across the miner pool with failover."""

    def __init__(
        self,
        config: RouterConfig,
        *,
        chain: Optional[ChainClient] = None,
        session: Optional[aiohttp.ClientSession] = None,
        ledger: Optional[ReceiptLedger] = None,
    ):
        self.config = config
        self._session = session
        self._owns_session = session is None
        self.registry = MinerRegistry(config, chain=chain, session=session)
        self.ledger = ledger or ReceiptLedger(config.receipts_db_path)
        self.sessions = StickySessions()
        self._started = False

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=10)
            )
            self._owns_session = True
        return self._session

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    async def start(self) -> None:
        if self._started or not self.config.enabled:
            return
        await self.registry.start()
        self._started = True
        logger.info("Violet router started: %s", self.registry.status()["healthy"])

    async def stop(self) -> None:
        if self._started:
            await self.registry.stop()
            self._started = False
        self.ledger.flush()
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    # -- headers -----------------------------------------------------------

    def _headers(self, miner: Optional[MinerEndpoint] = None) -> Dict[str, str]:
        token = ""
        if miner is not None and miner.access_token:
            token = miner.access_token
        elif self.config.access_token:
            token = self.config.access_token
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}"}

    # -- ASR ---------------------------------------------------------------

    async def transcribe(
        self,
        audio: bytes,
        *,
        filename: str = "audio.wav",
        language: str = "eng",
        response_format: str = "json",
        content_type: str = "audio/wav",
        audio_seconds: float = 0.0,
        timeout_s: float = 3600.0,
    ) -> RoutedResponse:
        """Batch transcription, with failover across miners.

        Mirrors ``ASRAPI/utils/utils.py:transcribe_single_file``: same multipart
        fields, same response handling, so the call site changes by one line.
        """

        def build_form() -> aiohttp.FormData:
            # Rebuilt per attempt: an aiohttp FormData is consumed on send and
            # cannot be replayed against the next miner.
            form = aiohttp.FormData()
            form.add_field("file", audio, filename=filename, content_type=content_type)
            form.add_field("language", language)
            form.add_field("response_format", response_format)
            return form

        return await self._request_with_failover(
            service=SERVICE_ASR,
            path=PATH_ASR_TRANSCRIBE,
            build_data=build_form,
            timeout_s=timeout_s,
            seconds=audio_seconds,
            fallback_url=self.config.fallback_asr_url,
        )

    # -- TTS ---------------------------------------------------------------

    async def synthesize(
        self,
        text: str,
        *,
        speaker_id: str,
        temperature: float = 0.7,
        timeout_s: float = 300.0,
    ) -> RoutedResponse:
        """Synthesize to a buffered PCM body.

        Mirrors ``ASRAPI/utils/tts_synthesis.py``, including the ``x-audio-*``
        framing headers the caller uses to wrap the PCM.
        """
        payload = {"text": text, "speaker_id": speaker_id, "temperature": temperature}
        # Work is recorded here rather than inside the failover helper: the
        # audio duration is only knowable once the body has been read, and
        # recording twice would double-count the request.
        response = await self._request_with_failover(
            service=SERVICE_TTS,
            path=PATH_TTS_STREAM,
            json_payload=payload,
            timeout_s=timeout_s,
            fallback_url=self.config.fallback_tts_url,
            count_work=False,
        )
        if response.status == 200 and response.body and not response.fallback:
            self.ledger.record(
                Receipt(
                    hotkey=response.hotkey,
                    service=SERVICE_TTS,
                    ok=True,
                    seconds=_pcm_seconds(response.body, response.headers),
                    latency_ms=response.latency_ms,
                    first_byte_ms=response.latency_ms,
                )
            )
        return response

    async def synthesize_stream(
        self,
        text: str,
        *,
        speaker_id: str,
        temperature: float = 0.7,
        timeout_s: float = 300.0,
    ) -> AsyncIterator[Tuple[Optional[Dict[str, str]], Optional[bytes]]]:
        """Stream synthesis, yielding headers first then PCM chunks.

        Failover applies only before the first byte. Once audio has been sent to
        the client, switching miners would splice two different voices into one
        utterance, so a mid-stream failure is surfaced rather than papered over.
        """
        payload = {"text": text, "speaker_id": speaker_id, "temperature": temperature}
        attempted: List[str] = []

        for _ in range(max(1, self.config.max_attempts)):
            miner = self._pick(SERVICE_TTS, exclude=attempted)
            if miner is None:
                break
            attempted.append(miner.hotkey)

            started = time.perf_counter()
            miner.inflight += 1
            first_byte_ms: Optional[float] = None
            total = 0
            try:
                async with self.session.post(
                    f"{miner.endpoint}{PATH_TTS_STREAM}",
                    json=payload,
                    headers=self._headers(miner),
                    timeout=aiohttp.ClientTimeout(total=timeout_s),
                ) as response:
                    if response.status != 200:
                        self.registry.mark_failure(miner)
                        continue
                    headers = dict(response.headers)
                    yield headers, None
                    async for chunk in response.content.iter_chunked(8192):
                        if first_byte_ms is None:
                            first_byte_ms = (time.perf_counter() - started) * 1000.0
                            self.registry.mark_success(miner, first_byte_ms)
                        total += len(chunk)
                        yield None, chunk

                self.ledger.record(
                    Receipt(
                        hotkey=miner.hotkey,
                        uid=miner.uid,
                        service=SERVICE_TTS,
                        ok=total > 0,
                        seconds=_pcm_seconds_from(total, headers),
                        latency_ms=(time.perf_counter() - started) * 1000.0,
                        first_byte_ms=first_byte_ms,
                    )
                )
                return
            except Exception as exc:
                if first_byte_ms is not None:
                    # Already streaming to the client: cannot retry elsewhere.
                    logger.error(
                        "stream from %s failed after %d bytes: %s",
                        miner.hotkey[:10], total, exc,
                    )
                    self.registry.mark_failure(miner)
                    raise
                logger.warning("miner %s failed before first byte: %s", miner.hotkey[:10], exc)
                self.registry.mark_failure(miner)
            finally:
                miner.inflight = max(0, miner.inflight - 1)

        # Fallback: the legacy single-host TTS server.
        if not self.config.fallback_tts_url:
            raise NoMinerAvailable("no TTS miner available and no fallback configured")

        logger.warning("falling back to the legacy TTS endpoint")
        async with self.session.post(
            f"{self.config.fallback_tts_url.rstrip('/')}{PATH_TTS_STREAM}",
            json=payload,
            headers={"Ngrok-Skip-Browser-Warning": "true"},
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as response:
            yield dict(response.headers), None
            async for chunk in response.content.iter_chunked(8192):
                yield None, chunk

    async def clone_voice(
        self,
        text: str,
        reference_audio: bytes,
        *,
        filename: str = "reference.wav",
        content_type: str = "audio/wav",
        temperature: float = 0.7,
        timeout_s: float = 300.0,
    ) -> RoutedResponse:
        def build_form() -> aiohttp.FormData:
            form = aiohttp.FormData()
            form.add_field("text", text)
            form.add_field("temperature", str(temperature))
            form.add_field(
                "reference_audio", reference_audio,
                filename=filename, content_type=content_type,
            )
            return form

        return await self._request_with_failover(
            service=SERVICE_TTS,
            path=PATH_TTS_CLONE,
            build_data=build_form,
            timeout_s=timeout_s,
            fallback_url=self.config.fallback_tts_url,
        )

    async def voices(self, timeout_s: float = 10.0) -> RoutedResponse:
        return await self._request_with_failover(
            service=SERVICE_TTS,
            path=PATH_TTS_VOICES,
            method="GET",
            timeout_s=timeout_s,
            fallback_url=self.config.fallback_tts_url,
            count_work=False,
        )

    # -- WebSocket targets -------------------------------------------------

    def stream_target(
        self, service: str, session_id: str, *, language: str = "eng"
    ) -> Tuple[Optional[str], Optional[MinerEndpoint], bool]:
        """Resolve the WebSocket URL for a streaming session.

        Returns ``(url, miner, is_fallback)``. Sticky: the same session_id keeps
        the same miner, because ASR decoder state lives on the miner and moving
        mid-stream would restart the transcript.
        """
        pinned = self.sessions.get(session_id)
        miner: Optional[MinerEndpoint] = None

        if pinned:
            candidate = self.registry.get(pinned)
            if candidate and candidate.healthy and candidate.serves(service):
                miner = candidate
            else:
                self.sessions.release(session_id)

        if miner is None:
            miner = self._pick(service)
            if miner is not None:
                self.sessions.pin(session_id, miner.hotkey)

        path = PATH_ASR_STREAM_WS if service == SERVICE_ASR else PATH_TTS_STREAM_WS
        if miner is not None:
            url = _to_ws(miner.endpoint) + path
            params = []
            if service == SERVICE_ASR:
                params.append(f"language={quote(language)}")
            if miner.access_token:
                params.append(f"token={quote(miner.access_token)}")
            elif self.config.access_token:
                params.append(f"token={quote(self.config.access_token)}")
            if params:
                url = f"{url}?{'&'.join(params)}"
            return url, miner, False

        fallback = (
            self.config.fallback_asr_url
            if service == SERVICE_ASR
            else self.config.fallback_tts_url
        )
        if not fallback:
            return None, None, True
        url = _to_ws(fallback.rstrip("/")) + path
        if service == SERVICE_ASR:
            url = f"{url}?language={language}"
        return url, None, True

    def end_stream(
        self,
        session_id: str,
        miner: Optional[MinerEndpoint],
        *,
        service: str,
        seconds: float,
        ok: bool,
        first_byte_ms: Optional[float] = None,
    ) -> None:
        """Close out a streaming session and record the work it did."""
        self.sessions.release(session_id)
        if miner is None:
            return
        if ok:
            self.registry.mark_success(miner, first_byte_ms)
        else:
            self.registry.mark_failure(miner)
        self.ledger.record(
            Receipt(
                hotkey=miner.hotkey,
                uid=miner.uid,
                service=service,
                ok=ok,
                seconds=seconds,
                first_byte_ms=first_byte_ms,
            )
        )

    # -- core --------------------------------------------------------------

    def _pick(
        self, service: str, exclude: Optional[List[str]] = None
    ) -> Optional[MinerEndpoint]:
        if not self.config.enabled:
            return None
        return select(self.registry.healthy_for(service), self.config, exclude=exclude)

    async def _request_with_failover(
        self,
        *,
        service: str,
        path: str,
        method: str = "POST",
        json_payload: Optional[dict] = None,
        build_data=None,
        timeout_s: float = 300.0,
        seconds: float = 0.0,
        fallback_url: str = "",
        count_work: bool = True,
    ) -> RoutedResponse:
        attempted: List[str] = []
        last_error = ""

        for attempt in range(max(1, self.config.max_attempts)):
            miner = self._pick(service, exclude=attempted)
            if miner is None:
                break
            attempted.append(miner.hotkey)

            started = time.perf_counter()
            miner.inflight += 1
            try:
                async with self._open(
                    method, f"{miner.endpoint}{path}", json_payload, build_data, timeout_s, miner=miner
                ) as response:
                    body = await response.read()
                    latency_ms = (time.perf_counter() - started) * 1000.0

                    if response.status == 503:
                        # Admission control, not a fault. Try the next miner
                        # without holding it against this one.
                        last_error = "at capacity"
                        logger.debug("miner %s at capacity", miner.hotkey[:10])
                        continue
                    if response.status >= 500:
                        last_error = f"HTTP {response.status}"
                        self.registry.mark_failure(miner)
                        continue

                    self.registry.mark_success(miner, latency_ms)
                    if count_work and response.status == 200:
                        self.ledger.record(
                            Receipt(
                                hotkey=miner.hotkey,
                                uid=miner.uid,
                                service=service,
                                ok=True,
                                seconds=seconds,
                                latency_ms=latency_ms,
                                first_byte_ms=latency_ms,
                            )
                        )
                    return RoutedResponse(
                        status=response.status,
                        body=body,
                        headers=dict(response.headers),
                        hotkey=miner.hotkey,
                        latency_ms=latency_ms,
                    )
            except asyncio.TimeoutError:
                last_error = f"timeout after {timeout_s:.0f}s"
                self.registry.mark_failure(miner)
            except aiohttp.ClientError as exc:
                last_error = str(exc)
                self.registry.mark_failure(miner)
            finally:
                miner.inflight = max(0, miner.inflight - 1)

        # No healthy miner served the request. Legacy fallback is opt-in via
        # VIOLET_FALLBACK_ASR_URL / VIOLET_FALLBACK_TTS_URL — leave empty to
        # keep traffic on the miner pool only.
        if not fallback_url:
            raise NoMinerAvailable(
                f"no {service.upper()} miner served the request "
                f"({len(attempted)} attempted; last error: {last_error or 'none available'})"
            )

        logger.warning(
            "falling back to the legacy %s endpoint after %d miner attempt(s): %s",
            service.upper(), len(attempted), last_error or "no miners available",
        )
        started = time.perf_counter()
        async with self._open(
            method, f"{fallback_url.rstrip('/')}{path}", json_payload, build_data, timeout_s,
            extra_headers={"Ngrok-Skip-Browser-Warning": "true"},
        ) as response:
            body = await response.read()
            return RoutedResponse(
                status=response.status,
                body=body,
                headers=dict(response.headers),
                hotkey="",
                latency_ms=(time.perf_counter() - started) * 1000.0,
                fallback=True,
            )

    def _open(
        self,
        method: str,
        url: str,
        json_payload: Optional[dict],
        build_data,
        timeout_s: float,
        extra_headers: Optional[Dict[str, str]] = None,
        miner: Optional[MinerEndpoint] = None,
    ):
        headers = {**self._headers(miner), **(extra_headers or {})}
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        if method == "GET":
            return self.session.get(url, headers=headers, timeout=timeout)
        return self.session.post(
            url,
            json=json_payload,
            data=build_data() if build_data else None,
            headers=headers,
            timeout=timeout,
        )

    # -- observability -----------------------------------------------------

    def status(self) -> Dict[str, object]:
        status = self.registry.status()
        status["enabled"] = self.config.enabled
        status["sticky_sessions"] = len(self.sessions)
        status["work_24h"] = self.ledger.stats(time.time() - 86400)
        return status

    def work_report(self, since: float) -> Dict[str, object]:
        return self.ledger.build_report(
            since,
            secret=self.config.work_report_signing_key,
            signer="avoices-router",
        )


def _to_ws(url: str) -> str:
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    return url


def _pcm_seconds(body: bytes, headers: Dict[str, str]) -> float:
    return _pcm_seconds_from(len(body), headers)


def _pcm_seconds_from(byte_count: int, headers: Dict[str, str]) -> float:
    """Audio duration of a PCM buffer, from the framing headers."""
    try:
        rate = int(headers.get(HEADER_SAMPLE_RATE, DEFAULT_SAMPLE_RATE))
        width = int(headers.get(HEADER_SAMPLE_WIDTH, DEFAULT_SAMPLE_WIDTH))
    except (TypeError, ValueError):
        rate, width = DEFAULT_SAMPLE_RATE, DEFAULT_SAMPLE_WIDTH
    if rate <= 0 or width <= 0:
        return 0.0
    return round(byte_count / float(rate * width), 3)
