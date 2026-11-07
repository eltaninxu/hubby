from app.budget import money, pot_refusal_reason


def test_money_format():
    assert money(4.5) == "$4.50"
    assert money(11.7) == "$11.70"
    assert money(None) == "--"


def test_pot_refusal_reason_is_fixed_text():
    assert pot_refusal_reason(0, 6) == "Pot has $0.00, you asked for $6.00. Fund it first."
    assert pot_refusal_reason(14, 15) == "Pot has $14.00, you asked for $15.00. Fund it first."
