"""The hard cap. Plain Python, no model involved -- this is the whole point.

Two checks, because one is not enough:

* ``over_paid``    -- money already captured this month + this request.
* ``over_reserved`` -- paid **plus orders already sitting in PayPal's hands**
  (approved, button shown, not captured yet). Without this, two pending orders
  can both look cheap on their own and together blow the monthly cap.

And the capture leg re-checks ``over_paid`` again, because the cap can be
lowered between approval and payment.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.db import Database


def money(amount: float | None) -> str:
    if amount is None:
        return "--"
    return f"${amount:.2f}"


def cap_label(cap: float) -> str:
    """Render 7.0 as $7, 120.0 as $120 (matches the demo copy)."""
    return f"${cap:g}"


def hard_cap_reason(used: float, cap: float) -> str:
    return f"Hard cap: ${used:.2f} of {cap_label(cap)} used this month"


def reserved_reason(committed: float, cap: float) -> str:
    return f"Hard cap: ${committed:.2f} of {cap_label(cap)} committed this month"


@dataclass(frozen=True)
class CapCheck:
    paid: float
    pending: float
    over_paid: bool
    over_reserved: bool

    @property
    def committed(self) -> float:
        return round(self.paid + self.pending, 2)


def assess_cap(db: Database, amount_usd: float, cap: float) -> CapCheck:
    paid = db.month_paid_usd()
    pending = db.month_pending_usd()
    limit = round(cap, 2)
    return CapCheck(
        paid=paid,
        pending=pending,
        over_paid=round(paid + amount_usd, 2) > limit,
        over_reserved=round(paid + pending + amount_usd, 2) > limit,
    )


def exceeds_cap(db: Database, amount_usd: float, cap: float) -> tuple[bool, float]:
    """Kept for the simple call sites: is this request over the paid budget?"""
    check = assess_cap(db, amount_usd, cap)
    return check.over_paid, check.paid
