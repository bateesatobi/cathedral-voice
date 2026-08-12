"""
HTTP front door for the Violet smart router.

ASRAPI (and other Avoices backends) call this service instead of importing
``violet-subnet`` in-process.  The router owns chain discovery, miner health,
load balancing, failover, and work receipts; callers only need ``httpx``.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..chain import ChainClient
from ..config import ChainConfig, load_config
from ..constants import SERVICE_ASR, SERVICE_TTS
from ..identity import verify_hotkey_signature
from ..router.client import NoMinerAvailable, VioletRouter
from .registry import MinerEndpoint

logger = logging.getLogger("violet.router.server")

_router: Optional[VioletRouter] = None


def _api_key() -> str:
    return os.getenv("VIOLET_ROUTER_API_KEY", "").strip()


async def require_auth(request: Request) -> None:
    expected = _api_key()
    if not expected:
        return
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="invalid router API key")


def miner_to_dict(miner: MinerEndpoint) -> Dict[str, Any]:
    declared = int(miner.max_concurrent_asr or 0) + int(miner.max_concurrent_tts or 0)
    return {
        "hotkey": miner.hotkey,
        "uid": miner.uid,
        "coldkey": miner.coldkey,
        "endpoint": miner.endpoint,
        "services": list(miner.services or []),
        "healthy": miner.healthy,
        "qualified": True,
        "blacklisted": False,
        "incentive": miner.incentive,
        "stake": 0.0,
        "trust": 0.0,
        "emission": 0.0,
        "gpu_model": miner.gpu_model,
        "gpu_count": miner.gpu_count,
        "gpu_tier": miner.gpu_tier,
        "vram_gb": miner.vram_gb,
        "gpu_summary": miner.gpu_summary,
        "capacity_units": miner.capacity_units,
        "load_factor": miner.load_factor,
        "max_concurrent_asr": miner.max_concurrent_asr,
        "max_concurrent_tts": miner.max_concurrent_tts,
        "declared_capacity": declared or None,
        "version": None,
    }


class TokensPayload(BaseModel):
    tokens: Dict[str, str] = Field(default_factory=dict)


class StreamEndPayload(BaseModel):
    session_id: str
    hotkey: str = ""
    service: str
    seconds: float = 0.0
    ok: bool = True


class SynthesizePayload(BaseModel):
    text: str
    speaker_id: str
    temperature: float = 0.7


class VerifySignaturePayload(BaseModel):
    hotkey: str
    message_b64: str
    signature: str


class VerifyRegistrationPayload(BaseModel):
    hotkey: str
    coldkey: Optional[str] = None
    network: str = "finney"
    netuid: int = 0


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _router
    config = load_config()
    chain = None
    if config.chain.netuid:
        config.chain.signing_enabled = False
        chain = await ChainClient(config.chain).connect()
    _router = VioletRouter(config.router, chain=chain)
    await _router.start()
    logger.info("Violet router HTTP service started: %s", _router.status())
    yield
    if _router is not None:
        await _router.stop()
        _router = None


def create_app() -> FastAPI:
    app = FastAPI(
        title="Violet Router",
        description="Routes Avoices ASR/TTS traffic to the miner pool",
        version="1.0.0",
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/status", dependencies=[Depends(require_auth)])
    async def status() -> Dict[str, object]:
        if _router is None:
            raise HTTPException(status_code=503, detail="router not started")
        out = _router.status()
        out["available"] = True
        return out

    @app.get("/v1/registry", dependencies=[Depends(require_auth)])
    async def registry() -> Dict[str, Any]:
        if _router is None:
            raise HTTPException(status_code=503, detail="router not started")
        miners = [miner_to_dict(m) for m in _router.registry.snapshot()]
        return {"miners": miners, "count": len(miners)}

    @app.post("/v1/registry/tokens", dependencies=[Depends(require_auth)])
    async def apply_tokens(payload: TokensPayload) -> Dict[str, int]:
        if _router is None:
            raise HTTPException(status_code=503, detail="router not started")
        _router.registry.apply_access_tokens(payload.tokens)
        return {"applied": len(payload.tokens)}

    @app.get("/v1/work-report", dependencies=[Depends(require_auth)])
    async def work_report(since: float = Query(7 * 86400.0, ge=0.0)) -> Dict[str, object]:
        if _router is None:
            raise HTTPException(status_code=503, detail="router not started")
        return _router.work_report(time.time() - since)

    @app.post("/v1/transcribe", dependencies=[Depends(require_auth)])
    async def transcribe(
        file: UploadFile = File(...),
        language: str = Form("eng"),
        response_format: str = Form("json"),
        audio_seconds: float = Form(0.0),
    ) -> JSONResponse:
        if _router is None:
            raise HTTPException(status_code=503, detail="router not started")
        audio = await file.read()
        content_type = file.content_type or "audio/wav"
        try:
            response = await _router.transcribe(
                audio,
                filename=file.filename or "audio.wav",
                language=language,
                response_format=response_format,
                content_type=content_type,
                audio_seconds=audio_seconds,
            )
        except NoMinerAvailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        return JSONResponse(
            {
                "status": response.status,
                "body_b64": base64.b64encode(response.body or b"").decode("ascii"),
                "headers": response.headers,
                "hotkey": response.hotkey,
                "latency_ms": response.latency_ms,
                "fallback": response.fallback,
                "response_format": response_format,
            },
            status_code=200 if response.status < 500 else 502,
        )

    @app.post("/v1/synthesize", dependencies=[Depends(require_auth)])
    async def synthesize(payload: SynthesizePayload) -> JSONResponse:
        if _router is None:
            raise HTTPException(status_code=503, detail="router not started")
        try:
            response = await _router.synthesize(
                payload.text,
                speaker_id=payload.speaker_id,
                temperature=payload.temperature,
            )
        except NoMinerAvailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        return JSONResponse(
            {
                "status": response.status,
                "body_b64": base64.b64encode(response.body or b"").decode("ascii"),
                "headers": response.headers,
                "hotkey": response.hotkey,
                "latency_ms": response.latency_ms,
                "fallback": response.fallback,
            },
            status_code=200 if response.status < 500 else 502,
        )

    @app.post("/v1/synthesize/stream", dependencies=[Depends(require_auth)])
    async def synthesize_stream(payload: SynthesizePayload) -> StreamingResponse:
        if _router is None:
            raise HTTPException(status_code=503, detail="router not started")

        async def body() -> AsyncIterator[bytes]:
            async for _headers, chunk in _router.synthesize_stream(
                payload.text,
                speaker_id=payload.speaker_id,
                temperature=payload.temperature,
            ):
                if chunk:
                    yield chunk

        try:
            # Fail fast when no miner is reachable before streaming starts.
            probe = _router._pick(SERVICE_TTS)
            if probe is None and not _router.config.fallback_tts_url:
                raise NoMinerAvailable("no TTS miner available")
        except NoMinerAvailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        return StreamingResponse(body(), media_type="application/octet-stream")

    @app.get("/v1/stream/target", dependencies=[Depends(require_auth)])
    async def stream_target(
        service: str = Query(..., pattern="^(asr|tts)$"),
        session_id: str = Query(..., min_length=1),
        language: str = Query("eng"),
    ) -> Dict[str, Any]:
        if _router is None:
            raise HTTPException(status_code=503, detail="router not started")
        svc = SERVICE_ASR if service == "asr" else SERVICE_TTS
        url, miner, is_fallback = _router.stream_target(svc, session_id, language=language)
        if not url:
            raise HTTPException(status_code=503, detail="no miner available for stream")
        return {
            "url": url,
            "hotkey": getattr(miner, "hotkey", None) if miner else None,
            "uid": getattr(miner, "uid", None) if miner else None,
            "endpoint": getattr(miner, "endpoint", None) if miner else None,
            "fallback": is_fallback,
        }

    @app.post("/v1/stream/end", dependencies=[Depends(require_auth)])
    async def stream_end(payload: StreamEndPayload) -> Dict[str, str]:
        if _router is None:
            raise HTTPException(status_code=503, detail="router not started")
        miner = _router.registry.get(payload.hotkey) if payload.hotkey else None
        _router.end_stream(
            payload.session_id,
            miner,
            service=payload.service,
            seconds=payload.seconds,
            ok=payload.ok,
        )
        return {"status": "ok"}

    @app.post("/v1/verify/signature", dependencies=[Depends(require_auth)])
    async def verify_signature(payload: VerifySignaturePayload) -> Dict[str, bool]:
        try:
            message = base64.b64decode(payload.message_b64)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid message_b64") from exc
        ok = verify_hotkey_signature(payload.hotkey, message, payload.signature)
        return {"valid": ok}

    @app.post("/v1/chain/verify-registration", dependencies=[Depends(require_auth)])
    async def verify_registration(payload: VerifyRegistrationPayload) -> Dict[str, Any]:
        config = ChainConfig()
        config.network = payload.network
        config.netuid = payload.netuid
        config.signing_enabled = False

        chain = await ChainClient(config).connect()
        try:
            graph = await chain.metagraph(commitments=False)
            neuron = None
            if hasattr(graph, "by_hotkey"):
                neuron = graph.by_hotkey(payload.hotkey)
            if neuron is None:
                for candidate in graph.neurons:
                    if str(candidate.hotkey) == payload.hotkey:
                        neuron = candidate
                        break
            if neuron is None:
                raise HTTPException(
                    status_code=403,
                    detail=f"hotkey is not registered on netuid {payload.netuid} ({payload.network})",
                )
            chain_coldkey = str(getattr(neuron, "coldkey", "") or "")
            if payload.coldkey and chain_coldkey and payload.coldkey != chain_coldkey:
                raise HTTPException(status_code=403, detail="coldkey does not match metagraph")
            return {
                "uid": int(neuron.uid),
                "coldkey": chain_coldkey or payload.coldkey or "",
            }
        finally:
            await chain.close()

    return app


app = create_app()
