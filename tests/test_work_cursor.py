"""Work cursor persistence + overlap rejection (G08)."""

from __future__ import annotations

from violet.validator.store import ValidatorStore
from violet.validator.work import WorkEntry, WorkReport, reject_overlapping_report


def test_work_cursor_persists(tmp_path):
    path = tmp_path / "v.db"
    store = ValidatorStore(str(path))
    assert store.get_work_cursor() == (0.0, None)
    store.set_work_cursor(1_700_000_000.0, "abc123")
    store.close()

    store2 = ValidatorStore(str(path))
    end, rid = store2.get_work_cursor()
    assert end == 1_700_000_000.0
    assert rid == "abc123"
    store2.close()


def test_reject_overlapping_period():
    report = WorkReport(
        report_id="r2",
        generated_at=200.0,
        period_start=50.0,
        entries=[WorkEntry(hotkey="hk", service="tts", requests=1)],
    )
    reason = reject_overlapping_report(report, last_period_end=100.0)
    assert reason is not None
    assert "overlapping" in reason


def test_accept_non_overlapping_period():
    report = WorkReport(
        report_id="r3",
        generated_at=200.0,
        period_start=100.0,
        entries=[WorkEntry(hotkey="hk", service="tts", requests=1)],
    )
    assert reject_overlapping_report(report, last_period_end=100.0) is None


def test_reject_duplicate_report_id_at_cursor():
    report = WorkReport(
        report_id="same",
        generated_at=200.0,
        period_start=150.0,
        entries=[],
    )
    reason = reject_overlapping_report(
        report, last_period_end=100.0, last_report_id="same"
    )
    assert reason is not None
    assert "duplicate" in reason
