"""The incentive mechanism: components, weighting, smoothing, weight vectors."""

from __future__ import annotations

import pytest

from violet.chain.weights import MAX_SINGLE_WEIGHT, normalize_scores
from violet.constants import PHASES, TARGET_FIRST_BYTE_MS
from violet.validator.scoring import (
    compute_components,
    latency_multiplier,
    normalize_component,
    score_miners,
    suggest_phase,
)
from violet.validator.store import WindowStats


def stats(
    hotkey: str = "hk",
    *,
    capacity: float = 1.0,
    requests: int = 0,
    quality: float | None = 0.9,
    availability: float = 1.0,
    first_byte_ms: float = 100.0,
    samples: int = 50,
    successes: int | None = None,
) -> WindowStats:
    return WindowStats(
        hotkey=hotkey,
        samples=samples,
        successes=samples if successes is None else successes,
        mean_quality=quality,
        p95_first_byte_ms=first_byte_ms,
        availability=availability,
        mean_online_capacity=capacity,
        capacity_samples=samples,
        requests=requests,
        work_seconds=requests * 10.0,
    )


class TestLatencyMultiplier:
    def test_full_credit_at_target(self):
        assert latency_multiplier(TARGET_FIRST_BYTE_MS) == 1.0
        assert latency_multiplier(50.0) == 1.0

    def test_decays_above_target(self):
        assert latency_multiplier(800.0) < 1.0

    def test_floors_rather_than_zeroing(self):
        # A distant-but-working miner must keep earning something, or the
        # network concentrates geographically.
        assert latency_multiplier(99999.0) > 0.0

    def test_monotonic(self):
        values = [latency_multiplier(ms) for ms in (100, 300, 600, 1200, 5000)]
        assert values == sorted(values, reverse=True)

    def test_unmeasured_is_neutral(self):
        assert latency_multiplier(None) == 1.0


class TestComponents:
    def test_unqualified_earns_nothing(self):
        result = compute_components(stats(capacity=10.0, requests=5000), qualified=False)
        assert result.capacity_raw == 0.0
        assert result.work_raw == 0.0
        assert result.quality_raw == 0.0
        assert "not qualified" in result.notes[0]

    def test_capacity_tracks_online_capacity(self):
        low = compute_components(stats(capacity=1.0))
        high = compute_components(stats(capacity=8.0))
        assert high.capacity_raw > low.capacity_raw

    def test_unhealthy_miner_has_capacity_decayed(self):
        healthy = compute_components(stats(capacity=10.0, availability=1.0))
        unhealthy = compute_components(stats(capacity=10.0, availability=0.2))
        assert unhealthy.capacity_raw < healthy.capacity_raw
        assert any("availability" in note for note in unhealthy.notes)

    def test_resource_penalty_applies_to_capacity_and_work(self):
        clean = compute_components(stats(capacity=10.0, requests=1000))
        penalised = compute_components(
            stats(capacity=10.0, requests=1000), resource_penalty=0.25
        )
        assert penalised.capacity_raw == pytest.approx(clean.capacity_raw * 0.25)
        assert penalised.work_raw == pytest.approx(clean.work_raw * 0.25)

    def test_work_grows_with_traffic_but_sublinearly(self):
        small = compute_components(stats(requests=100)).work_raw
        large = compute_components(stats(requests=10000)).work_raw
        assert large > small
        # Log compression: 100x the traffic must not mean 100x the score, or one
        # operator flattens everyone else to zero.
        assert large < small * 100

    def test_slow_miner_earns_less_work(self):
        fast = compute_components(stats(requests=1000, first_byte_ms=100)).work_raw
        slow = compute_components(stats(requests=1000, first_byte_ms=2000)).work_raw
        assert slow < fast

    def test_quality_scaled_by_success_rate(self):
        reliable = compute_components(stats(quality=0.9, samples=100, successes=100))
        flaky = compute_components(stats(quality=0.9, samples=100, successes=30))
        assert flaky.quality_raw < reliable.quality_raw

    def test_no_measurement_means_no_quality_credit(self):
        result = compute_components(stats(quality=None))
        assert result.quality_raw == 0.0


