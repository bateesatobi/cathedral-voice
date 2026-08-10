"""
Endpoint announcement and heartbeat (TDD 4.2, 4.3).

The miner republishes its announcement only when something material changes -
the endpoint, the declared services, or the GPU inventory. Commitments cost a
transaction, so a fixed-interval republish would burn TAO to say nothing new.
The periodic wake-up exists to notice change and to re-announce after a chain
reorg or a failed submission, not to spam the chain.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, Optional

from ..chain import ChainClient
from ..config import MinerConfig
from ..constants import SPEC_VERSION
from ..protocol import MinerAnnouncement
from .gpu import GpuMonitor

logger = logging.getLogger("violet.miner.announce")

#: Retry delay after a failed submission. Deliberately short relative to the
#: announce interval: an unannounced miner is invisible and earns nothing.
_RETRY_DELAY_S = 60.0


class Announcer:
    """Keeps the on-chain announcement in sync with the miner's actual state."""

    def __init__(
        self,
        config: MinerConfig,
        chain: ChainClient,
        gpu: GpuMonitor,
    ):
        self.config = config
        self.chain = chain
        self.gpu = gpu
        self._last_published: Optional[str] = None
        self._last_published_at = 0.0
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    def _fingerprint(self, announcement: MinerAnnouncement) -> str:
        """Identity of an announcement, ignoring its timestamp."""
        gpus = ",".join(f"{k}:{v}" for k, v in sorted(announcement.gpus.items()))
        services = ",".join(sorted(announcement.services))
        return "|".join(
            [
                announcement.endpoint,
                services,
                gpus,
                str(announcement.spec_version),
                announcement.asr_image,
                announcement.tts_image,
            ]
        )

    async def build(self) -> MinerAnnouncement:
        await self.gpu.refresh()
        return MinerAnnouncement(
            endpoint=self.config.public_endpoint.rstrip("/"),
            services=sorted(self.config.services),
            gpus=self.gpu.gpu_counts(),
            spec_version=SPEC_VERSION,
            announced_at=time.time(),
            asr_image=self.config.asr_image,
            tts_image=self.config.tts_image,
        )

    async def announce_once(self, *, force: bool = False) -> bool:
        """Publish if anything changed. Returns True when a write occurred."""
        announcement = await self.build()
        fingerprint = self._fingerprint(announcement)

        if not force and fingerprint == self._last_published:
            logger.debug("announcement unchanged; not republishing")
            return False

        if not announcement.gpus:
            logger.warning(
                "announcing with no accepted GPUs - this miner will pass health "
                "checks but earn no Capacity score until nvidia-smi reports an "
                "accepted card"
            )

        published = await self.chain.publish_announcement(announcement)
        if not published:
            return False

        self._last_published = fingerprint
        self._last_published_at = time.time()
        logger.info(
            "announced %s serving %s with %s (%.1f capacity units)",
            announcement.endpoint,
            "+".join(announcement.services),
            announcement.gpus or "no GPUs",
            announcement.capacity_units,
        )

        if self.config.serve_axon:
            await self.chain.serve_axon(announcement.endpoint)

        return True

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.announce_once()
                delay = self.config.announce_interval_s
            except Exception as exc:
                logger.error("announcement failed: %s", exc, exc_info=True)
                delay = _RETRY_DELAY_S
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                continue

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="violet-announce")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def status(self) -> Dict[str, object]:
        return {
            "last_published_at": self._last_published_at,
            "fingerprint": self._last_published,
            "running": bool(self._task and not self._task.done()),
        }
