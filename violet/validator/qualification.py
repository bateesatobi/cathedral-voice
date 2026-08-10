"""
The six mandatory qualification tests (TDD 4.4).

A miner earns nothing until it passes all six. The suite is also runnable by
operators against their own box before they spend anything on registration
(``scripts/run_qualification.py``), which is the point of TDD 4.3 step 1.

Sustained Availability is the odd one out: it needs a 30-60 minute observation
window, which cannot run inside a single evaluation sweep. It is therefore
evaluated from the health history the validator has already accumulated, rather
than by blocking a sweep for half an hour.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from ..constants import (
    QUALIFY_ASR_TIMEOUT_S,
    QUALIFY_HEALTH_TIMEOUT_S,
    QUALIFY_MAX_HEALTH_FAILURE_RATE,
    QUALIFY_MAX_WER,
    QUALIFY_STREAM_FIRST_CHUNK_S,
    QUALIFY_TTS_TIMEOUT_S,
    RESOURCE_CLAIM_TOLERANCE,
    SERVICE_ASR,
    SERVICE_TTS,
    VRAM_TOLERANCE_FRACTION,
    GPU_TIERS_BY_KEY,
)
from ..evalset import EvalSet
from ..protocol import CapacityReport, HealthReport
from .probes import MinerProbe, ProbeResult

logger = logging.getLogger("violet.validator.qualification")

TEST_HEALTH = "health_connectivity"
TEST_ASR = "asr_functional"
TEST_TTS = "tts_functional"
TEST_STREAMING = "streaming"
TEST_AVAILABILITY = "sustained_availability"
TEST_RESOURCES = "resource_accuracy"

ALL_TESTS = (
    TEST_HEALTH,
    TEST_ASR,
    TEST_TTS,
    TEST_STREAMING,
    TEST_AVAILABILITY,
    TEST_RESOURCES,
)


@dataclass
class TestOutcome:
    name: str
    passed: bool
    detail: str
    #: True when the test does not apply - e.g. the ASR test on a TTS-only
    #: miner. Skipped tests do not block admission.
    skipped: bool = False
    measurements: Dict[str, float] = field(default_factory=dict)


@dataclass
class QualificationResult:
    hotkey: str
    endpoint: str
    outcomes: List[TestOutcome] = field(default_factory=list)
    evaluated_at: float = field(default_factory=time.time)

    @property
    def passed(self) -> bool:
        return all(o.passed or o.skipped for o in self.outcomes) and bool(self.outcomes)

    @property
    def failures(self) -> List[TestOutcome]:
        return [o for o in self.outcomes if not o.passed and not o.skipped]

    def summary(self) -> str:
        if self.passed:
            return "all tests passed"
        return "; ".join(f"{o.name}: {o.detail}" for o in self.failures)

    def to_dict(self) -> Dict[str, object]:
        return {
            "hotkey": self.hotkey,
            "endpoint": self.endpoint,
            "passed": self.passed,
            "evaluated_at": self.evaluated_at,
            "tests": [
                {
                    "name": o.name,
                    "passed": o.passed,
                    "skipped": o.skipped,
                    "detail": o.detail,
                    "measurements": o.measurements,
                }
                for o in self.outcomes
            ],
        }


@dataclass
class AvailabilitySample:
    """One health observation, as recorded by the validator's health loop."""

    at: float
    ok: bool


