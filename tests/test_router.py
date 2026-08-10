"""Router: selection, failover, sticky sessions, receipts and work reports."""

from __future__ import annotations

import random
import time

import pytest

from violet.config import RouterConfig
from violet.router.receipts import Receipt
from violet.router.registry import MinerEndpoint
from violet.router.selector import StickySessions, latency_term, score_candidate, select


def endpoint(hotkey: str, **overrides) -> MinerEndpoint:
    defaults = dict(
        uid=0, endpoint=f"http://{hotkey}.example.com", services=["asr", "tts"],
        healthy=True, incentive=0.5, max_concurrent_asr=10, max_concurrent_tts=10,
    )
    defaults.update(overrides)
    return MinerEndpoint(hotkey=hotkey, **defaults)


@pytest.fixture
def config():
    cfg = RouterConfig()
    cfg.enabled = True
    return cfg


class TestLatencyTerm:
    def test_full_credit_at_target(self):
        assert latency_term(100.0) == 1.0

    def test_zero_when_hopeless(self):
        assert latency_term(99999.0) == 0.0

    def test_unmeasured_is_optimistic(self):
        # A newly discovered miner must get a first request or it can never be
        # measured at all.
        assert latency_term(None) > 0.5

    def test_monotonic(self):
        values = [latency_term(ms) for ms in (100, 300, 600, 1200)]
        assert values == sorted(values, reverse=True)


class TestSelection:
    def test_prefers_faster_miner(self, config):
        fast = endpoint("fast", latency_ms=80.0)
        slow = endpoint("slow", latency_ms=1400.0)
        assert score_candidate(fast, config) > score_candidate(slow, config)

    def test_prefers_less_loaded_miner(self, config):
        idle = endpoint("idle", load_factor=0.0)
        busy = endpoint("busy", load_factor=0.95)
        assert score_candidate(idle, config) > score_candidate(busy, config)

    def test_recent_failures_suppress_a_miner(self, config):
        healthy = endpoint("healthy")
        failing = endpoint("failing", success_ema=0.2)
        assert score_candidate(failing, config) < score_candidate(healthy, config)

    def test_excluded_miners_are_skipped(self, config):
        pool = [endpoint("a"), endpoint("b")]
        chosen = select(pool, config, exclude=["a"], rng=random.Random(0))
        assert chosen.hotkey == "b"

    def test_returns_none_when_pool_empty(self, config):
        assert select([], config) is None

    def test_all_excluded_returns_none(self, config):
        assert select([endpoint("a")], config, exclude=["a"]) is None

    def test_saturated_pool_still_returns_a_candidate(self, config):
        # Better to queue than to drop: the miner will 503 if it truly cannot.
        saturated = endpoint("s", load_factor=1.0, inflight=20)
        assert select([saturated], config) is not None

    def test_selection_spreads_load(self, config):
        """Deterministic best-pick would send every request to one miner."""
        pool = [endpoint(f"m{i}", latency_ms=100.0 + i) for i in range(3)]
        rng = random.Random(1234)
        chosen = {select(pool, config, rng=rng).hotkey for _ in range(60)}
        assert len(chosen) > 1


class TestStickySessions:
    def test_pin_and_retrieve(self):
        sessions = StickySessions()
        sessions.pin("s1", "hotkeyA")
        assert sessions.get("s1") == "hotkeyA"

    def test_unknown_session(self):
        assert StickySessions().get("nope") is None

    def test_release(self):
        sessions = StickySessions()
        sessions.pin("s1", "a")
        sessions.release("s1")
        assert sessions.get("s1") is None

    def test_expiry(self):
        sessions = StickySessions(ttl_s=0.01)
        sessions.pin("s1", "a")
        time.sleep(0.02)
        assert sessions.get("s1") is None

    def test_prune_removes_stale(self):
        sessions = StickySessions(ttl_s=0.01)
        sessions.pin("s1", "a")
        sessions.pin("s2", "b")
        time.sleep(0.02)
        assert sessions.prune() == 2
        assert len(sessions) == 0


