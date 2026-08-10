#!/usr/bin/env python3
"""
Publish a miner's endpoint announcement on chain (TDD 4.3 step 3).

The miner does this automatically at startup. Use this script to re-announce
after changing an endpoint, to inspect what is currently published, or to check
an encoding before paying for the transaction.

    python scripts/announce_endpoint.py --dry-run
    python scripts/announce_endpoint.py
    python scripts/announce_endpoint.py --show
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from violet.chain import ChainClient, decode_announcement, encode_announcement
from violet.config import load_config
from violet.logging_utils import setup_logging
from violet.miner.gpu import GpuMonitor
from violet.protocol import MinerAnnouncement

logger = setup_logging("announce")


async def show(chain: ChainClient) -> int:
    """Print every Violet announcement currently on the subnet."""
    announcements = await chain.announcements()
    if not announcements:
        print("no announcements found on this subnet")
        return 0

    print(f"{len(announcements)} announcement(s):\n")
    for hotkey, announcement in sorted(announcements.items()):
        age_h = (time.time() - announcement.announced_at) / 3600 if announcement.announced_at else 0
        print(f"  {hotkey}")
        print(f"    endpoint : {announcement.endpoint}")
        print(f"    services : {', '.join(announcement.services) or '(from /health)'}")
        print(
            f"    gpus     : {announcement.gpus or '(unknown)'} "
            f"= {announcement.capacity_units:.1f} capacity units"
        )
        print(f"    age      : {age_h:.1f}h  spec v{announcement.spec_version}\n")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description="Announce a Violet miner endpoint")
    parser.add_argument("--dry-run", action="store_true", help="encode and print, do not submit")
    parser.add_argument("--show", action="store_true", help="list current announcements and exit")
    parser.add_argument("--endpoint", default="", help="override MINER_PUBLIC_ENDPOINT")
    args = parser.parse_args()

    config = load_config()
    config.chain.validate()

    if args.endpoint:
        config.miner.public_endpoint = args.endpoint

    gpu = GpuMonitor()
    await gpu.refresh(force=True)

    announcement = MinerAnnouncement(
        endpoint=config.miner.public_endpoint.rstrip("/"),
        services=sorted(config.miner.services),
        gpus=gpu.gpu_counts(),
        announced_at=time.time(),
        asr_image=config.miner.asr_image,
        tts_image=config.miner.tts_image,
    )

    encoded = encode_announcement(announcement)
    print("Announcement")
    print(f"  endpoint       : {announcement.endpoint}")
    print(f"  services       : {', '.join(announcement.services)}")
    print(f"  gpus           : {announcement.gpus or 'none detected'}")
    print(f"  capacity units : {announcement.capacity_units:.1f}")
    print(f"  encoded        : {len(encoded)} bytes")
    print(f"  payload        : {encoded}")

    for warning in gpu.warnings():
        print(f"  warning        : {warning}")

    # Round-trip before spending anything: an announcement that does not decode
    # is an announcement no validator can read.
    decoded = decode_announcement(encoded)
    if decoded is None or decoded.endpoint != announcement.endpoint:
        print("\nERROR: the encoded announcement does not decode correctly", file=sys.stderr)
        return 2

    if args.dry_run:
        print("\ndry run: nothing submitted")
        return 0

    async with ChainClient(config.chain) as chain:
        if args.show:
            return await show(chain)

        print(f"\nsubmitting as {chain.hotkey_ss58} on netuid {config.chain.netuid}...")
        if not await chain.publish_announcement(announcement):
            print("submission failed; see the log above", file=sys.stderr)
            return 1
        print("announcement published")

        if config.miner.serve_axon:
            await chain.serve_axon(announcement.endpoint)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
