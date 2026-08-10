#!/usr/bin/env python3
"""
Run the qualification suite against a miner.

This is TDD 4.3 step 1 - "deploy the official Docker images and verify local
functionality" - made executable. Run it against your own endpoint before you
spend anything on registration, and you will know whether you would be admitted.

    python scripts/run_qualification.py http://localhost:8091
    python scripts/run_qualification.py https://miner.example.com --services tts
    python scripts/run_qualification.py http://localhost:8091 --full-availability
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp

from violet.constants import QUALIFY_AVAILABILITY_WINDOW_S, SERVICES
from violet.evalset import load_evalset
from violet.logging_utils import setup_logging
from violet.validator.probes import MinerProbe
from violet.validator.qualification import AvailabilitySample, run_qualification

logger = setup_logging("qualify")

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"


async def observe_availability(
    probe: MinerProbe, window_s: float, interval_s: float = 30.0
) -> list:
    """Poll /health for the full observation window.

    Off by default because it takes half an hour. It is the only way to actually
    satisfy the Sustained Availability test, though - a validator will not admit
    a miner it has not watched.
    """
    samples = []
    deadline = time.time() + window_s
    total = int(window_s / interval_s)

    print(f"\nObserving availability for {window_s / 60:.0f} minutes...")
    index = 0
    while time.time() < deadline:
        result = await probe.health()
        samples.append(AvailabilitySample(at=time.time(), ok=result.ok))
        index += 1
        failures = sum(1 for s in samples if not s.ok)
        marker = f"{GREEN}.{RESET}" if result.ok else f"{RED}x{RESET}"
        print(
            f"\r  [{index}/{total}] {marker} {failures} failure(s)  ",
            end="",
            flush=True,
        )
        await asyncio.sleep(min(interval_s, max(0.0, deadline - time.time())))
    print()
    return samples


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Violet qualification suite")
    parser.add_argument("endpoint", help="miner base URL, e.g. http://localhost:8091")
    parser.add_argument(
        "--services",
        default=",".join(SERVICES),
        help="comma-separated services to test (default: asr,tts)",
    )
    parser.add_argument("--evalset", default="", help="path to an evaluation manifest")
    parser.add_argument("--token", default="", help="bearer token, if the miner requires one")
    parser.add_argument(
        "--full-availability",
        action="store_true",
        help=f"run the real {QUALIFY_AVAILABILITY_WINDOW_S / 60:.0f}-minute availability observation",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    args = parser.parse_args()

    services = [s.strip() for s in args.services.split(",") if s.strip()]
    unknown = set(services) - set(SERVICES)
    if unknown:
        print(f"unknown service(s): {sorted(unknown)}", file=sys.stderr)
        return 2

    evalset = load_evalset(args.evalset or None)

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=10)
    ) as session:
        probe = MinerProbe(session, args.endpoint, access_token=args.token)

        if args.full_availability:
            availability = await observe_availability(probe, QUALIFY_AVAILABILITY_WINDOW_S)
            window = QUALIFY_AVAILABILITY_WINDOW_S
            skip_availability = False
        else:
            availability = []
            window = QUALIFY_AVAILABILITY_WINDOW_S
            skip_availability = True

        result = await run_qualification(
            probe,
            evalset,
            services=services,
            availability=availability,
            availability_window_s=window,
            seed=int(time.time()),
            skip_availability=skip_availability,
        )

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
        return 0 if result.passed else 1

    print(f"\nQualification: {args.endpoint}")
    print(f"Services tested: {', '.join(services)}")
    print(f"Evaluation set: {evalset.name}")
    print("-" * 72)

    for outcome in result.outcomes:
        if outcome.skipped:
            label, colour = "SKIP", YELLOW
        elif outcome.passed:
            label, colour = "PASS", GREEN
        else:
            label, colour = "FAIL", RED
        print(f"{colour}{label}{RESET}  {outcome.name:<24} {outcome.detail}")

    print("-" * 72)
    if result.passed:
        print(f"{GREEN}All tests passed.{RESET}")
        if skip_availability:
            print(
                f"{YELLOW}Note:{RESET} Sustained Availability was skipped. A "
                "validator observes it over 30-60 minutes before admitting you. "
                "Re-run with --full-availability to test it properly."
            )
        if evalset.synthetic_only:
            print(
                f"{YELLOW}Note:{RESET} the evaluation set has no real audio, so "
                "word error rate was not scored. Validators running a real "
                "corpus will measure it."
            )
        return 0

    print(f"{RED}Not qualified.{RESET} Fix the following before registering:")
    for outcome in result.failures:
        print(f"  - {outcome.name}: {outcome.detail}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
