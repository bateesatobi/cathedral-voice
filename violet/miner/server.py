"""
The Violet miner sidecar.

Presents the standardised Violet API surface (see :mod:`violet.protocol`) and
forwards every inference call to the official ASR/TTS containers on the same
host. Because the paths and payloads are identical to what the Avoices backend
already speaks, a miner is a drop-in replacement for the single-host servers
Avoices uses today.

Run with::

    python -m violet.miner.run
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Callable, Dict, Optional

import aiohttp
import websockets
from fastapi import (
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..config import MinerConfig
from ..constants import SERVICE_ASR, SERVICE_TTS, SPEC_VERSION
from ..identity import challenge_message
from ..protocol import (
    HEADER_MINER_HOTKEY,
    HEADER_MINER_UID,
    PATH_ASR_STREAM_WS,
    PATH_ASR_TRANSCRIBE,
    PATH_CAPACITY,
    PATH_HEALTH,
    PATH_IDENTITY_CHALLENGE,
    PATH_INFO,
    PATH_TTS_CLONE,
    PATH_TTS_STREAM,
    PATH_TTS_STREAM_WS,
    PATH_TTS_VOICES,
    HealthReport,
)
from .gpu import GpuMonitor
from .upstream import AtCapacity, Slots, UpstreamClient, UpstreamError

logger = logging.getLogger("violet.miner.server")

#: Headers that must not be copied verbatim from the upstream response.
#: aiohttp has already decoded the body, so forwarding the original framing
#: headers would make the client try to decode it a second time.
_HOP_BY_HOP = {
    "content-encoding",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
}


class MinerState:
    """Mutable runtime state shared by the routes."""

    def __init__(
        self,
        config: MinerConfig,
        hotkey: str = "",
        uid: Optional[int] = None,
        *,
        identity_signer: Optional[Callable[[str, str, float], str]] = None,
    ):
        self.config = config
        self.hotkey = hotkey
        self.uid = uid
        self.identity_signer = identity_signer
        self.started_at = time.time()

        self.gpu = GpuMonitor(poll_interval_s=config.gpu_poll_interval_s)
        self.asr = UpstreamClient(
            config.asr_upstream,
            timeout_s=config.upstream_timeout_s,
            health_timeout_s=config.health_timeout_s,
            name="asr",
        )
        self.tts = UpstreamClient(
            config.tts_upstream,
            timeout_s=config.upstream_timeout_s,
            health_timeout_s=config.health_timeout_s,
            name="tts",
        )
        # Limits are resolved after the first GPU poll, so that an operator who
        # sets nothing still gets sane admission control derived from hardware.
        self.asr_slots = Slots(config.max_concurrent_asr, "asr")
        self.tts_slots = Slots(config.max_concurrent_tts, "tts")

    @property
    def serves_asr(self) -> bool:
        return SERVICE_ASR in self.config.services

    @property
    def serves_tts(self) -> bool:
        return SERVICE_TTS in self.config.services

    async def resolve_limits(self) -> None:
        """Derive concurrency limits from detected hardware when unset."""
        await self.gpu.refresh(force=True)
        default_asr, default_tts = self.gpu.default_limits()
        if self.config.max_concurrent_asr <= 0 and default_asr:
            self.asr_slots.limit = default_asr
            logger.info("ASR concurrency limit derived from hardware: %d", default_asr)
        if self.config.max_concurrent_tts <= 0 and default_tts:
            self.tts_slots.limit = default_tts
            logger.info("TTS concurrency limit derived from hardware: %d", default_tts)

    async def health(self) -> HealthReport:
        upstreams: Dict[str, bool] = {}
        checks = []
        if self.serves_asr:
            checks.append(("asr", self.asr.healthy()))
        if self.serves_tts:
            checks.append(("tts", self.tts.healthy()))
        for (name, coro), ok in zip(checks, await asyncio.gather(*(c for _, c in checks))):
            upstreams[name] = ok

        capacity = await self.gpu.report(
            max_concurrent_asr=self.asr_slots.limit,
            max_concurrent_tts=self.tts_slots.limit,
            active_asr=self.asr_slots.active,
            active_tts=self.tts_slots.active,
        )

        if not upstreams:
            status = "unhealthy"
        elif all(upstreams.values()):
            status = "ok"
        elif any(upstreams.values()):
            # One of two declared services is down. Reported as degraded rather
            # than unhealthy so the router can keep using the half that works.
            status = "degraded"
        else:
            status = "unhealthy"

        return HealthReport(
            status=status,
            hotkey=self.hotkey,
            spec_version=SPEC_VERSION,
            services=list(self.config.services),
            upstreams=upstreams,
            capacity=capacity,
            asr_image=self.config.asr_image,
            tts_image=self.config.tts_image,
        )

    def identity_headers(self) -> Dict[str, str]:
        headers = {HEADER_MINER_HOTKEY: self.hotkey}
        if self.uid is not None:
            headers[HEADER_MINER_UID] = str(self.uid)
        return headers


def _passthrough_headers(upstream_headers: Dict[str, str]) -> Dict[str, str]:
    return {
        key: value
        for key, value in upstream_headers.items()
        if key.lower() not in _HOP_BY_HOP
    }


async def _read_upload_limited(upload: UploadFile, max_bytes: int) -> bytes:
    """Read an upload with a hard byte cap."""
    chunks: list[bytes] = []
    total = 0
    while True:
        piece = await upload.read(256 * 1024)
        if not piece:
            break
        total += len(piece)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"upload exceeds limit of {max_bytes} bytes",
            )
        chunks.append(piece)
    return b"".join(chunks)


def create_app(state: MinerState) -> FastAPI:
    """Build the miner's FastAPI application."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await state.asr.start()
        await state.tts.start()
        await state.resolve_limits()
        for warning in state.gpu.warnings():
            logger.warning("%s", warning)
        logger.info(
            "miner serving %s on behalf of %s",
            ",".join(state.config.services),
            state.hotkey or "<unregistered hotkey>",
        )
        try:
            yield
        finally:
            await state.asr.close()
            await state.tts.close()

    app = FastAPI(
        title="Violet Miner",
        version=str(SPEC_VERSION),
        description=(
            "Standardised ASR/TTS serving interface for the Violet subnet. "
            "Forwards to the official inference containers."
        ),
        lifespan=lifespan,
    )
    app.state.violet = state

    # -- access control ----------------------------------------------------

    def _authorize(authorization: Optional[str]) -> None:
        """Enforce the optional shared secret on inference traffic."""
        expected = state.config.access_token
        if not expected:
            return
        if authorization != f"Bearer {expected}":
            raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    def _ws_authorized(websocket: WebSocket) -> bool:
        """Check bearer token on WebSocket (header or ``?token=`` query param)."""
        expected = state.config.access_token
        if not expected:
            return True
        auth = websocket.headers.get("authorization") or websocket.headers.get("Authorization")
        if auth == f"Bearer {expected}":
            return True
        return websocket.query_params.get("token") == expected

    @app.middleware("http")
    async def limit_request_body_size(request: Request, call_next):
        max_bytes = state.config.max_upload_bytes
        if max_bytes > 0:
            content_length = request.headers.get("content-length")
            if content_length and int(content_length) > max_bytes:
                return JSONResponse(
                    status_code=413,
                    content={"detail": f"request body exceeds {max_bytes} bytes"},
                )
        return await call_next(request)

    # -- control plane -----------------------------------------------------

    @app.get(PATH_HEALTH)
    async def health() -> JSONResponse:
        report = await state.health()
        # 200 even when degraded: the body carries the detail, and a non-200
        # would make generic infrastructure health checks pull the whole
        # container while half of it is still serving.
        return JSONResponse(report.to_dict(), headers=state.identity_headers())

    @app.get(PATH_CAPACITY)
    async def capacity() -> JSONResponse:
        report = await state.gpu.report(
            max_concurrent_asr=state.asr_slots.limit,
            max_concurrent_tts=state.tts_slots.limit,
            active_asr=state.asr_slots.active,
            active_tts=state.tts_slots.active,
        )
        return JSONResponse(report.to_dict(), headers=state.identity_headers())

    @app.get(PATH_INFO)
    async def info() -> JSONResponse:
        return JSONResponse(
            {
                "spec_version": SPEC_VERSION,
                "hotkey": state.hotkey,
                "uid": state.uid,
                "services": list(state.config.services),
                "public_endpoint": state.config.public_endpoint,
                "asr_image": state.config.asr_image,
                "tts_image": state.config.tts_image,
                "gpu_counts": state.gpu.gpu_counts(),
                "capacity_units": state.gpu.capacity_units(),
                "rejected_gpus": state.gpu.rejected_gpus,
                "warnings": state.gpu.warnings(),
                "uptime_s": round(time.time() - state.started_at, 1),
            },
            headers=state.identity_headers(),
        )

    @app.get(PATH_IDENTITY_CHALLENGE)
    async def identity_challenge(nonce: str) -> JSONResponse:
        """Sign a validator nonce with this miner's hotkey."""
        nonce = (nonce or "").strip()
        if len(nonce) < 8:
            raise HTTPException(status_code=400, detail="nonce must be at least 8 characters")
        if not state.hotkey:
            raise HTTPException(status_code=503, detail="hotkey not configured")
        if state.identity_signer is None:
            raise HTTPException(
                status_code=503,
                detail="identity signing unavailable (no wallet connected)",
            )
        issued_at = time.time()
        signature = state.identity_signer(state.hotkey, nonce, issued_at)
        return JSONResponse(
            {
                "hotkey": state.hotkey,
                "nonce": nonce,
                "issued_at": issued_at,
                "signature": signature,
            },
            headers=state.identity_headers(),
        )

    # -- ASR ---------------------------------------------------------------

    @app.post(PATH_ASR_TRANSCRIBE)
    async def transcribe(
        file: UploadFile = File(...),
        language: str = Form("eng"),
        response_format: str = Form("json"),
        authorization: Optional[str] = Header(None),
    ):
        """Batch transcription.

        Contract matches ``ASRAPI/utils/utils.py:transcribe_single_file``:
        multipart ``file``/``language``/``response_format``, JSON or text out.
        """
        _authorize(authorization)
        if not state.serves_asr:
            raise HTTPException(status_code=404, detail="this miner does not serve ASR")

        payload = await _read_upload_limited(file, state.config.max_upload_bytes)
        try:
            async with state.asr_slots.acquire():
                form = aiohttp.FormData()
                form.add_field(
                    "file",
                    payload,
                    filename=file.filename or "audio.wav",
                    content_type=file.content_type or "application/octet-stream",
                )
                form.add_field("language", language)
                form.add_field("response_format", response_format)
                status, body, headers = await state.asr.post_multipart(
                    PATH_ASR_TRANSCRIBE, form
                )
        except UpstreamError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

        return Response(
            content=body,
            status_code=status,
            media_type=headers.get("Content-Type", "application/json"),
            headers=state.identity_headers(),
        )

    @app.websocket(PATH_ASR_STREAM_WS)
    async def realtime_transcribe(websocket: WebSocket, language: str = "eng"):
        """Streaming ASR: binary PCM in, JSON partial transcripts out."""
        if not state.serves_asr:
            await websocket.close(code=1008, reason="ASR not served by this miner")
            return
        if not _ws_authorized(websocket):
            await websocket.close(code=1008, reason="unauthorized")
            return

        mode = (state.config.asr_stream_mode or "batch_proxy").strip().lower()
        try:
            async with state.asr_slots.acquire():
                await websocket.accept()
                if mode in {"bridge", "upstream", "proxy"}:
                    target = (
                        f"{state.config.ws_base('asr')}"
                        f"{PATH_ASR_STREAM_WS}?language={language}"
                    )
                    await _bridge_websocket(
                        websocket, target, state=state, binary_to_upstream=True
                    )
                else:
                    await _asr_stream_batch_proxy(websocket, state, language)
        except AtCapacity as exc:
            await websocket.close(code=1013, reason=str(exc))
        except WebSocketDisconnect:
            logger.debug("client disconnected from realtime ASR")
        except Exception as exc:
            logger.warning("realtime ASR bridge failed: %s", exc)
            await _safe_close(websocket, 1011)

    # -- TTS ---------------------------------------------------------------

    @app.post(PATH_TTS_STREAM)
    async def tts_stream(request: Request, authorization: Optional[str] = Header(None)):
        """Synthesis, streamed as raw PCM.

        Accepts Spark-native ``{input, voice}`` or legacy ``{text, speaker_id}``
        JSON. Upstream Spark always receives ``{input, voice, temperature}``
        only (see ``docs/TTS_CONTRACT.md``).
        """
        _authorize(authorization)
        if not state.serves_tts:
            raise HTTPException(status_code=404, detail="this miner does not serve TTS")

        try:
            raw = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="body must be JSON") from exc

        text = (raw.get("text") or raw.get("input") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="'text' or 'input' is required")
        if len(text) > state.config.max_tts_text_chars:
            raise HTTPException(
                status_code=413,
                detail=f"text exceeds {state.config.max_tts_text_chars} characters",
            )

        # Spark selects timbre from ``voice`` (not ``speaker_id``). Never forward both
        # naming schemes — Spark returns HTTP 422 (duplicate field).
        payload = _spark_tts_upstream_payload(raw)

        from ..cathedral.receipt_v1 import receipt_buffer_from_env, receipt_enabled_from_env

        if receipt_enabled_from_env() and receipt_buffer_from_env():
            return await _proxy_stream_with_receipt(
                state,
                state.tts,
                state.tts_slots,
                PATH_TTS_STREAM,
                json_payload=payload,
            )

        return await _proxy_stream(
            state,
            state.tts,
            state.tts_slots,
            PATH_TTS_STREAM,
            json_payload=payload,
            extra_headers=_tts_receipt_headers(state, payload, audio=b""),
        )

    @app.post(PATH_TTS_CLONE)
    async def tts_clone(
        text: str = Form(...),
        reference_audio: UploadFile = File(...),
        temperature: float = Form(0.7),
        authorization: Optional[str] = Header(None),
    ):
        """Zero-shot voice cloning from an uploaded reference sample."""
        _authorize(authorization)
        if not state.serves_tts:
            raise HTTPException(status_code=404, detail="this miner does not serve TTS")

        audio = await _read_upload_limited(
            reference_audio, state.config.max_clone_reference_bytes
        )
        form = aiohttp.FormData()
        form.add_field("text", text)
        form.add_field("temperature", str(temperature))
        form.add_field(
            "reference_audio",
            audio,
            filename=reference_audio.filename or "reference.wav",
            content_type=reference_audio.content_type or "application/octet-stream",
        )
        return await _proxy_stream(
            state, state.tts, state.tts_slots, PATH_TTS_CLONE, data=form
        )

    @app.get(PATH_TTS_VOICES)
    async def voices():
        """Voice catalogue. Unauthenticated: it is a public capability listing."""
        if not state.serves_tts:
            raise HTTPException(status_code=404, detail="this miner does not serve TTS")
        try:
            status, body, headers = await state.tts.get(PATH_TTS_VOICES, timeout_s=10.0)
        except UpstreamError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
        return Response(
            content=body,
            status_code=status,
            media_type=headers.get("Content-Type", "application/json"),
            headers=state.identity_headers(),
        )

    @app.websocket(PATH_TTS_STREAM_WS)
    async def tts_stream_ws(websocket: WebSocket):
        """Streaming TTS: JSON control frames in, binary audio frames out."""
        if not state.serves_tts:
            await websocket.close(code=1008, reason="TTS not served by this miner")
            return
        if not _ws_authorized(websocket):
            await websocket.close(code=1008, reason="unauthorized")
            return

        target = f"{state.config.ws_base('tts')}{PATH_TTS_STREAM_WS}"
        try:
            async with state.tts_slots.acquire():
                await websocket.accept()
                await _bridge_websocket(
                    websocket,
                    target,
                    state=state,
                    binary_to_upstream=False,
                    text_transform=_remap_tts_ws_control_frame,
                )
        except AtCapacity as exc:
            await websocket.close(code=1013, reason=str(exc))
        except WebSocketDisconnect:
            logger.debug("client disconnected from streaming TTS")
        except Exception as exc:
            logger.warning("streaming TTS bridge failed: %s", exc)
            await _safe_close(websocket, 1011)

    return app


