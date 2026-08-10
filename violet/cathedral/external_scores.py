"""Cathedral SN39 external-score ingest client (violet_audio).

Cathedral thin validators do **not** accept scores directly. They only fetch a
signed weight vector from the publisher and call ``set_weights``. Violet /
cathedral-voice posts score reports here:

    POST {publisher}/v1/external-scores/violet
    source = violet_audio
    complete = true   # required for blending (incomplete reports are ignored)

See cathedralai/cathedral-validator docs/VIOLET_EXTERNAL_SCORES.md.
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

logger = logging.getLogger("violet.cathedral.external_scores")

SOURCE = "violet_audio"
SUBMIT_PATH = "/v1/external-scores/violet"
DEFAULT_PUBLISHER_URL = "https://api.cathedral.computer"


@dataclass
class CathedralScoreClientConfig:
    """Env-backed settings for posting Violet scores into Cathedral."""

    enabled: bool = False
    publisher_url: str = DEFAULT_PUBLISHER_URL
    token: str = ""
    hmac_secret: str = ""
    netuid: int = 39
    timeout_s: float = 15.0
    #: When true, still build/post reports but log instead of HTTP POST.
    dry_run: bool = False


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
) -> Dict[str, Any]:
    """Build a publisher-ready ``violet_audio`` report.

    ``complete=True`` is required for the publisher to *blend* the snapshot.
    Incomplete reports may store but never enter the signed weight vector.
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
        for key in ("uid", "quality", "validity", "tasks_scored", "confidence"):
            if key in raw and raw[key] is not None:
                entry[key] = raw[key]
        cleaned.append(entry)

    report: Dict[str, Any] = {
        "source": SOURCE,
        "mechanism": mechanism or SOURCE,
        "epoch": int(epoch),
        "complete": bool(complete),
        "generated_at": ms_iso(generated_at),
        "netuid": int(netuid),
        "scores": cleaned,
    }
    if metadata:
        report["metadata"] = dict(metadata)
    return report


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
        if not self.config.token and not self.config.dry_run:
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
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"
            headers["X-Cathedral-External-Token"] = self.config.token
        if self.config.hmac_secret:
            headers["X-Cathedral-External-Signature"] = compute_hmac_header(
                body, self.config.hmac_secret
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
    ) -> Dict[str, Any]:
        """Build a complete violet_audio report from MinerScore rows and POST it."""
        scores = scores_from_miner_scores(miners)
        ep = self.next_epoch(epoch)
        report = build_violet_report(
            scores,
            epoch=ep,
            netuid=self.config.netuid,
            complete=True,
            metadata=metadata,
        )
        result = await self.post_report(report)
        result["report"] = report
        if result.get("ok"):
            logger.info(
                "posted violet_audio scores to cathedral: epoch=%s n=%d status=%s",
                ep,
                len(scores),
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
        netuid=_int("CATHEDRAL_EXTERNAL_SCORES_NETUID", 39),
        timeout_s=_float("CATHEDRAL_EXTERNAL_SCORES_TIMEOUT_S", 15.0),
        dry_run=_bool("CATHEDRAL_EXTERNAL_SCORES_DRY_RUN", False),
    )
