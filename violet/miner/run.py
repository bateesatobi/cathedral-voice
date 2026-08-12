"""
Miner entrypoint.

    python -m violet.miner.run

Brings up the serving sidecar and, when a wallet is configured, the on-chain
announcer. The two are separable on purpose: an operator can run the server
alone to complete local verification (TDD 4.3 step 1) before spending anything
on registration.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from typing import Callable, Optional

import uvicorn

from ..chain import ChainClient
from ..config import load_config
from ..logging_utils import setup_logging
from ..identity import challenge_message
from .announce import Announcer
from .server import MinerState, create_app

logger = setup_logging("miner")


async def _resolve_identity(chain: ChainClient) -> tuple[str, Optional[int]]:
    """Look up this hotkey's uid on the subnet.

    A miner that is not registered still serves - it simply earns nothing - so
    an absent uid is reported and tolerated rather than being fatal. That lets
    an operator run the full qualification suite against their own box before
    paying the registration cost.
    """
    hotkey = chain.hotkey_ss58
    try:
        graph = await chain.metagraph(commitments=False)
    except Exception as exc:
        logger.warning("could not read the metagraph (%s); continuing unregistered", exc)
        return hotkey, None

    neuron = graph.by_hotkey(hotkey) if hasattr(graph, "by_hotkey") else None
    if neuron is None:
        for candidate in graph.neurons:
            if str(candidate.hotkey) == hotkey:
                neuron = candidate
                break

    if neuron is None:
        logger.warning(
            "hotkey %s is not registered on netuid %s - serving anyway, but no "
            "incentives accrue until registration completes",
            hotkey,
            chain.config.netuid,
        )
        return hotkey, None

    logger.info("registered as uid %s (hotkey %s)", neuron.uid, hotkey)
    return hotkey, int(neuron.uid)


def _make_identity_signer(chain) -> Optional[Callable[[str, str, float], str]]:
    if chain is None or not chain.config.signing_enabled:
        return None

    def sign(hotkey: str, nonce: str, issued_at: float) -> str:
        message = challenge_message(hotkey, nonce, issued_at)
        return chain.wallet.hotkey.sign(message).hex()

    return sign


async def _serve(args: argparse.Namespace) -> int:
    config = load_config()
    miner_config = config.miner
    chain_config = config.chain

    try:
        miner_config.validate()
    except ValueError as exc:
        logger.error("invalid miner configuration: %s", exc)
        return 2

    chain: Optional[ChainClient] = None
    announcer: Optional[Announcer] = None
    hotkey, uid = "", None

    if not args.no_chain:
        try:
            chain_config.validate()
            chain = await ChainClient(chain_config).connect()
            hotkey, uid = await _resolve_identity(chain)
        except Exception as exc:
            logger.error(
                "chain setup failed: %s. Start with --no-chain to serve without "
                "announcing (useful for local verification).",
                exc,
            )
            if chain:
                await chain.close()
            return 2

    state = MinerState(
        miner_config,
        hotkey=hotkey,
        uid=uid,
        identity_signer=_make_identity_signer(chain),
    )
    app = create_app(state)

    if chain is not None and not args.no_announce:
        announcer = Announcer(miner_config, chain, state.gpu)

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=miner_config.host,
            port=miner_config.port,
            log_level="info",
            # The router keeps sticky WebSocket sessions open for the length of
            # a stream; a keep-alive timeout shorter than that would sever them.
            timeout_keep_alive=75,
            ws_ping_interval=20,
            ws_ping_timeout=20,
        )
    )

    async def run_announcer() -> None:
        if announcer is None:
            return
        # Announce after the server binds, so the endpoint we publish is
        # already answering when a validator probes it.
        while not server.started:
            await asyncio.sleep(0.2)
        await announcer.announce_once(force=True)
        announcer.start()

    announcer_task = asyncio.create_task(run_announcer(), name="violet-announce-boot")
    try:
        await server.serve()
    finally:
        announcer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await announcer_task
        if announcer:
            await announcer.stop()
        if chain:
            await chain.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="violet-miner", description="Run the Violet miner sidecar"
    )
    parser.add_argument(
        "--no-chain",
        action="store_true",
        help="serve without connecting to Bittensor (local verification only)",
    )
    parser.add_argument(
        "--no-announce",
        action="store_true",
        help="connect to the chain but do not publish an announcement",
    )
    args = parser.parse_args()

    try:
        return asyncio.run(_serve(args))
    except KeyboardInterrupt:
        logger.info("shutting down")
        return 0


if __name__ == "__main__":
    sys.exit(main())
