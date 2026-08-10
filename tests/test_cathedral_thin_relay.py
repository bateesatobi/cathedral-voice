"""Tests for Cathedral thin relay helpers (no network)."""

import base64
import json
from pathlib import Path

import pytest

from violet.cathedral.thin_relay import (
    ThinRelayError,
    canonical_bytes,
    enforce_policy_fence,
    extract_hotkey_weights,
    map_hotkeys_to_uids,
    verify_signed_vector,
)


def test_extract_hotkey_weights():
    payload = {
        "weights": [
            {"miner_hotkey": "hkA", "weight": 0.7},
            {"miner_hotkey": "hkB", "weight": 0.3},
            {"miner_hotkey": "hkA", "weight": 0.1},  # dup ignored
            {"miner_hotkey": "hkC", "weight": 0.0},  # dropped
        ]
    }
    rows = extract_hotkey_weights(payload)
    assert rows == [("hkA", 0.7), ("hkB", 0.3)]


def test_map_hotkeys_to_uids():
    class N:
        def __init__(self, hotkey, uid):
            self.hotkey = hotkey
            self.uid = uid

    class G:
        neurons = [N("hkA", 1), N("hkB", 2)]

    uids, weights = map_hotkeys_to_uids(G(), [("hkA", 0.7), ("hkZ", 0.9), ("hkB", 0.3)])
    assert uids == [1, 2]
    assert weights == [0.7, 0.3]


def test_policy_fence_blocks_rollback(tmp_path: Path):
    state = tmp_path / "fence.json"
    enforce_policy_fence({"policy_version": 5}, state)
    enforce_policy_fence({"policy_version": 6}, state)
    with pytest.raises(ThinRelayError):
        enforce_policy_fence({"policy_version": 4}, state)


def test_verify_signature_roundtrip():
    cryptography = pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key().public_bytes_raw().hex()
    payload = {
        "key_id": "cathedral-weight-policy",
        "netuid": 39,
        "weights": [{"miner_hotkey": "hkA", "weight": 1.0}],
        "policy_version": 1,
    }
    sig = sk.sign(canonical_bytes(payload))
    payload["signature"] = base64.b64encode(sig).decode("ascii")
    verify_signed_vector(
        payload, public_key_hex=pk, expected_key_id="cathedral-weight-policy"
    )
    payload["weights"][0]["weight"] = 0.5
    with pytest.raises(ThinRelayError):
        verify_signed_vector(
            payload, public_key_hex=pk, expected_key_id="cathedral-weight-policy"
        )