class TestReceipts:
    def test_only_successful_work_is_counted(self, ledger):
        ledger.record(Receipt(hotkey="a", service="tts", ok=True, seconds=10.0))
        ledger.record(Receipt(hotkey="a", service="tts", ok=False, seconds=5.0))
        ledger.flush()

        rows = ledger.aggregate(time.time() - 3600)
        assert len(rows) == 1
        assert rows[0]["requests"] == 1
        assert rows[0]["seconds"] == pytest.approx(10.0)

    def test_grouped_by_hotkey_and_service(self, ledger):
        for hotkey in ("a", "b"):
            for service in ("asr", "tts"):
                ledger.record(Receipt(hotkey=hotkey, service=service, ok=True, seconds=1.0))
        ledger.flush()
        assert len(ledger.aggregate(time.time() - 3600)) == 4

    def test_window_boundary_is_respected(self, ledger):
        ledger.record(
            Receipt(hotkey="a", service="tts", ok=True, seconds=1.0, at=time.time() - 7200)
        )
        ledger.flush()
        assert ledger.aggregate(time.time() - 3600) == []

    def test_stats(self, ledger):
        ledger.record(Receipt(hotkey="a", service="tts", ok=True, seconds=3.0))
        ledger.record(Receipt(hotkey="a", service="tts", ok=False))
        stats = ledger.stats(time.time() - 3600)
        assert stats["requests"] == 2
        assert stats["succeeded"] == 1
        assert stats["failed"] == 1

    def test_prune(self, ledger):
        ledger.record(Receipt(hotkey="a", service="tts", ok=True, at=time.time() - 99999))
        ledger.flush()
        assert ledger.prune(time.time() - 3600) == 1


class TestWorkReports:
    def test_signature_round_trip(self, ledger):
        from violet.validator.work import parse_report, verify_report

        ledger.record(Receipt(hotkey="a", service="tts", ok=True, seconds=12.0))
        report = ledger.build_report(time.time() - 3600, secret="s3cret", signer="router")

        parsed = parse_report(report)
        # Assert the report is non-empty first: an empty report verifies
        # vacuously, which would make this test pass while proving nothing.
        assert parsed.entries
        assert parsed.total_requests == 1
        assert verify_report(parsed, "s3cret")
        assert not verify_report(parsed, "wrong")

    def test_tampering_invalidates_the_signature(self, ledger):
        from violet.validator.work import parse_report, verify_report

        ledger.record(Receipt(hotkey="a", service="tts", ok=True, seconds=12.0))
        report = ledger.build_report(time.time() - 3600, secret="s3cret")

        parsed = parse_report(report)
        parsed.entries[0].requests = 999_999
        assert not verify_report(parsed, "s3cret")

    def test_unsigned_report_is_never_accepted(self, ledger):
        from violet.validator.work import parse_report, verify_report

        report = ledger.build_report(time.time() - 3600)
        assert not verify_report(parse_report(report), "s3cret")

    def test_canonical_form_is_order_independent(self):
        from violet.validator.work import WorkEntry, WorkReport, canonical_payload

        a = WorkReport("id", 1.0, 0.0, [
            WorkEntry("h1", "asr", 1), WorkEntry("h2", "tts", 2),
        ])
        b = WorkReport("id", 1.0, 0.0, [
            WorkEntry("h2", "tts", 2), WorkEntry("h1", "asr", 1),
        ])
        # Any serialisation ambiguity would be a signature bypass.
        assert canonical_payload(a) == canonical_payload(b)


class TestWorkIngestionIdempotence:
    def test_same_report_is_not_double_counted(self, store):
        """A validator restart must not pay a miner twice for the same work."""
        for _ in range(3):
            store.record_work(
                "hk", "tts", requests=100, seconds=600.0, report_id="report-1"
            )
        stats = store.window_stats("hk", time.time() - 3600)
        assert stats.requests == 100

    def test_distinct_reports_accumulate(self, store):
        store.record_work("hk", "tts", requests=100, report_id="r1")
        store.record_work("hk", "tts", requests=50, report_id="r2")
        assert store.window_stats("hk", time.time() - 3600).requests == 150

    def test_reports_without_id_are_not_deduplicated(self, store):
        # Synthetic/local credits have no report id and are expected to repeat.
        store.record_work("hk", "tts", requests=10)
        store.record_work("hk", "tts", requests=10)
        assert store.window_stats("hk", time.time() - 3600).requests == 20
