"""SQLite storage. One file, no ORM, stdlib sqlite3 only.

v2: the ledger has three kinds of rows.

    request  -- someone asked Hubby for money
    deposit  -- money moved into the pot through PayPal Orders
    release  -- money moved out of the pot through PayPal Payouts

The pot balance is derived from the ledger, never stored:
    sum(deposit paid) - sum(release released or pending)
"""

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
    recipient_email TEXT    NOT NULL,
    currency        TEXT    NOT NULL DEFAULT 'USD',
    updated_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at              TEXT NOT NULL,
    kind                    TEXT NOT NULL DEFAULT 'request',
    raw_text                TEXT NOT NULL,
    amount_usd              REAL,
    category                TEXT,
    decision                TEXT,
    reason                  TEXT,
    status                  TEXT NOT NULL,
    request_id              INTEGER,
    paypal_order_id         TEXT,
    paypal_capture_id       TEXT,
    paid_at                 TEXT,
    paypal_payout_batch_id  TEXT,
    paypal_payout_item_id   TEXT,
    paypal_transaction_id   TEXT
);

CREATE INDEX IF NOT EXISTS ix_ledger_created ON ledger(created_at);
CREATE INDEX IF NOT EXISTS ix_ledger_kind ON ledger(kind);
CREATE INDEX IF NOT EXISTS ix_ledger_order ON ledger(paypal_order_id);
CREATE INDEX IF NOT EXISTS ix_ledger_batch ON ledger(paypal_payout_batch_id);
"""

DEFAULT_RULES = """One sweet thing per day: bubble tea OR dessert, not both. If one is already paid today, the other is a no.
Bubble tea at most once every 3 days, counted from the last paid bubble tea. No exceptions for "it's on sale" or "it expires".
Ask before buying, not after. Requests for things already bought get declined and logged.
Meals are fine. Fried chicken drumsticks are always fine.
Anything over $20 in one go needs a one-line reason in the request."""

SEED_DEPOSIT_USD = 5.70


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
    def init(self, *, recipient_email: str, seed: bool = True) -> None:
        with self.connect() as conn:
            self._migrate(conn)
            conn.executescript(SCHEMA)
            has_settings = conn.execute("SELECT 1 FROM settings WHERE id = 1").fetchone()
            if not has_settings:
                conn.execute(
                    "INSERT INTO settings (id, rules, recipient_email, currency, updated_at)"
                    " VALUES (1, ?, ?, 'USD', ?)",
                    (DEFAULT_RULES, recipient_email, iso(utcnow())),
                )
            if seed:
                has_rows = conn.execute("SELECT 1 FROM ledger LIMIT 1").fetchone()
                if not has_rows:
                    self._seed_ledger(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """v1 databases are demo data only; rebuild them instead of patching."""
        ledger_columns = {row["name"] for row in conn.execute("PRAGMA table_info(ledger)")}
        if ledger_columns and not {"kind", "request_id", "paypal_payout_batch_id"} <= ledger_columns:
            conn.executescript("DROP TABLE IF EXISTS ledger; DROP TABLE IF EXISTS settings;")
        settings_columns = {row["name"] for row in conn.execute("PRAGMA table_info(settings)")}
        if settings_columns and "recipient_email" not in settings_columns:
            conn.executescript("DROP TABLE IF EXISTS ledger; DROP TABLE IF EXISTS settings;")

    # -- seed ---------------------------------------------------------------
    @staticmethod
    def _seed_rows(now: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        today = now.date()
        yesterday = today - timedelta(days=1)

        def stamp(day) -> str:
            moment = datetime(day.year, day.month, day.day, 9, 0, tzinfo=timezone.utc)
            return iso(min(moment, now))

        releases = [
            {
                "created_at": stamp(yesterday),
                "raw_text": "bubble tea",
                "amount_usd": 4.50,
                "category": "drink",
                "reason": "seed",
            },
            {
                "created_at": stamp(today),
                "raw_text": "Pepsi",
                "amount_usd": 1.20,
                "category": "sweets",
                "reason": "seed",
            },
        ]
        deposit = {
            "created_at": stamp(today),
            "raw_text": "seed deposit",
            "amount_usd": SEED_DEPOSIT_USD,
            "reason": "seed",
        }
        return releases, deposit

    @classmethod
    def _seed_ledger(cls, conn: sqlite3.Connection) -> None:
        releases, deposit = cls._seed_rows(utcnow())
        for row in releases:
            conn.execute(
                "INSERT INTO ledger (created_at, kind, raw_text, amount_usd, category,"
                " decision, reason, status, paid_at)"
                " VALUES (?, 'release', ?, ?, ?, 'approve', ?, 'released', ?)",
                (
                    row["created_at"],
                    row["raw_text"],
                    row["amount_usd"],
                    row["category"],
                    row["reason"],
                    row["created_at"],
                ),
            )
        conn.execute(
            "INSERT INTO ledger (created_at, kind, raw_text, amount_usd, category,"
            " decision, reason, status, paid_at)"
            " VALUES (?, 'deposit', ?, ?, 'other', 'deposit', ?, 'paid', ?)",
            (
                deposit["created_at"],
                deposit["raw_text"],
                deposit["amount_usd"],
                deposit["reason"],
                deposit["created_at"],
            ),
        )

    def refresh_seed_dates(self) -> bool:
        """Keep the seeded demo rows relative to today, even weeks after first boot."""
        now = utcnow()
        releases, deposit = self._seed_rows(now)
        want = {"bubble tea": releases[0], "Pepsi": releases[1], "seed deposit": deposit}
        changed = False
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, raw_text, created_at FROM ledger WHERE reason = 'seed'"
            ).fetchall()
            for row in rows:
                target = want.get(row["raw_text"])
                if target and row["created_at"] != target["created_at"]:
                    conn.execute(
                        "UPDATE ledger SET created_at = ?, paid_at = ? WHERE id = ?",
                        (target["created_at"], target["created_at"], row["id"]),
                    )
                    changed = True
        return changed

    def reset_demo(self, recipient_email: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM ledger")
            conn.execute(
                "UPDATE settings SET rules = ?, recipient_email = ?, updated_at = ? WHERE id = 1",
                (DEFAULT_RULES, recipient_email, iso(utcnow())),
            )
            self._seed_ledger(conn)

    def hours_since_last_entry(self) -> float:
        with self.connect() as conn:
            row = conn.execute("SELECT MAX(created_at) AS latest FROM ledger").fetchone()
        if not row or not row["latest"]:
            return 1e9
        try:
            latest = datetime.fromisoformat(row["latest"])
        except ValueError:
            return 1e9
        return (utcnow() - latest).total_seconds() / 3600

    # -- settings -----------------------------------------------------------
    def get_settings(self) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
        return dict(row) if row else {}

    def save_settings(self, rules: str, recipient_email: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE settings SET rules = ?, recipient_email = ?, updated_at = ? WHERE id = 1",
                (rules, recipient_email, iso(utcnow())),
            )

    # -- ledger writes ------------------------------------------------------
    def add_entry(
        self,
        *,
        kind: str,
        raw_text: str,
        amount_usd: float | None,
        category: str | None,
        decision: str | None,
        reason: str | None,
        status: str,
        request_id: int | None = None,
        paypal_order_id: str | None = None,
        paid_at: str | None = None,
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO ledger (created_at, kind, raw_text, amount_usd, category,"
                " decision, reason, status, request_id, paypal_order_id, paid_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    iso(utcnow()),
                    kind,
                    raw_text,
                    amount_usd,
                    category,
                    decision,
                    reason,
                    status,
                    request_id,
                    paypal_order_id,
                    paid_at,
                ),
            )
            return int(cursor.lastrowid)

    def set_deposit_order(self, entry_id: int, order_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE ledger SET paypal_order_id = ? WHERE id = ?", (order_id, entry_id)
            )

    def mark_deposit(self, entry_id: int, status: str, capture_id: str | None = None) -> None:
        with self.connect() as conn:
            if status == "paid":
                conn.execute(
                    "UPDATE ledger SET status = ?, paypal_capture_id = ?, paid_at = ? WHERE id = ?",
                    (status, capture_id, iso(utcnow()), entry_id),
                )
            else:
                conn.execute("UPDATE ledger SET status = ? WHERE id = ?", (status, entry_id))

    def mark_release(
        self,
        entry_id: int,
        *,
        status: str,
        batch_id: str | None = None,
        item_id: str | None = None,
        transaction_id: str | None = None,
        reason: str | None = None,
        mark_paid_at: bool = False,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE ledger SET status = ?,"
                " paypal_payout_batch_id = COALESCE(?, paypal_payout_batch_id),"
                " paypal_payout_item_id = COALESCE(?, paypal_payout_item_id),"
                " paypal_transaction_id = COALESCE(?, paypal_transaction_id),"
                " reason = COALESCE(?, reason),"
                " paid_at = CASE WHEN ? THEN ? ELSE paid_at END"
                " WHERE id = ?",
                (
                    status,
                    batch_id,
                    item_id,
                    transaction_id,
                    reason,
                    1 if mark_paid_at else 0,
                    iso(utcnow()),
                    entry_id,
                ),
            )

    def add_request_with_release(
        self,
        *,
        raw_text: str,
        amount_usd: float,
        category: str,
        reason: str,
    ) -> tuple[int, int]:
        """Record an approved request and the release row that pays it."""
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO ledger (created_at, kind, raw_text, amount_usd, category,"
                " decision, reason, status) VALUES (?, 'request', ?, ?, ?, 'approve', ?, 'approved')",
                (iso(utcnow()), raw_text, amount_usd, category, reason),
            )
            request_id = int(cursor.lastrowid)
            release = conn.execute(
                "INSERT INTO ledger (created_at, kind, raw_text, amount_usd, category,"
                " decision, reason, status, request_id)"
                " VALUES (?, 'release', ?, ?, ?, 'approve', 'payout pending', 'pending', ?)",
                (iso(utcnow()), raw_text, amount_usd, category, request_id),
            )
            return request_id, int(release.lastrowid)

    # -- reads --------------------------------------------------------------
    def by_order_id(self, order_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM ledger WHERE paypal_order_id = ? AND kind = 'deposit'"
                " ORDER BY id DESC LIMIT 1",
                (order_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM ledger WHERE id = ?", (entry_id,)).fetchone()
        return dict(row) if row else None

    def release_for_request(self, request_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM ledger WHERE kind = 'release' AND request_id = ?"
                " ORDER BY id DESC LIMIT 1",
                (request_id,),
            ).fetchone()
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

    def pot_balance(self) -> float:
        """Deposits in, releases out. Pending payouts already count as spent."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT"
                " COALESCE(SUM(CASE WHEN kind = 'deposit' AND status = 'paid'"
                "                 THEN amount_usd END), 0)"
                " - COALESCE(SUM(CASE WHEN kind = 'release'"
                "                 AND status IN ('released', 'pending', 'unclaimed')"
                "                 THEN amount_usd END), 0) AS balance"
                " FROM ledger"
            ).fetchone()
        return round(float(row["balance"]), 2)

    def month_deposits(self, prefix: str | None = None) -> float:
        prefix = prefix or month_prefix()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount_usd), 0) AS total FROM ledger"
                " WHERE kind = 'deposit' AND status = 'paid' AND paid_at LIKE ?",
                (f"{prefix}%",),
            ).fetchone()
        return round(float(row["total"]), 2)

    def month_releases(self, prefix: str | None = None) -> float:
        prefix = prefix or month_prefix()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount_usd), 0) AS total FROM ledger"
                " WHERE kind = 'release' AND status IN ('released', 'pending', 'unclaimed')"
                " AND created_at LIKE ?",
                (f"{prefix}%",),
            ).fetchone()
        return round(float(row["total"]), 2)

    def category_totals(self, prefix: str | None = None) -> list[dict[str, Any]]:
        """Per-category spend, counting only money actually released."""
        prefix = prefix or month_prefix()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT COALESCE(category, 'other') AS category,"
                "       COALESCE(SUM(amount_usd), 0) AS total, COUNT(*) AS n"
                " FROM ledger WHERE kind = 'release'"
                "   AND status IN ('released', 'pending', 'unclaimed') AND created_at LIKE ?"
                " GROUP BY COALESCE(category, 'other') ORDER BY total DESC",
                (f"{prefix}%",),
            ).fetchall()
        return [dict(r) for r in rows]
