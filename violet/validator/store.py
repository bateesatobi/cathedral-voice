"""
Persistence for the rolling scoring window.

TDD 7 scores over six to seven days and TDD 9.2 relies on that window to dilute
short bursts. A validator that lost its history on restart would reset every
miner's rolling average, which is both unfair to steady operators and an
exploitable pattern - restart the validator, and a burst counts for everything.
So observations are durable.

SQLite rather than a service: a validator should be one process with one file,
and the write volume (a few rows per miner per sweep) is nowhere near needing
more.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Sequence

logger = logging.getLogger("violet.validator.store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    hotkey      TEXT    NOT NULL,
    uid         INTEGER,
    at          REAL    NOT NULL,
    kind        TEXT    NOT NULL,
    ok          INTEGER NOT NULL,
    latency_ms  REAL,
    first_byte_ms REAL,
    quality     REAL,
    wer         REAL,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_obs_hotkey_at ON observations (hotkey, at);
CREATE INDEX IF NOT EXISTS idx_obs_at ON observations (at);

CREATE TABLE IF NOT EXISTS capacity_samples (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    hotkey         TEXT    NOT NULL,
    at             REAL    NOT NULL,
    capacity_units REAL    NOT NULL,
    healthy        INTEGER NOT NULL,
    gpu_json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_cap_hotkey_at ON capacity_samples (hotkey, at);

CREATE TABLE IF NOT EXISTS work_credits (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    hotkey     TEXT    NOT NULL,
    at         REAL    NOT NULL,
    service    TEXT    NOT NULL,
    requests   INTEGER NOT NULL DEFAULT 0,
    seconds    REAL    NOT NULL DEFAULT 0,
    latency_ms REAL,
    source     TEXT    NOT NULL DEFAULT 'organic',
    report_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_work_hotkey_at ON work_credits (hotkey, at);
-- A work report may be fetched more than once (retry, restart); the unique
-- index makes ingestion idempotent so work cannot be double-counted.
CREATE UNIQUE INDEX IF NOT EXISTS idx_work_report ON work_credits (report_id, hotkey, service)
    WHERE report_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS qualifications (
    hotkey       TEXT PRIMARY KEY,
    endpoint     TEXT,
    passed       INTEGER NOT NULL,
    evaluated_at REAL    NOT NULL,
    detail       TEXT
);

CREATE TABLE IF NOT EXISTS scores (
    hotkey     TEXT NOT NULL,
    at         REAL NOT NULL,
    uid        INTEGER,
    capacity   REAL NOT NULL DEFAULT 0,
    work       REAL NOT NULL DEFAULT 0,
    quality    REAL NOT NULL DEFAULT 0,
    raw        REAL NOT NULL DEFAULT 0,
    smoothed   REAL NOT NULL DEFAULT 0,
    final      REAL NOT NULL DEFAULT 0,
    notes      TEXT,
    PRIMARY KEY (hotkey, at)
);
CREATE INDEX IF NOT EXISTS idx_scores_at ON scores (at);

CREATE TABLE IF NOT EXISTS coldkey_strikes (
    coldkey      TEXT PRIMARY KEY,
    strikes      INTEGER NOT NULL DEFAULT 0,
    last_strike  REAL,
    excluded_until REAL,
    blacklisted  INTEGER NOT NULL DEFAULT 0,
    detail       TEXT
);
"""


@dataclass
class Observation:
    """One probe outcome, as persisted."""

    hotkey: str
    at: float
    kind: str
    ok: bool
    latency_ms: Optional[float] = None
    first_byte_ms: Optional[float] = None
    quality: Optional[float] = None
    wer: Optional[float] = None
    detail: str = ""
    uid: Optional[int] = None


@dataclass
class WindowStats:
    """Aggregates for one miner over the rolling window."""

    hotkey: str
    samples: int = 0
    successes: int = 0
    mean_quality: Optional[float] = None
    mean_wer: Optional[float] = None
    mean_first_byte_ms: Optional[float] = None
    p95_first_byte_ms: Optional[float] = None
    #: Fraction of health observations that succeeded.
    availability: float = 0.0
    #: Mean of capacity_units over healthy samples - capacity kept *online*,
    #: not capacity merely claimed once.
    mean_online_capacity: float = 0.0
    capacity_samples: int = 0
    requests: int = 0
    work_seconds: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.successes / self.samples if self.samples else 0.0


