"""cathedral_voice_receipt_v1 — hybrid miner-owned voice receipts.

Design: ``docs/CATHEDRAL_VOICE_RECEIPT_v1.md``.

This module is intentionally hardware-agnostic: without TDX quote tooling the
miner emits ``status=unavailable`` and never claims attestation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

RECEIPT_VERSION = "cathedral_voice_receipt_v1"
HEADER_VOICE_RECEIPT = "X-Violet-Voice-Receipt"
EXECUTION_CLASS_HYBRID = "hybrid_gpu_preview"

GPU_STATUS_UNATTESTED = "unattested"
GPU_STATUS_TRUSTED_NOT_ATTESTED = "trusted_not_attested"
GPU_STATUS_UNAVAILABLE = "unavailable"

STATUS_OK = "ok"
STATUS_UNAVAILABLE = "unavailable"

_ALLOWED_GPU = frozenset(
    {GPU_STATUS_UNATTESTED, GPU_STATUS_TRUSTED_NOT_ATTESTED, GPU_STATUS_UNAVAILABLE}
)
_ALLOWED_STATUS = frozenset({STATUS_OK, STATUS_UNAVAILABLE})


def _ms_iso(ts: Optional[float] = None) -> str:
    if ts is None:
        dt = datetime.now(timezone.utc)
    else:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def request_hash(
    *,
    input_text: str,
    voice: str,
    temperature: float = 0.7,
) -> str:
    """Stable hash of the Spark TTS request fields."""
    body = {
        "input": (input_text or "").strip(),
        "temperature": float(temperature),
        "voice": (voice or "").strip(),
    }
    return sha256_hex(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def audio_content_hash(audio: bytes) -> str:
    return sha256_hex(audio or b"")


@dataclass
class CathedralVoiceReceiptV1:
    """Miner-owned hybrid receipt for a single TTS response."""

    miner_hotkey: str
    request_hash: str
    audio_content_hash: str
    version: str = RECEIPT_VERSION
    status: str = STATUS_OK
    execution_class: str = EXECUTION_CLASS_HYBRID
    #: Honest hybrid boundary: GPU path is never attested in this design.
    gpu_attested: bool = False
    gpu_memory_confidential: bool = False
    controller_measurement: Optional[str] = None
    gpu_attestation_status: str = GPU_STATUS_TRUSTED_NOT_ATTESTED
    issued_at: str = field(default_factory=_ms_iso)
    signature: Optional[str] = None
    probe_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if data.get("probe_id") is None:
            data.pop("probe_id", None)
        if data.get("signature") is None:
            data.pop("signature", None)
        if data.get("controller_measurement") is None:
            data.pop("controller_measurement", None)
        # Always emit honest boundary flags on scored work.
        data["gpu_attested"] = False
        data["gpu_memory_confidential"] = False
        data["execution_class"] = data.get("execution_class") or EXECUTION_CLASS_HYBRID
        return data

    def canonical_payload(self) -> Dict[str, Any]:
        """Fields covered by the signature (excludes ``signature``)."""
        data = self.to_dict()
        data.pop("signature", None)
        return data

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.canonical_payload(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CathedralVoiceReceiptV1":
        return cls(
            version=str(raw.get("version") or RECEIPT_VERSION),
            status=str(raw.get("status") or STATUS_OK),
            miner_hotkey=str(raw.get("miner_hotkey") or ""),
            request_hash=str(raw.get("request_hash") or ""),
            audio_content_hash=str(raw.get("audio_content_hash") or ""),
            execution_class=str(raw.get("execution_class") or EXECUTION_CLASS_HYBRID),
            gpu_attested=bool(raw.get("gpu_attested")),
            gpu_memory_confidential=bool(raw.get("gpu_memory_confidential")),
            controller_measurement=(
                None
                if raw.get("controller_measurement") in (None, "")
                else str(raw.get("controller_measurement"))
            ),
            gpu_attestation_status=str(
                raw.get("gpu_attestation_status") or GPU_STATUS_TRUSTED_NOT_ATTESTED
            ),
            issued_at=str(raw.get("issued_at") or _ms_iso()),
            signature=(
                None if raw.get("signature") in (None, "") else str(raw.get("signature"))
            ),
            probe_id=(
                None if raw.get("probe_id") in (None, "") else str(raw.get("probe_id"))
            ),
        )


def sign_receipt_hmac(receipt: CathedralVoiceReceiptV1, secret: str) -> str:
    """Dev/stub HMAC signature (prefer Ed25519 for production)."""
    digest = hmac.new(
        secret.encode("utf-8"), receipt.canonical_bytes(), hashlib.sha256
    ).hexdigest()
    return f"hmac-sha256={digest}"


def verify_receipt_hmac(receipt: CathedralVoiceReceiptV1, secret: str) -> bool:
    if not receipt.signature or not secret:
        return False
    expected = sign_receipt_hmac(receipt, secret)
    return hmac.compare_digest(receipt.signature, expected)


def sign_receipt_ed25519(receipt: CathedralVoiceReceiptV1, private_key_pem_or_raw: bytes | str) -> str:
    """Sign canonical receipt bytes with an Ed25519 private key.

    ``private_key_pem_or_raw`` may be PEM text or 32 raw private key bytes (hex).
    Returns ``ed25519:<hex>``.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        load_pem_private_key,
    )

    key: Ed25519PrivateKey
    if isinstance(private_key_pem_or_raw, str):
        text = private_key_pem_or_raw.strip()
        if text.startswith("-----"):
            key = load_pem_private_key(text.encode("utf-8"), password=None)  # type: ignore[assignment]
        else:
            raw = bytes.fromhex(text.removeprefix("0x"))
            key = Ed25519PrivateKey.from_private_bytes(raw)
    else:
        raw = private_key_pem_or_raw
        if raw.startswith(b"-----"):
            key = load_pem_private_key(raw, password=None)  # type: ignore[assignment]
        else:
            key = Ed25519PrivateKey.from_private_bytes(raw)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError("expected Ed25519 private key")
    sig = key.sign(receipt.canonical_bytes())
    return f"ed25519:{sig.hex()}"


