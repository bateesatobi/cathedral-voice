"""Cathedral SN39 thin relay — colocated inside cathedral-voice.

Trust model
-----------
* **Voice path** (this repo): probe ASR/TTS miners, score C/W/Q, POST
  ``violet_audio`` (``complete=true``) to the Cathedral **publisher**.
* **Thin path** (this module): fetch the publisher's *already blended + signed*
  vector, verify Ed25519, map hotkeys→UIDs, ``set_weights`` on SN39.

The publisher blend keeps both populations in one vector::

    weight[hk] = (1-f)*base_norm[hk] + f*violet_norm[hk]

Fail-closed on the publisher: missing/stale violet → base-only (Cathedral
compute/SAT miners still paid). Thin never subsets the vector to "voice only".

Bittensor context
-----------------
* One hotkey can validate multiple **netuids** (register + ``set_weights`` each).
* Within one subnet, **multiple incentive mechanisms** (formerly "sub-subnets")
  each get their own weight matrix / ``mechid`` (runtime max 2 today).
* Cathedral SN39 currently uses a **publisher-composed single mechanism** with
  external-score *lanes* (base + violet_audio + …), not separate chain mechs.
  Colocating thin+voice in one process is the operator pattern that mirrors
  "one validator serving both lanes" without dual ``set_weights`` authors.

Sole writer rule: when thin broadcast is enabled, local Violet
``ChainClient.set_weights`` must be skipped — same netuid 39 would otherwise
overwrite the signed blended vector.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

logger = logging.getLogger("violet.cathedral.thin_relay")

DEFAULT_PUBLISHER_URL = "https://api.cathedral.computer"
DEFAULT_PUBLIC_KEY_HEX = (
    "10890a66aa752479cb3b634f366d7bd27c374324d83f88d2d6b69ab066f25e26"
)
DEFAULT_KEY_ID = "cathedral-weight-policy"
WEIGHTS_PATH = "/v1/validator/weights/next"


class ThinRelayError(RuntimeError):
    """Signed-feed / thin-relay failure (isolated from the voice path)."""


@dataclass
class ThinRelayConfig:
    enabled: bool = False
    broadcast: bool = False
    publisher_url: str = DEFAULT_PUBLISHER_URL
    public_key_hex: str = DEFAULT_PUBLIC_KEY_HEX
    key_id: str = DEFAULT_KEY_ID
    netuid: int = 39
    network: str = "finney"
    interval_s: float = 1500.0
    state_path: str = ""
    #: Prefer the official cathedral-validator CLI when on PATH.
    prefer_subprocess: bool = True
    cathedral_validator_bin: str = "cathedral-validator"
    timeout_s: float = 60.0
    dry_run: bool = True

    @property
    def feed_url(self) -> str:
        return self.publisher_url.rstrip("/") + WEIGHTS_PATH

    @property
    def resolved_state_path(self) -> Path:
        if self.state_path:
            return Path(self.state_path).expanduser()
        return Path.home() / ".cathedral" / "cathedral_voice_thin_state.json"


def config_from_env() -> ThinRelayConfig:
    def _bool(name: str, default: bool = False) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() in ("1", "true", "yes", "on")

    def _float(name: str, default: float) -> float:
        try:
            return float(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return default

    def _int(name: str, default: int) -> int:
        try:
            return int(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return default

    enabled = _bool("CATHEDRAL_THIN_ENABLED", False)
    broadcast = _bool("CATHEDRAL_THIN_BROADCAST", False)
    dry = _bool("CATHEDRAL_THIN_DRY_RUN", not broadcast)
    return ThinRelayConfig(
        enabled=enabled,
        broadcast=broadcast,
        publisher_url=(
            os.getenv("CATHEDRAL_PUBLISHER_URL") or DEFAULT_PUBLISHER_URL
        ).rstrip("/"),
        public_key_hex=(
            os.getenv("CATHEDRAL_WEIGHT_POLICY_PUBLIC_KEY") or DEFAULT_PUBLIC_KEY_HEX
        ).strip(),
        key_id=(
            os.getenv("CATHEDRAL_WEIGHT_POLICY_KEY_ID") or DEFAULT_KEY_ID
        ).strip(),
        netuid=_int("CATHEDRAL_THIN_NETUID", _int("CATHEDRAL_EXTERNAL_SCORES_NETUID", 39)),
        network=(os.getenv("BT_NETWORK") or os.getenv("CATHEDRAL_THIN_NETWORK") or "finney"),
        interval_s=_float("CATHEDRAL_THIN_INTERVAL_S", 1500.0),
        state_path=os.getenv("CATHEDRAL_VALIDATOR_STATE", ""),
        prefer_subprocess=_bool("CATHEDRAL_THIN_PREFER_SUBPROCESS", True),
        cathedral_validator_bin=os.getenv(
            "CATHEDRAL_VALIDATOR_BIN", "cathedral-validator"
        ),
        timeout_s=_float("CATHEDRAL_THIN_TIMEOUT_S", 60.0),
        dry_run=dry and not broadcast,
    )


def canonical_bytes(payload: Dict[str, Any]) -> bytes:
    body = {k: v for k, v in payload.items() if k != "signature"}
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False
    ).encode("utf-8")


def verify_signed_vector(
    payload: Dict[str, Any], *, public_key_hex: str, expected_key_id: str
) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    sig_b64 = payload.get("signature") or ""
    if not str(sig_b64).strip():
        raise ThinRelayError("vector is missing signature")
    if payload.get("key_id") != expected_key_id:
        raise ThinRelayError(
            f"key_id mismatch: vector={payload.get('key_id')!r} pinned={expected_key_id!r}"
        )
    try:
        sig = base64.b64decode(str(sig_b64).encode("ascii"), validate=True)
    except Exception as exc:
        raise ThinRelayError(f"signature is not valid base64: {exc}") from exc
    try:
        pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex.strip()))
        pk.verify(sig, canonical_bytes(payload))
    except InvalidSignature as exc:
        raise ThinRelayError("ed25519 signature verify failed") from exc
    except Exception as exc:
        raise ThinRelayError(f"signature verify error: {exc}") from exc


def _load_fence(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _save_fence(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)


def enforce_policy_fence(payload: Dict[str, Any], state_path: Path) -> None:
    """Reject rollback to an older policy_version (cathedral thin fence)."""
    state = _load_fence(state_path)
    incoming = payload.get("policy_version")
    if incoming is None:
        return
    try:
        incoming_i = int(incoming)
    except (TypeError, ValueError):
        raise ThinRelayError(f"invalid policy_version: {incoming!r}")
    last = state.get("last_policy_version")
    if last is not None and incoming_i < int(last):
        raise ThinRelayError(
            f"policy_version rollback refused: {incoming_i} < {last}"
        )
    state["last_policy_version"] = incoming_i
    state["last_vector_id"] = payload.get("vector_id")
    state["updated_at"] = time.time()
    _save_fence(state_path, state)


def extract_hotkey_weights(payload: Dict[str, Any]) -> List[Tuple[str, float]]:
    rows = payload.get("weights") or []
    out: List[Tuple[str, float]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        hotkey = str(row.get("miner_hotkey") or row.get("hotkey") or "").strip()
        if not hotkey or hotkey in seen:
            continue
        raw = row.get("weight", row.get("score", row.get("raw_score")))
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value <= 0 or value != value:  # NaN guard
            continue
        seen.add(hotkey)
        out.append((hotkey, value))
    return out


def map_hotkeys_to_uids(
    graph: Any, hotkey_weights: List[Tuple[str, float]]
) -> Tuple[List[int], List[float]]:
    hk_to_uid: Dict[str, int] = {}
    for neuron in getattr(graph, "neurons", []) or []:
        try:
            hk_to_uid[str(neuron.hotkey)] = int(neuron.uid)
        except Exception:
            continue
    uids: List[int] = []
    weights: List[float] = []
    missing = 0
    for hotkey, weight in hotkey_weights:
        uid = hk_to_uid.get(hotkey)
        if uid is None:
            missing += 1
            continue
        uids.append(uid)
        weights.append(weight)
    if missing:
        logger.warning(
            "thin relay: %d signed hotkeys not on metagraph (skipped)", missing
        )
    if not uids:
        raise ThinRelayError("no signed hotkeys mapped to live SN39 UIDs")
    return uids, weights


async def fetch_signed_vector(
    session: aiohttp.ClientSession, url: str, *, timeout_s: float
) -> Dict[str, Any]:
    if not url.startswith("https://") and "127.0.0.1" not in url and "localhost" not in url:
        # Allow http only for local dry fixtures; production must be https.
        if not url.startswith("http://127.") and not url.startswith("http://localhost"):
            raise ThinRelayError("publisher URL must be https in production")
    async with session.get(
        url,
        timeout=aiohttp.ClientTimeout(total=timeout_s),
        headers={"User-Agent": "cathedral-voice-thin-relay/1.0"},
        allow_redirects=False,
    ) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise ThinRelayError(f"feed fetch status={resp.status} body={body[:300]}")
        payload = await resp.json(content_type=None)
    if not isinstance(payload, dict):
        raise ThinRelayError("feed payload is not a JSON object")
    return payload


async def run_thin_tick_subprocess(config: ThinRelayConfig) -> Dict[str, Any]:
    binary = shutil.which(config.cathedral_validator_bin)
    if not binary:
        raise ThinRelayError(
            f"{config.cathedral_validator_bin!r} not on PATH; "
            "install cathedral-validator or set CATHEDRAL_THIN_PREFER_SUBPROCESS=0"
        )
    cmd = [
        binary,
        "serve",
        "--once",
        "--publisher-url",
        config.publisher_url,
        "--public-key-hex",
        config.public_key_hex,
    ]
    if config.broadcast and not config.dry_run:
        cmd.append("--broadcast")
    logger.info("thin relay subprocess: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=config.timeout_s
        )
    except asyncio.TimeoutError as exc:
        proc.kill()
        raise ThinRelayError("cathedral-validator subprocess timed out") from exc
    out = {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": stdout.decode("utf-8", errors="replace")[-4000:],
        "stderr": stderr.decode("utf-8", errors="replace")[-4000:],
        "mode": "subprocess",
    }
    if proc.returncode != 0:
        raise ThinRelayError(
            f"cathedral-validator exited {proc.returncode}: {out['stderr'][-500:]}"
        )
    return out


async def run_thin_tick_inprocess(
    config: ThinRelayConfig,
    *,
    chain: Any,
    session: aiohttp.ClientSession,
) -> Dict[str, Any]:
    """Fetch → verify → fence → map → optional set_weights via violet ChainClient."""
    payload = await fetch_signed_vector(
        session, config.feed_url, timeout_s=config.timeout_s
    )
    verify_signed_vector(
        payload,
        public_key_hex=config.public_key_hex,
        expected_key_id=config.key_id,
    )
    # Soft netuid check when present on the payload.
    payload_netuid = payload.get("netuid")
    if payload_netuid is not None and int(payload_netuid) != int(config.netuid):
        raise ThinRelayError(
            f"feed netuid {payload_netuid} != configured {config.netuid}"
        )
    enforce_policy_fence(payload, config.resolved_state_path)

    hotkey_weights = extract_hotkey_weights(payload)
    if not hotkey_weights:
        raise ThinRelayError("signed vector has no positive weights")

    # Use a chain client pointed at SN39 if the voice chain netuid differs.
    graph = await chain.metagraph(commitments=False)
    uids, weights = map_hotkeys_to_uids(graph, hotkey_weights)

    result: Dict[str, Any] = {
        "ok": True,
        "mode": "inprocess",
        "vector_id": payload.get("vector_id"),
        "policy_version": payload.get("policy_version"),
        "n_signed": len(hotkey_weights),
        "n_mapped": len(uids),
        "broadcast": False,
    }

    if config.dry_run or not config.broadcast:
        logger.info(
            "thin relay dry-run: would set_weights netuid=%s n=%d vector=%s",
            config.netuid,
            len(uids),
            payload.get("vector_id"),
        )
        result["dry_run"] = True
        return result

    # Ensure chain client targets SN39.
    if getattr(chain.config, "netuid", None) != config.netuid:
        logger.warning(
            "voice chain netuid=%s but thin netuid=%s — set_weights uses chain.config",
            chain.config.netuid,
            config.netuid,
        )
    submitted = await chain.set_weights(uids, weights)
    result["broadcast"] = bool(submitted)
    result["ok"] = bool(submitted)
    if not submitted:
        raise ThinRelayError("set_weights returned false")
    logger.info(
        "thin relay set_weights ok netuid=%s n=%d vector=%s",
        config.netuid,
        len(uids),
        payload.get("vector_id"),
    )
    return result


async def run_thin_tick(
    config: ThinRelayConfig,
    *,
    chain: Any = None,
    session: Optional[aiohttp.ClientSession] = None,
) -> Dict[str, Any]:
    """One thin cycle. Prefer official CLI; fall back to in-process relay."""
    if not config.enabled:
        return {"ok": False, "error": "thin_disabled"}

    if config.prefer_subprocess:
        try:
            return await run_thin_tick_subprocess(config)
        except ThinRelayError as exc:
            logger.warning("subprocess thin tick unavailable (%s); trying in-process", exc)

    if session is None or chain is None:
        raise ThinRelayError("in-process thin tick requires chain + aiohttp session")
    return await run_thin_tick_inprocess(config, chain=chain, session=session)
