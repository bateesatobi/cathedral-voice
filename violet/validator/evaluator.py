"""
One evaluation sweep: probe every miner, persist what was measured.

Split from the run loop so a sweep can be executed once from a script or a test
without standing up the whole validator.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import aiohttp

from ..constants import (
    QUALIFICATION_TTL_HOURS,
    QUALIFY_AVAILABILITY_WINDOW_S,
    SERVICE_ASR,
    SERVICE_TTS,
)
from ..evalset import EvalSet
from .discovery import Discovery, MinerRecord, refresh_services_from_health
from .probes import MinerProbe, ProbeResult
from .qualification import (
    AvailabilitySample,
    QualificationResult,
    resource_penalty,
    run_qualification,
)
from .store import Observation, ValidatorStore

logger = logging.getLogger("violet.validator.evaluator")


@dataclass
class MinerEvaluation:
    """Everything measured about one miner in one sweep."""

    miner: MinerRecord
    healthy: bool = False
    capacity_units: float = 0.0
    qualification: Optional[QualificationResult] = None
    qualified: bool = False
    resource_multiplier: float = 1.0
    probes: List[ProbeResult] = field(default_factory=list)
    error: str = ""


class Evaluator:
    """Runs probe sweeps against the discovered miner set."""

    def __init__(
        self,
        store: ValidatorStore,
        evalset: EvalSet,
        *,
        concurrency: int = 8,
        access_token: str = "",
        availability_window_s: float = QUALIFY_AVAILABILITY_WINDOW_S,
    ):
        self.store = store
        self.evalset = evalset
        self.concurrency = max(1, concurrency)
        self.access_token = access_token
        self.availability_window_s = availability_window_s

    async def health_sweep(
        self, session: aiohttp.ClientSession, discovery: Discovery
    ) -> Dict[str, bool]:
        """Cheap liveness pass, run far more often than the full evaluation.

        Feeds both the availability history that the Sustained Availability test
        reads and the capacity-while-healthy series the Capacity score uses.
        """
        semaphore = asyncio.Semaphore(self.concurrency)

        async def probe_one(
            miner: MinerRecord,
        ) -> tuple[str, bool, float, List[dict], ProbeResult]:
            async with semaphore:
                probe = MinerProbe(
                    session, miner.endpoint,
                    hotkey=miner.hotkey, access_token=self.access_token,
                )
                result = await probe.health()
                capacity_units = 0.0
                gpus: List[dict] = []
                report = result.payload.get("health")
                if report and report.capacity:
                    capacity_units = report.capacity.capacity_units
                    gpus = [gpu.to_dict() for gpu in report.capacity.gpus]
                    refresh_services_from_health(miner, report.services)
                return miner.hotkey, result.ok, capacity_units, gpus, result

        results = await asyncio.gather(
            *(probe_one(miner) for miner in discovery.miners), return_exceptions=True
        )

        observations: List[Observation] = []
        healthy: Dict[str, bool] = {}
        now = time.time()

        for miner, outcome in zip(discovery.miners, results):
            if isinstance(outcome, BaseException):
                logger.debug("health probe raised for %s: %s", miner.hotkey[:8], outcome)
                healthy[miner.hotkey] = False
                observations.append(
                    Observation(
                        hotkey=miner.hotkey, uid=miner.uid, at=now, kind="health",
                        ok=False, detail=str(outcome)[:200],
                    )
                )
                self.store.record_capacity(miner.hotkey, 0.0, False, [])
                continue

            hotkey, ok, capacity_units, gpus, result = outcome
            healthy[hotkey] = ok
            observations.append(
                Observation(
                    hotkey=hotkey, uid=miner.uid, at=now, kind="health", ok=ok,
                    latency_ms=result.latency_ms, detail=result.detail,
                )
            )
            # Capacity is recorded on every health pass, healthy or not, so the
            # window mean reflects capacity actually kept online.
            self.store.record_capacity(hotkey, capacity_units, ok, gpus)

        self.store.record_observations(observations)
        online = sum(1 for ok in healthy.values() if ok)
        logger.info("health sweep: %d/%d miners healthy", online, len(healthy))
        return healthy

    async def evaluate(
        self, session: aiohttp.ClientSession, discovery: Discovery, *, seed: int
    ) -> List[MinerEvaluation]:
        """Full sweep: qualification plus quality/latency probes."""
        semaphore = asyncio.Semaphore(self.concurrency)

        async def evaluate_one(miner: MinerRecord) -> MinerEvaluation:
            async with semaphore:
                return await self._evaluate_miner(session, miner, seed=seed)

        outcomes = await asyncio.gather(
            *(evaluate_one(miner) for miner in discovery.miners), return_exceptions=True
        )

        evaluations: List[MinerEvaluation] = []
        for miner, outcome in zip(discovery.miners, outcomes):
            if isinstance(outcome, BaseException):
                logger.warning(
                    "evaluation raised for %s: %s", miner.hotkey[:8], outcome
                )
                evaluations.append(MinerEvaluation(miner=miner, error=str(outcome)[:200]))
                continue
            evaluations.append(outcome)

        qualified = sum(1 for e in evaluations if e.qualified)
        logger.info(
            "evaluation sweep: %d/%d miners qualified", qualified, len(evaluations)
        )
        return evaluations

    async def _evaluate_miner(
        self, session: aiohttp.ClientSession, miner: MinerRecord, *, seed: int
    ) -> MinerEvaluation:
        probe = MinerProbe(
            session, miner.endpoint, hotkey=miner.hotkey, access_token=self.access_token
        )
        evaluation = MinerEvaluation(miner=miner)
        now = time.time()

        availability = [
            AvailabilitySample(at=at, ok=ok)
            for at, ok in self.store.health_history(
                miner.hotkey, now - self.availability_window_s
            )
        ]

        qualification = await run_qualification(
            probe,
            self.evalset,
            services=miner.services,
            availability=availability,
            availability_window_s=self.availability_window_s,
            seed=seed,
        )
        evaluation.qualification = qualification
        evaluation.qualified = qualification.passed
        evaluation.resource_multiplier = resource_penalty(qualification)

        self.store.record_qualification(
            miner.hotkey, miner.endpoint, qualification.passed, qualification.summary()
        )

        if not qualification.passed:
            logger.info(
                "miner %s (uid %s) not qualified: %s",
                miner.hotkey[:8], miner.uid, qualification.summary(),
            )

        # Quality probes run regardless of qualification: a miner that is one
        # observation window away from admission should already have quality
        # history when it qualifies, rather than starting from nothing.
        observations: List[Observation] = []
        health_ok = any(
            o.name == "health_connectivity" and o.passed for o in qualification.outcomes
        )
        evaluation.healthy = health_ok

        if health_ok:
            probes = await self._quality_probes(probe, miner, seed=seed)
            evaluation.probes = probes
            for result in probes:
                observations.append(
                    Observation(
                        hotkey=miner.hotkey,
                        uid=miner.uid,
                        at=time.time(),
                        kind=result.kind,
                        ok=result.ok,
                        latency_ms=result.latency_ms,
                        first_byte_ms=result.first_byte_ms,
                        quality=result.quality,
                        wer=result.wer,
                        detail=result.detail,
                    )
                )

        capacity = await probe.capacity()
        if capacity:
            evaluation.capacity_units = capacity.capacity_units

        if observations:
            self.store.record_observations(observations)

        return evaluation

    async def _quality_probes(
        self, probe: MinerProbe, miner: MinerRecord, *, seed: int
    ) -> List[ProbeResult]:
        """The per-sweep quality and latency measurements.

        Two utterances per service per sweep, drawn from the rotating subset.
        Enough signal to move a rolling average without turning the validator
        into a meaningful share of the miner's load.
        """
        tasks = []
        if SERVICE_ASR in miner.services:
            for item in self.evalset.rotate_asr(seed, 2):
                tasks.append(probe.asr_batch(item))
            stream_items = self.evalset.rotate_asr(seed + 1, 1)
            if stream_items:
                tasks.append(probe.asr_stream(stream_items[0]))

        if SERVICE_TTS in miner.services:
            for item in self.evalset.rotate_tts(seed, 2):
                tasks.append(probe.tts_batch(item))
            stream_items = self.evalset.rotate_tts(seed + 1, 1)
            if stream_items:
                tasks.append(probe.tts_stream(stream_items[0]))

        if not tasks:
            return []

        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [
            result
            for result in results
            if isinstance(result, ProbeResult)
            # A miner that shed load is neither good nor bad; excluding the
            # sample keeps admission control from denting quality scores.
            and not result.payload.get("at_capacity")
        ]


def qualification_is_fresh(row, *, ttl_hours: float = QUALIFICATION_TTL_HOURS) -> bool:
    """Whether a stored qualification is recent enough to rely on."""
    if row is None:
        return False
    return (time.time() - float(row["evaluated_at"])) < ttl_hours * 3600.0
