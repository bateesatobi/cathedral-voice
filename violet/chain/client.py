"""
Thin async wrapper over the Bittensor SDK (11.x lean SDK).

Everything that touches the chain goes through :class:`ChainClient`, for three
reasons: the SDK surface moves between major versions and this keeps the blast
radius to one file; miner, validator and router need the same handful of
operations; and read-only consumers (the router, dashboards) must be able to run
without a coldkey, which the wrapper enforces.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from ..config import ChainConfig
from ..constants import SPEC_VERSION
from ..protocol import MinerAnnouncement
from .commitment import decode_announcement, encode_announcement

logger = logging.getLogger("violet.chain")

#: Axon protocol identifier. Violet miners serve plain HTTP(S), not the axon
#: wire protocol, so the axon record is used purely as an address book entry.
AXON_PROTOCOL_HTTP = 4


class ChainError(RuntimeError):
    """Raised when a chain operation fails in a way the caller must handle."""


def _require_bittensor():
    """Import the SDK only when chain access is actually needed.

    Lets miners and local validators run ``--no-chain`` / static endpoints
    without installing ``bittensor``.
    """
    try:
        import bittensor as bt
        from bittensor._generated.calls import Commitments
    except ImportError as exc:  # pragma: no cover - optional extra
        raise ChainError(
            "bittensor is required for chain operations. "
            "Install with: pip install 'violet-subnet[chain]' "
            "or run with --no-chain / VIOLET_STATIC_MINERS for local demos."
        ) from exc
    return bt, Commitments


class ChainClient:
    """Async access to the Violet subnet's chain state and extrinsics."""

    def __init__(self, config: ChainConfig):
        self.config = config
        self._client: Optional[Any] = None
        self._wallet: Optional[Any] = None
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------

    async def connect(self) -> "ChainClient":
        if self._client is None:
            bt, _Commitments = _require_bittensor()
            # ``allow_raw_calls`` is needed for ``Commitments.set_commitment``,
            # which the lean SDK does not expose as a first-class intent.
            policy = bt.Policy(
                allowed_netuids=[self.config.netuid] if self.config.netuid else None,
                allow_raw_calls=True,
            )
            client = bt.Client(self.config.network, policy=policy)
            self._client = await client.connect()
            logger.info(
                "connected to %s (netuid %s)", self.config.network, self.config.netuid
            )
        return self

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def __aenter__(self) -> "ChainClient":
        return await self.connect()

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    @property
    def client(self) -> Any:
        if self._client is None:
            raise ChainError("ChainClient.connect() has not been awaited")
        return self._client

    @property
    def wallet(self) -> Any:
        """The signing wallet, loaded lazily.

        Read-only deployments set ``BT_SIGNING_ENABLED=false`` and never reach
        here, so a missing keyfile surfaces as a clear error at the point of
        signing rather than at import time.
        """
        if not self.config.signing_enabled:
            raise ChainError(
                "signing is disabled on this component (BT_SIGNING_ENABLED=false)"
            )
        if self._wallet is None:
            bt, _Commitments = _require_bittensor()
            kwargs = {
                "name": self.config.wallet_name,
                "hotkey": self.config.wallet_hotkey,
            }
            if self.config.wallet_path:
                kwargs["path"] = self.config.wallet_path
            self._wallet = bt.Wallet(**kwargs)
        return self._wallet

    @property
    def hotkey_ss58(self) -> str:
        return self.wallet.hotkey.ss58_address

    # -- reads -------------------------------------------------------------

    async def block(self) -> int:
        return await self.client.block()

    async def metagraph(self, *, commitments: bool = True) -> Any:
        """Fetch the full subnet state.

        Raises rather than returning ``None`` so callers do not silently score
        an empty network and submit uniform weights.
        """
        bt, _Commitments = _require_bittensor()
        graph = await bt.metagraph.fetch(
            self.client, self.config.netuid, commitments=commitments
        )
        if graph is None:
            raise ChainError(
                f"subnet {self.config.netuid} not found on {self.config.network}"
            )
        return graph

    async def announcements(
        self, graph: Optional[Any] = None
    ) -> Dict[str, MinerAnnouncement]:
        """Decode every valid Violet announcement on the subnet, keyed by hotkey.

        Announcements come from two places, in priority order:

        1. an on-chain commitment, which carries the full payload including a
           DNS hostname and TLS-capable URL;
        2. the axon record, which only carries an IP and port, but is cheaper to
           publish and is what most operators will start with.

        A miner that has done neither is undiscoverable and simply does not
        appear here.
        """
        graph = graph or await self.metagraph(commitments=True)
        out: Dict[str, MinerAnnouncement] = {}

        uid_to_hotkey = {
            int(neuron.uid): str(neuron.hotkey) for neuron in graph.neurons
        }
        hotkey_set = set(uid_to_hotkey.values())

        for key, commitment in (graph.commitments or {}).items():
            data = getattr(commitment, "data", "") or ""
            announcement = decode_announcement(data)
            if not announcement:
                continue
            key_str = str(key)
            if key_str in hotkey_set:
                hotkey = key_str
            else:
                try:
                    hotkey = uid_to_hotkey.get(int(key))
                except (TypeError, ValueError):
                    hotkey = None
            if not hotkey:
                logger.debug("skipping commitment with unknown key %r", key)
                continue
            out[hotkey] = announcement

        for neuron in graph.neurons:
            hotkey = str(neuron.hotkey)
            if hotkey in out:
                continue
            endpoint = _axon_endpoint(neuron)
            if not endpoint:
                continue
            # GPU inventory is unknown from the axon record alone; the validator
            # fills it in from the miner's live /capacity response, and an empty
            # inventory scores zero capacity until it does.
            out[hotkey] = MinerAnnouncement(
                endpoint=endpoint,
                services=[],
                gpus={},
                spec_version=SPEC_VERSION,
            )

        return out

    # -- writes ------------------------------------------------------------

    async def publish_announcement(self, announcement: MinerAnnouncement) -> bool:
        """Publish the miner's endpoint payload as an on-chain commitment."""
        _bt, Commitments = _require_bittensor()
        encoded = encode_announcement(announcement).encode("utf-8")
        info = {"fields": [{f"Raw{len(encoded)}": encoded}]}
        call = Commitments.set_commitment(netuid=self.config.netuid, info=info)

        async with self._lock:
            result = await self.client.submit_call(call, self.wallet, signer="hotkey")

        if not result.success:
            logger.error("commitment publish failed: %s", result.message or result.error)
            return False
        logger.info(
            "published announcement (%d bytes) at %s", len(encoded), result.extrinsic_id
        )
        return True

    async def serve_axon(self, endpoint: str) -> bool:
        """Publish the miner's address via the axon record.

        Only useful when the endpoint resolves to a literal IPv4/IPv6 address;
        the axon record has no room for a hostname. Hostname-based operators
        rely on the commitment instead, so a failure here is logged and
        tolerated rather than fatal.
        """
        parsed = urlparse(endpoint)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            ipaddress.ip_address(host)
        except ValueError:
            logger.info(
                "endpoint %s is a hostname, not an IP - skipping axon serve, "
                "the on-chain commitment carries the address instead",
                endpoint,
            )
            return False

        bt, _Commitments = _require_bittensor()
        async with self._lock:
            result = await self.client.execute(
                bt.ServeAxon(
                    netuid=self.config.netuid,
                    ip=host,
                    port=port,
                    protocol=AXON_PROTOCOL_HTTP,
                    version=SPEC_VERSION,
                ),
                self.wallet,
            )
        if not result.success:
            logger.warning("serve_axon failed: %s", result.message or result.error)
            return False
        logger.info("axon record published: %s:%s", host, port)
        return True

    async def set_weights(
        self, uids: List[int], weights: List[float], *, version_key: int = SPEC_VERSION
    ) -> bool:
        """Submit weights for the scored miner set.

        The SDK normalises and quantises; this only guards against the two
        mistakes that produce a silently useless submission - mismatched
        lengths, and an all-zero vector.
        """
        if len(uids) != len(weights):
            raise ChainError(
                f"uid/weight length mismatch: {len(uids)} uids, {len(weights)} weights"
            )
        if not uids:
            logger.warning("no uids to weight; skipping submission")
            return False
        if sum(weights) <= 0:
            logger.warning(
                "every miner scored zero; skipping submission rather than "
                "publishing an all-zero weight vector"
            )
            return False

        bt, _Commitments = _require_bittensor()
        async with self._lock:
            result = await self.client.execute(
                bt.SetWeights(
                    netuid=self.config.netuid,
                    uids=list(uids),
                    weights=list(weights),
                    version_key=version_key,
                ),
                self.wallet,
            )
        if not result.success:
            logger.error("set_weights failed: %s", result.message or result.error)
            return False
        logger.info("weights submitted for %d miners (%s)", len(uids), result.extrinsic_id)
        return True


def _axon_endpoint(neuron) -> Optional[str]:
    """Build an HTTP base URL from a neuron's axon record, if it has one."""
    axon = getattr(neuron, "axon", None)
    if not axon:
        return None

    ip = None
    port = None
    if isinstance(axon, str):
        # Bittensor 11.x metagraph exposes axon as "ip:port".
        if ":" in axon:
            host_part, _, port_part = axon.rpartition(":")
            ip = host_part.strip()
            try:
                port = int(port_part.strip())
            except ValueError:
                return None
    else:
        ip = getattr(axon, "ip", None) or (
            axon.get("ip") if isinstance(axon, dict) else None
        )
        port = getattr(axon, "port", None) or (
            axon.get("port") if isinstance(axon, dict) else None
        )
    if not ip or not port:
        return None

    ip = str(ip)
    try:
        parsed_ip = ipaddress.ip_address(ip)
    except ValueError:
        return None
    # 0.0.0.0 is what an unserved axon looks like on chain.
    if parsed_ip.is_unspecified:
        return None

    host = f"[{ip}]" if parsed_ip.version == 6 else ip
    scheme = "https" if int(port) == 443 else "http"
    return f"{scheme}://{host}:{int(port)}"