class TestNormalizeComponent:
    def test_best_scores_one(self):
        result = normalize_component({"a": 2.0, "b": 4.0})
        assert result["b"] == 1.0
        assert result["a"] == 0.5

    def test_all_zero_stays_zero(self):
        assert normalize_component({"a": 0.0, "b": 0.0}) == {"a": 0.0, "b": 0.0}

    def test_empty(self):
        assert normalize_component({}) == {}


class TestScoreMiners:
    def test_weights_are_applied(self):
        components = [
            compute_components(stats("cap", capacity=10.0, requests=0, quality=0.5)),
            compute_components(stats("work", capacity=0.1, requests=50000, quality=0.5)),
        ]
        launch = {s.hotkey: s.final for s in score_miners(components, PHASES["launch"])}
        mature = {s.hotkey: s.final for s in score_miners(components, PHASES["mature"])}

        # The whole point of the phase schedule: capacity dominates at launch,
        # work dominates at maturity.
        assert launch["cap"] > launch["work"]
        assert mature["work"] > mature["cap"]

    def test_smoothing_blends_with_prior(self):
        components = [compute_components(stats("hk", capacity=1.0))]
        without = score_miners(components, PHASES["launch"])[0].final
        with_prior = score_miners(
            components, PHASES["launch"], previous_scores={"hk": 0.0}
        )[0].final
        assert with_prior < without

    def test_first_appearance_is_not_smoothed(self):
        components = [compute_components(stats("new", capacity=1.0))]
        result = score_miners(components, PHASES["launch"], previous_scores={})[0]
        assert result.smoothed == result.raw

    def test_empty_set(self):
        assert score_miners([], PHASES["launch"]) == []


class TestWeightVector:
    def test_sums_to_one(self):
        _, weights = normalize_scores({0: 1.0, 1: 0.5, 2: 0.25})
        assert sum(weights) == pytest.approx(1.0, abs=1e-5)

    def test_all_zero_returns_nothing(self):
        # The caller must treat this as "do not submit", never as "submit zeros".
        uids, weights = normalize_scores({0: 0.0, 1: 0.0})
        assert uids == [] and weights == []

    def test_negative_scores_excluded(self):
        uids, _ = normalize_scores({0: 1.0, 1: -5.0})
        assert uids == [0]

    def test_dust_is_dropped(self):
        uids, _ = normalize_scores({0: 1.0, 1: 0.0001})
        assert 1 not in uids

    def test_concentration_is_capped(self):
        # One miner scoring far above the rest must not take the whole subnet:
        # that is one outage away from having no capacity.
        scores = {0: 100.0, **{i: 1.0 for i in range(1, 12)}}
        uids, weights = normalize_scores(scores)
        assert max(weights) <= MAX_SINGLE_WEIGHT + 1e-6
        assert sum(weights) == pytest.approx(1.0, abs=1e-5)

    def test_cap_unreachable_with_few_miners(self):
        # With 2 miners the 25% cap cannot hold; the function must return a
        # valid vector rather than looping.
        uids, weights = normalize_scores({0: 1.0, 1: 1.0})
        assert sum(weights) == pytest.approx(1.0)
        assert len(uids) == 2

    def test_aligned_output(self):
        uids, weights = normalize_scores({5: 1.0, 2: 2.0})
        assert uids == sorted(uids)
        assert len(uids) == len(weights)


class TestPhaseTransition:
    def test_no_suggestion_at_low_volume(self):
        assert suggest_phase(100, 7, "launch") is None

    def test_suggests_growth_at_moderate_volume(self):
        assert suggest_phase(20_000 * 7, 7, "launch") == "growth"

    def test_suggests_mature_at_high_volume(self):
        assert suggest_phase(200_000 * 7, 7, "launch") == "mature"

    def test_never_suggests_going_backwards(self):
        # A quiet week is not a reason to re-weight the whole network back
        # toward capacity.
        assert suggest_phase(0, 7, "mature") is None

    def test_no_suggestion_when_already_correct(self):
        assert suggest_phase(20_000 * 7, 7, "growth") is None
