"""
Shared fixtures.

The integration fixtures boot the sample ASR/TTS containers as in-process ASGI
apps and put a real miner sidecar in front of them, so the tests exercise the
actual HTTP and WebSocket paths rather than mocks. That is the point: the
contract between the sidecar and the inference images is the thing most likely
to break, and it cannot be tested by patching it out.
"""

from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_sample_app(service: str):
    """Import a sample container's ``app.py`` by path.

    Both sample services are ``app.py`` in sibling directories, mirroring how
    they are copied into their image. That makes them un-importable by module
    name, so they are loaded from their file path under distinct names.
    """
    import importlib.util

    path = ROOT / "docker" / f"mock-{service}" / "app.py"
    spec = importlib.util.spec_from_file_location(f"violet_sample_{service}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load sample {service} app from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class BackgroundServer:
    """Runs a uvicorn app on its own thread and event loop."""

    def __init__(self, app, port: int):
        import uvicorn

        self.port = port
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self, timeout: float = 20.0) -> None:
        self._thread.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._server.started:
                return
            time.sleep(0.05)
        raise RuntimeError(f"server on port {self.port} did not start")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@pytest.fixture(scope="session")
def sample_asr():
    server = BackgroundServer(load_sample_app("asr").app, free_port())
    server.start()
    yield server
    server.stop()


@pytest.fixture(scope="session")
def sample_tts():
    server = BackgroundServer(load_sample_app("tts").app, free_port())
    server.start()
    yield server
    server.stop()


@pytest.fixture(scope="session")
def miner(sample_asr, sample_tts):
    """A real miner sidecar fronting the sample inference containers."""
    from violet.config import MinerConfig
    from violet.miner.server import MinerState, create_app

    port = free_port()
    config = MinerConfig()
    config.host = "127.0.0.1"
    config.port = port
    config.public_endpoint = f"http://127.0.0.1:{port}"
    config.services = ["asr", "tts"]
    config.asr_upstream = sample_asr.url
    config.tts_upstream = sample_tts.url
    config.max_concurrent_asr = 4
    config.max_concurrent_tts = 4

    state = MinerState(config, hotkey="5TestHotkeyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    server = BackgroundServer(create_app(state), port)
    server.start()
    yield server
    server.stop()


@pytest.fixture
def sample_wav():
    """A short mono 16 kHz WAV, as an ASR client would upload."""
    import io
    import math
    import struct
    import wave

    rate, seconds = 16000, 2.0
    samples = [
        int(math.sin(2 * math.pi * 220 * i / rate) * 12000)
        for i in range(int(rate * seconds))
    ]
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return buffer.getvalue()


@pytest.fixture
def store(tmp_path):
    from violet.validator.store import ValidatorStore

    instance = ValidatorStore(str(tmp_path / "validator.sqlite3"))
    yield instance
    instance.close()


@pytest.fixture
def ledger(tmp_path):
    from violet.router.receipts import ReceiptLedger

    instance = ReceiptLedger(str(tmp_path / "receipts.sqlite3"))
    yield instance
    instance.close()
