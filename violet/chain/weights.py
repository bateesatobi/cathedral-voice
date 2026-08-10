"""
Turning scores into an on-chain weight vector.

Kept separate from :mod:`violet.chain.client` so the transformation can be unit
tested and simulated (``scripts/simulate_scoring.py``) without a chain
connection.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Tuple

logger = logging.getLogger("violet.chain.weights")

#: Scores below this fraction of the maximum are dropped to zero before
#: normalisation. Without it, the long tail of near-dead miners consumes a
#: meaningful share of emissions purely by existing.
RELATIVE_FLOOR = 0.01

#: Ceiling on any single miner's share of the weight vector. A subnet whose
#: emissions concentrate on one operator is one outage away from having no
#: capacity, so the mechanism caps concentration even when one miner genuinely
#: dominates on score. The surplus is redistributed across the remainder.
MAX_SINGLE_WEIGHT = 0.25


def normalize_scores(
    scores: Dict[int, float],
    *,
    relative_floor: float = RELATIVE_FLOOR,
    max_single_weight: float = MAX_SINGLE_WEIGHT,
) -> Tuple[List[int], List[float]]:
    """Convert ``{uid: score}`` into aligned ``(uids, weights)`` summing to 1.

    Returns empty lists when nothing scored above zero, which the caller must
    treat as "do not submit" rather than "submit zeros".
    """
    positive = {uid: float(score) for uid, score in scores.items() if score > 0}
    if not positive:
        return [], []

    peak = max(positive.values())
    cutoff = peak * relative_floor
    kept = {uid: score for uid, score in positive.items() if score >= cutoff}
    if not kept:
        kept = positive

    dropped = len(positive) - len(kept)
    if dropped:
        logger.debug("dropped %d miners below the relative floor", dropped)

    total = sum(kept.values())
    weights = {uid: score / total for uid, score in kept.items()}
    weights = _apply_cap(weights, max_single_weight)

    uids = sorted(weights)
    return uids, [round(weights[uid], 6) for uid in uids]


def _apply_cap(weights: Dict[int, float], cap: float) -> Dict[int, float]:
    """Clamp each weight to ``cap`` and redistribute the excess proportionally.

    Iterative, because redistributing can push another miner over the cap. With
    fewer uids than ``1/cap`` the cap is unreachable, so the loop exits and the
    weights are returned uncapped rather than looping forever.
    """
    if cap <= 0 or cap >= 1 or len(weights) * cap <= 1.0:
        return weights

    working = dict(weights)
    for _ in range(50):
        over = {uid: w for uid, w in working.items() if w > cap + 1e-9}
        if not over:
            break
        excess = sum(w - cap for w in over.values())
        under = {uid: w for uid, w in working.items() if w <= cap + 1e-9}
        under_total = sum(under.values())
        if under_total <= 0:
            break
        for uid in over:
            working[uid] = cap
        for uid, w in under.items():
            working[uid] = w + excess * (w / under_total)

    total = sum(working.values())
    if total <= 0:
        return weights
    return {uid: w / total for uid, w in working.items()}


def describe(uids: List[int], weights: List[float], top: int = 10) -> str:
    """Human-readable summary of a weight vector, for logs and the dashboard."""
    if not uids:
        return "no weights"
    pairs = sorted(zip(uids, weights), key=lambda pair: pair[1], reverse=True)
    head = ", ".join(f"uid{uid}={weight:.4f}" for uid, weight in pairs[:top])
    suffix = f" (+{len(pairs) - top} more)" if len(pairs) > top else ""
    return f"{len(pairs)} miners: {head}{suffix}"
