"""Unit tests for Cathedral violet_audio + hybrid report shaping."""

import pytest

from violet.cathedral.external_scores import (
    SOURCE,
    SOURCE_HYBRID,
    build_hybrid_report,
    build_violet_report,
    canonical_body,
    compute_hmac_header,
    scores_from_miner_scores,
)
from violet.cathedral.receipt_v1 import build_receipt


class _Score:
    def __init__(self, hotkey, uid, final, quality=0.5):
        self.hotkey = hotkey
        self.uid = uid
        self.final = final
        self.quality = quality


def test_scores_max_normalized():
    rows = scores_from_miner_scores(
        [_Score("hkA", 1, 2.0, 0.9), _Score("hkB", 2, 1.0, 0.4)]
    )
    by_hk = {r["miner_hotkey"]: r for r in rows}
    assert by_hk["hkA"]["score"] == 1.0
    assert by_hk["hkB"]["score"] == 0.5
    assert by_hk["hkA"]["uid"] == 1
    assert by_hk["hkA"]["quality"] == 0.9


def test_build_report_requires_complete_for_blend_contract():
    report = build_violet_report(
        [{"miner_hotkey": "hkA", "score": 0.8}],
        epoch=100,
        netuid=39,
        complete=True,
    )
    assert report["source"] == SOURCE
    assert report["mechanism"] == SOURCE
    assert report["complete"] is True
    assert report["epoch"] == 100
    assert report["netuid"] == 39
    assert report["scores"][0]["miner_hotkey"] == "hkA"
    assert "generated_at" in report
    assert report["generated_at"].endswith("Z")


def test_hmac_stable():
    report = build_violet_report(
        [{"miner_hotkey": "hkA", "score": 1.0}],
        epoch=1,
        complete=True,
        generated_at=__import__("datetime").datetime(
            2026, 1, 1, tzinfo=__import__("datetime").timezone.utc
        ),
    )
    body = canonical_body(report)
    a = compute_hmac_header(body, "secret")
    b = compute_hmac_header(body, "secret")
    assert a == b
    assert a.startswith("sha256=")


def test_hybrid_report_receipt_gated():
    secret = "hybrid-secret"
    receipt = build_receipt(
        miner_hotkey="hkA",
        input_text="hello",
        voice="eng_female_1",
        audio=b"pcm-bytes",
        controller_measurement="quote",
        hmac_secret=secret,
    )
    report = build_hybrid_report(
        [{"miner_hotkey": "hkA", "score": 0.91, "receipt": receipt.to_dict()}],
        epoch=42,
        hmac_secret=secret,
    )
    assert report["source"] == SOURCE_HYBRID
    assert report["mechanism"] == SOURCE_HYBRID
    assert report["metadata"]["receipt_verified"] is True
    assert report["metadata"]["gpu_attested"] is False
    assert report["metadata"]["gpu_memory_confidential"] is False
    assert report["scores"][0]["receipt"]["version"] == "cathedral_voice_receipt_v1"
    assert report["scores"][0]["receipt"]["gpu_attested"] is False


def test_hybrid_report_fail_closed_without_receipt():
    with pytest.raises(ValueError, match="receipt missing"):
        build_hybrid_report(
            [{"miner_hotkey": "hkA", "score": 0.5}],
            epoch=1,
            hmac_secret="s",
        )


def test_hybrid_report_fail_closed_bad_sig():
    receipt = build_receipt(
        miner_hotkey="hkA",
        input_text="hello",
        voice="eng_female_1",
        audio=b"pcm",
        hmac_secret="correct",
    )
    with pytest.raises(ValueError, match="signature"):
        build_hybrid_report(
            [{"miner_hotkey": "hkA", "score": 0.5, "receipt": receipt.to_dict()}],
            epoch=1,
            hmac_secret="wrong",
        )


def test_violet_audio_path_unchanged_without_receipt():
    report = build_violet_report(
        [{"miner_hotkey": "hkA", "score": 0.3}],
        epoch=7,
        complete=True,
    )
    assert report["source"] == SOURCE
    assert "receipt" not in report["scores"][0]
