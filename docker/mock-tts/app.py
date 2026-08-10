"""
Sample TTS container.

Stands in for the official Violet TTS image, implementing the same contract the
Avoices backend already calls: ``POST /v1/audio/speech/stream``,
``GET /v1/voices``, ``POST /v1/audio/speech/clone/upload`` and
``WS /v1/audio/speech/stream/ws``.

Audio is a synthesised tone sequence, not speech. That is enough to exercise
framing, streaming, first-byte latency and the audio-length checks in the
validator; the quality metrics that need real speech degrade gracefully and are
documented as such in ``violet.validator.metrics``.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import struct
import time
from typing import AsyncIterator, List

from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="Violet Sample TTS", version="1.0")

SAMPLE_RATE = int(os.getenv("MOCK_TTS_SAMPLE_RATE", "24000"))
CHANNELS = 1
SAMPLE_WIDTH = 2
#: Seconds of audio generated per character of input.
SECONDS_PER_CHAR = float(os.getenv("MOCK_TTS_SECONDS_PER_CHAR", "0.06"))
#: Simulated model warm-up before the first frame, seconds.
FIRST_FRAME_DELAY_S = float(os.getenv("MOCK_TTS_FIRST_FRAME_DELAY_S", "0.05"))
CHUNK_MS = 40

STARTED_AT = time.time()

VOICES: List[dict] = [
    {"id": "eng_female_1", "language": "en", "gender": "female", "name": "Sarah"},
    {"id": "eng_male_1", "language": "en", "gender": "male", "name": "James"},
    {"id": "lug_female_1", "language": "lug", "gender": "female", "name": "Namubiru"},
    {"id": "swa_male_3", "language": "swa", "gender": "male", "name": "Bakari"},
    {"id": "kin_female_1", "language": "kin", "gender": "female", "name": "Mutoni"},
    {"id": "yor_female_1", "language": "yor", "gender": "female", "name": "Bisi"},
]

AUDIO_HEADERS = {
    "x-audio-sample-rate": str(SAMPLE_RATE),
    "x-audio-channels": str(CHANNELS),
    "x-audio-sample-width": str(SAMPLE_WIDTH),
}


def _speaker_frequency(speaker_id: str) -> float:
    """Stable per-speaker pitch, so different voices produce different audio."""
    base = 110.0 + (hash(speaker_id) % 120)
    return base


def _pcm_chunk(frequency: float, phase: float, frames: int) -> tuple[bytes, float]:
    """A chunk of 16-bit mono PCM, continuing from ``phase``."""
    step = 2 * math.pi * frequency / SAMPLE_RATE
    samples = []
    for index in range(frames):
        value = math.sin(phase + step * index)
        # Gentle envelope so the tone does not clip and resembles speech energy.
        envelope = 0.35 + 0.25 * math.sin(step * index / 40.0)
        samples.append(int(max(-1.0, min(1.0, value * envelope)) * 32767))
    return struct.pack(f"<{len(samples)}h", *samples), phase + step * frames


async def _synthesize(text: str, speaker_id: str) -> AsyncIterator[bytes]:
    duration_s = max(0.25, len(text) * SECONDS_PER_CHAR)
    frequency = _speaker_frequency(speaker_id)
    frames_per_chunk = int(SAMPLE_RATE * CHUNK_MS / 1000)
    total_frames = int(SAMPLE_RATE * duration_s)

    await asyncio.sleep(FIRST_FRAME_DELAY_S)

    phase = 0.0
    emitted = 0
    while emitted < total_frames:
        frames = min(frames_per_chunk, total_frames - emitted)
        chunk, phase = _pcm_chunk(frequency, phase, frames)
        emitted += frames
        yield chunk
        # Emit faster than real time, as a GPU would.
        await asyncio.sleep(CHUNK_MS / 1000.0 * 0.1)


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "service": "tts",
            "model": os.getenv("MOCK_TTS_MODEL", "violet-tts-sample"),
            "voices": len(VOICES),
            "uptime_s": round(time.time() - STARTED_AT, 1),
        }
    )


@app.get("/v1/voices")
async def voices() -> JSONResponse:
    return JSONResponse({"voices": VOICES})


@app.post("/v1/audio/speech/stream")
async def speech_stream(payload: dict):
    text = (payload.get("text") or "").strip()
    speaker_id = payload.get("speaker_id") or "eng_female_1"
    if not text:
        return JSONResponse({"error": "'text' is required"}, status_code=400)

    return StreamingResponse(
        _synthesize(text, speaker_id), media_type="audio/pcm", headers=AUDIO_HEADERS
    )


@app.post("/v1/audio/speech/clone/upload")
async def clone(
    text: str = Form(...),
    reference_audio: UploadFile = File(...),
    temperature: float = Form(0.7),
):
    reference = await reference_audio.read()
    # Derive the pitch from the reference so cloning is observably different
    # from the catalogue voices.
    speaker_id = f"clone-{len(reference)}"
    return StreamingResponse(
        _synthesize(text, speaker_id), media_type="audio/pcm", headers=AUDIO_HEADERS
    )


@app.websocket("/v1/audio/speech/stream/ws")
async def speech_stream_ws(websocket: WebSocket):
    """JSON control frames in, binary PCM frames out."""
    await websocket.accept()
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                request = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(
                    json.dumps({"type": "error", "message": "invalid JSON"})
                )
                continue

            text = (request.get("text") or "").strip()
            if not text:
                await websocket.send_text(
                    json.dumps({"type": "error", "message": "'text' is required"})
                )
                continue

            speaker_id = request.get("speaker_id") or "eng_female_1"
            await websocket.send_text(
                json.dumps({"type": "start", **AUDIO_HEADERS})
            )
            async for chunk in _synthesize(text, speaker_id):
                await websocket.send_bytes(chunk)
            await websocket.send_text(json.dumps({"type": "end"}))
    except WebSocketDisconnect:
        return
    except Exception:
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
