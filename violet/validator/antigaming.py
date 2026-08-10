"""
Anti-gaming controls (TDD 9.1, 9.2).

The multi-UID policy is the sharpest rule in the document and is implemented
literally: only the highest-scoring hotkey under a given coldkey keeps a
non-zero score in any window; every sibling is zeroed. Repeat offenders are
temporarily excluded, then permanently blacklisted.

A note on what this does and does not do. Collapsing per coldkey removes the
economic advantage of splitting one operator's capacity across many UIDs. It
does nothing against an adversary who funds several unrelated coldkeys, which
TDD 9.2 acknowledges as residual risk. What raises the cost there is the long
window and continuous evaluation, not this module - so this module does not
pretend otherwise, and the dashboard reports coldkey concentration so the
behaviour is at least visible.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from ..constants import (
    MULTI_UID_EXCLUSION_HOURS,
    MULTI_UID_STRIKE_BLACKLIST,
    MULTI_UID_STRIKE_EXCLUSION,
)
from .scoring import MinerScore
from .store import ValidatorStore

logger = logging.getLogger("violet.validator.antigaming")


@dataclass
class CollapseReport:
    """What the multi-UID rule did in one window."""

    #: hotkeys zeroed, mapped to the coldkey they share.
    zeroed: Dict[str, str] = field(default_factory=dict)
    #: coldkey -> retained hotkey.
    retained: Dict[str, str] = field(default_factory=dict)
    #: coldkeys excluded entirely this window.
    excluded: List[str] = field(default_factory=list)
    blacklisted: List[str] = field(default_factory=list)

    @property
    def offending_coldkeys(self) -> List[str]:
        return sorted(set(self.zeroed.values()))

    def summary(self) -> str:
        if not self.zeroed and not self.excluded:
            return "no multi-UID activity"
        parts = []
        if self.zeroed:
            parts.append(
                f"{len(self.zeroed)} hotkey(s) zeroed across "
                f"{len(self.offending_coldkeys)} coldkey(s)"
            )
        if self.excluded:
            parts.append(f"{len(self.excluded)} coldkey(s) excluded")
        if self.blacklisted:
            parts.append(f"{len(self.blacklisted)} coldkey(s) blacklisted")
        return "; ".join(parts)


def apply_multi_uid_policy(
    scores: Sequence[MinerScore],
    coldkeys: Dict[str, str],
    store: Optional[ValidatorStore] = None,
    *,
    now: Optional[float] = None,
) -> tuple[List[MinerScore], CollapseReport]:
    """Collapse each coldkey's scores onto its best hotkey.

    ``coldkeys`` maps hotkey -> coldkey, from the metagraph.

    Returns the adjusted scores and a report. Scores are mutated in place on
    copies, so the caller's originals are untouched and the pre-collapse values
    remain available for the dashboard.
    """
    now = now or time.time()
    report = CollapseReport()

    by_coldkey: Dict[str, List[MinerScore]] = {}
    for score in scores:
        coldkey = coldkeys.get(score.hotkey)
        if not coldkey:
            # No coldkey known (deregistered mid-window); nothing to collapse.
            continue
        by_coldkey.setdefault(coldkey, []).append(score)

    adjusted = {score.hotkey: score for score in scores}

    for coldkey, siblings in by_coldkey.items():
        state = store.coldkey_state(coldkey) if store else {
            "strikes": 0, "excluded_until": 0.0, "blacklisted": False,
        }

        if state.get("blacklisted"):
            for sibling in siblings:
                sibling.final = 0.0
                sibling.notes.append("coldkey permanently blacklisted")
            report.blacklisted.append(coldkey)
            continue

        if float(state.get("excluded_until") or 0.0) > now:
            remaining_h = (float(state["excluded_until"]) - now) / 3600.0
            for sibling in siblings:
                sibling.final = 0.0
                sibling.notes.append(
                    f"coldkey temporarily excluded for {remaining_h:.1f}h more"
                )
            report.excluded.append(coldkey)

            # Still running multiple UIDs while serving an exclusion is what
            # "persistent" means (TDD 9.1). Without this, exclusion would
            # short-circuit striking and the blacklist would be unreachable.
            if len(siblings) > 1 and store:
                strikes = store.add_coldkey_strike(
                    coldkey,
                    excluded_until=float(state["excluded_until"]),
                    detail=f"{len(siblings)} hotkeys while excluded",
                )
                if strikes >= MULTI_UID_STRIKE_BLACKLIST:
                    store.add_coldkey_strike(
                        coldkey,
                        blacklist=True,
                        detail=f"blacklisted after {strikes} multi-UID windows",
                    )
                    report.blacklisted.append(coldkey)
                    logger.warning(
                        "coldkey %s blacklisted: continued multi-UID registration "
                        "through %d windows, including while excluded",
                        coldkey, strikes,
                    )
            continue

        if len(siblings) <= 1:
            # Single UID under this coldkey: the normal, compliant case. Clear
            # any historical strikes so one past mistake is not permanent.
            if store and int(state.get("strikes") or 0) > 0:
                store.clear_coldkey_strikes(coldkey)
            if siblings:
                report.retained[coldkey] = siblings[0].hotkey
            continue

        # Multi-UID: keep the best, zero the rest.
        ranked = sorted(siblings, key=lambda s: s.final, reverse=True)
        winner, losers = ranked[0], ranked[1:]
        report.retained[coldkey] = winner.hotkey
        winner.notes.append(
            f"highest-scoring of {len(siblings)} hotkeys under this coldkey"
        )

        for loser in losers:
            loser.final = 0.0
            loser.notes.append(
                f"zeroed: coldkey {coldkey[:8]}... also operates "
                f"{winner.hotkey[:8]}..., which scored higher (TDD 9.1)"
            )
            report.zeroed[loser.hotkey] = coldkey

        if store:
            strikes = store.add_coldkey_strike(
                coldkey,
                detail=f"{len(siblings)} hotkeys observed in one window",
            )
            if strikes >= MULTI_UID_STRIKE_BLACKLIST:
                store.add_coldkey_strike(
                    coldkey,
                    blacklist=True,
                    detail=f"blacklisted after {strikes} multi-UID windows",
                )
                for sibling in siblings:
                    sibling.final = 0.0
                    sibling.notes.append("coldkey blacklisted this window")
                report.blacklisted.append(coldkey)
                logger.warning(
                    "coldkey %s blacklisted after %d multi-UID windows", coldkey, strikes
                )
            elif strikes >= MULTI_UID_STRIKE_EXCLUSION:
                until = now + MULTI_UID_EXCLUSION_HOURS * 3600.0
                store.add_coldkey_strike(
                    coldkey,
                    excluded_until=until,
                    detail=f"excluded after {strikes} multi-UID windows",
                )
                winner.final = 0.0
                winner.notes.append(
                    f"coldkey excluded for {MULTI_UID_EXCLUSION_HOURS}h after "
                    f"{strikes} multi-UID windows"
                )
                report.excluded.append(coldkey)
                logger.warning(
                    "coldkey %s excluded for %dh after %d multi-UID windows",
                    coldkey, MULTI_UID_EXCLUSION_HOURS, strikes,
                )

    if report.zeroed or report.excluded:
        logger.info("multi-UID policy: %s", report.summary())

    return list(adjusted.values()), report


def endpoint_collisions(endpoints: Dict[str, str]) -> Dict[str, List[str]]:
    """Group hotkeys that announce the same endpoint.

    A shared endpoint across hotkeys with *different* coldkeys is the shape a
    coldkey-splitting Sybil takes: the capacity is announced many times but is
    physically one box. The coldkey rule alone would not catch it, so it is
    surfaced here for the caller to penalise and for the dashboard to display.
    """
    grouped: Dict[str, List[str]] = {}
    for hotkey, endpoint in endpoints.items():
        if not endpoint:
            continue
        grouped.setdefault(_normalize_endpoint(endpoint), []).append(hotkey)
    return {
        endpoint: sorted(hotkeys)
        for endpoint, hotkeys in grouped.items()
        if len(hotkeys) > 1
    }


def _normalize_endpoint(endpoint: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(endpoint.rstrip("/"))
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{host}:{port}"


def apply_endpoint_collision_penalty(
    scores: Sequence[MinerScore],
    endpoints: Dict[str, str],
    coldkeys: Dict[str, str],
) -> Dict[str, List[str]]:
    """Split credit among hotkeys sharing one physical endpoint.

    Not zeroed, because a legitimate case exists: a hosting provider serving
    several independent operators from one address. Dividing the score means one
    box earns one box's worth however many UIDs point at it, which removes the
    incentive to fragment without punishing the legitimate case outright.
    """
    collisions = endpoint_collisions(endpoints)
    if not collisions:
        return {}

    by_hotkey = {score.hotkey: score for score in scores}
    for endpoint, hotkeys in collisions.items():
        distinct_coldkeys = {coldkeys.get(hotkey) for hotkey in hotkeys} - {None}
        share = 1.0 / len(hotkeys)
        for hotkey in hotkeys:
            score = by_hotkey.get(hotkey)
            if score is None:
                continue
            score.final *= share
            score.notes.append(
                f"shares endpoint {endpoint} with {len(hotkeys) - 1} other "
                f"hotkey(s) across {len(distinct_coldkeys)} coldkey(s): "
                f"score divided by {len(hotkeys)}"
            )
        logger.warning(
            "endpoint %s announced by %d hotkeys across %d coldkeys",
            endpoint, len(hotkeys), len(distinct_coldkeys),
        )
    return collisions
