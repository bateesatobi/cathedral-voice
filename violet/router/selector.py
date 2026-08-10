"""
Miner selection (TDD 8).

    "The smart router maintains sticky WebSocket sessions, applies least-loaded
     and latency-aware selection, and rejects overloaded endpoints."

Selection scores each candidate on three terms - headroom, latency and on-chain
quality - and picks probabilistically among the top few rather than always
taking the maximum. Deterministic best-pick has a failure mode worth avoiding:
every router instance and every request converges on the same miner until it
saturates, producing exactly the thundering herd that load balancing exists to
prevent.
"""

from __future__ import annotations

import logging
import random
from typing import Dict, List, Optional, Sequence

from ..config import RouterConfig
from ..constants import MAX_ACCEPTABLE_FIRST_BYTE_MS, TARGET_FIRST_BYTE_MS
from .registry import MinerEndpoint

logger = logging.getLogger("violet.router.selector")

#: Sample from this many top-ranked candidates.
TOP_K = 3


def latency_term(latency_ms: Optional[float]) -> float:
    """Map observed latency to ``[0, 1]``; 1 means at or under the target."""
    if latency_ms is None:
        # Unmeasured. Treated optimistically so a newly discovered miner gets
        # its first request and can be measured at all.
        return 0.8
    if latency_ms <= TARGET_FIRST_BYTE_MS:
        return 1.0
    if latency_ms >= MAX_ACCEPTABLE_FIRST_BYTE_MS:
        return 0.0
    span = MAX_ACCEPTABLE_FIRST_BYTE_MS - TARGET_FIRST_BYTE_MS
    return 1.0 - (latency_ms - TARGET_FIRST_BYTE_MS) / span


def score_candidate(
    miner: MinerEndpoint, config: RouterConfig, *, max_incentive: float = 0.0
) -> float:
    """Rank one candidate. Higher is better."""
    load = miner.capacity_headroom
    latency = latency_term(miner.latency_ms)
    # On-chain incentive is the network's own quality judgement, already
    # incorporating a week of validator measurement. Reusing it here means the
    # router does not need its own quality model.
    quality = (miner.incentive / max_incentive) if max_incentive > 0 else 0.5

    score = (
        config.weight_load * load
        + config.weight_latency * latency
        + config.weight_quality * quality
    )
    # Recent failures suppress a miner even before it crosses the health
    # threshold, so degradation is acted on immediately.
    return score * miner.success_ema


def select(
    candidates: Sequence[MinerEndpoint],
    config: RouterConfig,
    *,
    exclude: Optional[Sequence[str]] = None,
    rng: Optional[random.Random] = None,
) -> Optional[MinerEndpoint]:
    """Pick a miner, or ``None`` when nothing is available."""
    excluded = set(exclude or ())
    pool = [
        miner
        for miner in candidates
        if miner.hotkey not in excluded and miner.capacity_headroom > 0.0
    ]

    if not pool:
        # Everything is saturated. Fall back to ignoring headroom rather than
        # failing outright: a queued request beats a dropped one, and the miner
        # will return 503 if it truly cannot take it.
        pool = [miner for miner in candidates if miner.hotkey not in excluded]
    if not pool:
        return None

    max_incentive = max((miner.incentive for miner in pool), default=0.0)
    ranked = sorted(
        pool,
        key=lambda miner: score_candidate(miner, config, max_incentive=max_incentive),
        reverse=True,
    )

    top = ranked[:TOP_K]
    if len(top) == 1:
        return top[0]

    weights = [
        max(1e-6, score_candidate(miner, config, max_incentive=max_incentive))
        for miner in top
    ]
    chooser = rng or random
    return chooser.choices(top, weights=weights, k=1)[0]


class StickySessions:
    """Pins a streaming session to one miner for its lifetime (TDD 8).

    A WebSocket ASR stream carries decoder state on the miner; moving mid-stream
    would restart it and lose the partial transcript. So a session ID maps to
    one miner until it ends, and only a hard failure re-selects.
    """

    def __init__(self, ttl_s: float = 3600.0):
        self.ttl_s = ttl_s
        self._sessions: Dict[str, tuple] = {}

    def get(self, session_id: str) -> Optional[str]:
        import time

        entry = self._sessions.get(session_id)
        if not entry:
            return None
        hotkey, pinned_at = entry
        if time.time() - pinned_at > self.ttl_s:
            self._sessions.pop(session_id, None)
            return None
        return hotkey

    def pin(self, session_id: str, hotkey: str) -> None:
        import time

        self._sessions[session_id] = (hotkey, time.time())

    def release(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def prune(self) -> int:
        import time

        now = time.time()
        stale = [
            session_id
            for session_id, (_, pinned_at) in self._sessions.items()
            if now - pinned_at > self.ttl_s
        ]
        for session_id in stale:
            self._sessions.pop(session_id, None)
        return len(stale)

    def __len__(self) -> int:
        return len(self._sessions)


def explain(miner: MinerEndpoint, config: RouterConfig, max_incentive: float = 0.0) -> str:
    """Human-readable breakdown of why a miner ranked where it did."""
    return (
        f"{miner.hotkey[:10]} headroom={miner.capacity_headroom:.2f} "
        f"latency={miner.latency_ms or 0:.0f}ms "
        f"success={miner.success_ema:.2f} "
        f"score={score_candidate(miner, config, max_incentive=max_incentive):.3f}"
    )
