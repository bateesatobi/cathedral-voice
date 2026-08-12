"""
Miner access tokens: challenge format shared between ASRAPI and the fetch script.

ASRAPI derives tokens as HMAC(master_key, hotkey|netuid|version) so the backend
never stores plaintext tokens — only version + revocation state in Postgres.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Optional

TOKEN_PURPOSE = "miner_access_token"


def token_challenge_message(
    hotkey: str,
    nonce: str,
    network: str,
    netuid: int,
    *,
    issued_at: Optional[float] = None,
) -> bytes:
    """Canonical bytes the miner signs to obtain an access token."""
    body = {
        "hotkey": hotkey,
        "issued_at": round(issued_at if issued_at is not None else time.time(), 3),
        "netuid": int(netuid),
        "network": network.strip().lower(),
        "nonce": nonce,
        "purpose": TOKEN_PURPOSE,
    }
    return json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")


def derive_access_token(hotkey: str, netuid: int, version: int, master_key: str) -> str:
    """Deterministic bearer token for one hotkey/version pair."""
    if not master_key:
        raise ValueError("VIOLET_MINER_TOKEN_MASTER_KEY is not configured")
    digest = hmac.new(
        master_key.encode("utf-8"),
        f"violet-miner-token:v1:{hotkey}:{netuid}:{version}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"vm_{digest}"


def resolve_network(netuid: int) -> str:
    from ..constants import NETUID_MAINNET, NETUID_TESTNET

    if netuid == NETUID_TESTNET:
        return "test"
    if netuid == NETUID_MAINNET:
        return "finney"
    return "custom"
