"""cathedral_voice_receipt_v1 schema + fail-closed verify."""

from __future__ import annotations

from violet.cathedral.receipt_v1 import (
    RECEIPT_VERSION,
    STATUS_UNAVAILABLE,
    build_receipt,
    build_unavailable_receipt,
    request_hash,
    sign_receipt_hmac,
    verify_receipt,
)


def test_request_hash_stable():
    a = request_hash(input_text="hello", voice="eng_female_1", temperature=0.7)
    b = request_hash(input_text="hello", voice="eng_female_1", temperature=0.7)
    c = request_hash(input_text="hello!", voice="eng_female_1", temperature=0.7)
    assert a == b
    assert a != c


def test_round_trip_signed_receipt():
    secret = "test-secret"
    receipt = build_receipt(
        miner_hotkey="5FhotkeyExample",
        input_text="say hello",
        voice="eng_female_1",
        audio=b"\x00\x01pcm",
        controller_measurement="fake-tdx-quote",
        hmac_secret=secret,
    )
    assert receipt.version == RECEIPT_VERSION
    assert receipt.signature and receipt.signature.startswith("hmac-sha256=")
    raw = receipt.to_dict()
    ok = verify_receipt(raw, require=True, hmac_secret=secret)
    assert ok.ok
    assert ok.receipt is not None
    assert ok.receipt.miner_hotkey == "5FhotkeyExample"


def test_verify_fail_closed_missing():
    result = verify_receipt(None, require=True)
    assert not result.ok
    assert "missing" in result.detail


def test_verify_fail_closed_bad_signature():
    receipt = build_receipt(
        miner_hotkey="hk",
        input_text="x",
        voice="v",
        audio=b"abc",
        hmac_secret="correct",
    )
    raw = receipt.to_dict()
    result = verify_receipt(raw, require=True, hmac_secret="wrong")
    assert not result.ok
    assert "signature" in result.detail


def test_verify_fail_closed_unavailable_when_required():
    receipt = build_unavailable_receipt("hk")
    assert receipt.status == STATUS_UNAVAILABLE
    result = verify_receipt(receipt.to_dict(), require=True, hmac_secret="")
    # unavailable + require → reject; also no signature
    assert not result.ok


def test_verify_require_tdx():
    secret = "s"
    receipt = build_receipt(
        miner_hotkey="hk",
        input_text="hi",
        voice="v",
        audio=b"pcm",
        controller_measurement=None,
        hmac_secret=secret,
    )
    result = verify_receipt(
        receipt.to_dict(), require=True, hmac_secret=secret, require_tdx=True
    )
    assert not result.ok
    assert "TDX" in result.detail


def test_hash_mismatch():
    secret = "s"
    receipt = build_receipt(
        miner_hotkey="hk",
        input_text="hi",
        voice="v",
        audio=b"pcm",
        hmac_secret=secret,
    )
    result = verify_receipt(
        receipt.to_dict(),
        require=True,
        hmac_secret=secret,
        expected_audio_hash="deadbeef",
    )
    assert not result.ok
    assert "audio_content_hash" in result.detail


def test_verify_rejects_gpu_overclaim():
    secret = "s"
    receipt = build_receipt(
        miner_hotkey="hk",
        input_text="hi",
        voice="v",
        audio=b"pcm",
        controller_measurement="quote",
        hmac_secret=secret,
    )
    raw = receipt.to_dict()
    raw["gpu_attested"] = True
    result = verify_receipt(raw, require=True, hmac_secret=secret)
    assert not result.ok
    assert "over-claim" in result.detail


def test_sign_is_over_canonical_without_signature_field():
    secret = "s"
    receipt = build_receipt(
        miner_hotkey="hk",
        input_text="hi",
        voice="v",
        audio=b"x",
        hmac_secret=secret,
    )
    again = sign_receipt_hmac(receipt, secret)
    assert again == receipt.signature
