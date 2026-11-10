import pytest

from app.budget import money, pot_refusal_reason, stated_amount
from app.db import Database


def test_money_format():
    assert money(4.5) == "$4.50"
    assert money(11.7) == "$11.70"
    assert money(None) == "--"


def test_pot_refusal_reason_is_fixed_text():
    assert pot_refusal_reason(0, 6) == "Pot has $0.00, you asked for $6.00. Fund it first."
    assert pot_refusal_reason(14, 15) == "Pot has $14.00, you asked for $15.00. Fund it first."


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("fried chicken drumstick $6", 6.0),
        ("bubble tea $4.50", 4.5),
        ("$5", 5.0),
        ("lunch $ 15", 15.0),
        ("rent $1,200", 1200.0),
        ("bubble tea $4.", 4.0),
        ("lunch $15!  ", 15.0),
    ],
)
def test_stated_amount_reads_one_plain_dollar_amount(text, expected):
    assert stated_amount(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "$5k",  # a letter glued to the number
        "$6.999",  # more than two decimals
        "$1,20",  # not a thousands separator
        "2 teas at $4 each",  # another digit elsewhere
        "was $20 now $6",  # two amounts
        "coffee with my $5 off coupon",  # the number is not the price
        "bubble tea $4, please",  # words after the number
        "$5 lunch",
        "$5 million yacht",
        "$$5",
        "$.50",
        "5$",  # no number after the sign
        "$",
        "six dollars",
        "a cake",
        "",
        "$" + "9" * 400,  # overflows to inf
    ],
)
def test_stated_amount_is_none_unless_unambiguous(text):
    assert stated_amount(text) is None


def test_unclaimed_counts_as_spent_but_failed_does_not(tmp_path):
    db = Database(tmp_path / "pot.db")
    db.init(recipient_email="me@example.com", seed=False)
    db.add_entry(
        kind="deposit",
        raw_text="fund the pot",
        amount_usd=20.0,
        category="other",
        decision="deposit",
        reason="fund",
        status="paid",
        paid_at="2026-11-07T09:00:00+00:00",
    )
    assert db.pot_balance() == 20.0

    db.add_entry(
        kind="release",
        raw_text="gum",
        amount_usd=1.0,
        category="sweets",
        decision="approve",
        reason="",
        status="unclaimed",
    )
    assert db.pot_balance() == 19.0

    db.add_entry(
        kind="release",
        raw_text="lunch",
        amount_usd=2.0,
        category="food",
        decision="approve",
        reason="",
        status="failed",
    )
    assert db.pot_balance() == 19.0

    db.add_entry(
        kind="release",
        raw_text="coffee",
        amount_usd=3.0,
        category="drink",
        decision="approve",
        reason="",
        status="pending",
    )
    assert db.pot_balance() == 16.0
