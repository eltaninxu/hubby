from app.budget import money, pot_refusal_reason
from app.db import Database


def test_money_format():
    assert money(4.5) == "$4.50"
    assert money(11.7) == "$11.70"
    assert money(None) == "--"


def test_pot_refusal_reason_is_fixed_text():
    assert pot_refusal_reason(0, 6) == "Pot has $0.00, you asked for $6.00. Fund it first."
    assert pot_refusal_reason(14, 15) == "Pot has $14.00, you asked for $15.00. Fund it first."


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
