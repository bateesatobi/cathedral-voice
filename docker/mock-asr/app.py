"""
Sample ASR container.

Stands in for the official Violet ASR image. It implements the same wire
contract - ``POST /transcribe``, ``WS /realtime/transcribe``, ``GET /health`` -
so the miner sidecar, the validator's qualification suite and the smart router
can all be exercised end-to-end on a laptop with no GPU.

Swap ``MINER_ASR_UPSTREAM`` to the real image and nothing else changes.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import wave
from io import BytesIO
from typing import List

from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, PlainTextResponse

app = FastAPI(title="Violet Sample ASR", version="1.0")

#: Simulated per-second-of-audio processing cost, so latency-aware routing and
#: the Work score's latency multiplier have something realistic to measure.
REALTIME_FACTOR = float(os.getenv("MOCK_ASR_RTF", "0.05"))
STARTED_AT = time.time()

#: Returned verbatim for any audio. The validator's evaluation set pairs this
#: text with the sample it ships, so WER computes to zero on a healthy mock.
FIXTURE_TRANSCRIPT = os.getenv(
    "MOCK_ASR_TRANSCRIPT",
    "the quick brown fox jumps over the lazy dog",
)


def _audio_duration_seconds(payload: bytes) -> float:
    """Duration of a WAV payload; falls back to a PCM assumption."""
    try:
        with wave.open(BytesIO(payload), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate() or 16000
            return frames / float(rate)
    except (wave.Error, EOFError):
        # Treat as raw 16 kHz mono 16-bit PCM.
        return len(payload) / float(16000 * 2)


def _segments(text: str, duration: float) -> List[dict]:
    words = text.split()
    if not words:
        return []
    per_segment = max(1, len(words) // 3)
    chunks = [
        " ".join(words[i : i + per_segment]) for i in range(0, len(words), per_segment)
    ]
    span = duration / len(chunks) if chunks else duration
    return [
        {
            "text": chunk,
            "start": round(index * span, 3),
            "end": round((index + 1) * span, 3),
            "confidence": 0.95,
        }
        for index, chunk in enumerate(chunks)
    ]


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "service": "asr",
            "model": os.getenv("MOCK_ASR_MODEL", "violet-asr-sample"),
            "uptime_s": round(time.time() - STARTED_AT, 1),
        }
    )


@app.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    language: str = Form("eng"),
    response_format: str = Form("json"),
):
    payload = await file.read()
    duration = _audio_duration_seconds(payload)
    await asyncio.sleep(min(duration * REALTIME_FACTOR, 5.0))

    text = FIXTURE_TRANSCRIPT
    segments = _segments(text, duration)

    if response_format == "text":
        return PlainTextResponse(text)
    if response_format in {"srt", "vtt"}:
        return PlainTextResponse(_as_srt(segments))
    return JSONResponse(
        {"text": text, "language": language, "duration": round(duration, 3), "segments": segments}
    )


def _as_srt(segments: List[dict]) -> str:
    def stamp(seconds: float) -> str:
        millis = int(seconds * 1000)
        hours, millis = divmod(millis, 3_600_000)
        minutes, millis = divmod(millis, 60_000)
        secs, millis = divmod(millis, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

    lines = []
    for index, segment in enumerate(segments, start=1):
        lines.append(str(index))
        lines.append(f"{stamp(segment['start'])} --> {stamp(segment['end'])}")
        lines.append(segment["text"])
        lines.append("")
    return "\n".join(lines)


@app.websocket("/realtime/transcribe")
async def realtime_transcribe(websocket: WebSocket, language: str = "eng"):
    """Emits a partial transcript per burst of audio, then a final on close."""
    await websocket.accept()
    words = FIXTURE_TRANSCRIPT.split()
    spoken = 0
    received_bytes = 0

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break

            if message.get("bytes") is not None:
                received_bytes += len(message["bytes"])
                # One word per ~0.4 s of 16 kHz 16-bit mono audio.
                target = min(len(words), int(received_bytes / (16000 * 2 * 0.4)) + 1)
                if target > spoken:
                    spoken = target
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "partial",
                                "text": " ".join(words[:spoken]),
                                "language": language,
                                "is_final": False,
                            }
                        )
                    )
            elif message.get("text") is not None:
                if message["text"].strip().lower() in {"eos", '{"type":"eos"}', "end"}:
                    break
    except WebSocketDisconnect:
        return
    finally:
        try:
            await websocket.send_text(
                json.dumps(
                    {"type": "final", "text": FIXTURE_TRANSCRIPT, "is_final": True}
                )
            )
            await websocket.close()
        except Exception:
            pass
