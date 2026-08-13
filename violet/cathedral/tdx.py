"""TDX controller measurement for cathedral_voice_receipt_v1 (G04).

Trust boundary
--------------
* Controller (CPU / TDX): measured via ``cathedral_tdx_measurement_v1``.
* GPU path: never attested — see receipt ``gpu_attested=false``.

This module verifies the *software contract* of a measurement blob (MR allow-list,
debug-off, challenge/audience/hotkey/endpoint binding, quote presence). Full Intel
DCAP/PCS collateral verification is operator-pluggable via
``VIOLET_TDX_QUOTE_VERIFIER`` (external command) when available. Without hardware
or an external verifier, use ``CATHEDRAL_TDX_SIMULATION=1`` for CI only — never
for reward-bearing production.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

logger = logging.getLogger("violet.cathedral.tdx")

MEASUREMENT_FORMAT = "cathedral_tdx_measurement_v1"
DEFAULT_AUDIENCE = "cathedral_voice_hybrid"
DEFAULT_WORKLOAD_ID = "spark-tts-controller"


@dataclass
class TdxVerifyPolicy:
    """What the validator / Cathedral require of a controller measurement."""

    require_measurement: bool = True
    reject_debug: bool = True
    allowed_mrtd: List[str] = field(default_factory=list)
    allowed_rtmr0: List[str] = field(default_factory=list)
    audience: str = DEFAULT_AUDIENCE
    expected_challenge: str = ""
    expected_hotkey: str = ""
    expected_endpoint: str = ""
    #: When true, accept simulation quotes (CI only).
    allow_simulation: bool = False
    #: Optional external verifier: receives quote b64 on stdin, exit 0 = ok.
    quote_verifier_cmd: str = ""
    min_quote_bytes: int = 64


@dataclass
class TdxVerifyResult:
    ok: bool
    detail: str = ""
    measurement: Optional[Dict[str, Any]] = None
    simulated: bool = False


@dataclass
class ControllerMeasurement:
    """Canonical measured-controller blob embedded in receipts."""

    mrtd: str
    rtmr0: str = ""
    rtmr1: str = ""
    rtmr2: str = ""
    rtmr3: str = ""
    quote: str = ""
    debug: bool = False
    challenge: str = ""
    audience: str = DEFAULT_AUDIENCE
    hotkey: str = ""
    endpoint: str = ""
    workload_id: str = DEFAULT_WORKLOAD_ID
    format: str = MEASUREMENT_FORMAT
    simulated: bool = False
    issued_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if not data.get("issued_at"):
            data["issued_at"] = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%f"
            )[:-3] + "Z"
        return data

    def encode(self) -> str:
        """Compact JSON string stored in ``controller_measurement``."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def decode(cls, raw: str | Mapping[str, Any]) -> "ControllerMeasurement":
        if isinstance(raw, Mapping):
            data = dict(raw)
        else:
            data = json.loads(raw)
        return cls(
            format=str(data.get("format") or MEASUREMENT_FORMAT),
            mrtd=str(data.get("mrtd") or ""),
            rtmr0=str(data.get("rtmr0") or ""),
            rtmr1=str(data.get("rtmr1") or ""),
            rtmr2=str(data.get("rtmr2") or ""),
            rtmr3=str(data.get("rtmr3") or ""),
            quote=str(data.get("quote") or ""),
            debug=bool(data.get("debug")),
            challenge=str(data.get("challenge") or ""),
            audience=str(data.get("audience") or DEFAULT_AUDIENCE),
            hotkey=str(data.get("hotkey") or ""),
            endpoint=str(data.get("endpoint") or ""),
            workload_id=str(data.get("workload_id") or DEFAULT_WORKLOAD_ID),
            simulated=bool(data.get("simulated")),
            issued_at=str(data.get("issued_at") or ""),
        )


def _norm_hex(value: str) -> str:
    return (value or "").strip().lower().removeprefix("0x")


def _allowlist_hit(value: str, allowed: Sequence[str]) -> bool:
    if not allowed:
        return True
    needle = _norm_hex(value)
    return any(_norm_hex(item) == needle for item in allowed if item)


