"""Production preflight + hybrid auth cutover tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from violet.cathedral.external_scores import (
    SOURCE_HYBRID,
    CathedralScoreClientConfig,
    config_from_env,
)


ROOT = Path(__file__).resolve().parents[1]


def _load_preflight():
    import sys

    path = ROOT / "scripts" / "cathedral_voice_production_preflight.py"
    spec = importlib.util.spec_from_file_location("cv_preflight", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cv_preflight"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_preflight_fails_on_simulation(monkeypatch):
    mod = _load_preflight()
    monkeypatch.setenv("CATHEDRAL_TDX_SIMULATION", "1")
    results = mod.check_common()
    assert any(not r.ok and r.code == "tdx_simulation_forbidden" for r in results)


def test_preflight_validator_requires_hybrid_creds(monkeypatch):
    mod = _load_preflight()
    for key in (
        "CATHEDRAL_TDX_SIMULATION",
        "CATHEDRAL_VOICE_HYBRID_ALLOW_SIMULATION",
        "VALIDATOR_TRUSTED_ASR_URL",
        "VALIDATOR_TTS_SEMANTIC_REQUIRED",
        "VALIDATOR_TTS_HOLDOUT_PATH",
        "CATHEDRAL_EXTERNAL_SCORES_ENABLED",
        "CATHEDRAL_EXTERNAL_SCORES_DRY_RUN",
        "CATHEDRAL_HYBRID_SCORES_ENABLED",
        "CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_VOICE_HYBRID",
        "CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET_CATHEDRAL_VOICE_HYBRID",
        "CATHEDRAL_RECEIPT_ED25519_PUBLIC_KEY",
        "CATHEDRAL_HYBRID_REQUIRE_TDX",
        "VIOLET_TDX_QUOTE_VERIFIER",
    ):
        monkeypatch.delenv(key, raising=False)

    results = mod.check_validator()
    codes = {r.code for r in results if not r.ok}
    assert "trusted_asr_missing" in codes
    assert "hybrid_token_missing" in codes
    assert "hybrid_hmac_missing" in codes


def test_preflight_validator_passes_when_configured(monkeypatch):
    mod = _load_preflight()
    monkeypatch.delenv("CATHEDRAL_TDX_SIMULATION", raising=False)
    monkeypatch.delenv("CATHEDRAL_VOICE_HYBRID_ALLOW_SIMULATION", raising=False)
    monkeypatch.setenv("VIOLET_REQUIRE_IMAGE_DIGESTS", "1")
    monkeypatch.setenv("VALIDATOR_TRUSTED_ASR_URL", "http://127.0.0.1:9000")
    monkeypatch.setenv("VALIDATOR_TTS_SEMANTIC_REQUIRED", "1")
    monkeypatch.setenv("VALIDATOR_TTS_HOLDOUT_PATH", "/tmp/holdout.jsonl")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_ENABLED", "1")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_DRY_RUN", "0")
    monkeypatch.setenv("CATHEDRAL_HYBRID_SCORES_ENABLED", "1")
    monkeypatch.setenv(
        "CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_VOICE_HYBRID", "tok"
    )
    monkeypatch.setenv(
        "CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET_CATHEDRAL_VOICE_HYBRID", "hmac"
    )
    monkeypatch.setenv("CATHEDRAL_RECEIPT_ED25519_PUBLIC_KEY", "ab" * 32)
    monkeypatch.setenv("CATHEDRAL_HYBRID_REQUIRE_TDX", "1")
    monkeypatch.setenv("VIOLET_TDX_QUOTE_VERIFIER", "/usr/bin/true")
    results = mod.check_common() + mod.check_validator()
    assert all(r.ok for r in results), [r for r in results if not r.ok]


def test_hybrid_auth_uses_dedicated_credentials(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN", "shared")
    monkeypatch.setenv("CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET", "shared-hmac")
    monkeypatch.setenv(
        "CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_VOICE_HYBRID", "hybrid-tok"
    )
    monkeypatch.setenv(
        "CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET_CATHEDRAL_VOICE_HYBRID",
        "hybrid-hmac",
    )
    cfg = config_from_env()
    token, secret = cfg.auth_for_source(SOURCE_HYBRID)
    assert token == "hybrid-tok"
    assert secret == "hybrid-hmac"
    shared_token, shared_secret = cfg.auth_for_source("violet_audio")
    assert shared_token == "shared"
    assert shared_secret == "shared-hmac"


def test_auth_for_source_fallback():
    cfg = CathedralScoreClientConfig(token="t", hmac_secret="h")
    token, secret = cfg.auth_for_source(SOURCE_HYBRID)
    assert token == "t"
    assert secret == "h"


@pytest.mark.asyncio
async def test_live_e2e_refuses_simulation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CATHEDRAL_TDX_SIMULATION", "1")
    path = ROOT / "scripts" / "cathedral_voice_e2e_proof.py"
    spec = importlib.util.spec_from_file_location("cathedral_voice_e2e_proof", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    meas = tmp_path / "m.json"
    meas.write_text('{"format":"cathedral_tdx_measurement_v1","mrtd":"aa"}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="refusing --live"):
        await mod.run_e2e_proof(
            out_path=tmp_path / "out.json",
            live=True,
            measurement_file=meas,
            ed25519_private_hex="00" * 32,
            ed25519_public_hex="11" * 32,
        )