async def run_qualification(
    probe: MinerProbe,
    evalset: EvalSet,
    *,
    services: Sequence[str],
    availability: Sequence[AvailabilitySample] = (),
    availability_window_s: float,
    seed: int = 0,
    max_wer: float = QUALIFY_MAX_WER,
    skip_availability: bool = False,
) -> QualificationResult:
    """Run the full suite against one miner."""
    result = QualificationResult(hotkey=probe.hotkey, endpoint=probe.endpoint)

    # 1. Health & connectivity -------------------------------------------
    health_probe = await probe.health(timeout_s=QUALIFY_HEALTH_TIMEOUT_S)
    health_report: Optional[HealthReport] = health_probe.payload.get("health")
    result.outcomes.append(
        TestOutcome(
            name=TEST_HEALTH,
            passed=health_probe.ok,
            detail=(
                f"{health_probe.detail} in {health_probe.latency_ms:.0f}ms"
                if health_probe.ok
                else health_probe.detail
            ),
            measurements={"latency_ms": round(health_probe.latency_ms, 1)},
        )
    )

    if not health_probe.ok:
        # Nothing else can be measured against an unreachable miner. Record the
        # remaining tests as failed-by-dependency so the operator sees the
        # cause rather than five unexplained failures.
        for name in (TEST_ASR, TEST_TTS, TEST_STREAMING, TEST_AVAILABILITY, TEST_RESOURCES):
            result.outcomes.append(
                TestOutcome(name, passed=False, detail="skipped: miner unreachable")
            )
        return result

    serves_asr = SERVICE_ASR in services
    serves_tts = SERVICE_TTS in services

    # 2. ASR functional ---------------------------------------------------
    asr_item = (evalset.rotate_asr(seed, 1) or [None])[0]
    if not serves_asr or asr_item is None:
        result.outcomes.append(
            TestOutcome(TEST_ASR, True, "not offered by this miner", skipped=True)
        )
        asr_probe = None
    else:
        asr_probe = await probe.asr_batch(asr_item, timeout_s=QUALIFY_ASR_TIMEOUT_S)
        # When the corpus has no real audio, WER measures nothing; requiring it
        # would fail every honest miner. Fall back to "returned a transcript
        # within the latency bound" and say so.
        if evalset.synthetic_only:
            passed = asr_probe.ok
            detail = (
                f"transcript returned in {asr_probe.latency_ms:.0f}ms "
                "(WER not scored: evaluation corpus has no real audio)"
                if passed
                else asr_probe.detail
            )
        else:
            wer = asr_probe.wer if asr_probe.wer is not None else 1.0
            passed = asr_probe.ok and wer <= max_wer
            detail = (
                f"WER {wer:.3f} <= {max_wer:.2f} in {asr_probe.latency_ms:.0f}ms"
                if passed
                else (asr_probe.detail if not asr_probe.ok else f"WER {wer:.3f} exceeds {max_wer:.2f}")
            )
        result.outcomes.append(
            TestOutcome(
                TEST_ASR,
                passed,
                detail,
                measurements={
                    "latency_ms": round(asr_probe.latency_ms, 1),
                    "wer": round(asr_probe.wer, 4) if asr_probe.wer is not None else -1.0,
                },
            )
        )

    # 3. TTS functional ---------------------------------------------------
    tts_item = (evalset.rotate_tts(seed, 1) or [None])[0]
    if not serves_tts or tts_item is None:
        result.outcomes.append(
            TestOutcome(TEST_TTS, True, "not offered by this miner", skipped=True)
        )
        tts_probe = None
    else:
        tts_probe = await probe.tts_batch(tts_item, timeout_s=QUALIFY_TTS_TIMEOUT_S)
        passed = tts_probe.ok and (tts_probe.quality or 0.0) >= 0.5
        result.outcomes.append(
            TestOutcome(
                TEST_TTS,
                passed,
                (
                    f"{tts_probe.payload.get('bytes', 0)} bytes, quality "
                    f"{tts_probe.quality:.2f} ({tts_probe.detail})"
                    if tts_probe.ok
                    else tts_probe.detail
                ),
                measurements={
                    "latency_ms": round(tts_probe.latency_ms, 1),
                    "quality": round(tts_probe.quality or 0.0, 4),
                    "first_byte_ms": round(tts_probe.first_byte_ms or 0.0, 1),
                },
            )
        )

    # 4. Streaming --------------------------------------------------------
    stream_results: List[ProbeResult] = []
    if serves_asr and asr_item is not None:
        stream_results.append(
            await probe.asr_stream(
                asr_item, first_chunk_timeout_s=QUALIFY_STREAM_FIRST_CHUNK_S
            )
        )
    if serves_tts and tts_item is not None:
        stream_results.append(
            await probe.tts_stream(
                tts_item, first_chunk_timeout_s=QUALIFY_STREAM_FIRST_CHUNK_S
            )
        )

    if not stream_results:
        result.outcomes.append(
            TestOutcome(TEST_STREAMING, True, "no streaming service offered", skipped=True)
        )
    else:
        # Every offered service must stream. A miner that streams TTS but not
        # ASR cannot serve the ASR half of a real-time session.
        failed = [r for r in stream_results if not r.ok]
        first_bytes = [r.first_byte_ms for r in stream_results if r.first_byte_ms is not None]
        result.outcomes.append(
            TestOutcome(
                TEST_STREAMING,
                passed=not failed,
                detail=(
                    "; ".join(f"{r.kind}: {r.detail}" for r in failed)
                    if failed
                    else "; ".join(
                        f"{r.kind} first chunk {r.first_byte_ms:.0f}ms ({r.detail})"
                        for r in stream_results
                        if r.first_byte_ms is not None
                    )
                    or "streaming verified"
                ),
                measurements={
                    "first_byte_ms": round(min(first_bytes), 1) if first_bytes else -1.0
                },
            )
        )

    # 5. Sustained availability ------------------------------------------
    if skip_availability:
        result.outcomes.append(
            TestOutcome(
                TEST_AVAILABILITY, True, "skipped by request", skipped=True
            )
        )
    else:
        result.outcomes.append(
            _availability_outcome(availability, availability_window_s)
        )

    # 6. Resource accuracy ------------------------------------------------
    capacity = health_report.capacity if health_report else None
    throughput = None
    if serves_tts and tts_item is not None and capacity and capacity.gpus:
        throughput = await probe.throughput(tts_item, parallel=4)
    result.outcomes.append(_resource_outcome(capacity, throughput))

    return result