def tdx_simulation_enabled() -> bool:
    return os.getenv("CATHEDRAL_TDX_SIMULATION", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def tdx_policy_from_env() -> TdxVerifyPolicy:
    def _list(name: str) -> List[str]:
        raw = os.getenv(name, "").strip()
        if not raw:
            return []
        return [part.strip() for part in raw.split(",") if part.strip()]

    return TdxVerifyPolicy(
        require_measurement=os.getenv("CATHEDRAL_HYBRID_REQUIRE_TDX", "1").strip().lower()
        not in ("0", "false", "no", "off"),
        reject_debug=True,
        allowed_mrtd=_list("VIOLET_TDX_ALLOWED_MRTD"),
        allowed_rtmr0=_list("VIOLET_TDX_ALLOWED_RTMR0"),
        audience=os.getenv("VIOLET_TDX_AUDIENCE", DEFAULT_AUDIENCE).strip()
        or DEFAULT_AUDIENCE,
        expected_challenge=os.getenv("VIOLET_TDX_CHALLENGE", "").strip(),
        expected_hotkey=os.getenv("VIOLET_TDX_EXPECTED_HOTKEY", "").strip(),
        expected_endpoint=os.getenv("VIOLET_TDX_EXPECTED_ENDPOINT", "").strip(),
        allow_simulation=tdx_simulation_enabled(),
        quote_verifier_cmd=os.getenv("VIOLET_TDX_QUOTE_VERIFIER", "").strip(),
    )


def simulate_controller_measurement(
    *,
    hotkey: str,
    challenge: str,
    endpoint: str = "http://127.0.0.1:8091",
    audience: str = DEFAULT_AUDIENCE,
    mrtd: Optional[str] = None,
) -> ControllerMeasurement:
    """Build a clearly marked simulation measurement for CI / dry-run."""
    seed = f"{hotkey}|{challenge}|{endpoint}|{audience}".encode("utf-8")
    digest = hashlib.sha256(seed).hexdigest()
    quote = base64.b64encode(
        b"SIM-TDX-QUOTE:" + digest.encode("utf-8") + b":" + os.urandom(16)
    ).decode("ascii")
    return ControllerMeasurement(
        mrtd=mrtd or ("sim" + digest[:60]),
        rtmr0="sim" + hashlib.sha256(digest.encode()).hexdigest()[:60],
        rtmr1="",
        rtmr2="",
        rtmr3="",
        quote=quote,
        debug=False,
        challenge=challenge,
        audience=audience,
        hotkey=hotkey,
        endpoint=endpoint,
        simulated=True,
        workload_id=DEFAULT_WORKLOAD_ID,
    )


def _run_external_quote_verifier(cmd: str, quote_b64: str) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            cmd,
            input=quote_b64.encode("utf-8"),
            capture_output=True,
            timeout=30,
            shell=True,
            check=False,
        )
    except Exception as exc:
        return False, f"quote verifier error: {exc}"
    if completed.returncode != 0:
        err = (completed.stderr or completed.stdout or b"").decode("utf-8", errors="replace")
        return False, f"quote verifier failed: {err[:200]}"
    return True, "quote verified externally"


def verify_controller_measurement(
    raw: Optional[str | Mapping[str, Any]],
    policy: Optional[TdxVerifyPolicy] = None,
) -> TdxVerifyResult:
    """Fail-closed verification of a controller measurement blob."""
    policy = policy or tdx_policy_from_env()
    if not raw:
        if policy.require_measurement:
            return TdxVerifyResult(False, "controller_measurement missing")
        return TdxVerifyResult(True, "measurement not required")

    try:
        measurement = ControllerMeasurement.decode(raw)
    except Exception as exc:
        return TdxVerifyResult(False, f"measurement parse error: {exc}")

    if measurement.format != MEASUREMENT_FORMAT:
        return TdxVerifyResult(False, f"unsupported measurement format: {measurement.format}")

    if policy.reject_debug and measurement.debug:
        return TdxVerifyResult(False, "TDX debug mode rejected")

    if measurement.simulated and not policy.allow_simulation:
        return TdxVerifyResult(
            False,
            "simulated TDX measurement rejected (set CATHEDRAL_TDX_SIMULATION=1 for CI only)",
        )

    if not _norm_hex(measurement.mrtd):
        return TdxVerifyResult(False, "mrtd required")

    if policy.allowed_mrtd and not _allowlist_hit(measurement.mrtd, policy.allowed_mrtd):
        return TdxVerifyResult(False, "mrtd not in allow-list")

    if policy.allowed_rtmr0 and measurement.rtmr0:
        if not _allowlist_hit(measurement.rtmr0, policy.allowed_rtmr0):
            return TdxVerifyResult(False, "rtmr0 not in allow-list")

    if policy.audience and measurement.audience != policy.audience:
        return TdxVerifyResult(
            False,
            f"audience mismatch: {measurement.audience!r} != {policy.audience!r}",
        )

    if policy.expected_challenge and measurement.challenge != policy.expected_challenge:
        return TdxVerifyResult(False, "challenge mismatch")

    if policy.expected_hotkey and measurement.hotkey != policy.expected_hotkey:
        return TdxVerifyResult(False, "measurement hotkey mismatch")

    if policy.expected_endpoint and measurement.endpoint.rstrip("/") != policy.expected_endpoint.rstrip("/"):
        return TdxVerifyResult(False, "measurement endpoint mismatch")

    if not measurement.quote.strip():
        return TdxVerifyResult(False, "TDX quote missing")

    try:
        quote_bytes = base64.b64decode(measurement.quote, validate=False)
    except Exception:
        return TdxVerifyResult(False, "TDX quote is not valid base64")

    if len(quote_bytes) < policy.min_quote_bytes:
        return TdxVerifyResult(False, "TDX quote too short")

    if policy.quote_verifier_cmd:
        ok, detail = _run_external_quote_verifier(
            policy.quote_verifier_cmd, measurement.quote
        )
        if not ok:
            return TdxVerifyResult(False, detail, measurement=measurement.to_dict())

    detail = "ok"
    if measurement.simulated:
        detail = "ok (simulated)"
    return TdxVerifyResult(
        True,
        detail,
        measurement=measurement.to_dict(),
        simulated=measurement.simulated,
    )
