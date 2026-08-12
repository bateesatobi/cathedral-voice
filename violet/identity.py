"""
Cryptographic binding between a miner's endpoint and its on-chain hotkey.

Validators issue a random nonce; the miner signs ``{hotkey, nonce, issued_at}``
with its registered hotkey. A proxy forwarding another operator's traffic cannot
produce a valid signature without the private key.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Optional

PATH_IDENTITY_CHALLENGE = "/violet/identity/challenge"

#: Reject challenges older than this — limits replay of captured responses.
CHALLENGE_MAX_AGE_S = 300.0


def challenge_message(hotkey: str, nonce: str, issued_at: float) -> bytes:
    """Canonical bytes covered by the miner's signature."""
    body = {
        "hotkey": hotkey,
        "issued_at": round(issued_at, 3),
        "nonce": nonce,
    }
    return json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")


def new_nonce() -> str:
    return secrets.token_hex(16)


def verify_hotkey_signature(hotkey: str, message: bytes, signature_hex: str) -> bool:
    """Return whether ``signature_hex`` validates ``message`` for ``hotkey``."""
    if not hotkey or not signature_hex:
        return False
    sig_raw = signature_hex[2:] if signature_hex.startswith("0x") else signature_hex
    try:
        signature = bytes.fromhex(sig_raw)
    except ValueError:
        return False
    try:
        import bittensor as bt

        keypair = bt.Keypair(ss58_address=hotkey)
        return bool(keypair.verify(message, signature))
    except Exception:
        return False


def challenge_is_fresh(issued_at: float, *, now: Optional[float] = None) -> bool:
    now = time.time() if now is None else now
    return abs(now - issued_at) <= CHALLENGE_MAX_AGE_S
