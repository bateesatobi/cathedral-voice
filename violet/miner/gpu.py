"""
GPU and host inventory for the miner sidecar.

Reads ``nvidia-smi`` rather than a Python CUDA binding on purpose: the sidecar
runs alongside the ASR/TTS containers, not inside them, and should not need a
CUDA runtime or a torch install of its own.

TDD 9.3 is explicit that this is a *claim*, not an attestation - there is no
cryptographic proof of hardware in v1.4. The validator cross-checks the claim
against observed throughput (``violet.validator.antigaming``), and the roadmap
calls for TEE or challenge-response VRAM proofs in a later version.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import shutil
import time
from typing import Dict, List, Optional, Tuple

from ..constants import (
    MAX_ACCEPTABLE_FIRST_BYTE_MS,
    TARGET_CONCURRENT_ASR_STREAMS,
    TARGET_CONCURRENT_TTS_STREAMS,
    classify_gpu,
)
from ..protocol import CapacityReport, GpuInfo

logger = logging.getLogger("violet.miner.gpu")

_NVIDIA_SMI_QUERY = (
    "index,name,memory.total,memory.used,utilization.gpu"
)
_NVIDIA_SMI_TIMEOUT_S = 10.0

#: Per-GPU concurrency the network assumes for an unmodified official image on
#: a baseline A100 40 GB (multiplier 1.0). Higher tiers scale by multiplier.
#: These become the miner's default admission limits when the operator does not
#: set MINER_MAX_CONCURRENT_* explicitly.
BASE_ASR_STREAMS_PER_UNIT = 8
BASE_TTS_STREAMS_PER_UNIT = 10


async def _run_nvidia_smi() -> Optional[str]:
    binary = shutil.which("nvidia-smi")
    if not binary:
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            binary,
            f"--query-gpu={_NVIDIA_SMI_QUERY}",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=_NVIDIA_SMI_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        logger.warning("nvidia-smi timed out after %.0fs", _NVIDIA_SMI_TIMEOUT_S)
        return None
    except OSError as exc:
        logger.warning("nvidia-smi could not be executed: %s", exc)
        return None

    if process.returncode != 0:
        logger.warning(
            "nvidia-smi exited %s: %s",
            process.returncode,
            stderr.decode("utf-8", "replace").strip(),
        )
        return None
    return stdout.decode("utf-8", "replace")


def parse_nvidia_smi(output: str) -> Tuple[List[GpuInfo], List[str]]:
    """Parse ``nvidia-smi`` CSV into accepted GPUs plus a list of rejections.

    Rejected GPUs are returned rather than dropped so the miner can tell the
    operator *why* a card earns nothing, instead of silently reporting less
    capacity than the machine has.
    """
    accepted: List[GpuInfo] = []
    rejected: List[str] = []

    for line in (output or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parts = [part.strip() for part in next(csv.reader(io.StringIO(line)))]
        except csv.Error:
            parts = [part.strip() for part in line.split(",")]
        if len(parts) < 3:
            continue

        try:
            index = int(parts[0])
        except ValueError:
            continue
        name = parts[1]

        vram_gb = _to_float(parts[2])
        vram_gb = vram_gb / 1024.0 if vram_gb else 0.0  # nvidia-smi reports MiB
        used_gb = _to_float(parts[3]) if len(parts) > 3 else None
        used_gb = used_gb / 1024.0 if used_gb else None
        utilization = _to_float(parts[4]) if len(parts) > 4 else None

        tier = classify_gpu(name, vram_gb)
        if tier is None:
            rejected.append(f"{name} ({vram_gb:.0f} GB)")
            continue

        accepted.append(
            GpuInfo(
                index=index,
                product_name=name,
                vram_gb=round(vram_gb, 1),
                tier_key=tier.key,
                multiplier=tier.multiplier,
                utilization_pct=utilization,
                memory_used_gb=round(used_gb, 2) if used_gb is not None else None,
            )
        )

    return accepted, rejected


def _to_float(value: str) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _host_resources() -> Tuple[float, int]:
    """Total system memory in GB and CPU count, without a psutil dependency."""
    cpu_count = os.cpu_count() or 0
    memory_gb = 0.0
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        page_count = os.sysconf("SC_PHYS_PAGES")
        memory_gb = (page_size * page_count) / (1024 ** 3)
    except (ValueError, OSError, AttributeError):
        # Not available on every platform; capacity scoring does not depend on
        # it, it only feeds the operator-facing warning below.
        pass
    return round(memory_gb, 1), cpu_count


class GpuMonitor:
    """Caches the host's GPU inventory and answers capacity queries.

    Polling is throttled: ``nvidia-smi`` costs tens of milliseconds and the
    health endpoint is probed frequently by both validators and the router.
    """

    def __init__(self, poll_interval_s: float = 30.0):
        self.poll_interval_s = poll_interval_s
        self._gpus: List[GpuInfo] = []
        self._rejected: List[str] = []
        self._last_poll = 0.0
        self._started_at = time.time()
        self._lock = asyncio.Lock()
        self._memory_gb, self._cpu_count = _host_resources()
        self._warned_no_gpu = False

    @property
    def rejected_gpus(self) -> List[str]:
        return list(self._rejected)

    @property
    def uptime_s(self) -> float:
        return time.time() - self._started_at

    async def refresh(self, *, force: bool = False) -> List[GpuInfo]:
        now = time.time()
        if not force and (now - self._last_poll) < self.poll_interval_s and self._gpus:
            return self._gpus

        async with self._lock:
            # Another coroutine may have refreshed while we waited.
            if not force and (time.time() - self._last_poll) < self.poll_interval_s and self._gpus:
                return self._gpus

            output = await _run_nvidia_smi()
            self._last_poll = time.time()

            if output is None:
                if not self._warned_no_gpu:
                    logger.warning(
                        "nvidia-smi unavailable - reporting zero capacity. This "
                        "miner will pass health checks but earn no Capacity score."
                    )
                    self._warned_no_gpu = True
                self._gpus = []
                self._rejected = []
                return self._gpus

            self._gpus, self._rejected = parse_nvidia_smi(output)
            if self._rejected:
                logger.warning(
                    "%d GPU(s) are not on the accepted list and contribute no "
                    "capacity: %s",
                    len(self._rejected),
                    ", ".join(self._rejected),
                )
            return self._gpus

    def capacity_units(self) -> float:
        return round(sum(gpu.multiplier for gpu in self._gpus), 4)

    def gpu_counts(self) -> Dict[str, int]:
        """``{tier_key: count}`` as published in the on-chain announcement."""
        counts: Dict[str, int] = {}
        for gpu in self._gpus:
            counts[gpu.tier_key] = counts.get(gpu.tier_key, 0) + 1
        return counts

    def default_limits(self) -> Tuple[int, int]:
        """Suggested (asr, tts) concurrency limits for this hardware."""
        units = self.capacity_units()
        if units <= 0:
            return 0, 0
        return (
            max(1, int(units * BASE_ASR_STREAMS_PER_UNIT)),
            max(1, int(units * BASE_TTS_STREAMS_PER_UNIT)),
        )

    async def report(
        self,
        *,
        max_concurrent_asr: int,
        max_concurrent_tts: int,
        active_asr: int,
        active_tts: int,
    ) -> CapacityReport:
        await self.refresh()
        return CapacityReport(
            gpus=list(self._gpus),
            system_memory_gb=self._memory_gb,
            cpu_count=self._cpu_count,
            max_concurrent_asr=max_concurrent_asr,
            max_concurrent_tts=max_concurrent_tts,
            active_asr=active_asr,
            active_tts=active_tts,
            uptime_s=self.uptime_s,
        )

    def warnings(self) -> List[str]:
        """Operator-facing warnings surfaced on ``/violet/info``."""
        from ..constants import MIN_SYSTEM_MEMORY_GB

        issues: List[str] = []
        if not self._gpus:
            issues.append(
                "no accepted GPU detected; capacity score will be zero"
            )
        if self._memory_gb and self._memory_gb < MIN_SYSTEM_MEMORY_GB:
            issues.append(
                f"system memory {self._memory_gb:.0f} GB is below the "
                f"{MIN_SYSTEM_MEMORY_GB} GB minimum"
            )
        if self._rejected:
            issues.append(
                "unaccepted GPUs present: " + ", ".join(self._rejected)
            )
        units = self.capacity_units()
        if units and units * BASE_ASR_STREAMS_PER_UNIT < TARGET_CONCURRENT_ASR_STREAMS / 4:
            issues.append(
                "this host contributes a small fraction of the network's target "
                f"concurrency ({TARGET_CONCURRENT_ASR_STREAMS} ASR / "
                f"{TARGET_CONCURRENT_TTS_STREAMS} TTS streams)"
            )
        return issues


def latency_multiplier(first_byte_ms: float) -> float:
    """Shared latency shaping used by both the miner's self-report and scoring.

    Full credit at or below the 200 ms target, decaying linearly to the floor at
    ``MAX_ACCEPTABLE_FIRST_BYTE_MS``.
    """
    from ..constants import LATENCY_MULTIPLIER_FLOOR, TARGET_FIRST_BYTE_MS

    if first_byte_ms <= TARGET_FIRST_BYTE_MS:
        return 1.0
    if first_byte_ms >= MAX_ACCEPTABLE_FIRST_BYTE_MS:
        return LATENCY_MULTIPLIER_FLOOR
    span = MAX_ACCEPTABLE_FIRST_BYTE_MS - TARGET_FIRST_BYTE_MS
    decayed = (first_byte_ms - TARGET_FIRST_BYTE_MS) / span
    return 1.0 - decayed * (1.0 - LATENCY_MULTIPLIER_FLOOR)
