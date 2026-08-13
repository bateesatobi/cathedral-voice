"""Cathedral SN39 external-score ingest client (violet_audio + hybrid).

Cathedral thin validators do **not** accept scores directly. They only fetch a
signed weight vector from the publisher and call ``set_weights``. Violet /
cathedral-voice posts score reports here:

    POST {publisher}/v1/external-scores/violet
    source = violet_audio                 # legacy path (unchanged)
    source = cathedral_voice_hybrid       # receipt-gated (Brief 3)
    complete = true   # required for blending (incomplete reports are ignored)

See cathedralai/cathedral-validator docs/VIOLET_EXTERNAL_SCORES.md and
``docs/CATHEDRAL_VOICE_RECEIPT_v1.md``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

import aiohttp

from .receipt_v1 import verify_receipt

logger = logging.getLogger("violet.cathedral.external_scores")

SOURCE = "violet_audio"
SOURCE_HYBRID = "cathedral_voice_hybrid"
SUBMIT_PATH = "/v1/external-scores/violet"
DEFAULT_PUBLISHER_URL = "https://api.cathedral.computer"


@dataclass
class CathedralScoreClientConfig:
    """Env-backed settings for posting Violet scores into Cathedral."""

    enabled: bool = False
    publisher_url: str = DEFAULT_PUBLISHER_URL
    token: str = ""
    hmac_secret: str = ""
    #: Dedicated hybrid ingest credentials (publisher per-source token/HMAC).
    #: When set, ``cathedral_voice_hybrid`` POSTs use these instead of shared.
    hybrid_token: str = ""
    hybrid_hmac_secret: str = ""
    netuid: int = 39
    timeout_s: float = 15.0
    #: When true, still build/post reports but log instead of HTTP POST.
    dry_run: bool = False
    #: When true, attempt receipt-gated ``cathedral_voice_hybrid`` posts.
    hybrid_enabled: bool = False
    #: Prefer hybrid-only (skip violet_audio). Fail closed if no receipts.
    hybrid_only: bool = False
    #: HMAC for receipt verify (may differ from publisher body HMAC).
    receipt_hmac_secret: str = ""
    #: Ed25519 public key (hex) for receipt verify.
    receipt_ed25519_public_key_hex: str = ""
    require_tdx: bool = False

    def auth_for_source(self, source: str) -> tuple[str, str]:
        """Return ``(bearer_token, hmac_secret)`` for a report source."""
        if source == SOURCE_HYBRID and (self.hybrid_token or self.hybrid_hmac_secret):
            return (
                (self.hybrid_token or self.token).strip(),
                (self.hybrid_hmac_secret or self.hmac_secret).strip(),
            )
        return self.token.strip(), self.hmac_secret.strip()


def ms_iso(dt: Optional[datetime] = None) -> str:
    """ISO-8601 UTC with millisecond precision (publisher freshness gate)."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def clamp01(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return max(0.0, min(1.0, float(value)))


