"""
Work receipts: the router's record of what each miner actually served.

This is the input to the Work component of the incentive mechanism (TDD 7). It
is written by the party that dispatched the traffic, never by the miner, so a
miner cannot inflate its own work.

Receipts are aggregated into signed reports that validators pull. Aggregation
rather than raw receipts because a week of production traffic is millions of
rows and validators need counters, not a request log - and a request log would
leak Avoices usage patterns to anyone running a validator.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger("violet.router.receipts")


def _deterministic_report_id(since: float, until: float, entries) -> str:
    """Stable ID for a reporting window so duplicate fetches dedupe correctly."""
    digest = hashlib.sha256()
    digest.update(f"{int(since)}-{int(until)}".encode())
    for entry in sorted(entries, key=lambda e: (e.hotkey, e.service)):
        digest.update(
            f"{entry.hotkey}:{entry.service}:{entry.requests}:{entry.seconds:.3f}".encode()
        )
    return digest.hexdigest()[:32]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    hotkey      TEXT    NOT NULL,
    uid         INTEGER,
    at          REAL    NOT NULL,
    service     TEXT    NOT NULL,
    ok          INTEGER NOT NULL,
    latency_ms  REAL,
    first_byte_ms REAL,
    -- Audio seconds processed (ASR) or synthesised (TTS). The unit the Work
    -- score calls "streaming minutes".
    seconds     REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_receipts_at ON receipts (at);
CREATE INDEX IF NOT EXISTS idx_receipts_hotkey ON receipts (hotkey, at);
"""


@dataclass
class Receipt:
    """One completed unit of work."""

    hotkey: str
    service: str
    ok: bool
    seconds: float = 0.0
    latency_ms: Optional[float] = None
    first_byte_ms: Optional[float] = None
    uid: Optional[int] = None
    at: Optional[float] = None


class ReceiptLedger:
    """Durable, append-only record of served work."""

    def __init__(self, path: str):
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._pending: List[Receipt] = []

    def close(self) -> None:
        self.flush()
        self._conn.close()

    def record(self, receipt: Receipt) -> None:
        """Buffer a receipt.

        Buffered rather than written through: this sits on the hot path of every
        Avoices request, and a synchronous fsync per request would add latency
        to the product to serve a scoring concern. Losing a few seconds of
        receipts on an unclean shutdown costs a miner a negligible slice of a
        seven-day window.
        """
        # Stamped now, not at flush time. Buffering must not move a receipt's
        # timestamp forward, or work completed just before a report is built
        # lands after the report's end boundary and is silently dropped.
        if receipt.at is None:
            receipt.at = time.time()
        self._pending.append(receipt)
        if len(self._pending) >= 32:
            self.flush()

    def flush(self) -> int:
        if not self._pending:
            return 0
        batch, self._pending = self._pending, []
        try:
            self._conn.executemany(
                """
                INSERT INTO receipts
                    (hotkey, uid, at, service, ok, latency_ms, first_byte_ms, seconds)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        r.hotkey, r.uid, r.at or time.time(), r.service, int(r.ok),
                        r.latency_ms, r.first_byte_ms, r.seconds,
                    )
                    for r in batch
                ],
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            logger.error("could not flush %d receipts: %s", len(batch), exc)
            self._pending = batch + self._pending
            return 0
        return len(batch)

    def aggregate(self, since: float, until: Optional[float] = None) -> List[Dict[str, object]]:
        """Per-hotkey, per-service counters over a period.

        Only successful work is counted: a failed request is not service
        delivered, and counting it would pay a miner for failing.
        """
        self.flush()
        until = until or time.time()
        cursor = self._conn.execute(
            """
            SELECT hotkey,
                   service,
                   COUNT(*)            AS requests,
                   COALESCE(SUM(seconds), 0) AS seconds,
                   AVG(first_byte_ms)  AS mean_first_byte_ms
            FROM receipts
            WHERE at >= ? AND at < ? AND ok = 1
            GROUP BY hotkey, service
            ORDER BY hotkey, service
            """,
            (since, until),
        )
        return [
            {
                "hotkey": row["hotkey"],
                "service": row["service"],
                "requests": int(row["requests"]),
                "seconds": round(float(row["seconds"] or 0.0), 3),
                "mean_latency_ms": (
                    round(float(row["mean_first_byte_ms"]), 1)
                    if row["mean_first_byte_ms"] is not None
                    else None
                ),
            }
            for row in cursor.fetchall()
        ]

    def build_report(
        self, since: float, secret: str = "", signer: str = ""
    ) -> Dict[str, object]:
        """Build a signed work report for validator consumption."""
        from ..validator.work import WorkEntry, WorkReport, sign_report

        # Flush before fixing the end boundary, so buffered receipts fall inside
        # the period they were actually served in.
        self.flush()
        until = time.time()
        entries_raw = self.aggregate(since, until)
        entries = [
            WorkEntry(
                hotkey=str(row["hotkey"]),
                service=str(row["service"]),
                requests=int(row["requests"]),
                seconds=float(row["seconds"]),
                mean_latency_ms=row["mean_latency_ms"],
            )
            for row in entries_raw
        ]

        report = WorkReport(
            report_id=_deterministic_report_id(since, until, entries),
            generated_at=until,
            period_start=since,
            entries=entries,
            signer=signer,
        )
        if secret:
            report.signature = sign_report(report, secret)
        else:
            logger.warning(
                "serving an unsigned work report: set VIOLET_WORK_REPORT_SIGNING_KEY "
                "so validators can verify it"
            )

        return {
            "report_id": report.report_id,
            "generated_at": report.generated_at,
            "period_start": report.period_start,
            "signer": report.signer,
            "signature": report.signature,
            "entries": [
                {
                    "hotkey": entry.hotkey,
                    "service": entry.service,
                    "requests": entry.requests,
                    "seconds": round(entry.seconds, 3),
                    "mean_latency_ms": entry.mean_latency_ms,
                }
                for entry in report.entries
            ],
        }

    def stats(self, since: float) -> Dict[str, object]:
        self.flush()
        cursor = self._conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(ok)  AS succeeded,
                   COALESCE(SUM(seconds), 0) AS seconds,
                   COUNT(DISTINCT hotkey) AS miners
            FROM receipts WHERE at >= ?
            """,
            (since,),
        )
        row = cursor.fetchone()
        total = int(row["total"] or 0)
        succeeded = int(row["succeeded"] or 0)
        return {
            "requests": total,
            "succeeded": succeeded,
            "failed": total - succeeded,
            "seconds_served": round(float(row["seconds"] or 0.0), 1),
            "miners_used": int(row["miners"] or 0),
        }

    def prune(self, older_than: float) -> int:
        cursor = self._conn.execute("DELETE FROM receipts WHERE at < ?", (older_than,))
        self._conn.commit()
        return cursor.rowcount or 0
