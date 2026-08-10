"""
Encoding of miner announcements into on-chain commitments.

Why commitments
---------------
TDD 4.3 step 3 requires miners to announce their public endpoint, supported
services and GPU inventory. Doing that through a central registry would put a
trusted third party between miners and validators, and would need to be run,
secured and paid for. An on-chain commitment is signed by the hotkey by
construction, is readable by anyone (validators *and* the smart router read the
same bytes), and needs no extra infrastructure.

The cost is space. Commitment payloads are small and every byte is paid for, so
the wire form uses single-character keys and a compact GPU encoding rather than
verbose JSON.

Wire format
-----------
``violet1|<base64url of compact JSON>``

The compact JSON is::

    {"e": "https://miner.example.com", "s": "at", "g": "h100_80:4,a100_80:2",
     "v": 1, "t": 1754700000, "ia": "sha256:...", "it": "sha256:..."}

``s`` uses one letter per service (``a``=asr, ``t``=tts) so the common
both-services case costs two bytes.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any, Dict, Optional

from ..constants import COMMITMENT_MAGIC, GPU_TIERS_BY_KEY, SERVICE_ASR, SERVICE_TTS, SPEC_VERSION
from ..protocol import MinerAnnouncement

#: Maximum encoded length we will attempt to publish. The chain's own limit is
#: larger, but staying small keeps the deposit and fee predictable.
MAX_COMMITMENT_BYTES = 512

_SERVICE_TO_CHAR = {SERVICE_ASR: "a", SERVICE_TTS: "t"}
_CHAR_TO_SERVICE = {char: service for service, char in _SERVICE_TO_CHAR.items()}


class CommitmentError(ValueError):
    """Raised when an announcement cannot be encoded or decoded."""


def _encode_gpus(gpus: Dict[str, int]) -> str:
    parts = []
    for tier_key in sorted(gpus):
        count = int(gpus[tier_key])
        if count <= 0:
            continue
        if tier_key not in GPU_TIERS_BY_KEY:
            raise CommitmentError(f"unknown GPU tier {tier_key!r}")
        parts.append(f"{tier_key}:{count}")
    return ",".join(parts)


def _decode_gpus(raw: str) -> Dict[str, int]:
    gpus: Dict[str, int] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        tier_key, _, count = part.partition(":")
        tier_key = tier_key.strip()
        if tier_key not in GPU_TIERS_BY_KEY:
            # An unknown tier is not fatal: it may come from a newer spec
            # version. Ignore it rather than discarding the whole announcement.
            continue
        try:
            parsed = int(count)
        except ValueError:
            continue
        if parsed > 0:
            gpus[tier_key] = parsed
    return gpus


def encode_announcement(announcement: MinerAnnouncement) -> str:
    """Serialise an announcement into its on-chain string form."""
    if not announcement.endpoint:
        raise CommitmentError("announcement has no endpoint")
    if not announcement.services:
        raise CommitmentError("announcement lists no services")

    services = "".join(
        _SERVICE_TO_CHAR[service]
        for service in sorted(announcement.services)
        if service in _SERVICE_TO_CHAR
    )
    if not services:
        raise CommitmentError("announcement lists no recognised services")

    payload: Dict[str, Any] = {
        "e": announcement.endpoint.rstrip("/"),
        "s": services,
        "g": _encode_gpus(announcement.gpus),
        "v": int(announcement.spec_version),
        "t": int(announcement.announced_at),
    }
    if announcement.asr_image:
        payload["ia"] = announcement.asr_image
    if announcement.tts_image:
        payload["it"] = announcement.tts_image

    blob = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    encoded = base64.urlsafe_b64encode(blob).decode("ascii").rstrip("=")
    result = f"{COMMITMENT_MAGIC}|{encoded}"

    if len(result.encode("utf-8")) > MAX_COMMITMENT_BYTES:
        raise CommitmentError(
            f"encoded announcement is {len(result)} bytes, over the "
            f"{MAX_COMMITMENT_BYTES}-byte limit; shorten the endpoint URL "
            "or drop the image digests"
        )
    return result


def decode_announcement(raw: str) -> Optional[MinerAnnouncement]:
    """Parse an on-chain commitment, or return ``None`` if it is not ours.

    Never raises on malformed input: the subnet shares its commitment space with
    anything a hotkey chooses to publish, so garbage is expected and must be
    skipped silently rather than stalling a validator sweep.
    """
    if not raw or not isinstance(raw, str):
        return None

    text = raw.strip()
    magic, sep, encoded = text.partition("|")
    if sep != "|" or magic != COMMITMENT_MAGIC:
        return None

    try:
        padding = "=" * (-len(encoded) % 4)
        blob = base64.urlsafe_b64decode(encoded + padding)
        payload = json.loads(blob.decode("utf-8"))
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    endpoint = str(payload.get("e", "") or "").rstrip("/")
    if not endpoint.startswith(("http://", "https://")):
        return None

    services = [
        _CHAR_TO_SERVICE[char]
        for char in str(payload.get("s", "") or "")
        if char in _CHAR_TO_SERVICE
    ]
    if not services:
        return None

    try:
        announced_at = float(payload.get("t", 0) or 0)
    except (TypeError, ValueError):
        announced_at = 0.0

    try:
        spec_version = int(payload.get("v", 0) or 0)
    except (TypeError, ValueError):
        spec_version = 0

    return MinerAnnouncement(
        endpoint=endpoint,
        services=services,
        gpus=_decode_gpus(str(payload.get("g", "") or "")),
        spec_version=spec_version,
        announced_at=announced_at or time.time(),
        asr_image=str(payload.get("ia", "") or ""),
        tts_image=str(payload.get("it", "") or ""),
    )


def is_compatible(announcement: MinerAnnouncement) -> bool:
    """Whether an announcement's spec version is one this build can serve.

    Older minor versions are accepted so that a validator upgrade does not
    instantly zero out every miner that has not yet restarted.
    """
    return 1 <= announcement.spec_version <= SPEC_VERSION
