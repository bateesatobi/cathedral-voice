"""Unit tests for Cathedral violet_audio report shaping."""

from violet.cathedral.external_scores import (
    SOURCE,
    build_violet_report,
    canonical_body,
    compute_hmac_header,
    scores_from_miner_scores,
)


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
        generated_at=__import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").timezone.utc),
    )
    body = canonical_body(report)
    a = compute_hmac_header(body, "secret")
    b = compute_hmac_header(body, "secret")
    assert a == b
    assert a.startswith("sha256=")
