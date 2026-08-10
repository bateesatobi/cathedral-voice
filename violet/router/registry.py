"""
Live inventory of healthy miner endpoints (TDD 2, 8).

The registry is the router's view of the network: who exists (from the chain),
who is currently answering (from health probes), and how each has been
performing (from recent request outcomes). Discovery and health run on separate
cadences because chain state changes slowly and endpoint liveness changes fast.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import aiohttp

from ..chain import ChainClient
from ..config import RouterConfig
from ..constants import GPU_TIERS_BY_KEY, SERVICE_ASR, SERVICE_TTS, SERVICES
from ..protocol import PATH_HEALTH, GpuInfo, HealthReport

logger = logging.getLogger("violet.router.registry")

#: Weight of a new latency sample in the exponential moving average. Low so a
#: single slow request does not evict an otherwise fast miner from rotation.
LATENCY_EMA_ALPHA = 0.2


def _short_gpu_name(product_name: str, tier_key: str = "") -> str:
    """Human label for leaderboards — prefer H200 over full NVIDIA strings."""
    if tier_key and tier_key in GPU_TIERS_BY_KEY:
        return GPU_TIERS_BY_KEY[tier_key].display_name
    name = (product_name or "").strip()
    if not name and tier_key:
        return tier_key.upper().replace("_", " ")
    # Strip common vendor prefixes for a tighter table cell.
    for prefix in ("NVIDIA ", "nvidia ", "Tesla ", "GeForce "):
        if name.startswith(prefix):
            name = name[len(prefix) :]
    return name or "GPU"


def summarize_gpus(gpus: List[GpuInfo]) -> tuple[str, int, str, float, str]:
    """Return ``(model, count, tier_key, vram_gb, summary)`` from a capacity inventory."""
    if not gpus:
        return "", 0, "", 0.0, ""

    counts: Dict[str, int] = {}
    labels: Dict[str, str] = {}
    for gpu in gpus:
        key = gpu.tier_key or gpu.product_name or "unknown"
        counts[key] = counts.get(key, 0) + 1
        labels[key] = _short_gpu_name(gpu.product_name, gpu.tier_key)

    primary = max(gpus, key=lambda g: (g.multiplier, g.vram_gb))
    model = _short_gpu_name(primary.product_name, primary.tier_key)
    summary = " + ".join(
        f"{count}×{labels[key]}" for key, count in sorted(counts.items(), key=lambda kv: -kv[1])
    )
    return model, len(gpus), primary.tier_key or "", float(primary.vram_gb or 0.0), summary


def summarize_announced_gpus(gpus: Dict[str, int]) -> tuple[str, int, str, float, str]:
    """Same shape as :func:`summarize_gpus` for on-chain ``{tier_key: count}``."""
    if not gpus:
        return "", 0, "", 0.0, ""
    parts = []
    total = 0
    best_key = ""
    best_mult = -1.0
    for key, count in gpus.items():
        n = int(count or 0)
        if n <= 0:
            continue
        total += n
        tier = GPU_TIERS_BY_KEY.get(key)
        label = tier.display_name if tier else key.upper().replace("_", " ")
        parts.append((n, label, key, tier.multiplier if tier else 0.0, tier.vram_gb if tier else 0.0))
        if (tier.multiplier if tier else 0.0) > best_mult:
            best_mult = tier.multiplier if tier else 0.0
            best_key = key
    if not parts:
        return "", 0, "", 0.0, ""
    parts.sort(key=lambda p: (-p[3], -p[0]))
    primary = next(p for p in parts if p[2] == best_key) if best_key else parts[0]
    summary = " + ".join(f"{n}×{label}" for n, label, *_ in parts)
    return primary[1], total, primary[2], float(primary[4] or 0.0), summary


@dataclass
class MinerEndpoint:
    """One routable miner."""

    hotkey: str
    uid: Optional[int]
    endpoint: str
    services: List[str] = field(default_factory=list)
    incentive: float = 0.0
    capacity_units: float = 0.0

    # Live state
    healthy: bool = False
    consecutive_failures: int = 0
    last_health_at: float = 0.0
    load_factor: float = 0.0
    max_concurrent_asr: int = 0
    max_concurrent_tts: int = 0

    #: Hardware claimed on ``/health`` (and/or the on-chain announcement).
    gpu_model: str = ""
    gpu_count: int = 0
    gpu_tier: str = ""
    vram_gb: float = 0.0
    #: Compact inventory string, e.g. ``"2×H200 + 1×H100"``.
    gpu_summary: str = ""

    #: Requests this router currently has in flight against the miner. Tracked
    #: locally because the miner's own /capacity is only as fresh as the last
    #: health probe, and selection needs the count as of right now.
    inflight: int = 0

    #: EMA of observed first-byte latency, milliseconds.
    latency_ms: Optional[float] = None
    #: Rolling success ratio over recent requests.
    success_ema: float = 1.0

    def serves(self, service: str) -> bool:
        return service in self.services

    def observe_latency(self, latency_ms: float) -> None:
        if self.latency_ms is None:
            self.latency_ms = latency_ms
        else:
            self.latency_ms = (
                LATENCY_EMA_ALPHA * latency_ms + (1 - LATENCY_EMA_ALPHA) * self.latency_ms
            )

    def observe_outcome(self, ok: bool) -> None:
        self.success_ema = (
            LATENCY_EMA_ALPHA * (1.0 if ok else 0.0)
            + (1 - LATENCY_EMA_ALPHA) * self.success_ema
        )

    @property
    def capacity_headroom(self) -> float:
        """Fraction of declared concurrency still free, in ``[0, 1]``."""
        limit = self.max_concurrent_asr + self.max_concurrent_tts
        if limit <= 0:
            return 1.0 - min(1.0, self.load_factor)
        used = max(self.inflight, self.load_factor * limit)
        return max(0.0, 1.0 - used / limit)


class MinerRegistry:
    """Discovers miners from the chain and tracks their liveness."""

    def __init__(
        self,
        config: RouterConfig,
        chain: Optional[ChainClient] = None,
        session: Optional[aiohttp.ClientSession] = None,
    ):
        self.config = config
        self.chain = chain
        self._session = session
        self._owns_session = session is None
        self._miners: Dict[str, MinerEndpoint] = {}
        self._lock = asyncio.Lock()
        self._tasks: List[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._last_discovery_at = 0.0
        self._last_discovery_error = ""

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=5)
            )
            self._owns_session = True
        return self._session

    def snapshot(self) -> List[MinerEndpoint]:
        return list(self._miners.values())

    def healthy_for(self, service: str) -> List[MinerEndpoint]:
        return [
            miner
            for miner in self._miners.values()
            if miner.healthy and miner.serves(service)
            and miner.incentive >= self.config.min_incentive
        ]

    def get(self, hotkey: str) -> Optional[MinerEndpoint]:
        return self._miners.get(hotkey)

    # -- discovery ---------------------------------------------------------

    async def discover(self) -> int:
        """Refresh the miner set from the chain (and keep any static seeds).

        Existing live state is carried over: rediscovery must not reset a
        miner's latency history or mark a healthy miner unknown, or the router
        would briefly lose its ranking every discovery interval.
        """
        static = self._static_miners()
        if self.chain is None:
            if static and not self._miners:
                async with self._lock:
                    self._miners = dict(static)
                logger.info("router seeded %d static miner(s) (no chain)", len(static))
            return len(self._miners)

        try:
            graph = await self.chain.metagraph(commitments=True)
            announcements = await self.chain.announcements(graph)
        except Exception as exc:
            self._last_discovery_error = str(exc)
            logger.error("router discovery failed: %s", exc)
            return len(self._miners)

        neurons = {str(neuron.hotkey): neuron for neuron in graph.neurons}
        discovered: Dict[str, MinerEndpoint] = dict(static)

        for hotkey, announcement in announcements.items():
            neuron = neurons.get(hotkey)
            if neuron is None:
                continue
            if getattr(neuron, "validator_permit", False):
                continue

            existing = self._miners.get(hotkey) or discovered.get(hotkey)
            miner = existing or MinerEndpoint(
                hotkey=hotkey,
                uid=int(neuron.uid),
                endpoint=announcement.endpoint,
                services=announcement.services or list(SERVICES),
            )
            miner.uid = int(neuron.uid)
            miner.endpoint = announcement.endpoint
            miner.services = announcement.services or miner.services or list(SERVICES)
            miner.incentive = float(getattr(neuron, "incentive", 0.0) or 0.0)
            miner.capacity_units = announcement.capacity_units
            # Seed hardware from the commitment until /health fills live inventory.
            model, count, tier, vram, summary = summarize_announced_gpus(
                announcement.gpus or {}
            )
            if count and not miner.gpu_count:
                miner.gpu_model = model
                miner.gpu_count = count
                miner.gpu_tier = tier
                miner.vram_gb = vram
                miner.gpu_summary = summary
            discovered[hotkey] = miner

        async with self._lock:
            departed = set(self._miners) - set(discovered)
            if departed:
                logger.info("%d miner(s) left the metagraph", len(departed))
            self._miners = discovered

        self._last_discovery_at = time.time()
        self._last_discovery_error = ""
        logger.info("router discovered %d miners", len(discovered))
        return len(discovered)

    def _static_miners(self) -> Dict[str, MinerEndpoint]:
        """Local/offline miner seeds from ``VIOLET_STATIC_MINERS``."""
        raw = (self.config.static_miners or "").strip()
        if not raw:
            return {}
        out: Dict[str, MinerEndpoint] = {}
        for index, piece in enumerate(raw.split(",")):
            endpoint = piece.strip().rstrip("/")
            if not endpoint:
                continue
            hotkey = f"static-local-{index}"
            out[hotkey] = MinerEndpoint(
                hotkey=hotkey,
                uid=9000 + index,
                endpoint=endpoint,
                services=list(SERVICES),
                incentive=1.0,
                healthy=False,  # health sweep marks them live
                max_concurrent_asr=8,
                max_concurrent_tts=10,
            )
        return out

    # -- health ------------------------------------------------------------

    async def health_sweep(self) -> None:
        miners = self.snapshot()
        if not miners:
            return

        async def probe(miner: MinerEndpoint) -> None:
            try:
                async with self.session.get(
                    f"{miner.endpoint}{PATH_HEALTH}",
                    timeout=aiohttp.ClientTimeout(total=self.config.health_timeout_s),
                ) as response:
                    payload = await response.json()
                report = HealthReport.from_dict(payload)
                # "degraded" still routes: the working half of a two-service
                # miner is better than no miner.
                ok = report.status in {"ok", "degraded"}
                self._apply_health(miner, ok, report)
            except Exception:
                self._apply_health(miner, False, None)

        await asyncio.gather(*(probe(miner) for miner in miners))
        healthy = sum(1 for miner in self._miners.values() if miner.healthy)
        logger.debug("router health: %d/%d healthy", healthy, len(self._miners))

    def _apply_health(
        self, miner: MinerEndpoint, ok: bool, report: Optional[HealthReport]
    ) -> None:
        miner.last_health_at = time.time()
        if ok:
            if not miner.healthy and miner.consecutive_failures:
                logger.info("miner %s is healthy again", miner.hotkey[:10])
            miner.healthy = True
            miner.consecutive_failures = 0
            if report:
                # The live service list wins over the announcement: a miner may
                # have had one upstream go down since it announced.
                live_services = [
                    service
                    for service in report.services
                    if service in SERVICES
                    and report.upstreams.get(service, True)
                ]
                if live_services:
                    miner.services = live_services
                if report.capacity:
                    miner.load_factor = report.capacity.load_factor
                    miner.max_concurrent_asr = report.capacity.max_concurrent_asr
                    miner.max_concurrent_tts = report.capacity.max_concurrent_tts
                    miner.capacity_units = report.capacity.capacity_units
                    model, count, tier, vram, summary = summarize_gpus(
                        report.capacity.gpus or []
                    )
                    if count:
                        miner.gpu_model = model
                        miner.gpu_count = count
                        miner.gpu_tier = tier
                        miner.vram_gb = vram
                        miner.gpu_summary = summary
        else:
            miner.consecutive_failures += 1
            if (
                miner.healthy
                and miner.consecutive_failures >= self.config.unhealthy_threshold
            ):
                logger.warning(
                    "miner %s removed from rotation after %d failed health checks",
                    miner.hotkey[:10], miner.consecutive_failures,
                )
                miner.healthy = False
            elif not miner.healthy:
                miner.healthy = False

    def mark_failure(self, miner: MinerEndpoint) -> None:
        """Record a failed production request.

        Counts toward the same threshold as health probes so a miner failing
        real traffic drops out without waiting for the next health sweep.
        """
        miner.observe_outcome(False)
        miner.consecutive_failures += 1
        if miner.consecutive_failures >= self.config.unhealthy_threshold:
            if miner.healthy:
                logger.warning(
                    "miner %s removed from rotation after %d failed requests",
                    miner.hotkey[:10], miner.consecutive_failures,
                )
            miner.healthy = False

    def mark_success(self, miner: MinerEndpoint, latency_ms: Optional[float]) -> None:
        miner.observe_outcome(True)
        miner.consecutive_failures = 0
        if latency_ms is not None:
            miner.observe_latency(latency_ms)

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        await self.discover()
        await self.health_sweep()
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._discovery_loop(), name="violet-router-discovery"),
            asyncio.create_task(self._health_loop(), name="violet-router-health"),
        ]

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self._owns_session and self._session and not self._session.closed:
            await self._session.close()

    async def _discovery_loop(self) -> None:
        while not self._stop.is_set():
            await self._sleep(self.config.discovery_interval_s)
            if self._stop.is_set():
                break
            try:
                await self.discover()
            except Exception as exc:
                logger.error("discovery loop error: %s", exc)

    async def _health_loop(self) -> None:
        while not self._stop.is_set():
            await self._sleep(self.config.health_interval_s)
            if self._stop.is_set():
                break
            try:
                await self.health_sweep()
            except Exception as exc:
                logger.error("health loop error: %s", exc)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def status(self) -> Dict[str, object]:
        """Operational summary, surfaced through the Avoices admin console."""
        miners = self.snapshot()
        return {
            "total": len(miners),
            "healthy": sum(1 for miner in miners if miner.healthy),
            "asr": len(self.healthy_for(SERVICE_ASR)),
            "tts": len(self.healthy_for(SERVICE_TTS)),
            "capacity_units": round(
                sum(miner.capacity_units for miner in miners if miner.healthy), 2
            ),
            "last_discovery_at": self._last_discovery_at,
            "last_discovery_error": self._last_discovery_error,
            "miners": [
                {
                    "hotkey": miner.hotkey,
                    "uid": miner.uid,
                    "endpoint": miner.endpoint,
                    "services": miner.services,
                    "healthy": miner.healthy,
                    "load_factor": round(miner.load_factor, 3),
                    "inflight": miner.inflight,
                    "latency_ms": round(miner.latency_ms, 1) if miner.latency_ms else None,
                    "success_rate": round(miner.success_ema, 3),
                    "capacity_units": miner.capacity_units,
                    "incentive": round(miner.incentive, 6),
                    "gpu_model": miner.gpu_model,
                    "gpu_count": miner.gpu_count,
                    "gpu_tier": miner.gpu_tier,
                    "vram_gb": miner.vram_gb,
                    "gpu_summary": miner.gpu_summary,
                }
                for miner in sorted(miners, key=lambda m: m.hotkey)
            ],
        }
