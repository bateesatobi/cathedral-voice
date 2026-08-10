"""CLI: post Violet scores to Cathedral publisher (cathedral-voice)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

from .external_scores import (
    CathedralScoreClient,
    build_violet_report,
    config_from_env,
    epoch_from_unix,
    scores_from_miner_scores,
)


def _parse_score_flag(raw: str) -> Dict[str, Any]:
    if "=" not in raw:
        raise argparse.ArgumentTypeError(f"expected hotkey=score, got {raw!r}")
    hotkey, _, score_s = raw.partition("=")
    return {"miner_hotkey": hotkey.strip(), "score": float(score_s)}


async def _fetch_dashboard(url: str) -> List[Dict[str, Any]]:
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            payload = await resp.json()
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("scores") or payload.get("miners") or payload.get("rows") or []
    else:
        rows = []
    return scores_from_miner_scores(rows)


async def _async_main(args: argparse.Namespace) -> int:
    cfg = config_from_env()
    if args.publisher:
        cfg.publisher_url = args.publisher.rstrip("/")
    if args.token:
        cfg.token = args.token
    if args.hmac_secret:
        cfg.hmac_secret = args.hmac_secret
    if args.netuid:
        cfg.netuid = args.netuid
    cfg.dry_run = bool(args.dry_run or cfg.dry_run)
    cfg.enabled = True

    scores: List[Dict[str, Any]] = []
    if args.from_dashboard:
        scores = await _fetch_dashboard(args.from_dashboard)
    for item in args.score or []:
        scores.append(item)
    if args.scores_json:
        data = json.loads(Path(args.scores_json).read_text())
        if isinstance(data, dict) and "scores" in data:
            scores.extend(data["scores"])
        elif isinstance(data, list):
            scores.extend(scores_from_miner_scores(data))

    if not scores and not args.allow_empty:
        print(
            "no scores provided; use --score, --from-dashboard, or --scores-json",
            file=sys.stderr,
        )
        return 2

    epoch = args.epoch or epoch_from_unix()
    report = build_violet_report(
        scores,
        epoch=epoch,
        netuid=cfg.netuid,
        complete=not args.incomplete,
        metadata={"scorer": "cathedral-voice", "tool": "cathedral-voice-scores"},
    )

    client = CathedralScoreClient(cfg)
    try:
        if args.print_only:
            print(json.dumps(report, indent=2))
            return 0
        result = await client.post_report(report)
        print(
            json.dumps(
                {
                    "result": {k: v for k, v in result.items() if k != "report"},
                    "report": report,
                },
                indent=2,
            )
        )
        return 0 if result.get("ok") else 1
    finally:
        await client.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Post violet_audio scores to Cathedral publisher "
            "(NOT to cathedral-validator directly)."
        )
    )
    p.add_argument("--publisher", default="", help="override CATHEDRAL_PUBLISHER_URL")
    p.add_argument("--token", default="", help="override CATHEDRAL_EXTERNAL_SCORES_TOKEN")
    p.add_argument("--hmac-secret", default="", help="optional body HMAC secret")
    p.add_argument("--netuid", type=int, default=0, help="default 39")
    p.add_argument("--epoch", type=int, default=0, help="monotonic epoch (default: unix time)")
    p.add_argument("--score", action="append", type=_parse_score_flag, help="hotkey=score")
    p.add_argument("--scores-json", default="", help="path to JSON list or {scores:[...]}")
    p.add_argument("--from-dashboard", default="", help="validator dashboard scores URL")
    p.add_argument("--incomplete", action="store_true", help="complete=false (NOT blended)")
    p.add_argument("--allow-empty", action="store_true", help="allow complete revoke-all")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--print-only", action="store_true")
    return p


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(_async_main(args)))


if __name__ == "__main__":
    main()
