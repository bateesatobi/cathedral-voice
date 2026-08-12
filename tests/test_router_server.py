"""Tests for the Violet Router HTTP service."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from violet.router.client import RoutedResponse
from violet.router.registry import MinerEndpoint


@pytest.fixture
def mock_router():
    router = MagicMock()
    miner = MinerEndpoint(
        hotkey="5HotkeyExample",
        uid=47,
        endpoint="http://127.0.0.1:40201",
        services=["asr", "tts"],
        healthy=True,
    )
    router.registry.snapshot.return_value = [miner]
    router.registry.get.return_value = miner
    router.status.return_value = {"healthy": 1, "enabled": True}
    router.work_report.return_value = {"entries": []}
    router.transcribe = AsyncMock(
        return_value=RoutedResponse(
            status=200,
            body=b'{"text":"hello"}',
            headers={"content-type": "application/json"},
            hotkey=miner.hotkey,
            latency_ms=12.5,
        )
    )
    router.stream_target.return_value = (
        "ws://127.0.0.1:40201/realtime/transcribe?language=eng",
        miner,
        False,
    )
    router.start = AsyncMock()
    router.stop = AsyncMock()
    router.config.fallback_tts_url = ""
    router._pick = MagicMock(return_value=miner)
    return router


@pytest.fixture
def client(mock_router, monkeypatch):
    monkeypatch.setenv("VIOLET_ROUTER_API_KEY", "test-key")
    with patch("violet.router.server._router", mock_router):
        with patch("violet.router.server.VioletRouter", return_value=mock_router):
            with patch("violet.router.server.ChainClient") as chain_cls:
                chain_cls.return_value.connect = AsyncMock(return_value=chain_cls.return_value)
                from violet.router.server import create_app

                with TestClient(create_app()) as test_client:
                    yield test_client


def _auth_headers():
    return {"Authorization": "Bearer test-key"}


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_registry_requires_auth(client):
    resp = client.get("/v1/registry")
    assert resp.status_code == 401


def test_registry(client, mock_router):
    resp = client.get("/v1/registry", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert data["miners"][0]["hotkey"] == "5HotkeyExample"


def test_apply_tokens(client, mock_router):
    resp = client.post(
        "/v1/registry/tokens",
        headers=_auth_headers(),
        json={"tokens": {"5HotkeyExample": "vm_abc123"}},
    )
    assert resp.status_code == 200
    mock_router.registry.apply_access_tokens.assert_called_once()


def test_transcribe(client, mock_router):
    resp = client.post(
        "/v1/transcribe",
        headers=_auth_headers(),
        data={"language": "eng", "response_format": "json", "audio_seconds": "1.0"},
        files={"file": ("sample.wav", b"RIFF", "audio/wav")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == 200
    assert body["hotkey"] == "5HotkeyExample"
    mock_router.transcribe.assert_awaited_once()


def test_stream_target(client):
    resp = client.get(
        "/v1/stream/target",
        headers=_auth_headers(),
        params={"service": "asr", "session_id": "sess-1", "language": "eng"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["url"].startswith("ws://")
    assert data["hotkey"] == "5HotkeyExample"
