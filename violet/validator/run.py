"""
Validator entrypoint.

    python -m violet.validator.run

Runs three loops concurrently:

* a fast health loop, feeding the availability history and the online-capacity
  series;
* a slower full evaluation loop, running qualification and quality probes;
* a weight loop, scoring the window and submitting weights roughly every 150
  blocks (TDD 5, 7).

They are separate because their natural cadences differ by two orders of
magnitude, and because a slow evaluation sweep must never delay a weight
submission - a validator that misses submissions loses influence (TDD 5).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import time
from typing import Dict, List, Optional

import aiohttp
import uvicorn

from ..chain import ChainClient, describe, normalize_scores
from ..config import load_config
from ..constants import SCORE_WINDOW_DAYS
from ..evalset import load_evalset
from ..logging_utils import setup_logging
from .antigaming import apply_endpoint_collision_penalty, apply_multi_uid_policy
from .dashboard import DashboardState, create_dashboard
from .discovery import Discovery, discover
from .evaluator import Evaluator, MinerEvaluation, qualification_is_fresh
from .scoring import compute_components, describe_weights, score_miners, suggest_phase
from .store import ValidatorStore
from .work import WorkReportClient, to_store_rows

logger = setup_logging("validator")

#: Nominal seconds per block, used to translate the 150-block cadence into a
#: sleep when the chain is unreachable.
BLOCK_TIME_S = 12.0


class Validator:
    def __init__(self, args: argparse.Namespace):
        config = load_config()
        self.chain_config = config.chain
        self.config = config.validator
        self.args = args

        self.weights = self.config.resolved_weights()
        self.evalset = load_evalset(self.config.evalset_path or None)
        self.store = ValidatorStore(self.config.db_path)
        self.evaluator = Evaluator(
            self.store,
            self.evalset,
            concurrency=self.config.concurrency,
            access_token=config.router.access_token,
            require_identity=self.config.require_endpoint_identity,
            release_manifest_path=self.config.release_manifest_path,
        )
        self.dashboard = DashboardState()
        self.dashboard.phase = self.weights.name
        self.dashboard.weights_description = describe_weights(self.weights)
        self.dashboard.dry_run = self.config.dry_run or args.dry_run
        self.dashboard.evalset_name = self.evalset.name
        self.dashboard.evalset_synthetic = self.evalset.synthetic_only

        self.chain: Optional[ChainClient] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self._discovery = Discovery()
        self._evaluations: Dict[str, MinerEvaluation] = {}
        self._stop = asyncio.Event()
        self._cathedral = None
        self._thin = None
        if self.config.cathedral_scores_enabled:
            from ..cathedral import CathedralScoreClient, CathedralScoreClientConfig

            self._cathedral = CathedralScoreClient(
                CathedralScoreClientConfig(
                    enabled=True,
                    publisher_url=self.config.cathedral_publisher_url,
                    token=self.config.cathedral_scores_token,
                    hmac_secret=self.config.cathedral_scores_hmac,
                    netuid=self.config.cathedral_scores_netuid,
                    dry_run=self.config.cathedral_scores_dry_run or self.dashboard.dry_run,
                )
            )
        if self.config.cathedral_thin_enabled:
            from ..cathedral.thin_relay import config_from_env as thin_config_from_env

            self._thin = thin_config_from_env()
            self._thin.enabled = True
            self._thin.broadcast = self.config.cathedral_thin_broadcast
            # Broadcast implies not dry unless explicitly forced dry.
            if self.config.cathedral_thin_broadcast and not self.config.cathedral_thin_dry_run:
                self._thin.dry_run = False
            else:
                self._thin.dry_run = True
            self._thin.interval_s = self.config.cathedral_thin_interval_s
            self._thin.publisher_url = self.config.cathedral_publisher_url
            # Sole SN39 writer: never let the voice path overwrite the signed blend.
            if self._thin.broadcast and not self._thin.dry_run:
                self.config.cathedral_skip_local_weights = True

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self.chain_config.validate()
        self.chain = await ChainClient(self.chain_config).connect()
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10)
        )
        logger.info(
            "validator started | netuid %s | %s | window %d days",
            self.chain_config.netuid,
            describe_weights(self.weights),
            self.config.window_days,
        )
        if self.dashboard.dry_run:
            logger.warning("DRY RUN: scores are computed but no weights are submitted")
        if self._cathedral:
            logger.info(
                "Cathedral voice scores ENABLED → %s (netuid %s)",
                self.config.cathedral_publisher_url.rstrip("/")
                + "/v1/external-scores/violet",
                self.config.cathedral_scores_netuid,
            )
        if self._thin:
            logger.info(
                "Cathedral thin SN39 relay ENABLED → %s (broadcast=%s dry_run=%s)",
                self._thin.feed_url,
                self._thin.broadcast,
                self._thin.dry_run,
            )
            if self.config.cathedral_skip_local_weights:
                logger.warning(
                    "sole SN39 writer mode: local Violet set_weights DISABLED "
                    "(thin signed feed owns the chain vector)"
                )

    async def stop(self) -> None:
        self._stop.set()
        if self._cathedral:
            await self._cathedral.close()
        if self.session:
            await self.session.close()
        if self.chain:
            await self.chain.close()
        self.store.close()

    @property
    def self_hotkey(self) -> str:
        try:
            return self.chain.hotkey_ss58 if self.chain else ""
        except Exception:
            return ""

    # -- loops -------------------------------------------------------------

    async def discovery_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._discovery = await discover(self.chain, self_hotkey=self.self_hotkey)
                self.dashboard.block = self._discovery.block
                self.dashboard.miner_count = len(self._discovery.miners)
                self.dashboard.unannounced = self._discovery.unannounced
            except Exception as exc:
                logger.error("discovery failed: %s", exc)
                self.dashboard.errors.append(f"discovery: {exc}")
            await self._sleep(self.config.eval_interval_s)

    async def health_loop(self) -> None:
        while not self._stop.is_set():
            if self._discovery.miners:
                try:
                    healthy = await self.evaluator.health_sweep(
                        self.session, self._discovery
                    )
                    self.dashboard.healthy_count = sum(1 for ok in healthy.values() if ok)
                except Exception as exc:
                    logger.error("health sweep failed: %s", exc)
                    self.dashboard.errors.append(f"health: {exc}")
            await self._sleep(self.config.health_interval_s)

    async def evaluation_loop(self) -> None:
        while not self._stop.is_set():
            if self._discovery.miners:
                try:
                    # Seeding the rotation on block height means every validator
                    # in a round draws the same utterances, so honest validators
                    # do not diverge from consensus through sampling noise.
                    seed = self._discovery.block or int(time.time() // 3600)
                    evaluations = await self.evaluator.evaluate(
                        self.session, self._discovery, seed=seed
                    )
                    self._evaluations = {e.miner.hotkey: e for e in evaluations}
                    self.dashboard.qualified_count = sum(
                        1 for e in evaluations if e.qualified
                    )
                    self.dashboard.qualification_detail = {
                        e.miner.hotkey: e.qualification.to_dict()
                        for e in evaluations
                        if e.qualification
                    }
                    self.dashboard.last_round_at = time.time()
                except Exception as exc:
                    logger.error("evaluation sweep failed: %s", exc, exc_info=True)
                    self.dashboard.errors.append(f"evaluation: {exc}")
            await self._sleep(self.config.eval_interval_s)

    async def work_loop(self) -> None:
        """Pull signed organic work counters from the Avoices backend."""
        if not self.config.work_report_url:
            logger.info(
                "no VIOLET_WORK_REPORT_URL configured: the Work component will "
                "score zero for every miner, and emissions will be decided by "
                "Capacity and Quality alone"
            )
            return

        client = WorkReportClient(
            self.session,
            self.config.work_report_url,
            token=self.config.work_report_token,
            secret=self.config.work_report_hmac_secret,
        )
        window_start = time.time() - self.config.window_days * 86400
        cursor_end, cursor_report_id = self.store.get_work_cursor()
        since = max(window_start, cursor_end) if cursor_end > 0 else window_start

        while not self._stop.is_set():
            try:
                cursor_end, cursor_report_id = self.store.get_work_cursor()
                since = max(since, cursor_end) if cursor_end > 0 else since
                report = await client.fetch(
                    since,
                    last_period_end=cursor_end,
                    last_report_id=cursor_report_id,
                )
                if report:
                    ingested = 0
                    for row in to_store_rows(report):
                        if self.store.record_work(**row):
                            ingested += 1
                    logger.info("recorded %d new work rows", ingested)
                    # Advance durable cursor only after successful ingest attempt.
                    self.store.set_work_cursor(report.generated_at, report.report_id)
                    since = max(since, report.generated_at)
            except Exception as exc:
                logger.error("work ingestion failed: %s", exc)
                self.dashboard.errors.append(f"work: {exc}")
            await self._sleep(max(60.0, self.config.eval_interval_s))

    async def weight_loop(self) -> None:
        interval_s = self.config.weight_interval_blocks * BLOCK_TIME_S
        # Wait for one evaluation sweep before the first submission, so weights
        # are never published from an empty history.
        await self._sleep(min(interval_s, self.config.eval_interval_s + 30))

        while not self._stop.is_set():
            try:
                await self.score_and_submit()
            except Exception as exc:
                logger.error("weight round failed: %s", exc, exc_info=True)
                self.dashboard.errors.append(f"weights: {exc}")
            await self._sleep(interval_s)

    async def thin_loop(self) -> None:
        """Cathedral thin SN39 path — isolated from voice probing failures."""
        if not self._thin:
            return
        from ..cathedral.thin_relay import ThinRelayError, run_thin_tick

        # Stagger so the first voice eval can post scores before the first broadcast.
        await self._sleep(min(60.0, self._thin.interval_s / 10.0))
        while not self._stop.is_set():
            try:
                result = await run_thin_tick(
                    self._thin, chain=self.chain, session=self.session
                )
                logger.info("thin SN39 tick: %s", {k: result.get(k) for k in (
                    "ok", "mode", "vector_id", "n_mapped", "broadcast", "dry_run", "error"
                ) if k in result or result.get(k) is not None})
            except ThinRelayError as exc:
                logger.error("thin SN39 tick failed (voice path continues): %s", exc)
                self.dashboard.errors.append(f"thin: {exc}")
            except Exception as exc:
                logger.error(
                    "thin SN39 tick crashed (voice path continues): %s",
                    exc,
                    exc_info=True,
                )
                self.dashboard.errors.append(f"thin: {exc}")
            await self._sleep(self._thin.interval_s)

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    # -- scoring -----------------------------------------------------------

    async def score_and_submit(self) -> List:
        """Score the rolling window, apply anti-gaming, submit weights."""
        miners = self._discovery.miners
        if not miners:
            logger.warning("no miners discovered; nothing to score")
            return []

        since = time.time() - self.config.window_days * 86400
        components = []
        total_requests = 0

        for miner in miners:
            stats = self.store.window_stats(miner.hotkey, since)
            total_requests += stats.requests

            evaluation = self._evaluations.get(miner.hotkey)
            row = self.store.qualification(miner.hotkey)
            qualified = (
                bool(row and row["passed"])
                and qualification_is_fresh(row)
            ) if row else False
            resource_multiplier = (
                evaluation.resource_multiplier if evaluation else 1.0
            )

            components.append(
                compute_components(
                    stats,
                    uid=miner.uid,
                    qualified=qualified,
                    resource_penalty=resource_multiplier,
                    window_days=self.config.window_days,
                )
            )

        previous = {
            component.hotkey: self.store.previous_score(component.hotkey) or 0.0
            for component in components
        }
        scores = score_miners(components, self.weights, previous_scores=previous)

        # Anti-gaming, in order: collapse coldkey siblings, then divide credit
        # among hotkeys that share one physical endpoint.
        scores, collapse = apply_multi_uid_policy(
            scores, self._discovery.coldkeys, self.store
        )
        collisions = apply_endpoint_collision_penalty(
            scores, self._discovery.endpoints, self._discovery.coldkeys
        )

        self.dashboard.multi_uid_summary = collapse.summary()
        self.dashboard.endpoint_collisions = collisions

        now = time.time()
        for score in scores:
            self.store.record_score(
                score.hotkey,
                uid=score.uid,
                capacity=score.capacity,
                work=score.work,
                quality=score.quality,
                raw=score.raw,
                smoothed=score.smoothed,
                final=score.final,
                notes="; ".join(score.notes),
                at=now,
            )

        recommendation = suggest_phase(
            total_requests, self.config.window_days, self.weights.name
        )
        self.dashboard.phase_recommendation = recommendation
        if recommendation:
            logger.warning(
                "phase transition warranted: %d requests over %d days supports "
                "the '%s' weighting (currently '%s'). This is a governance "
                "decision; set VIOLET_PHASE to apply it.",
                total_requests, self.config.window_days, recommendation, self.weights.name,
            )

        uids, weights = normalize_scores({s.uid: s.final for s in scores if s.uid is not None})
        logger.info("scored round: %s", describe(uids, weights))

        if self._cathedral is not None:
            try:
                result = await self._cathedral.publish_miner_scores(
                    scores,
                    metadata={
                        "phase": self.weights.name,
                        "window_days": self.config.window_days,
                        "violet_netuid": self.chain_config.netuid,
                        "scorer": "cathedral-voice",
                        "gpu_attested": False,
                        "gpu_memory_confidential": False,
                        "execution_class": "hybrid_gpu_preview",
                    },
                )
                if not result.get("ok"):
                    logger.error(
                        "cathedral score publish failed: %s",
                        result.get("error") or result.get("body"),
                    )
            except Exception as exc:
                logger.error("cathedral score publish error: %s", exc, exc_info=True)

        if self.dashboard.dry_run:
            logger.info("dry run: not submitting weights")
            return scores

        if self.config.cathedral_skip_local_weights:
            logger.info(
                "CATHEDRAL_SKIP_LOCAL_WEIGHTS: skipping Violet set_weights "
                "(Cathedral publisher owns SN39 vector)"
            )
        elif uids and await self.chain.set_weights(uids, weights):
            self.dashboard.last_weights_at = time.time()
            self.dashboard.last_weights_block = await self.chain.block()

        # Retention is one window plus a margin, so history is never pruned out
        # from under an in-flight score.
        self.store.prune(time.time() - (self.config.window_days + 2) * 86400)
        return scores

    # -- orchestration -----------------------------------------------------

    async def run(self) -> None:
        await self.start()

        tasks = [
            asyncio.create_task(self.discovery_loop(), name="discovery"),
            asyncio.create_task(self.health_loop(), name="health"),
            asyncio.create_task(self.evaluation_loop(), name="evaluation"),
            asyncio.create_task(self.work_loop(), name="work"),
            asyncio.create_task(self.weight_loop(), name="weights"),
        ]
        if self._thin:
            tasks.append(asyncio.create_task(self.thin_loop(), name="cathedral-thin"))

        if self.config.dashboard_enabled:
            app = create_dashboard(self.store, self.dashboard, self.evalset)
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host=self.config.dashboard_host,
                    port=self.config.dashboard_port,
                    log_level="warning",
                )
            )
            tasks.append(asyncio.create_task(server.serve(), name="dashboard"))
            logger.info(
                "dashboard on http://%s:%d/api/overview",
                self.config.dashboard_host, self.config.dashboard_port,
            )

        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.stop()


async def _run_once(args: argparse.Namespace) -> int:
    """Single sweep, for operators verifying a validator before running it live."""
    validator = Validator(args)
    await validator.start()
    try:
        validator._discovery = await discover(
            validator.chain, self_hotkey=validator.self_hotkey
        )
        if not validator._discovery.miners:
            logger.warning("no miners discovered")
            return 0
        await validator.evaluator.health_sweep(validator.session, validator._discovery)
        seed = validator._discovery.block
        evaluations = await validator.evaluator.evaluate(
            validator.session, validator._discovery, seed=seed
        )
        validator._evaluations = {e.miner.hotkey: e for e in evaluations}
        scores = await validator.score_and_submit()
        for score in sorted(scores, key=lambda s: s.final, reverse=True)[:20]:
            logger.info(
                "uid %-4s %s C=%.3f W=%.3f Q=%.3f -> %.4f %s",
                score.uid, score.hotkey[:10], score.capacity, score.work,
                score.quality, score.final,
                f"({'; '.join(score.notes)})" if score.notes else "",
            )
        return 0
    finally:
        await validator.stop()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="violet-validator", description="Run the Violet validator"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="score but never submit weights"
    )
    parser.add_argument(
        "--once", action="store_true", help="run a single sweep and exit"
    )
    args = parser.parse_args()

    try:
        if args.once:
            return asyncio.run(_run_once(args))
        return asyncio.run(Validator(args).run())
    except KeyboardInterrupt:
        logger.info("shutting down")
        return 0
    except ValueError as exc:
        # Configuration problems are the common case here and deserve a
        # readable message rather than a traceback.
        logger.error("configuration error: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