class ValidatorStore:
    """Durable observation history backing the rolling window."""

    def __init__(self, path: str):
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL so the dashboard can read while the evaluation loop writes.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # -- writes ------------------------------------------------------------

    def record_observations(self, observations: Sequence[Observation]) -> None:
        if not observations:
            return
        with self._tx() as conn:
            conn.executemany(
                """
                INSERT INTO observations
                    (hotkey, uid, at, kind, ok, latency_ms, first_byte_ms, quality, wer, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        o.hotkey, o.uid, o.at, o.kind, int(o.ok),
                        o.latency_ms, o.first_byte_ms, o.quality, o.wer, o.detail[:500],
                    )
                    for o in observations
                ],
            )

    def record_capacity(
        self, hotkey: str, capacity_units: float, healthy: bool, gpus: Optional[List[dict]] = None
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO capacity_samples (hotkey, at, capacity_units, healthy, gpu_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (hotkey, time.time(), capacity_units, int(healthy), json.dumps(gpus or [])),
            )

    def record_work(
        self,
        hotkey: str,
        service: str,
        *,
        requests: int = 0,
        seconds: float = 0.0,
        latency_ms: Optional[float] = None,
        source: str = "organic",
        report_id: Optional[str] = None,
        at: Optional[float] = None,
    ) -> bool:
        """Record completed work. Returns False when already ingested."""
        try:
            with self._tx() as conn:
                conn.execute(
                    """
                    INSERT INTO work_credits
                        (hotkey, at, service, requests, seconds, latency_ms, source, report_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        hotkey, at or time.time(), service, requests, seconds,
                        latency_ms, source, report_id,
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            logger.debug("work report %s for %s already ingested", report_id, hotkey)
            return False

    def record_qualification(
        self, hotkey: str, endpoint: str, passed: bool, detail: str, at: Optional[float] = None
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO qualifications (hotkey, endpoint, passed, evaluated_at, detail)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(hotkey) DO UPDATE SET
                    endpoint=excluded.endpoint,
                    passed=excluded.passed,
                    evaluated_at=excluded.evaluated_at,
                    detail=excluded.detail
                """,
                (hotkey, endpoint, int(passed), at or time.time(), detail[:1000]),
            )

    def record_score(
        self,
        hotkey: str,
        *,
        uid: Optional[int],
        capacity: float,
        work: float,
        quality: float,
        raw: float,
        smoothed: float,
        final: float,
        notes: str = "",
        at: Optional[float] = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO scores
                    (hotkey, at, uid, capacity, work, quality, raw, smoothed, final, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (hotkey, at or time.time(), uid, capacity, work, quality, raw, smoothed, final, notes[:500]),
            )

    # -- reads -------------------------------------------------------------

    def qualification(self, hotkey: str) -> Optional[sqlite3.Row]:
        cursor = self._conn.execute(
            "SELECT * FROM qualifications WHERE hotkey = ?", (hotkey,)
        )
        return cursor.fetchone()

    def health_history(self, hotkey: str, since: float) -> List[tuple]:
        cursor = self._conn.execute(
            """
            SELECT at, ok FROM observations
            WHERE hotkey = ? AND kind = 'health' AND at >= ?
            ORDER BY at ASC
            """,
            (hotkey, since),
        )
        return [(row["at"], bool(row["ok"])) for row in cursor.fetchall()]

    def previous_score(self, hotkey: str) -> Optional[float]:
        """Most recent smoothed score, used as the smoothing prior."""
        cursor = self._conn.execute(
            "SELECT smoothed FROM scores WHERE hotkey = ? ORDER BY at DESC LIMIT 1",
            (hotkey,),
        )
        row = cursor.fetchone()
        return float(row["smoothed"]) if row else None

    def window_stats(self, hotkey: str, since: float) -> WindowStats:
        """Aggregate every signal for one miner over the window."""
        stats = WindowStats(hotkey=hotkey)

        cursor = self._conn.execute(
            """
            SELECT kind, ok, first_byte_ms, quality, wer
            FROM observations WHERE hotkey = ? AND at >= ?
            """,
            (hotkey, since),
        )
        rows = cursor.fetchall()

        qualities: List[float] = []
        wers: List[float] = []
        first_bytes: List[float] = []
        health_total = health_ok = 0

        for row in rows:
            if row["kind"] == "health":
                health_total += 1
                health_ok += int(row["ok"])
                continue
            stats.samples += 1
            stats.successes += int(row["ok"])
            if row["quality"] is not None:
                qualities.append(float(row["quality"]))
            if row["wer"] is not None and row["wer"] >= 0:
                wers.append(float(row["wer"]))
            if row["first_byte_ms"] is not None and row["ok"]:
                first_bytes.append(float(row["first_byte_ms"]))

        stats.mean_quality = sum(qualities) / len(qualities) if qualities else None
        stats.mean_wer = sum(wers) / len(wers) if wers else None
        if first_bytes:
            first_bytes.sort()
            stats.mean_first_byte_ms = sum(first_bytes) / len(first_bytes)
            # p95 rather than max: one network hiccup should not define a
            # miner's latency profile for a week.
            index = min(len(first_bytes) - 1, int(len(first_bytes) * 0.95))
            stats.p95_first_byte_ms = first_bytes[index]
        stats.availability = (health_ok / health_total) if health_total else 0.0

        cursor = self._conn.execute(
            """
            SELECT capacity_units, healthy FROM capacity_samples
            WHERE hotkey = ? AND at >= ?
            """,
            (hotkey, since),
        )
        capacity_rows = cursor.fetchall()
        if capacity_rows:
            stats.capacity_samples = len(capacity_rows)
            # Capacity counts only while healthy: TDD 7 rewards "hardware kept
            # online and healthy", not hardware that merely exists.
            online = [
                float(row["capacity_units"]) for row in capacity_rows if row["healthy"]
            ]
            stats.mean_online_capacity = (
                sum(online) / len(capacity_rows) if online else 0.0
            )

        cursor = self._conn.execute(
            """
            SELECT COALESCE(SUM(requests), 0) AS requests,
                   COALESCE(SUM(seconds), 0)  AS seconds
            FROM work_credits WHERE hotkey = ? AND at >= ?
            """,
            (hotkey, since),
        )
        work_row = cursor.fetchone()
        stats.requests = int(work_row["requests"] or 0)
        stats.work_seconds = float(work_row["seconds"] or 0.0)

        return stats

    def latest_scores(self, limit: int = 256) -> List[sqlite3.Row]:
        cursor = self._conn.execute(
            """
            SELECT s.* FROM scores s
            INNER JOIN (
                SELECT hotkey, MAX(at) AS at FROM scores GROUP BY hotkey
            ) latest ON latest.hotkey = s.hotkey AND latest.at = s.at
            ORDER BY s.final DESC LIMIT ?
            """,
            (limit,),
        )
        return cursor.fetchall()

    def score_history(self, hotkey: str, since: float) -> List[sqlite3.Row]:
        cursor = self._conn.execute(
            "SELECT * FROM scores WHERE hotkey = ? AND at >= ? ORDER BY at ASC",
            (hotkey, since),
        )
        return cursor.fetchall()

    # -- coldkey strikes (TDD 9.1) ----------------------------------------

    def coldkey_state(self, coldkey: str) -> Dict[str, object]:
        cursor = self._conn.execute(
            "SELECT * FROM coldkey_strikes WHERE coldkey = ?", (coldkey,)
        )
        row = cursor.fetchone()
        if not row:
            return {"strikes": 0, "excluded_until": 0.0, "blacklisted": False, "detail": ""}
        return {
            "strikes": int(row["strikes"]),
            "excluded_until": float(row["excluded_until"] or 0.0),
            "blacklisted": bool(row["blacklisted"]),
            "detail": row["detail"] or "",
        }

    def add_coldkey_strike(
        self, coldkey: str, *, excluded_until: float = 0.0, blacklist: bool = False, detail: str = ""
    ) -> int:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO coldkey_strikes (coldkey, strikes, last_strike, excluded_until, blacklisted, detail)
                VALUES (?, 1, ?, ?, ?, ?)
                ON CONFLICT(coldkey) DO UPDATE SET
                    strikes = coldkey_strikes.strikes + 1,
                    last_strike = excluded.last_strike,
                    excluded_until = MAX(coldkey_strikes.excluded_until, excluded.excluded_until),
                    blacklisted = MAX(coldkey_strikes.blacklisted, excluded.blacklisted),
                    detail = excluded.detail
                """,
                (coldkey, time.time(), excluded_until, int(blacklist), detail[:500]),
            )
        return int(self.coldkey_state(coldkey)["strikes"])

    def clear_coldkey_strikes(self, coldkey: str) -> None:
        """Reset strikes after a clean window, so a single mistake is not permanent."""
        with self._tx() as conn:
            conn.execute(
                "UPDATE coldkey_strikes SET strikes = 0, excluded_until = 0 "
                "WHERE coldkey = ? AND blacklisted = 0",
                (coldkey,),
            )

    def all_coldkey_states(self) -> List[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM coldkey_strikes ORDER BY strikes DESC"
        ).fetchall()

    # -- maintenance -------------------------------------------------------

    def prune(self, older_than: float) -> int:
        """Drop observations outside the retention horizon."""
        with self._tx() as conn:
            cursor = conn.execute("DELETE FROM observations WHERE at < ?", (older_than,))
            deleted = cursor.rowcount or 0
            conn.execute("DELETE FROM capacity_samples WHERE at < ?", (older_than,))
            conn.execute("DELETE FROM work_credits WHERE at < ?", (older_than,))
            conn.execute("DELETE FROM scores WHERE at < ?", (older_than,))
        return deleted
