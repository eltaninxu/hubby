from app.budget import cap_label, hard_cap_reason, money


def test_money_format():
    assert money(4.5) == "$4.50"
    assert money(11.7) == "$11.70"
    assert money(None) == "--"


def test_cap_label_matches_demo_copy():
    assert cap_label(7) == "$7"
    assert cap_label(120) == "$120"


def test_hard_cap_reason_is_fixed_text():
    assert hard_cap_reason(11.7, 7) == "Hard cap: $11.70 of $7 used this month"
    assert hard_cap_reason(5.7, 120) == "Hard cap: $5.70 of $120 used this month"
