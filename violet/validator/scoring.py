"""
The incentive mechanism (TDD 7).

    Final Score = w_c * C + w_w * W + w_q * Q

with the weights shifting from capacity-heavy at launch to work-heavy at
maturity. Each component is normalised to ``[0, 1]`` across the *active set*
before weighting, so the three are commensurable: an absolute capacity figure
and an absolute request count are not otherwise comparable quantities.

On the bootstrap bias
---------------------
The document is candid that early capacity weighting risks "prolonged
over-rewarding of idle capacity if organic traffic growth is slower than
anticipated". Two guards are implemented here rather than left to policy:

* capacity is credited only while the miner is *healthy* (see
  ``ValidatorStore.window_stats``), so idle-but-online is rewarded while
  idle-and-broken is not;
* :func:`suggest_phase` computes, from observed request volume, whether the
  network has outgrown its configured phase, and the validator logs the
  recommendation. That turns "predefined phase-transition criteria" from an
  intention into a number someone can act on.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from ..constants import (
    LATENCY_MULTIPLIER_FLOOR,
    MAX_ACCEPTABLE_FIRST_BYTE_MS,
    PHASES,
    SCORE_SMOOTHING_ALPHA,
    TARGET_FIRST_BYTE_MS,
    UNHEALTHY_SCORE_DECAY,
    PhaseWeights,
)
from .store import WindowStats

logger = logging.getLogger("violet.validator.scoring")

#: Availability below this is treated as a broken miner rather than a busy one.
MIN_AVAILABILITY_FOR_CREDIT = 0.5

#: Requests per day across the network above which the next phase is warranted.
#: Derived from TDD 8's dimensioning: 100 concurrent users at a few requests per
#: minute is order 10^5 requests/day, which is when work signal stops being noise.
PHASE_THRESHOLDS_REQUESTS_PER_DAY = {
    "launch": 0.0,
    "growth": 20_000.0,
    "mature": 150_000.0,
}


@dataclass
class ComponentScores:
    """The three raw components for one miner, before cross-miner normalisation."""

    hotkey: str
    uid: Optional[int] = None
    capacity_raw: float = 0.0
    work_raw: float = 0.0
    quality_raw: float = 0.0
    availability: float = 0.0
    latency_multiplier: float = 1.0
    notes: List[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.notes.append(message)


@dataclass
class MinerScore:
    """Final scored result for one miner."""

    hotkey: str
    uid: Optional[int]
    capacity: float
    work: float
    quality: float
    raw: float
    smoothed: float
    final: float
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "hotkey": self.hotkey,
            "uid": self.uid,
            "capacity": round(self.capacity, 6),
            "work": round(self.work, 6),
            "quality": round(self.quality, 6),
            "raw": round(self.raw, 6),
            "smoothed": round(self.smoothed, 6),
            "final": round(self.final, 6),
            "notes": list(self.notes),
        }


def latency_multiplier(first_byte_ms: Optional[float]) -> float:
    """Latency shaping applied to the Work component (TDD 7).

    Full credit at or under the 200 ms target, decaying linearly to a floor.
    A floor rather than zero because a miner serving a distant region will never
    hit 200 ms from every validator, and zeroing it would concentrate the
    network geographically - the opposite of what a decentralised subnet wants.
    """
    if first_byte_ms is None:
        return 1.0
    if first_byte_ms <= TARGET_FIRST_BYTE_MS:
        return 1.0
    if first_byte_ms >= MAX_ACCEPTABLE_FIRST_BYTE_MS:
        return LATENCY_MULTIPLIER_FLOOR
    span = MAX_ACCEPTABLE_FIRST_BYTE_MS - TARGET_FIRST_BYTE_MS
    decayed = (first_byte_ms - TARGET_FIRST_BYTE_MS) / span
    return 1.0 - decayed * (1.0 - LATENCY_MULTIPLIER_FLOOR)


def compute_components(
    stats: WindowStats,
    *,
    uid: Optional[int] = None,
    qualified: bool = True,
    resource_penalty: float = 1.0,
    window_days: float = 7.0,
) -> ComponentScores:
    """Turn one miner's window aggregates into raw C, W and Q."""
    scores = ComponentScores(hotkey=stats.hotkey, uid=uid)
    scores.availability = stats.availability

    if not qualified:
        scores.note("not qualified: no components credited")
        return scores

    # -- Capacity ---------------------------------------------------------
    # Mean capacity units held online and healthy across the window.
    capacity = stats.mean_online_capacity
    if stats.availability < MIN_AVAILABILITY_FOR_CREDIT:
        capacity *= UNHEALTHY_SCORE_DECAY
        scores.note(
            f"availability {stats.availability:.0%} below "
            f"{MIN_AVAILABILITY_FOR_CREDIT:.0%}: capacity decayed"
        )
    scores.capacity_raw = max(0.0, capacity * resource_penalty)
    if resource_penalty < 1.0:
        scores.note(f"resource misreport penalty x{resource_penalty:.2f}")

    # -- Work -------------------------------------------------------------
    # Requests and streaming seconds are different units; both are compressed
    # logarithmically so that a miner with 10x the traffic scores meaningfully
    # more without one high-volume operator flattening everyone else to zero.
    latency_mult = latency_multiplier(stats.p95_first_byte_ms)
    scores.latency_multiplier = latency_mult

    request_term = math.log1p(max(0, stats.requests))
    # 1 stream-minute is treated as comparable to 1 request.
    minutes_term = math.log1p(max(0.0, stats.work_seconds / 60.0))
    scores.work_raw = (request_term + minutes_term) * latency_mult * resource_penalty
    if latency_mult < 1.0 and stats.p95_first_byte_ms:
        scores.note(
            f"p95 first byte {stats.p95_first_byte_ms:.0f}ms: work x{latency_mult:.2f}"
        )

    # -- Quality ----------------------------------------------------------
    # Probe quality is the primary signal; availability and success rate shape
    # it, because a miner that answers beautifully one time in three is not
    # delivering quality service.
    measured = stats.mean_quality
    if measured is None:
        # No successful quality-bearing probe in the window. Not zero - the
        # miner may be new - but nothing to credit either.
        scores.quality_raw = 0.0
        if stats.samples:
            scores.note("no quality measurement in window")
    else:
        scores.quality_raw = measured * stats.success_rate * max(
            stats.availability, 0.0
        )
        if stats.success_rate < 1.0:
            scores.note(f"success rate {stats.success_rate:.0%}")

    return scores