def _availability_outcome(
    samples: Sequence[AvailabilitySample], window_s: float
) -> TestOutcome:
    """Judge availability from accumulated health history."""
    now = time.time()
    in_window = [s for s in samples if now - s.at <= window_s]

    if not in_window:
        return TestOutcome(
            TEST_AVAILABILITY,
            passed=False,
            detail="no health history yet; observation window still accumulating",
            measurements={"samples": 0.0},
        )

    span = now - min(s.at for s in in_window)
    if span < window_s * 0.8:
        # Not enough elapsed time to judge. Explicitly *not* a pass: a fresh
        # miner must be observed before it is admitted (TDD 4.4).
        return TestOutcome(
            TEST_AVAILABILITY,
            passed=False,
            detail=(
                f"observed for {span / 60:.0f} of {window_s / 60:.0f} required minutes"
            ),
            measurements={"observed_s": round(span, 1), "samples": float(len(in_window))},
        )

    failures = sum(1 for s in in_window if not s.ok)
    failure_rate = failures / len(in_window)
    passed = failure_rate <= QUALIFY_MAX_HEALTH_FAILURE_RATE
    return TestOutcome(
        TEST_AVAILABILITY,
        passed=passed,
        detail=(
            f"{failures}/{len(in_window)} health checks failed "
            f"({failure_rate:.1%}, limit {QUALIFY_MAX_HEALTH_FAILURE_RATE:.0%})"
        ),
        measurements={
            "failure_rate": round(failure_rate, 4),
            "samples": float(len(in_window)),
            "observed_s": round(span, 1),
        },
    )


