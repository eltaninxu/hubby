"""SQLite storage. One file, no ORM, stdlib sqlite3 only."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    id              INTEGER PRIMARY KEY CHECK (id = 1),
    rules           TEXT    NOT NULL,
    monthly_cap_usd REAL    NOT NULL,
    currency        TEXT    NOT NULL DEFAULT 'USD',
    updated_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT NOT NULL,
    raw_text          TEXT NOT NULL,
    amount_usd        REAL,
    category          TEXT,
    decision          TEXT NOT NULL,
    reason            TEXT,
    status            TEXT NOT NULL,
    paypal_order_id   TEXT,
    paypal_capture_id TEXT,
    paid_at           TEXT
);

CREATE INDEX IF NOT EXISTS ix_ledger_created ON ledger(created_at);
CREATE INDEX IF NOT EXISTS ix_ledger_order ON ledger(paypal_order_id);
"""

DEFAULT_RULES = """One sweet thing per day: bubble tea OR dessert, not both. If one is already paid today, the other is a no.
Bubble tea at most once every 3 days, counted from the last paid bubble tea. No exceptions for "it's on sale" or "it expires".
Ask before buying, not after. Requests for things already bought get declined and logged.
Meals are fine. Fried chicken drumsticks are always fine.
Anything over $20 in one go needs a one-line reason in the request."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


def today_iso() -> str:
    return utcnow().date().isoformat()


def month_prefix(moment: datetime | None = None) -> str:
    return (moment or utcnow()).strftime("%Y-%m")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- setup --------------------------------------------------------------
    def init(self, monthly_cap_usd: float, *, seed: bool = True) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            has_settings = conn.execute("SELECT 1 FROM settings WHERE id = 1").fetchone()
            if not has_settings:
                conn.execute(
                    "INSERT INTO settings (id, rules, monthly_cap_usd, currency, updated_at)"
                    " VALUES (1, ?, ?, 'USD', ?)",
                    (DEFAULT_RULES, monthly_cap_usd, iso(utcnow())),
                )
            if seed:
                has_rows = conn.execute("SELECT 1 FROM ledger LIMIT 1").fetchone()
                if not has_rows:
                    self._seed_ledger(conn)

    @staticmethod
    def _seed_ledger(conn: sqlite3.Connection) -> None:
        now = utcnow()
        today = now.date()
        yesterday = today - timedelta(days=1)

        def paid_at(day) -> datetime:
            stamp = datetime(day.year, day.month, day.day, 9, 0, tzinfo=timezone.utc)
            return min(stamp, now)

        rows = [
            (iso(paid_at(yesterday)), "bubble tea", 4.50, "drink", "Seeded example: yesterday's bubble tea."),
            (iso(paid_at(today)), "Pepsi", 1.20, "sweets", "Seeded example: a sweet already paid today."),
        ]
        for created_at, raw, amount, category, reason in rows:
            conn.execute(
                "INSERT INTO ledger (created_at, raw_text, amount_usd, category, decision,"
                " reason, status, paid_at) VALUES (?, ?, ?, ?, 'approve', ?, 'paid', ?)",
                (created_at, raw, amount, category, reason, created_at),
            )

    # -- settings -----------------------------------------------------------
    def get_settings(self) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
        return dict(row) if row else {}

    def save_settings(self, rules: str, monthly_cap_usd: float) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE settings SET rules = ?, monthly_cap_usd = ?, updated_at = ? WHERE id = 1",
                (rules, monthly_cap_usd, iso(utcnow())),
            )

    # -- ledger -------------------------------------------------------------
    def add_entry(
        self,
        *,
        raw_text: str,
        amount_usd: float | None,
        category: str | None,
        decision: str,
        reason: str,
        status: str,
        paypal_order_id: str | None = None,
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO ledger (created_at, raw_text, amount_usd, category, decision,"
                " reason, status, paypal_order_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    iso(utcnow()),
                    raw_text,
                    amount_usd,
                    category,
                    decision,
                    reason,
                    status,
                    paypal_order_id,
                ),
            )
            return int(cursor.lastrowid)

    def set_order_id(self, entry_id: int, order_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE ledger SET paypal_order_id = ? WHERE id = ?", (order_id, entry_id)
            )

    def by_order_id(self, order_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM ledger WHERE paypal_order_id = ? ORDER BY id DESC LIMIT 1",
                (order_id,),
            ).fetchone()
        return dict(row) if row else None

    def mark_paid(self, entry_id: int, capture_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE ledger SET status = 'paid', paypal_capture_id = ?, paid_at = ?"
                " WHERE id = ?",
                (capture_id, iso(utcnow()), entry_id),
            )

    def mark_capture_failed(self, entry_id: int, reason: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE ledger SET status = 'capture_failed', reason = ? WHERE id = ?",
                (reason, entry_id),
            )

    def mark_cancelled(self, entry_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE ledger SET status = 'cancelled' WHERE id = ?", (entry_id,)
            )

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM ledger WHERE id = ?", (entry_id,)).fetchone()
        return dict(row) if row else None

    def recent(self, limit: int = 5) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ledger ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def all_entries(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM ledger ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    def since(self, days: int = 30) -> list[dict[str, Any]]:
        cutoff = iso(utcnow() - timedelta(days=days))
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM ledger WHERE created_at >= ? ORDER BY created_at ASC",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    def month_paid_usd(self, prefix: str | None = None) -> float:
        prefix = prefix or month_prefix()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount_usd), 0) AS total FROM ledger"
                " WHERE status = 'paid' AND paid_at LIKE ?",
                (f"{prefix}%",),
            ).fetchone()
        return round(float(row["total"]), 2)

    def category_totals(self, prefix: str | None = None) -> list[dict[str, Any]]:
        prefix = prefix or month_prefix()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT COALESCE(category, 'other') AS category,"
                "       COALESCE(SUM(amount_usd), 0) AS total, COUNT(*) AS n"
                " FROM ledger WHERE status = 'paid' AND paid_at LIKE ?"
                " GROUP BY COALESCE(category, 'other') ORDER BY total DESC",
                (f"{prefix}%",),
            ).fetchall()
        return [dict(r) for r in rows]