def normalize_component(values: Dict[str, float]) -> Dict[str, float]:
    """Scale a component to ``[0, 1]`` across the active set.

    Max-normalisation, not z-scores: the mechanism needs a bounded, monotone
    mapping where the best miner scores 1 and nothing goes negative.
    """
    if not values:
        return {}
    peak = max(values.values())
    if peak <= 0:
        return {key: 0.0 for key in values}
    return {key: max(0.0, value / peak) for key, value in values.items()}


def score_miners(
    components: Sequence[ComponentScores],
    weights: PhaseWeights,
    *,
    previous_scores: Optional[Dict[str, float]] = None,
    smoothing_alpha: float = SCORE_SMOOTHING_ALPHA,
) -> List[MinerScore]:
    """Combine components into final scores for the whole active set.

    Smoothing (TDD 9.2) blends this window with the previous published score, so
    rankings move gradually and a single bad sweep - a validator network blip,
    say - cannot swing emissions.
    """
    if not components:
        return []

    capacity = normalize_component({c.hotkey: c.capacity_raw for c in components})
    work = normalize_component({c.hotkey: c.work_raw for c in components})
    quality = normalize_component({c.hotkey: c.quality_raw for c in components})

    previous = previous_scores or {}
    results: List[MinerScore] = []

    for component in components:
        hotkey = component.hotkey
        c_value = capacity.get(hotkey, 0.0)
        w_value = work.get(hotkey, 0.0)
        q_value = quality.get(hotkey, 0.0)

        raw = (
            weights.capacity * c_value
            + weights.work * w_value
            + weights.quality * q_value
        )

        prior = previous.get(hotkey)
        if prior is None:
            # First appearance: nothing to blend with, so the raw score stands.
            smoothed = raw
        else:
            smoothed = smoothing_alpha * raw + (1.0 - smoothing_alpha) * prior

        results.append(
            MinerScore(
                hotkey=hotkey,
                uid=component.uid,
                capacity=c_value,
                work=w_value,
                quality=q_value,
                raw=raw,
                smoothed=smoothed,
                final=smoothed,
                notes=list(component.notes),
            )
        )

    return results


def suggest_phase(
    total_requests: int, window_days: float, current_phase: str
) -> Optional[str]:
    """Recommend a phase transition from observed traffic, or ``None``.

    TDD 12 lists "formal phase-transition triggers based on observed request
    volume" as future work. This is that trigger, computed and reported; the
    switch itself stays a governance decision (TDD 11), so nothing changes
    automatically.
    """
    if window_days <= 0:
        return None
    per_day = total_requests / window_days

    warranted = "launch"
    for phase, threshold in sorted(
        PHASE_THRESHOLDS_REQUESTS_PER_DAY.items(), key=lambda kv: kv[1]
    ):
        if per_day >= threshold:
            warranted = phase

    if warranted == current_phase:
        return None

    order = ["launch", "growth", "mature"]
    try:
        if order.index(warranted) <= order.index(current_phase):
            # Only ever suggest moving forward. Traffic dipping for a week is
            # not a reason to re-weight the whole network back toward capacity.
            return None
    except ValueError:
        return None

    return warranted


def describe_weights(weights: PhaseWeights) -> str:
    return (
        f"{weights.name}: capacity {weights.capacity:.0%}, "
        f"work {weights.work:.0%}, quality {weights.quality:.0%}"
    )


def phase_for_name(name: str) -> PhaseWeights:
    if name not in PHASES:
        raise ValueError(f"unknown phase {name!r}; expected one of {sorted(PHASES)}")
    return PHASES[name]
