"""The rolling scoring window and its persistence guarantees."""

from __future__ import annotations

import time

import pytest

from violet.evalset import load_evalset, synthetic_wav
from violet.validator.store import Observation


class TestWindowStats:
    def test_empty_miner(self, store):
        stats = store.window_stats("unknown", time.time() - 3600)
        assert stats.samples == 0
        assert stats.availability == 0.0
        assert stats.mean_quality is None

    def test_availability_from_health_observations(self, store):
        now = time.time()
        store.record_observations(
            [Observation("hk", now - i, "health", ok=(i % 4 != 0)) for i in range(20)]
        )
        stats = store.window_stats("hk", now - 3600)
        assert 0.7 < stats.availability < 0.8
        # Health observations must not inflate the probe sample count.
        assert stats.samples == 0

    def test_quality_and_latency_aggregates(self, store):
        now = time.time()
        store.record_observations(
            [
                Observation("hk", now - i, "tts", ok=True, quality=0.8 + i * 0.01,
                            first_byte_ms=100.0 + i * 10)
                for i in range(10)
            ]
        )
        stats = store.window_stats("hk", now - 3600)
        assert stats.samples == 10
        assert stats.mean_quality == pytest.approx(0.845, abs=0.01)
        assert stats.p95_first_byte_ms is not None
        assert stats.p95_first_byte_ms >= stats.mean_first_byte_ms

    def test_p95_is_robust_to_a_single_outlier(self, store):
        """One network hiccup must not define a miner's latency for a week."""
        now = time.time()
        observations = [
            Observation("hk", now - i, "tts", ok=True, quality=0.9, first_byte_ms=100.0)
            for i in range(50)
        ]
        observations.append(
            Observation("hk", now, "tts", ok=True, quality=0.9, first_byte_ms=60000.0)
        )
        store.record_observations(observations)

        stats = store.window_stats("hk", now - 3600)
        assert stats.p95_first_byte_ms < 1000

    def test_capacity_counted_only_while_healthy(self, store):
        """TDD 7 rewards hardware kept online, not hardware merely claimed."""
        for _ in range(10):
            store.record_capacity("online", 8.0, healthy=True)
        for _ in range(10):
            store.record_capacity("offline", 8.0, healthy=False)

        since = time.time() - 3600
        assert store.window_stats("online", since).mean_online_capacity == pytest.approx(8.0)
        assert store.window_stats("offline", since).mean_online_capacity == 0.0

    def test_intermittent_capacity_is_prorated(self, store):
        for index in range(10):
            store.record_capacity("flaky", 10.0, healthy=index < 5)
        stats = store.window_stats("flaky", time.time() - 3600)
        assert stats.mean_online_capacity == pytest.approx(5.0)

    def test_observations_outside_the_window_are_excluded(self, store):
        now = time.time()
        store.record_observations([Observation("hk", now - 999999, "tts", ok=True, quality=1.0)])
        assert store.window_stats("hk", now - 3600).samples == 0

    def test_success_rate(self, store):
        now = time.time()
        store.record_observations(
            [Observation("hk", now, "tts", ok=i < 8) for i in range(10)]
        )
        assert store.window_stats("hk", now - 3600).success_rate == pytest.approx(0.8)


class TestPersistence:
    def test_history_survives_reopen(self, tmp_path):
        """A restart must not reset the window - that would be exploitable."""
        from violet.validator.store import ValidatorStore

        path = str(tmp_path / "v.sqlite3")
        first = ValidatorStore(path)
        first.record_observations(
            [Observation("hk", time.time(), "tts", ok=True, quality=0.9)]
        )
        first.close()

        second = ValidatorStore(path)
        assert second.window_stats("hk", time.time() - 3600).samples == 1
        second.close()

    def test_previous_score_used_for_smoothing(self, store):
        store.record_score("hk", uid=1, capacity=0.5, work=0.5, quality=0.5,
                           raw=0.5, smoothed=0.4, final=0.4)
        assert store.previous_score("hk") == pytest.approx(0.4)

    def test_previous_score_absent(self, store):
        assert store.previous_score("nobody") is None

    def test_qualification_upsert(self, store):
        store.record_qualification("hk", "http://a", False, "failed streaming")
        store.record_qualification("hk", "http://a", True, "all tests passed")
        row = store.qualification("hk")
        assert bool(row["passed"]) is True
        assert row["detail"] == "all tests passed"

    def test_prune_removes_old_rows_only(self, store):
        now = time.time()
        store.record_observations(
            [
                Observation("hk", now - 999999, "tts", ok=True),
                Observation("hk", now, "tts", ok=True),
            ]
        )
        store.prune(now - 3600)
        assert store.window_stats("hk", now - 3600).samples == 1


class TestEvalSet:
    def test_builtin_loads(self):
        evalset = load_evalset()
        assert evalset.asr and evalset.tts
        # The built-in set has no audio on disk, and must say so rather than
        # letting a validator report meaningless WER.
        assert evalset.synthetic_only

    def test_rotation_is_deterministic(self):
        evalset = load_evalset()
        first = [item.id for item in evalset.rotate_asr(seed=42, count=3)]
        second = [item.id for item in evalset.rotate_asr(seed=42, count=3)]
        # Every validator in a round must draw the same utterances, or honest
        # validators diverge from consensus through sampling noise alone.
        assert first == second

    def test_rotation_varies_with_seed(self):
        evalset = load_evalset()
        seeds = {
            tuple(item.id for item in evalset.rotate_asr(seed=seed, count=2))
            for seed in range(12)
        }
        assert len(seeds) > 1

    def test_rotation_clamps_to_available_items(self):
        evalset = load_evalset()
        assert len(evalset.rotate_tts(seed=1, count=999)) <= len(evalset.tts)

    def test_synthetic_audio_is_deterministic_and_valid(self):
        first = synthetic_wav("seed-a", 1.0)
        assert first == synthetic_wav("seed-a", 1.0)
        assert first != synthetic_wav("seed-b", 1.0)
        assert first[:4] == b"RIFF"

    def test_language_coverage_includes_african_languages(self):
        from violet.evalset import language_coverage

        coverage = language_coverage(load_evalset())
        assert {"swa", "lug"} <= set(coverage)
