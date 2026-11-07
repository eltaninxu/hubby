"""The hard cap. Plain Python, no model involved -- this is the whole point."""

from __future__ import annotations

from app.db import Database


def money(amount: float | None) -> str:
    if amount is None:
        return "--"
    return f"${amount:.2f}"


def cap_label(cap: float) -> str:
    """Render 7.0 as $7, 120.0 as $120, 7.5 as $7.5 (matches the demo copy)."""
    return f"${cap:g}"


def hard_cap_reason(used: float, cap: float) -> str:
    return f"Hard cap: ${used:.2f} of {cap_label(cap)} used this month"


def exceeds_cap(db: Database, amount_usd: float, cap: float) -> tuple[bool, float]:
    used = db.month_paid_usd()
    return round(used + amount_usd, 2) > round(cap, 2), used