def _resource_outcome(
    capacity: Optional[CapacityReport], throughput: Optional[ProbeResult]
) -> TestOutcome:
    """Check reported hardware is internally consistent and behaves like itself.

    v1.4 has no cryptographic attestation (TDD 9.3), so this is a consistency
    argument, not a proof: does the reported VRAM match the claimed model, and
    does the box absorb concurrency the way that much hardware should. It raises
    the cost of a false claim without pretending to eliminate it.
    """
    if capacity is None:
        return TestOutcome(
            TEST_RESOURCES, passed=False, detail="miner reported no capacity data"
        )

    if not capacity.gpus:
        # Honest zero-GPU report. Not a misrepresentation, so the test passes;
        # the miner simply earns no Capacity score.
        return TestOutcome(
            TEST_RESOURCES,
            passed=True,
            detail="no accepted GPU reported; capacity score will be zero",
            measurements={"capacity_units": 0.0},
        )

    problems: List[str] = []
    for gpu in capacity.gpus:
        tier = GPU_TIERS_BY_KEY.get(gpu.tier_key)
        if tier is None:
            problems.append(f"GPU {gpu.index}: unknown tier {gpu.tier_key!r}")
            continue
        if abs(gpu.multiplier - tier.multiplier) > 1e-6:
            problems.append(
                f"GPU {gpu.index}: multiplier {gpu.multiplier} does not match "
                f"tier {tier.key} ({tier.multiplier})"
            )
        if gpu.vram_gb > 0:
            deviation = abs(gpu.vram_gb - tier.vram_gb) / tier.vram_gb
            if deviation > VRAM_TOLERANCE_FRACTION:
                problems.append(
                    f"GPU {gpu.index}: reports {gpu.vram_gb:.0f} GB but tier "
                    f"{tier.key} is {tier.vram_gb} GB"
                )

    measurements = {"capacity_units": capacity.capacity_units}

    if throughput is not None and throughput.ok:
        succeeded = float(throughput.payload.get("succeeded", 0))
        measurements["concurrent_succeeded"] = succeeded
        measurements["mean_latency_ms"] = round(
            float(throughput.payload.get("mean_latency_ms", 0.0)), 1
        )
        parallel = float(throughput.payload.get("parallel", 1)) or 1.0
        # A miner claiming multi-GPU capacity that cannot serve even half of a
        # 4-way concurrent burst is claiming more than it has.
        if capacity.capacity_units >= 2.0 and succeeded / parallel < 0.5:
            problems.append(
                f"claims {capacity.capacity_units:.1f} capacity units but served "
                f"only {int(succeeded)}/{int(parallel)} concurrent requests"
            )
    elif throughput is not None:
        problems.append(f"concurrency check failed: {throughput.detail}")

    if capacity.max_concurrent_asr + capacity.max_concurrent_tts == 0 and capacity.gpus:
        problems.append(
            "reports GPUs but advertises zero concurrency; the router cannot "
            "route to a miner that admits nothing"
        )

    passed = not problems
    return TestOutcome(
        TEST_RESOURCES,
        passed=passed,
        detail=(
            "; ".join(problems)
            if problems
            else f"{len(capacity.gpus)} GPU(s), {capacity.capacity_units:.1f} capacity units"
        ),
        measurements=measurements,
    )


def resource_penalty(result: QualificationResult) -> float:
    """Multiplier applied to a miner that failed the resource-accuracy test."""
    from ..constants import RESOURCE_MISREPORT_PENALTY

    for outcome in result.outcomes:
        if outcome.name == TEST_RESOURCES and not outcome.passed and not outcome.skipped:
            return RESOURCE_MISREPORT_PENALTY
    return 1.0


__all__ = [
    "ALL_TESTS",
    "AvailabilitySample",
    "QualificationResult",
    "TestOutcome",
    "TEST_ASR",
    "TEST_AVAILABILITY",
    "TEST_HEALTH",
    "TEST_RESOURCES",
    "TEST_STREAMING",
    "TEST_TTS",
    "resource_penalty",
    "run_qualification",
]
