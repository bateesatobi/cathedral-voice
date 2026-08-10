#!/usr/bin/env python3
"""
Local validator loop for PHOSAI / Polaris offline demos (no Bittensor chain).

Seeds one or more miner endpoints from VIOLET_STATIC_MINERS (or --endpoint),
runs health + qualification continuously, and serves the dashboard.

    VIOLET_STATIC_MINERS=http://127.0.0.1:8091 \
      python scripts/run_local_validator.py

    python scripts/run_local_validator.py --endpoint http://127.0.0.1:8091
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp
import uvicorn

from violet.config import load_config
from violet.constants import SERVICES
from violet.evalset import load_evalset
from violet.logging_utils import setup_logging
from violet.validator.dashboard import DashboardState, create_dashboard
from violet.validator.discovery import Discovery, MinerRecord
from violet.validator.evaluator import Evaluator
from violet.validator.store import ValidatorStore

logger = setup_logging("local-validator")


def _endpoints(args: argparse.Namespace) -> list[str]:
    if args.endpoint:
        return [args.endpoint.rstrip("/")]
    raw = os.getenv("VIOLET_STATIC_MINERS", "").strip()
    return [p.strip().rstrip("/") for p in raw.split(",") if p.strip()]


def _discovery(endpoints: list[str]) -> Discovery:
    miners = []
    for i, endpoint in enumerate(endpoints):
        miners.append(
            MinerRecord(
                uid=9000 + i,
                hotkey=f"static-local-{i}",
                coldkey="static-local-cold",
                endpoint=endpoint,
                services=list(SERVICES),
                incentive=1.0,
            )
        )
    return Discovery(miners=miners, block=0)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default="", help="single miner URL")
    parser.add_argument("--port", type=int, default=8092, help="dashboard port")
    parser.add_argument("--interval", type=float, default=60.0, help="eval interval seconds")
    args = parser.parse_args()

    endpoints = _endpoints(args)
    if not endpoints:
        logger.error("Set --endpoint or VIOLET_STATIC_MINERS")
        return 2

    config = load_config().validator
    store = ValidatorStore(config.db_path or "./data/local_validator.sqlite3")
    evalset = load_evalset(config.evalset_path or None)
    evaluator = Evaluator(store, evalset, concurrency=config.concurrency)
    dashboard = DashboardState()
    dashboard.phase = "local"
    dashboard.weights_description = "local offline (no chain weights)"
    dashboard.dry_run = True
    dashboard.evalset_name = evalset.name
    dashboard.evalset_synthetic = evalset.synthetic_only
    dashboard.miner_count = len(endpoints)

    app = create_dashboard(store, dashboard, evalset)
    server = uvicorn.Server(
        uvicorn.Config(app, host="0.0.0.0", port=args.port, log_level="info")
    )

    stop = asyncio.Event()

    async def evaluate_loop() -> None:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10)
        ) as session:
            while not stop.is_set():
                discovery = _discovery(endpoints)
                dashboard.miner_count = len(discovery.miners)
                try:
                    healthy = await evaluator.health_sweep(session, discovery)
                    dashboard.healthy_count = sum(1 for ok in healthy.values() if ok)
                    evaluations = await evaluator.evaluate(
                        session, discovery, seed=int(time.time() // 60)
                    )
                    dashboard.qualified_count = sum(1 for e in evaluations if e.qualified)
                    dashboard.qualification_detail = {
                        e.miner.hotkey: e.qualification.to_dict()
                        for e in evaluations
                        if e.qualification
                    }
                    dashboard.last_round_at = time.time()
                    for e in evaluations:
                        logger.info(
                            "miner %s qualified=%s %s",
                            e.miner.hotkey,
                            e.qualified,
                            (e.qualification.summary() if e.qualification else ""),
                        )
                except Exception as exc:
                    logger.error("local eval failed: %s", exc, exc_info=True)
                    dashboard.errors.append(str(exc))
                try:
                    await asyncio.wait_for(stop.wait(), timeout=args.interval)
                except asyncio.TimeoutError:
                    continue

    eval_task = asyncio.create_task(evaluate_loop(), name="local-eval")
    try:
        logger.info(
            "local validator dashboard on :%s | miners=%s",
            args.port,
            endpoints,
        )
        await server.serve()
    finally:
        stop.set()
        eval_task.cancel()
        await asyncio.gather(eval_task, return_exceptions=True)
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
