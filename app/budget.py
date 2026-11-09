"""The pot: the only budget Hubby has, and it is hard because it is money.

Balance is derived from the ledger, never stored:

    sum(deposit paid) - sum(release released, pending or unclaimed)

An approved payout that is still pending or unclaimed already counts as gone:
the money left the pot the moment Hubby asked PayPal to send it. A failed one
does not.
"""

from __future__ import annotations

from app.db import Database


def money(amount: float | None) -> str:
    if amount is None:
        return "--"
    return f"${amount:.2f}"


def pot_refusal_reason(balance: float, amount_usd: float) -> str:
    return f"Pot has ${balance:.2f}, you asked for ${amount_usd:.2f}. Fund it first."


def pot_covers(db: Database, amount_usd: float) -> tuple[bool, float]:
    balance = db.pot_balance()
    return round(balance, 2) >= round(amount_usd, 2), balance
