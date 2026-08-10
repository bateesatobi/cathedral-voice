"""
Proxying to the official ASR and TTS containers.

The miner sidecar owns no models. It fronts the published Docker images running
on the same host, adding exactly three things the images do not provide:
admission control, health aggregation, and identity headers. Everything else is
passed through byte-for-byte so that the serving interface stays unmodified
(TDD 4.2, 9.2).
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Optional, Tuple

import aiohttp

logger = logging.getLogger("violet.miner.upstream")


class UpstreamError(RuntimeError):
    """An upstream container failed or was unreachable."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


class AtCapacity(UpstreamError):
    """Admission control rejected the request.

    Distinct from a generic failure because the router treats 503 as "try
    another miner" rather than "this miner is broken" (TDD 8).
    """

    def __init__(self, service: str, limit: int):
        super().__init__(
            f"{service} at capacity ({limit} concurrent)", status=503
        )


class Slots:
    """Counting semaphore that refuses rather than queues.

    Queueing would hide saturation from the router: the request would eventually
    succeed, but slowly, and the miner would keep attracting traffic it cannot
    serve. Refusing immediately lets the router shed to another miner while the
    first byte is still within budget.
    """

    def __init__(self, limit: int, service: str):
        self.limit = max(0, int(limit))
        self.service = service
        self._active = 0
        self._lock = asyncio.Lock()

    @property
    def active(self) -> int:
        return self._active

    @property
    def load_factor(self) -> float:
        if self.limit <= 0:
            return 0.0
        return min(1.0, self._active / self.limit)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        async with self._lock:
            # A limit of zero means "unbounded" - the operator has not declared
            # one and no GPU was detected, so there is nothing to protect.
            if self.limit > 0 and self._active >= self.limit:
                raise AtCapacity(self.service, self.limit)
            self._active += 1
        try:
            yield
        finally:
            async with self._lock:
                self._active = max(0, self._active - 1)


class UpstreamClient:
    """HTTP client for one upstream container."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 600.0,
        health_timeout_s: float = 4.0,
        name: str = "upstream",
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.health_timeout_s = health_timeout_s
        self.name = name
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_health: Optional[bool] = None
        self._last_health_at = 0.0
        self._last_latency_ms: Optional[float] = None

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            # No global timeout: batch transcription of a 170-minute video is a
            # legitimate multi-minute request. Per-call timeouts are applied at
            # the call site instead.
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=10)
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            raise UpstreamError(f"{self.name} client not started")
        return self._session

    def url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    @property
    def last_latency_ms(self) -> Optional[float]:
        return self._last_latency_ms

    async def healthy(self, *, max_age_s: float = 5.0, path: str = "/health") -> bool:
        """Probe the upstream, reusing a recent result.

        Cached because ``/health`` on the sidecar is hit by every validator and
        by the router's health loop; forwarding each one to the model container
        would put avoidable load on the GPU host.
        """
        now = time.time()
        if self._last_health is not None and (now - self._last_health_at) < max_age_s:
            return self._last_health

        ok = False
        try:
            started = time.perf_counter()
            async with self.session.get(
                self.url(path),
                timeout=aiohttp.ClientTimeout(total=self.health_timeout_s),
            ) as response:
                await response.read()
                ok = response.status < 500
                self._last_latency_ms = (time.perf_counter() - started) * 1000.0
        except Exception as exc:
            logger.debug("%s health probe failed: %s", self.name, exc)
            ok = False

        self._last_health = ok
        self._last_health_at = now
        return ok

    async def post_json(
        self, path: str, payload: dict, *, timeout_s: Optional[float] = None
    ) -> Tuple[int, bytes, Dict[str, str]]:
        """POST JSON and buffer the whole response."""
        try:
            async with self.session.post(
                self.url(path),
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout_s or self.timeout_s),
            ) as response:
                body = await response.read()
                return response.status, body, dict(response.headers)
        except asyncio.TimeoutError as exc:
            raise UpstreamError(f"{self.name} timed out on {path}", 504) from exc
        except aiohttp.ClientError as exc:
            raise UpstreamError(f"{self.name} unreachable: {exc}", 502) from exc

    async def stream_post(
        self,
        path: str,
        *,
        json_payload: Optional[dict] = None,
        data: Optional[aiohttp.FormData] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout_s: Optional[float] = None,
        chunk_size: int = 8192,
    ) -> AsyncIterator[Tuple[Optional[Tuple[int, Dict[str, str]]], Optional[bytes]]]:
        """Stream a POST response.

        Yields ``((status, headers), None)`` first so the caller can propagate
        the upstream's status and PCM framing headers before any audio arrives,
        then ``(None, chunk)`` for each body chunk. Structured this way because
        FastAPI needs the status line before the response body starts.
        """
        try:
            async with self.session.post(
                self.url(path),
                json=json_payload,
                data=data,
                headers=headers or {},
                timeout=aiohttp.ClientTimeout(total=timeout_s or self.timeout_s),
            ) as response:
                yield (response.status, dict(response.headers)), None
                async for chunk in response.content.iter_chunked(chunk_size):
                    yield None, chunk
        except asyncio.TimeoutError as exc:
            raise UpstreamError(f"{self.name} timed out streaming {path}", 504) from exc
        except aiohttp.ClientError as exc:
            raise UpstreamError(f"{self.name} stream failed: {exc}", 502) from exc

    async def get(
        self, path: str, *, timeout_s: Optional[float] = None
    ) -> Tuple[int, bytes, Dict[str, str]]:
        try:
            async with self.session.get(
                self.url(path),
                timeout=aiohttp.ClientTimeout(total=timeout_s or self.timeout_s),
            ) as response:
                body = await response.read()
                return response.status, body, dict(response.headers)
        except asyncio.TimeoutError as exc:
            raise UpstreamError(f"{self.name} timed out on {path}", 504) from exc
        except aiohttp.ClientError as exc:
            raise UpstreamError(f"{self.name} unreachable: {exc}", 502) from exc

    async def post_multipart(
        self, path: str, data: aiohttp.FormData, *, timeout_s: Optional[float] = None
    ) -> Tuple[int, bytes, Dict[str, str]]:
        try:
            async with self.session.post(
                self.url(path),
                data=data,
                timeout=aiohttp.ClientTimeout(total=timeout_s or self.timeout_s),
            ) as response:
                body = await response.read()
                return response.status, body, dict(response.headers)
        except asyncio.TimeoutError as exc:
            raise UpstreamError(f"{self.name} timed out on {path}", 504) from exc
        except aiohttp.ClientError as exc:
            raise UpstreamError(f"{self.name} unreachable: {exc}", 502) from exc