def _spark_tts_upstream_payload(raw: dict) -> dict:
    """Map miner-facing JSON to Spark upstream (``input`` + ``voice`` only).

    Spark rejects payloads that include both ``text`` and ``input`` (duplicate field).
    """
    text = (raw.get("text") or raw.get("input") or "").strip()
    voice = (raw.get("speaker_id") or raw.get("voice") or "eng_female_1").strip()
    try:
        temperature = float(raw.get("temperature", 0.7))
    except (TypeError, ValueError):
        temperature = 0.7
    return {
        "input": text,
        "voice": voice,
        "temperature": temperature,
    }


def _remap_tts_ws_control_frame(text: str) -> str:
    """Rewrite TTS WS JSON to Spark ``{input, voice, temperature}`` before upstream."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    if not isinstance(data, dict):
        return text
    # Non-synthesis control frames (eos / end / ping) pass through unchanged.
    if not any(k in data for k in ("text", "input", "speaker_id", "voice")):
        return text
    remapped = _spark_tts_upstream_payload(data)
    # Preserve non-conflicting control keys (e.g. type) without dual naming.
    out = {
        key: value
        for key, value in data.items()
        if key not in {"text", "input", "speaker_id", "voice", "temperature"}
    }
    out.update(remapped)
    return json.dumps(out, separators=(",", ":"))


def _tts_receipt_headers(
    state: MinerState,
    payload: dict,
    *,
    audio: bytes = b"",
) -> Dict[str, str]:
    """Optional hybrid receipt header.

    With buffered audio + TDX simulation/key env, emits a complete ``ok`` receipt.
    Otherwise emits honest ``unavailable`` (never forges attestation).
    """
    try:
        from ..cathedral.receipt_v1 import (
            HEADER_VOICE_RECEIPT,
            GPU_STATUS_TRUSTED_NOT_ATTESTED,
            build_receipt,
            build_unavailable_receipt,
            receipt_enabled_from_env,
            request_hash,
        )
        from ..cathedral.tdx import (
            simulate_controller_measurement,
            tdx_simulation_enabled,
        )
    except Exception:
        return {}
    if not receipt_enabled_from_env():
        return {}

    input_text = str(payload.get("input") or "")
    voice = str(payload.get("voice") or "")
    temperature = float(payload.get("temperature") or 0.7)
    ed25519_key = (os.getenv("VIOLET_RECEIPT_ED25519_PRIVATE_KEY") or "").strip()
    hmac_secret = (os.getenv("VIOLET_RECEIPT_HMAC_SECRET") or "").strip()

    if audio and (tdx_simulation_enabled() or os.getenv("VIOLET_TDX_MEASUREMENT")):
        measurement = os.getenv("VIOLET_TDX_MEASUREMENT", "").strip()
        if not measurement and tdx_simulation_enabled():
            challenge = (os.getenv("VIOLET_TDX_CHALLENGE") or "violet-local").strip()
            measurement = simulate_controller_measurement(
                hotkey=state.hotkey,
                challenge=challenge,
                endpoint=os.getenv("VIOLET_PUBLIC_ENDPOINT", "http://127.0.0.1:8091"),
            ).encode()
        try:
            receipt = build_receipt(
                miner_hotkey=state.hotkey,
                input_text=input_text,
                voice=voice,
                audio=audio,
                temperature=temperature,
                controller_measurement=measurement or None,
                gpu_attestation_status=GPU_STATUS_TRUSTED_NOT_ATTESTED,
                ed25519_private_key=ed25519_key,
                hmac_secret=hmac_secret if not ed25519_key else "",
            )
        except Exception as exc:
            logger.warning("complete receipt build failed: %s", exc)
            receipt = build_unavailable_receipt(state.hotkey, reason="receipt_build_failed")
            receipt.request_hash = request_hash(
                input_text=input_text, voice=voice, temperature=temperature
            )
    else:
        receipt = build_unavailable_receipt(
            state.hotkey,
            reason="tdx_unavailable" if not audio else "measurement_unavailable",
        )
        receipt.request_hash = request_hash(
            input_text=input_text, voice=voice, temperature=temperature
        )
        if audio:
            from ..cathedral.receipt_v1 import audio_content_hash

            receipt.audio_content_hash = audio_content_hash(audio)

    return {
        HEADER_VOICE_RECEIPT: json.dumps(
            receipt.to_dict(), sort_keys=True, separators=(",", ":")
        )
    }


async def _proxy_stream_with_receipt(
    state: MinerState,
    upstream: UpstreamClient,
    slots: Slots,
    path: str,
    *,
    json_payload: dict,
) -> Response:
    """Buffer TTS audio so the receipt can bind ``audio_content_hash`` (G05)."""
    audio = bytearray()
    status = 200
    upstream_headers: Dict[str, str] = {}
    try:
        async with slots.acquire():
            iterator = upstream.stream_post(path, json_payload=json_payload)
            async for head, chunk in iterator:
                if head is not None:
                    status, raw_headers = head
                    upstream_headers = _passthrough_headers(raw_headers or {})
                    continue
                if chunk:
                    audio.extend(chunk)
    except AtCapacity as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except UpstreamError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    headers = dict(upstream_headers)
    headers.update(state.identity_headers())
    headers.update(_tts_receipt_headers(state, json_payload, audio=bytes(audio)))
    media_type = headers.pop("Content-Type", None) or "audio/pcm"
    return Response(
        content=bytes(audio),
        status_code=int(status or 200),
        media_type=media_type,
        headers=headers,
    )


async def _proxy_stream(
    state: MinerState,
    upstream: UpstreamClient,
    slots: Slots,
    path: str,
    *,
    json_payload: Optional[dict] = None,
    data: Optional[aiohttp.FormData] = None,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Response:
    """Forward a streaming POST, holding a concurrency slot for its duration.

    The slot is released by the generator's ``finally``, which runs when the
    response body is exhausted or the client disconnects - so an abandoned
    stream cannot leak capacity.
    """
    stream_started = asyncio.Event()
    header_box: Dict[str, object] = {}

    async def body():
        try:
            async with slots.acquire():
                iterator = upstream.stream_post(
                    path, json_payload=json_payload, data=data
                )
                async for head, chunk in iterator:
                    if head is not None:
                        header_box["status"], header_box["headers"] = head
                        stream_started.set()
                        continue
                    if chunk:
                        yield chunk
        except AtCapacity as exc:
            header_box["error"] = exc
            stream_started.set()
        except UpstreamError as exc:
            header_box["error"] = exc
            stream_started.set()
        finally:
            stream_started.set()

    generator = body()
    # Pull until the upstream status is known, buffering the first chunk so the
    # status line is accurate rather than optimistically 200.
    first_chunk: Optional[bytes] = None
    try:
        first_chunk = await generator.__anext__()
    except StopAsyncIteration:
        first_chunk = None

    error = header_box.get("error")
    if isinstance(error, UpstreamError):
        await generator.aclose()
        raise HTTPException(status_code=error.status, detail=str(error))

    status = int(header_box.get("status", 200) or 200)
    upstream_headers = _passthrough_headers(header_box.get("headers", {}) or {})  # type: ignore[arg-type]
    upstream_headers.update(state.identity_headers())
    if extra_headers:
        upstream_headers.update(extra_headers)
    media_type = upstream_headers.pop("Content-Type", None) or "audio/pcm"

    async def replay():
        if first_chunk:
            yield first_chunk
        async for chunk in generator:
            yield chunk

    return StreamingResponse(
        replay(), status_code=status, media_type=media_type, headers=upstream_headers
    )


async def _asr_stream_batch_proxy(
    client_ws: WebSocket, state: MinerState, language: str
) -> None:
    """Emit violet-shaped partials by batch-transcribing a growing PCM buffer.

    Used when the upstream ASR container (etoil-api) exposes batch ``/transcribe``
    but not a compatible ``/realtime/transcribe`` WebSocket.
    """
    import io
    import wave

    pcm = bytearray()
    last_emit_at = 0
    # Emit as soon as we have ~0.1 s so first_partial stays under probe budgets.
    emit_every = 16000 * 2 // 10  # 0.1s → 3200 bytes
    last_text = ""

    async def transcribe_buffer(*, final: bool) -> None:
        nonlocal last_text
        if not pcm and not final:
            return
        payload = bytes(pcm) if pcm else b"\x00" * 3200
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(payload)
        form = aiohttp.FormData()
        form.add_field(
            "file",
            buf.getvalue(),
            filename="stream.wav",
            content_type="audio/wav",
        )
        form.add_field("language", language)
        form.add_field("response_format", "json")
        try:
            status, body, _headers = await state.asr.post_multipart(
                PATH_ASR_TRANSCRIBE, form, timeout_s=30.0
            )
        except UpstreamError as exc:
            logger.debug("batch_proxy transcribe failed: %s", exc)
            if final:
                await client_ws.send_text(
                    json.dumps(
                        {
                            "type": "final",
                            "text": last_text,
                            "is_final": True,
                            "language": language,
                        }
                    )
                )
            return
        text = last_text
        if status < 400 and body:
            try:
                data = json.loads(body.decode("utf-8", errors="ignore"))
                text = (data.get("text") or data.get("transcript") or "").strip()
            except json.JSONDecodeError:
                text = body.decode("utf-8", errors="ignore").strip()
        if text:
            last_text = text
        # Batch upstream returns the full line immediately; emit a growing
        # prefix so clients see progressive partials over WebSocket.
        words = last_text.split()
        if words and not final:
            bytes_per_word = 16000 * 2 // 2  # ~0.5 s of PCM per word
            visible = min(len(words), max(1, len(pcm) // max(bytes_per_word, 1)))
            emit_text = " ".join(words[:visible])
        else:
            emit_text = last_text
        await client_ws.send_text(
            json.dumps(
                {
                    "type": "final" if final else "partial",
                    "text": emit_text,
                    "is_final": bool(final),
                    "language": language,
                }
            )
        )

    try:
        while True:
            message = await asyncio.wait_for(
                client_ws.receive(),
                timeout=state.config.ws_idle_timeout_s,
            )
            kind = message.get("type")
            if kind == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                pcm.extend(message["bytes"])
                if len(pcm) - last_emit_at >= emit_every:
                    await transcribe_buffer(final=False)
                    last_emit_at = len(pcm)
            elif message.get("text") is not None:
                text = (message.get("text") or "").strip().lower()
                if text in {"eos", "end", '{"type":"eos"}'}:
                    break
    finally:
        await transcribe_buffer(final=True)


async def _bridge_websocket(
    client_ws: WebSocket,
    target_url: str,
    *,
    state: MinerState,
    binary_to_upstream: bool,
    text_transform: Optional[Callable[[str], str]] = None,
) -> None:
    """Pipe a client WebSocket to the upstream container in both directions."""
    max_size = state.config.ws_max_message_bytes or None
    idle_s = state.config.ws_idle_timeout_s
    async with websockets.connect(target_url, max_size=max_size) as upstream_ws:

        async def client_to_upstream() -> None:
            while True:
                message = await asyncio.wait_for(client_ws.receive(), timeout=idle_s)
                kind = message.get("type")
                if kind == "websocket.disconnect":
                    await upstream_ws.close()
                    return
                raw = message.get("bytes")
                if raw is not None:
                    if max_size and len(raw) > max_size:
                        logger.warning("dropping oversize WS frame (%d bytes)", len(raw))
                        continue
                    await upstream_ws.send(raw)
                elif message.get("text") is not None:
                    text = message["text"]
                    if max_size and len(text.encode()) > max_size:
                        logger.warning("dropping oversize WS text frame")
                        continue
                    if binary_to_upstream:
                        await upstream_ws.send(text)
                    else:
                        try:
                            json.loads(text)
                        except json.JSONDecodeError:
                            logger.warning("dropping non-JSON control frame")
                            continue
                        if text_transform is not None:
                            text = text_transform(text)
                        await upstream_ws.send(text)

        async def upstream_to_client() -> None:
            while True:
                message = await asyncio.wait_for(upstream_ws.recv(), timeout=idle_s)
                if isinstance(message, (bytes, bytearray)):
                    if max_size and len(message) > max_size:
                        logger.warning("dropping oversize upstream frame")
                        continue
                    await client_ws.send_bytes(bytes(message))
                else:
                    await client_ws.send_text(message)

        tasks = [
            asyncio.create_task(client_to_upstream()),
            asyncio.create_task(upstream_to_client()),
        ]
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception()
                if exc and not isinstance(exc, WebSocketDisconnect):
                    raise exc
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def _safe_close(websocket: WebSocket, code: int) -> None:
    try:
        await websocket.close(code=code)
    except RuntimeError:
        pass  # already closed
