"""
Turning chain state into a list of miners to evaluate.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..chain import ChainClient, is_compatible
from ..constants import SERVICES
from ..protocol import MinerAnnouncement

logger = logging.getLogger("violet.validator.discovery")

#: Announcements older than this are treated as abandoned. Long enough that a
#: stable miner never has to republish just to stay visible.
ANNOUNCEMENT_MAX_AGE_S = 30 * 24 * 3600


@dataclass
class MinerRecord:
    """A miner as the validator sees it: chain identity plus announcement."""

    uid: int
    hotkey: str
    coldkey: str
    endpoint: str
    services: List[str] = field(default_factory=list)
    announcement: Optional[MinerAnnouncement] = None
    validator_permit: bool = False
    stake: float = 0.0
    incentive: float = 0.0

    @property
    def announced_capacity(self) -> float:
        return self.announcement.capacity_units if self.announcement else 0.0


@dataclass
class Discovery:
    """The result of one discovery pass."""

    miners: List[MinerRecord] = field(default_factory=list)
    block: int = 0
    #: Registered neurons with no usable endpoint - reported so operators can
    #: see why they are not being evaluated.
    unannounced: List[str] = field(default_factory=list)
    skipped: Dict[str, str] = field(default_factory=dict)

    @property
    def coldkeys(self) -> Dict[str, str]:
        return {miner.hotkey: miner.coldkey for miner in self.miners}

    @property
    def endpoints(self) -> Dict[str, str]:
        return {miner.hotkey: miner.endpoint for miner in self.miners}


async def discover(chain: ChainClient, *, self_hotkey: str = "") -> Discovery:
    """Read the metagraph and build the evaluable miner set."""
    graph = await chain.metagraph(commitments=True)
    announcements = await chain.announcements(graph)
    result = Discovery(block=int(getattr(graph, "block", 0) or 0))

    now = time.time()
    for neuron in graph.neurons:
        hotkey = str(neuron.hotkey)

        if hotkey == self_hotkey:
            continue

        # Validators are not evaluated as miners. A neuron may hold both roles,
        # but scoring one's peers as miners would be double-counting.
        if getattr(neuron, "validator_permit", False):
            result.skipped[hotkey] = "holds a validator permit"
            continue

        announcement = announcements.get(hotkey)
        if announcement is None or not announcement.endpoint:
            result.unannounced.append(hotkey)
            continue

        if announcement.announced_at and (now - announcement.announced_at) > ANNOUNCEMENT_MAX_AGE_S:
            result.skipped[hotkey] = "announcement is stale"
            continue

        if announcement.services and not is_compatible(announcement):
            result.skipped[hotkey] = (
                f"spec version {announcement.spec_version} not supported"
            )
            continue

        # An axon-only announcement carries no service list. Assume both and let
        # the probes discover the truth from /health, rather than excluding a
        # miner for using the cheaper announcement path.
        services = announcement.services or list(SERVICES)

        result.miners.append(
            MinerRecord(
                uid=int(neuron.uid),
                hotkey=hotkey,
                coldkey=str(neuron.coldkey),
                endpoint=announcement.endpoint,
                services=services,
                announcement=announcement,
                validator_permit=bool(getattr(neuron, "validator_permit", False)),
                stake=float(getattr(neuron, "total_stake", 0.0) or 0.0),
                incentive=float(getattr(neuron, "incentive", 0.0) or 0.0),
            )
        )

    logger.info(
        "discovered %d evaluable miners at block %d (%d unannounced, %d skipped)",
        len(result.miners), result.block, len(result.unannounced), len(result.skipped),
    )
    if result.unannounced:
        logger.debug(
            "unannounced hotkeys (no commitment and no axon): %s",
            ", ".join(h[:8] + "..." for h in result.unannounced[:10]),
        )
    return result


def refresh_services_from_health(miner: MinerRecord, health_services: List[str]) -> None:
    """Reconcile announced services with what /health actually reports.

    The live report wins. An announcement is a claim made at publication time;
    the health endpoint is the miner speaking about itself right now, and a
    miner that announced ASR+TTS but is only serving TTS should be evaluated as
    TTS-only rather than failing an ASR test it never intended to take.
    """
    if not health_services:
        return
    filtered = [service for service in health_services if service in SERVICES]
    if filtered and set(filtered) != set(miner.services):
        logger.debug(
            "miner %s announced %s but reports %s; using the live report",
            miner.hotkey[:8], miner.services, filtered,
        )
        miner.services = filtered
