"""G04/G05/G10 — TDX measurement, Ed25519 receipts, in-repo E2E proof."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from violet.cathedral.receipt_v1 import (
    build_receipt,
    generate_ed25519_keypair,
    verify_receipt,
)
from violet.cathedral.tdx import (
    TdxVerifyPolicy,
    simulate_controller_measurement,
    verify_controller_measurement,
)


@pytest.fixture(autouse=True)
def _sim_tdx(monkeypatch):
    monkeypatch.setenv("CATHEDRAL_TDX_SIMULATION", "1")


def test_tdx_simulation_verify_and_bindings():
    hotkey = "hkA"
    challenge = "chal-1"
    m = simulate_controller_measurement(
        hotkey=hotkey, challenge=challenge, endpoint="http://miner:8091"
    )
    assert m.simulated is True
    ok = verify_controller_measurement(
        m.encode(),
        TdxVerifyPolicy(
            allow_simulation=True,
            expected_challenge=challenge,
            expected_hotkey=hotkey,
            expected_endpoint="http://miner:8091",
        ),
    )
    assert ok.ok
    assert ok.simulated


def test_tdx_rejects_simulation_without_flag():
    m = simulate_controller_measurement(hotkey="hk", challenge="c")
    result = verify_controller_measurement(
        m.encode(),
        TdxVerifyPolicy(allow_simulation=False, require_measurement=True),
    )
    assert not result.ok
    assert "simulated" in result.detail


def test_tdx_rejects_debug():
    m = simulate_controller_measurement(hotkey="hk", challenge="c")
    m.debug = True
    result = verify_controller_measurement(
        m.encode(),
        TdxVerifyPolicy(allow_simulation=True, reject_debug=True),
    )
    assert not result.ok
    assert "debug" in result.detail


def test_ed25519_receipt_round_trip_with_tdx():
    priv, pub = generate_ed25519_keypair()
    hotkey = "hkEd"
    challenge = "c2"
    m = simulate_controller_measurement(hotkey=hotkey, challenge=challenge)
    policy = TdxVerifyPolicy(
        allow_simulation=True,
        expected_challenge=challenge,
        expected_hotkey=hotkey,
    )
    receipt = build_receipt(
        miner_hotkey=hotkey,
        input_text="hello attested world",
        voice="eng_female_1",
        audio=b"pcm-bytes-here",
        controller_measurement=m.encode(),
        ed25519_private_key=priv,
    )
    assert receipt.signature and receipt.signature.startswith("ed25519:")
    result = verify_receipt(
        receipt.to_dict(),
        require=True,
        ed25519_public_key_hex=pub,
        require_tdx=True,
        tdx_policy=policy,
    )
    assert result.ok
    assert result.tdx_simulated


def test_verify_fail_closed_without_tdx_when_required():
    priv, pub = generate_ed25519_keypair()
    receipt = build_receipt(
        miner_hotkey="hk",
        input_text="x",
        voice="v",
        audio=b"a",
        ed25519_private_key=priv,
    )
    result = verify_receipt(
        receipt.to_dict(),
        require=True,
        ed25519_public_key_hex=pub,
        require_tdx=True,
        tdx_policy=TdxVerifyPolicy(allow_simulation=True),
    )
    assert not result.ok


@pytest.mark.asyncio
async def test_e2e_proof_script(tmp_path: Path):
    import importlib.util

    path = Path(__file__).resolve().parents[1] / "scripts" / "cathedral_voice_e2e_proof.py"
    spec = importlib.util.spec_from_file_location("cathedral_voice_e2e_proof", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    out = tmp_path / "proof.json"
    proof = await mod.run_e2e_proof(out_path=out)
    assert proof["ok"] is True
    assert proof["source"] == "cathedral_voice_hybrid"
    assert proof["gpu_attested"] is False
    assert out.is_file()
    assert proof["digests"]["request_hash"]
    assert proof["digests"]["audio_content_hash"]
    assert proof["digests"]["mrtd"]
