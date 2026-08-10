"""
Public scoring dashboard.

TDD 9.3's mitigation roadmap calls for "public dashboards that increase
transparency of scoring and ranking", and TDD 7 relies on transparency to keep
the capacity-heavy launch phase honest. This serves the validator's own view of
the network as JSON: every score with its three components, the reason for any
penalty, the multi-UID actions taken, and the phase-transition recommendation.

Read-only and unauthenticated on purpose - a dashboard that only the operator
can see does not provide the accountability the document is asking for.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..constants import SCORE_WINDOW_DAYS
from ..evalset import EvalSet, language_coverage
from .scoring import describe_weights
from .store import ValidatorStore

logger = logging.getLogger("violet.validator.dashboard")


class DashboardState:
    """Snapshot of the last completed round, published by the run loop."""

    def __init__(self) -> None:
        self.block: int = 0
        self.last_round_at: float = 0.0
        self.last_weights_at: float = 0.0
        self.last_weights_block: int = 0
        self.phase: str = ""
        self.weights_description: str = ""
        self.phase_recommendation: Optional[str] = None
        self.miner_count: int = 0
        self.qualified_count: int = 0
        self.healthy_count: int = 0
        self.multi_uid_summary: str = ""
        self.endpoint_collisions: Dict[str, List[str]] = {}
        self.unannounced: List[str] = []
        self.dry_run: bool = False
        self.evalset_name: str = ""
        self.evalset_synthetic: bool = True
        self.qualification_detail: Dict[str, Dict[str, Any]] = {}
        self.errors: List[str] = []


def create_dashboard(
    store: ValidatorStore, state: DashboardState, evalset: EvalSet
) -> FastAPI:
    app = FastAPI(
        title="Violet Validator Dashboard",
        description="Public scoring and ranking transparency for the Violet subnet.",
    )
    # Any origin: the point is that anyone can audit it, including from the
    # Avoices admin console on a different host.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health() -> JSONResponse:
        stale = state.last_round_at and (time.time() - state.last_round_at) > 3600
        return JSONResponse(
            {
                "status": "stale" if stale else "ok",
                "last_round_at": state.last_round_at,
                "last_weights_at": state.last_weights_at,
                "dry_run": state.dry_run,
            }
        )

    @app.get("/api/overview")
    async def overview() -> JSONResponse:
        return JSONResponse(
            {
                "block": state.block,
                "phase": state.phase,
                "weights": state.weights_description,
                "phase_recommendation": state.phase_recommendation,
                "window_days": SCORE_WINDOW_DAYS,
                "miners": state.miner_count,
                "qualified": state.qualified_count,
                "healthy": state.healthy_count,
                "last_round_at": state.last_round_at,
                "last_weights_at": state.last_weights_at,
                "last_weights_block": state.last_weights_block,
                "multi_uid": state.multi_uid_summary,
                "endpoint_collisions": state.endpoint_collisions,
                "unannounced_hotkeys": len(state.unannounced),
                "dry_run": state.dry_run,
                "evalset": {
                    "name": state.evalset_name,
                    "synthetic_only": state.evalset_synthetic,
                    "asr_items": len(evalset.asr),
                    "tts_items": len(evalset.tts),
                    "languages": language_coverage(evalset),
                },
                "errors": state.errors[-10:],
            }
        )

    @app.get("/api/scores")
    async def scores(limit: int = 256) -> JSONResponse:
        rows = store.latest_scores(limit=limit)
        return JSONResponse(
            {
                "block": state.block,
                "generated_at": state.last_round_at,
                "scores": [
                    {
                        "hotkey": row["hotkey"],
                        "uid": row["uid"],
                        "capacity": round(row["capacity"], 6),
                        "work": round(row["work"], 6),
                        "quality": round(row["quality"], 6),
                        "raw": round(row["raw"], 6),
                        "smoothed": round(row["smoothed"], 6),
                        "final": round(row["final"], 6),
                        "notes": row["notes"],
                        "at": row["at"],
                    }
                    for row in rows
                ],
            }
        )

    @app.get("/api/miner/{hotkey}")
    async def miner(hotkey: str, days: float = SCORE_WINDOW_DAYS) -> JSONResponse:
        since = time.time() - days * 86400
        stats = store.window_stats(hotkey, since)
        qualification = store.qualification(hotkey)
        history = store.score_history(hotkey, since)

        return JSONResponse(
            {
                "hotkey": hotkey,
                "window_days": days,
                "stats": {
                    "samples": stats.samples,
                    "success_rate": round(stats.success_rate, 4),
                    "availability": round(stats.availability, 4),
                    "mean_quality": stats.mean_quality,
                    "mean_wer": stats.mean_wer,
                    "mean_first_byte_ms": stats.mean_first_byte_ms,
                    "p95_first_byte_ms": stats.p95_first_byte_ms,
                    "mean_online_capacity": round(stats.mean_online_capacity, 4),
                    "requests": stats.requests,
                    "work_seconds": round(stats.work_seconds, 1),
                },
                "qualification": (
                    {
                        "passed": bool(qualification["passed"]),
                        "evaluated_at": qualification["evaluated_at"],
                        "detail": qualification["detail"],
                        "endpoint": qualification["endpoint"],
                    }
                    if qualification
                    else None
                ),
                "tests": state.qualification_detail.get(hotkey, {}),
                "history": [
                    {
                        "at": row["at"],
                        "capacity": round(row["capacity"], 6),
                        "work": round(row["work"], 6),
                        "quality": round(row["quality"], 6),
                        "final": round(row["final"], 6),
                    }
                    for row in history
                ],
            }
        )

    @app.get("/api/enforcement")
    async def enforcement() -> JSONResponse:
        """Every anti-gaming action taken, so penalties are contestable."""
        rows = store.all_coldkey_states()
        now = time.time()
        return JSONResponse(
            {
                "coldkeys": [
                    {
                        "coldkey": row["coldkey"],
                        "strikes": row["strikes"],
                        "last_strike": row["last_strike"],
                        "excluded": bool((row["excluded_until"] or 0) > now),
                        "excluded_until": row["excluded_until"],
                        "blacklisted": bool(row["blacklisted"]),
                        "detail": row["detail"],
                    }
                    for row in rows
                ],
                "endpoint_collisions": state.endpoint_collisions,
                "multi_uid_summary": state.multi_uid_summary,
            }
        )

    return app