def verify_receipt_ed25519(receipt: CathedralVoiceReceiptV1, public_key_hex: str) -> bool:
    if not receipt.signature or not public_key_hex:
        return False
    sig = receipt.signature.strip()
    if not sig.startswith("ed25519:"):
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        key = Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(public_key_hex.strip().removeprefix("0x"))
        )
        key.verify(bytes.fromhex(sig[len("ed25519:") :]), receipt.canonical_bytes())
        return True
    except Exception:
        return False


def generate_ed25519_keypair() -> tuple[str, str]:
    """Return ``(private_key_hex, public_key_hex)`` for CI / operators."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    return (
        private.private_bytes_raw().hex(),
        private.public_key().public_bytes_raw().hex(),
    )


@dataclass
class ReceiptVerifyResult:
    ok: bool
    detail: str = ""
    receipt: Optional[CathedralVoiceReceiptV1] = None
    tdx_simulated: bool = False


def verify_receipt(
    raw: Optional[Mapping[str, Any]],
    *,
    require: bool = True,
    hmac_secret: str = "",
    ed25519_public_key_hex: str = "",
    require_tdx: bool = False,
    tdx_policy: Any = None,
    expected_request_hash: Optional[str] = None,
    expected_audio_hash: Optional[str] = None,
) -> ReceiptVerifyResult:
    """Fail-closed verification for Cathedral / validator intake.

    Prefer Ed25519 (``ed25519:<hex>``) over HMAC. When ``require_tdx`` is set,
    controller measurement must pass :func:`violet.cathedral.tdx.verify_controller_measurement`.
    """
    if raw is None:
        if require:
            return ReceiptVerifyResult(False, "receipt missing")
        return ReceiptVerifyResult(True, "receipt not required")

    try:
        receipt = CathedralVoiceReceiptV1.from_dict(raw)
    except Exception as exc:
        return ReceiptVerifyResult(False, f"receipt parse error: {exc}")

    if receipt.version != RECEIPT_VERSION:
        return ReceiptVerifyResult(False, f"unsupported version: {receipt.version}")

    if receipt.status not in _ALLOWED_STATUS:
        return ReceiptVerifyResult(False, f"invalid status: {receipt.status}")

    if receipt.gpu_attestation_status not in _ALLOWED_GPU:
        return ReceiptVerifyResult(
            False, f"invalid gpu_attestation_status: {receipt.gpu_attestation_status}"
        )

    if require and receipt.status != STATUS_OK:
        return ReceiptVerifyResult(
            False, f"receipt status={receipt.status} (fail closed)"
        )

    if not receipt.miner_hotkey.strip():
        return ReceiptVerifyResult(False, "miner_hotkey required")

    if not receipt.request_hash or not receipt.audio_content_hash:
        return ReceiptVerifyResult(False, "request/audio hashes required")

    if expected_request_hash and receipt.request_hash != expected_request_hash:
        return ReceiptVerifyResult(False, "request_hash mismatch")

    if expected_audio_hash and receipt.audio_content_hash != expected_audio_hash:
        return ReceiptVerifyResult(False, "audio_content_hash mismatch")

    # Reject over-claims on the GPU boundary (G06).
    if receipt.gpu_attested or receipt.gpu_memory_confidential:
        return ReceiptVerifyResult(False, "GPU attestation over-claim rejected")
    if bool(raw.get("gpu_attested")) or bool(raw.get("gpu_memory_confidential")):
        return ReceiptVerifyResult(False, "GPU attestation over-claim rejected")

    tdx_simulated = False
    if require_tdx or (receipt.controller_measurement and tdx_policy is not None):
        from .tdx import TdxVerifyPolicy, verify_controller_measurement

        policy = tdx_policy or TdxVerifyPolicy(require_measurement=True)
        if require_tdx and hasattr(policy, "require_measurement"):
            policy.require_measurement = True
        tdx = verify_controller_measurement(receipt.controller_measurement, policy)
        if not tdx.ok:
            return ReceiptVerifyResult(False, f"TDX: {tdx.detail}")
        tdx_simulated = bool(tdx.simulated)
        # Bind measurement hotkey to receipt hotkey when present.
        if tdx.measurement and tdx.measurement.get("hotkey"):
            if tdx.measurement["hotkey"] != receipt.miner_hotkey:
                return ReceiptVerifyResult(False, "TDX hotkey != receipt miner_hotkey")
    elif require_tdx and not (receipt.controller_measurement or "").strip():
        return ReceiptVerifyResult(False, "TDX controller_measurement required")

    sig = (receipt.signature or "").strip()
    if ed25519_public_key_hex:
        if not verify_receipt_ed25519(receipt, ed25519_public_key_hex):
            return ReceiptVerifyResult(False, "ed25519 signature verify failed")
    elif hmac_secret:
        if not verify_receipt_hmac(receipt, hmac_secret):
            return ReceiptVerifyResult(False, "signature verify failed")
    elif require and not sig:
        return ReceiptVerifyResult(False, "signature required")

    detail = "ok"
    if tdx_simulated:
        detail = "ok (tdx simulated)"
    return ReceiptVerifyResult(
        True, detail, receipt=receipt, tdx_simulated=tdx_simulated
    )


def build_unavailable_receipt(miner_hotkey: str, *, reason: str = "tdx_unavailable") -> CathedralVoiceReceiptV1:
    """Emit when receipt mode is on but TDX measurement cannot be produced."""
    return CathedralVoiceReceiptV1(
        miner_hotkey=miner_hotkey,
        request_hash=sha256_hex(b""),
        audio_content_hash=sha256_hex(b""),
        status=STATUS_UNAVAILABLE,
        controller_measurement=None,
        gpu_attestation_status=GPU_STATUS_UNAVAILABLE,
        probe_id=reason,
    )


def build_receipt(
    *,
    miner_hotkey: str,
    input_text: str,
    voice: str,
    audio: bytes,
    temperature: float = 0.7,
    controller_measurement: Optional[str] = None,
    gpu_attestation_status: str = GPU_STATUS_TRUSTED_NOT_ATTESTED,
    probe_id: Optional[str] = None,
    hmac_secret: str = "",
    ed25519_private_key: str | bytes = "",
) -> CathedralVoiceReceiptV1:
    """Build a receipt. Prefer Ed25519; HMAC remains for legacy stubs."""
    receipt = CathedralVoiceReceiptV1(
        miner_hotkey=miner_hotkey,
        request_hash=request_hash(
            input_text=input_text, voice=voice, temperature=temperature
        ),
        audio_content_hash=audio_content_hash(audio),
        controller_measurement=controller_measurement,
        gpu_attestation_status=gpu_attestation_status,
        probe_id=probe_id,
        issued_at=_ms_iso(time.time()),
    )
    if ed25519_private_key:
        receipt.signature = sign_receipt_ed25519(receipt, ed25519_private_key)
    elif hmac_secret:
        receipt.signature = sign_receipt_hmac(receipt, hmac_secret)
    return receipt


def receipt_enabled_from_env() -> bool:
    raw = os.getenv("VIOLET_TTS_RECEIPT_ENABLED", "")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def receipt_buffer_from_env() -> bool:
    """When true, buffer TTS audio so the receipt can include audio_content_hash."""
    raw = os.getenv("VIOLET_TTS_RECEIPT_BUFFER", "")
    if raw.strip():
        return raw.strip().lower() in ("1", "true", "yes", "on")
    # Default on when receipts are enabled — complete digests for G05.
    return receipt_enabled_from_env()
