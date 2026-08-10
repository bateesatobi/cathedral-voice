#!/usr/bin/env python3
"""
Simulate the incentive mechanism without a chain or any miners.

Answers the questions the design document raises but does not quantify: how much
does a large idle operator earn versus a small busy one, what actually changes
at a phase transition, and how much does the multi-UID rule cost a Sybil.

    python scripts/simulate_scoring.py
    python scripts/simulate_scoring.py --phase mature
    python scripts/simulate_scoring.py --sybil
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from violet.chain.weights import normalize_scores
from violet.constants import PHASES
from violet.validator.antigaming import apply_multi_uid_policy
from violet.validator.scoring import compute_components, score_miners
from violet.validator.store import ValidatorStore, WindowStats


def make_stats(
    hotkey: str,
    *,
    capacity: float,
    requests: int,
    quality: float,
    availability: float = 1.0,
    first_byte_ms: float = 150.0,
) -> WindowStats:
    return WindowStats(
        hotkey=hotkey,
        samples=100,
        successes=int(100 * availability),
        mean_quality=quality,
        mean_first_byte_ms=first_byte_ms,
        p95_first_byte_ms=first_byte_ms * 1.4,
        availability=availability,
        mean_online_capacity=capacity,
        capacity_samples=100,
        requests=requests,
        work_seconds=requests * 12.0,
    )


#: Deliberately spread across the space the document worries about: a large
#: operator with no traffic, a small operator saturated with it, and a
#: high-capacity host that is frequently down.
SCENARIOS = [
    # name,            capacity, requests, quality, availability, latency
    ("whale-idle",         28.0,        0,    0.92,         1.00,   180),
    ("whale-busy",         28.0,    40000,    0.94,         0.99,   140),
    ("mid-steady",          6.4,     8000,    0.91,         0.99,   160),
    ("small-busy",          1.0,     6000,    0.93,         1.00,   120),
    ("small-idle",          1.0,        0,    0.90,         1.00,   210),
    ("flaky-large",        14.0,     3000,    0.88,         0.55,   700),
    ("slow-but-up",         3.5,     4000,    0.89,         1.00,  1200),
]


def run(phase_name: str, sybil: bool) -> None:
    weights = PHASES[phase_name]
    print(f"\nPhase: {phase_name}  "
          f"(capacity {weights.capacity:.0%} / work {weights.work:.0%} / quality {weights.quality:.0%})")
    print("=" * 96)

    scenarios = list(SCENARIOS)
    coldkeys = {name: f"cold-{name}" for name, *_ in scenarios}

    if sybil:
        # One operator splits a 28-unit machine across four hotkeys, hoping four
        # UIDs earn more than one. TDD 9.1 says they will not.
        for index in range(4):
            name = f"sybil-{index}"
            scenarios.append((name, 7.0, 10000, 0.92, 0.99, 150))
            coldkeys[name] = "cold-sybil"

    components = []
    for uid, (name, capacity, requests, quality, availability, latency) in enumerate(scenarios):
        stats = make_stats(
            name, capacity=capacity, requests=requests, quality=quality,
            availability=availability, first_byte_ms=latency,
        )
        components.append(compute_components(stats, uid=uid, qualified=True))

    scores = score_miners(components, weights)

    db = Path(tempfile.mkdtemp()) / "sim.sqlite3"
    store = ValidatorStore(str(db))
    scores, report = apply_multi_uid_policy(scores, coldkeys, store)
    store.close()

    uids, weight_vector = normalize_scores({s.uid: s.final for s in scores})
    emission = dict(zip(uids, weight_vector))

    print(f"{'miner':<14} {'cap':>6} {'reqs':>7} {'C':>6} {'W':>6} {'Q':>6} "
          f"{'score':>7} {'emission':>9}")
    print("-" * 96)

    for score, (name, capacity, requests, *_rest) in zip(scores, scenarios):
        share = emission.get(score.uid, 0.0)
        print(
            f"{name:<14} {capacity:>6.1f} {requests:>7} "
            f"{score.capacity:>6.3f} {score.work:>6.3f} {score.quality:>6.3f} "
            f"{score.final:>7.4f} {share:>8.1%}"
        )

    if report.zeroed:
        print(f"\nmulti-UID: {report.summary()}")
        sybil_share = sum(
            emission.get(score.uid, 0.0)
            for score in scores
            if score.hotkey.startswith("sybil-")
        )
        print(
            f"the Sybil's four hotkeys hold 28 capacity units and earn "
            f"{sybil_share:.1%} of emissions - the same as one honest hotkey "
            "with that hardware would"
        )

    for score in scores:
        if score.notes and not score.hotkey.startswith("sybil-"):
            print(f"\n  {score.hotkey}: {'; '.join(score.notes)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Simulate Violet scoring")
    parser.add_argument(
        "--phase", default="", choices=sorted(PHASES) + [""],
        help="one phase to simulate (default: all three, for comparison)",
    )
    parser.add_argument(
        "--sybil", action="store_true",
        help="add an operator splitting one machine across four hotkeys",
    )
    args = parser.parse_args()

    phases = [args.phase] if args.phase else ["launch", "growth", "mature"]
    for phase in phases:
        run(phase, args.sybil)

    if not args.phase:
        print(
            "\nNote how 'whale-idle' loses share as the phase advances while "
            "'small-busy' gains: that is the intended migration from capacity "
            "attraction to usage-aligned reward (TDD 7)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
