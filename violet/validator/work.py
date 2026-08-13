"""
Ingestion of organic work reports (TDD 7, Work score).

The Work component must count *real* traffic. Two sources are possible and only
one is trustworthy:

* asking the miner how much work it did - self-reported, trivially inflated,
  and therefore not used;
* asking the party that actually dispatched the traffic - the Avoices smart
  router - for a signed, monotonic counter per hotkey. That is what this module
  consumes.

The report is signed by the router's key and validators verify the signature
before ingesting. This does place the router (the subnet owner's backend) in a
position of trust over one of three score components. That is a real
centralisation, and it is stated plainly here rather than glossed: at launch the
Work weight is 10-15%, so the exposure is bounded, and the mitigation as the
weight grows is multiple independent reporters cross-checked against each
other - not a claim that the current design is trustless.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import aiohttp

logger = logging.getLogger("violet.validator.work")


@dataclass
class WorkEntry:
    """Work attributed to one hotkey for one service."""

    hotkey: str
    service: str
    requests: int = 0
    seconds: float = 0.0
    mean_latency_ms: Optional[float] = None


@dataclass
class WorkReport:
    """A signed batch of work counters."""

    report_id: str
    generated_at: float
    #: Start of the period this report covers, unix seconds.
    period_start: float
    entries: List[WorkEntry] = field(default_factory=list)
    signature: str = ""
    signer: str = ""

    @property
    def total_requests(self) -> int:
        return sum(entry.requests for entry in self.entries)


def canonical_payload(report: WorkReport) -> bytes:
    """Bytes that the signature covers.

    Canonical form matters: any ambiguity in serialisation is a signature
    bypass. Sorted keys, no whitespace, entries ordered by (hotkey, service).
    """
    body = {
        "report_id": report.report_id,
        "generated_at": round(report.generated_at, 3),
        "period_start": round(report.period_start, 3),
        "entries": [
            {
                "hotkey": entry.hotkey,
                "service": entry.service,
                "requests": entry.requests,
                "seconds": round(entry.seconds, 3),
                "mean_latency_ms": (
                    round(entry.mean_latency_ms, 1)
                    if entry.mean_latency_ms is not None
                    else None
                ),
            }
            for entry in sorted(report.entries, key=lambda e: (e.hotkey, e.service))
        ],
    }
    return json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")


def sign_report(report: WorkReport, secret: str) -> str:
    """HMAC-SHA256 over the canonical payload."""
    return hmac.new(
        secret.encode("utf-8"), canonical_payload(report), hashlib.sha256
    ).hexdigest()


def verify_report(report: WorkReport, secret: str) -> bool:
    """Constant-time signature check."""
    if not report.signature:
        return False
    expected = sign_report(report, secret)
    return hmac.compare_digest(expected, report.signature)


def parse_report(payload: dict) -> WorkReport:
    entries = [
        WorkEntry(
            hotkey=str(raw.get("hotkey", "")),
            service=str(raw.get("service", "")),
            requests=int(raw.get("requests", 0) or 0),
            seconds=float(raw.get("seconds", 0.0) or 0.0),
            mean_latency_ms=(
                float(raw["mean_latency_ms"])
                if raw.get("mean_latency_ms") is not None
                else None
            ),
        )
        for raw in payload.get("entries", []) or []
        if raw.get("hotkey")
    ]
    return WorkReport(
        report_id=str(payload.get("report_id", "")),
        generated_at=float(payload.get("generated_at", 0.0) or 0.0),
        period_start=float(payload.get("period_start", 0.0) or 0.0),
        entries=entries,
        signature=str(payload.get("signature", "") or ""),
        signer=str(payload.get("signer", "") or ""),
    )


def reject_overlapping_report(
    report: WorkReport,
    *,
    last_period_end: float,
    last_report_id: Optional[str] = None,
    epsilon_s: float = 1.0,
) -> Optional[str]:
    """Return a rejection reason if the report overlaps a prior cursor, else None."""
    if last_report_id and report.report_id == last_report_id:
        return "duplicate report_id (already at cursor)"
    if last_period_end <= 0:
        return None
    # Exclusive window: next batch must start at/after the previous end.
    if report.period_start + epsilon_s < last_period_end:
        return (
            f"overlapping period_start={report.period_start:.0f} "
            f"< cursor={last_period_end:.0f}"
        )
    if report.generated_at + epsilon_s <= last_period_end:
        return (
            f"stale generated_at={report.generated_at:.0f} "
            f"<= cursor={last_period_end:.0f}"
        )
    return None


class WorkReportClient:
    """Pulls signed work reports from the Avoices backend."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        *,
        token: str = "",
        secret: str = "",
    ):
        self.session = session
        self.url = url
        self.token = token
        self.secret = secret
        self._last_report_id: Optional[str] = None

    async def fetch(
        self,
        since: float,
        *,
        timeout_s: float = 20.0,
        last_period_end: float = 0.0,
        last_report_id: Optional[str] = None,
    ) -> Optional[WorkReport]:
        """Fetch work completed since ``since``. Returns ``None`` on any failure.

        ``since`` is an absolute unix timestamp. Overlapping / replayed batches
        relative to ``last_period_end`` are rejected.

        Failure is non-fatal by design: the Work component simply contributes
        nothing that round. Halting a sweep because the product backend is
        briefly unreachable would stop weights being set at all, which is worse.
        """
        if not self.url:
            return None

        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            async with self.session.get(
                self.url,
                params={"since": f"{since:.0f}"},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as response:
                if response.status != 200:
                    logger.warning(
                        "work report endpoint returned HTTP %s", response.status
                    )
                    return None
                payload = await response.json()
        except Exception as exc:
            logger.warning("could not fetch work report: %s", exc)
            return None

        report = parse_report(payload)
        if not report.report_id:
            logger.warning("work report has no report_id; refusing to ingest")
            return None

        if self.secret:
            if not verify_report(report, self.secret):
                logger.error(
                    "work report %s failed signature verification; discarding. "
                    "Work scores will be unaffected this round.",
                    report.report_id,
                )
                return None
        else:
            logger.warning(
                "no VIOLET_WORK_REPORT_TOKEN secret configured: ingesting work "
                "report %s unverified. Set the shared secret before the Work "
                "weight grows beyond the launch phase.",
                report.report_id,
            )

        # A report claiming a period that starts in the future, or counters from
        # long before the window, is either a clock problem or a manipulation.
        now = time.time()
        if report.generated_at > now + 300:
            logger.error("work report %s is timestamped in the future; discarding", report.report_id)
            return None

        overlap = reject_overlapping_report(
            report,
            last_period_end=last_period_end,
            last_report_id=last_report_id or self._last_report_id,
        )
        if overlap:
            logger.warning("rejecting work report %s: %s", report.report_id, overlap)
            return None

        self._last_report_id = report.report_id
        logger.info(
            "ingested work report %s: %d entries, %d requests",
            report.report_id, len(report.entries), report.total_requests,
        )
        return report


def to_store_rows(report: WorkReport) -> List[Dict[str, object]]:
    """Flatten a report into rows for :meth:`ValidatorStore.record_work`."""
    return [
        {
            "hotkey": entry.hotkey,
            "service": entry.service,
            "requests": entry.requests,
            "seconds": entry.seconds,
            "latency_ms": entry.mean_latency_ms,
            "source": "organic",
            "report_id": report.report_id,
            "at": report.generated_at or time.time(),
        }
        for entry in report.entries
    ]