def canonical_body(report: Mapping[str, Any]) -> bytes:
    return json.dumps(report, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_hmac_header(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def epoch_from_unix(ts: Optional[float] = None) -> int:
    """Monotonic-ish epoch: unix seconds. Publisher requires increasing epochs."""
    return int(ts if ts is not None else time.time())


def scores_from_miner_scores(
    miners: Sequence[Any],
    *,
    score_attr: str = "final",
) -> List[Dict[str, Any]]:
    """Map Violet ``MinerScore`` (or duck-typed objects) to Cathedral score rows.

    Scores are max-normalized into ``[0, 1]`` as the publisher expects.
    """
    rows: List[tuple] = []
    for m in miners:
        hotkey = getattr(m, "hotkey", None) or (m.get("hotkey") if isinstance(m, dict) else None)
        if not hotkey:
            continue
        raw = getattr(m, score_attr, None)
        if raw is None and isinstance(m, dict):
            raw = m.get(score_attr, m.get("score"))
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value) or value < 0:
            continue
        uid = getattr(m, "uid", None)
        if uid is None and isinstance(m, dict):
            uid = m.get("uid")
        quality = getattr(m, "quality", None)
        if quality is None and isinstance(m, dict):
            quality = m.get("quality")
        rows.append((str(hotkey), value, uid, quality))

    if not rows:
        return []

    peak = max(v for _, v, _, _ in rows) or 1.0
    out: List[Dict[str, Any]] = []
    for hotkey, value, uid, quality in rows:
        score = clamp01(value / peak if peak > 0 else 0.0)
        entry: Dict[str, Any] = {
            "miner_hotkey": hotkey,
            "score": round(score, 9),
        }
        if uid is not None:
            try:
                entry["uid"] = int(uid)
            except (TypeError, ValueError):
                pass
        if quality is not None:
            try:
                entry["quality"] = round(clamp01(float(quality)), 6)
            except (TypeError, ValueError):
                pass
        out.append(entry)
    return out


def build_violet_report(
    scores: Sequence[Mapping[str, Any]],
    *,
    epoch: int,
    netuid: int = 39,
    complete: bool = True,
    generated_at: Optional[datetime] = None,
    mechanism: str = SOURCE,
    metadata: Optional[Mapping[str, Any]] = None,
    source: str = SOURCE,
) -> Dict[str, Any]:
    """Build a publisher-ready external score report.

    Default ``source`` remains ``violet_audio`` for backward compatibility.
    Use :func:`build_hybrid_report` for receipt-gated ``cathedral_voice_hybrid``.
    """
    cleaned: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for raw in scores:
        hotkey = str(raw.get("miner_hotkey") or "").strip()
        if not hotkey or hotkey in seen:
            continue
        try:
            score = clamp01(float(raw["score"]))
        except (KeyError, TypeError, ValueError):
            continue
        seen.add(hotkey)
        entry: Dict[str, Any] = {"miner_hotkey": hotkey, "score": score}
        for key in ("uid", "quality", "validity", "tasks_scored", "confidence", "receipt"):
            if key in raw and raw[key] is not None:
                entry[key] = raw[key]
        cleaned.append(entry)

    report: Dict[str, Any] = {
        "source": source or SOURCE,
        "mechanism": mechanism or source or SOURCE,
        "epoch": int(epoch),
        "complete": bool(complete),
        "generated_at": ms_iso(generated_at),
        "netuid": int(netuid),
        "scores": cleaned,
    }
    if metadata:
        report["metadata"] = dict(metadata)
    return report


def build_hybrid_report(
    scores: Sequence[Mapping[str, Any]],
    *,
    epoch: int,
    netuid: int = 39,
    complete: bool = True,
    generated_at: Optional[datetime] = None,
    metadata: Optional[Mapping[str, Any]] = None,
    hmac_secret: str = "",
    ed25519_public_key_hex: str = "",
    require_tdx: bool = False,
    tdx_policy: Any = None,
) -> Dict[str, Any]:
    """Build a ``cathedral_voice_hybrid`` report — fail closed without receipts.

    Each score row must include a ``receipt`` object that passes
    :func:`verify_receipt`. Does **not** alter C/W/Q weight tables; this only
    shapes publisher ingest.
    """
    if not scores:
        raise ValueError("hybrid report requires at least one score row")

    cleaned: List[Dict[str, Any]] = []
    for raw in scores:
        hotkey = str(raw.get("miner_hotkey") or "").strip()
        if not hotkey:
            continue
        receipt_raw = raw.get("receipt")
        result = verify_receipt(
            receipt_raw if isinstance(receipt_raw, Mapping) else None,
            require=True,
            hmac_secret=hmac_secret,
            ed25519_public_key_hex=ed25519_public_key_hex,
            require_tdx=require_tdx,
            tdx_policy=tdx_policy,
        )
        if not result.ok:
            raise ValueError(
                f"hybrid intake rejected {hotkey}: {result.detail}"
            )
        if result.receipt and result.receipt.miner_hotkey != hotkey:
            raise ValueError(
                f"hybrid intake rejected {hotkey}: receipt hotkey mismatch"
            )
        try:
            score = clamp01(float(raw["score"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"hybrid intake rejected {hotkey}: bad score") from exc
        entry: Dict[str, Any] = {
            "miner_hotkey": hotkey,
            "score": score,
            "receipt": result.receipt.to_dict() if result.receipt else receipt_raw,
        }
        for key in ("uid", "quality", "validity", "tasks_scored", "confidence"):
            if key in raw and raw[key] is not None:
                entry[key] = raw[key]
        cleaned.append(entry)

    if not cleaned:
        raise ValueError("hybrid report has no valid receipt-gated rows")

    meta = dict(metadata or {})
    meta["receipt_verified"] = True
    meta["receipt_schema"] = "cathedral_voice_receipt_v1"
    meta["execution_class"] = "hybrid_gpu_preview"
    meta["gpu_attested"] = False
    meta["gpu_memory_confidential"] = False
    if any(
        (isinstance(r.get("receipt"), dict) and r["receipt"].get("controller_measurement"))
        for r in cleaned
    ):
        meta["tdx_controller_measured"] = True

    return build_violet_report(
        cleaned,
        epoch=epoch,
        netuid=netuid,
        complete=complete,
        generated_at=generated_at,
        mechanism=SOURCE_HYBRID,
        source=SOURCE_HYBRID,
        metadata=meta,
    )


class CathedralScoreClient:
    """HTTP client for ``POST /v1/external-scores/violet``."""

    def __init__(self, config: CathedralScoreClientConfig):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_epoch: int = 0

    @property
    def submit_url(self) -> str:
        return self.config.publisher_url.rstrip("/") + SUBMIT_PATH

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.config.timeout_s)
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def next_epoch(self, preferred: Optional[int] = None) -> int:
        epoch = int(preferred if preferred is not None else epoch_from_unix())
        if epoch <= self._last_epoch:
            epoch = self._last_epoch + 1
        return epoch

    async def post_report(self, report: Mapping[str, Any]) -> Dict[str, Any]:
        """POST a report. Returns ``{ok, status, body, error, idempotent}``."""
        if not self.config.enabled:
            return {
                "ok": False,
                "status": None,
                "body": None,
                "error": "cathedral_external_scores_disabled",
                "idempotent": False,
            }
        source = str(report.get("source") or SOURCE)
        token, hmac_secret = self.config.auth_for_source(source)
        if not token and not self.config.dry_run:
            return {
                "ok": False,
                "status": None,
                "body": None,
                "error": "cathedral_external_scores_token_missing",
                "idempotent": False,
            }

        body = canonical_body(report)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "cathedral-voice/violet-subnet",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
            headers["X-Cathedral-External-Token"] = token
        if hmac_secret:
            headers["X-Cathedral-External-Signature"] = compute_hmac_header(
                body, hmac_secret
            )

        if self.config.dry_run:
            logger.info(
                "cathedral dry-run: would POST %s (%d scores, epoch=%s, complete=%s)",
                self.submit_url,
                len(report.get("scores") or []),
                report.get("epoch"),
                report.get("complete"),
            )
            return {
                "ok": True,
                "status": 202,
                "body": {"status": "dry_run", "epoch": report.get("epoch")},
                "error": None,
                "idempotent": False,
            }

        session = await self._session_get()
        try:
            async with session.post(self.submit_url, data=body, headers=headers) as resp:
                raw = await resp.read()
                try:
                    parsed: Any = json.loads(raw.decode("utf-8"))
                except Exception:
                    parsed = {"raw": raw.decode("utf-8", errors="replace")[:500]}
                ok = 200 <= resp.status < 300
                if ok:
                    self._last_epoch = max(
                        self._last_epoch, int(report.get("epoch") or 0)
                    )
                else:
                    logger.error(
                        "cathedral score POST failed status=%s body=%s",
                        resp.status,
                        parsed,
                    )
                return {
                    "ok": ok,
                    "status": resp.status,
                    "body": parsed,
                    "error": None if ok else f"http_{resp.status}",
                    "idempotent": bool(
                        isinstance(parsed, dict) and parsed.get("idempotent")
                    ),
                }
        except Exception as exc:
            logger.error("cathedral score POST error: %s", exc)
            return {
                "ok": False,
                "status": None,
                "body": None,
                "error": f"{type(exc).__name__}: {exc}",
                "idempotent": False,
            }

    async def publish_miner_scores(
        self,
        miners: Sequence[Any],
        *,
        epoch: Optional[int] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        receipts_by_hotkey: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Build and POST Cathedral score report(s).

        Default path remains ``violet_audio``. When ``hybrid_enabled``, also (or
        only) posts ``cathedral_voice_hybrid`` — fail closed without verified
        receipts. Does not alter subnet C/W/Q weight tables.
        """
        results: Dict[str, Any] = {"ok": False, "violet_audio": None, "hybrid": None}
        receipts_by_hotkey = receipts_by_hotkey or {}

        if self.config.hybrid_enabled:
            hybrid = await self.publish_hybrid_miner_scores(
                miners,
                epoch=epoch,
                metadata=metadata,
                receipts_by_hotkey=receipts_by_hotkey,
            )
            results["hybrid"] = hybrid
            if self.config.hybrid_only:
                results["ok"] = bool(hybrid.get("ok"))
                results["error"] = hybrid.get("error")
                results["report"] = hybrid.get("report")
                return results

        if not self.config.hybrid_only:
            scores = scores_from_miner_scores(miners)
            ep = self.next_epoch(epoch)
            report = build_violet_report(
                scores,
                epoch=ep,
                netuid=self.config.netuid,
                complete=True,
                metadata=metadata,
            )
            violet = await self.post_report(report)
            violet["report"] = report
            results["violet_audio"] = violet
            if violet.get("ok"):
                logger.info(
                    "posted violet_audio scores to cathedral: epoch=%s n=%d status=%s",
                    ep,
                    len(scores),
                    violet.get("status"),
                )
            results["ok"] = bool(violet.get("ok"))
            results["error"] = violet.get("error")
            results["report"] = report
            results["status"] = violet.get("status")
            results["body"] = violet.get("body")
        return results

    async def publish_hybrid_miner_scores(
        self,
        miners: Sequence[Any],
        *,
        epoch: Optional[int] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        receipts_by_hotkey: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """POST receipt-gated ``cathedral_voice_hybrid`` scores — fail closed."""
        receipts_by_hotkey = receipts_by_hotkey or {}
        base_scores = scores_from_miner_scores(miners)
        rows: List[Dict[str, Any]] = []
        for entry in base_scores:
            hotkey = entry["miner_hotkey"]
            receipt = receipts_by_hotkey.get(hotkey)
            if not receipt:
                continue
            row = dict(entry)
            row["receipt"] = receipt
            rows.append(row)

        if not rows:
            return {
                "ok": False,
                "status": None,
                "body": None,
                "error": "hybrid_no_verified_receipts",
                "idempotent": False,
            }

        ep = self.next_epoch(epoch)
        try:
            report = build_hybrid_report(
                rows,
                epoch=ep,
                netuid=self.config.netuid,
                complete=True,
                metadata=metadata,
                hmac_secret=self.config.receipt_hmac_secret or self.config.hmac_secret,
                ed25519_public_key_hex=self.config.receipt_ed25519_public_key_hex,
                require_tdx=self.config.require_tdx,
            )
        except ValueError as exc:
            return {
                "ok": False,
                "status": None,
                "body": None,
                "error": str(exc),
                "idempotent": False,
            }

        result = await self.post_report(report)
        result["report"] = report
        if result.get("ok"):
            logger.info(
                "posted cathedral_voice_hybrid scores: epoch=%s n=%d status=%s",
                ep,
                len(rows),
                result.get("status"),
            )
        return result


def config_from_env() -> CathedralScoreClientConfig:
    """Load client config from environment (lazy import of violet helpers)."""
    import os

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

    return CathedralScoreClientConfig(
        enabled=_bool("CATHEDRAL_EXTERNAL_SCORES_ENABLED", False)
        or _bool("CATHEDRAL_VOICE_SCORES_ENABLED", False),
        publisher_url=(
            os.getenv("CATHEDRAL_PUBLISHER_URL")
            or os.getenv("CATHEDRAL_EXTERNAL_SCORES_URL")
            or DEFAULT_PUBLISHER_URL
        ).rstrip("/"),
        token=os.getenv("CATHEDRAL_EXTERNAL_SCORES_TOKEN", "").strip(),
        hmac_secret=os.getenv("CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET", "").strip(),
        hybrid_token=os.getenv(
            "CATHEDRAL_EXTERNAL_SCORES_TOKEN_CATHEDRAL_VOICE_HYBRID", ""
        ).strip(),
        hybrid_hmac_secret=os.getenv(
            "CATHEDRAL_EXTERNAL_SCORES_HMAC_SECRET_CATHEDRAL_VOICE_HYBRID", ""
        ).strip(),
        netuid=_int("CATHEDRAL_EXTERNAL_SCORES_NETUID", 39),
        timeout_s=_float("CATHEDRAL_EXTERNAL_SCORES_TIMEOUT_S", 15.0),
        dry_run=_bool("CATHEDRAL_EXTERNAL_SCORES_DRY_RUN", False),
        hybrid_enabled=_bool("CATHEDRAL_HYBRID_SCORES_ENABLED", False),
        hybrid_only=_bool("CATHEDRAL_HYBRID_ONLY", False),
        receipt_hmac_secret=os.getenv("CATHEDRAL_RECEIPT_HMAC_SECRET", "").strip(),
        receipt_ed25519_public_key_hex=os.getenv(
            "CATHEDRAL_RECEIPT_ED25519_PUBLIC_KEY", ""
        ).strip(),
        require_tdx=_bool("CATHEDRAL_HYBRID_REQUIRE_TDX", False),
    )
