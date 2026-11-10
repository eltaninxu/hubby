"""The pot: the only budget Hubby has, and it is hard because it is money.

Balance is derived from the ledger, never stored:

    sum(deposit paid) - sum(release released, pending or unclaimed)

An approved payout that is still pending or unclaimed already counts as gone:
the money left the pot the moment Hubby asked PayPal to send it. A failed one
does not.
"""

from __future__ import annotations

import math
import re

from app.db import Database

# "$6", "$4.50", "$ 15", "$1,200": a dollar sign, then digits (optionally with
# thousands commas and at most two decimals), not glued to a letter or digit.
_DOLLAR_AMOUNT = re.compile(
    r"\$\s*((?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]{1,2})?)(?!\w)"
)


def money(amount: float | None) -> str:
    if amount is None:
        return "--"
    return f"${amount:.2f}"


def pot_refusal_reason(balance: float, amount_usd: float) -> str:
    return f"Pot has ${balance:.2f}, you asked for ${amount_usd:.2f}. Fund it first."


def stated_amount(text: str) -> float | None:
    """The price, only when the sentence states it unambiguously.

    Exactly one amount written with a dollar sign, it is the last thing in the
    sentence, and there is no other digit anywhere in the text. "2 teas at $4
    each", "was $20 now $6" and "$5 off coupon" are not unambiguous. Anything
    else returns None and is left to the model. Only used to decline early, so
    None is always the safe answer.
    """
    if text.count("$") != 1:
        return None
    match = _DOLLAR_AMOUNT.search(text)
    if match is None:
        return None
    if text[match.end() :].strip(" \t\r\n.,!?;:)"):
        return None  # words after the number: "$5 off", "$5 each", "$5 million"
    rest = text[: match.start()] + " " + text[match.end() :]
    if any(ch.isdigit() for ch in rest):
        return None
    amount = float(match.group(1).replace(",", ""))
    return amount if math.isfinite(amount) else None


def pot_covers(db: Database, amount_usd: float) -> tuple[bool, float]:
    balance = db.pot_balance()
    return round(balance, 2) >= round(amount_usd, 2), balance
