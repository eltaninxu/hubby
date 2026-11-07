from app.llm import build_decide_prompt, normalize_category


def test_decide_prompt_carries_pot_but_no_monthly_cap():
    prompt = build_decide_prompt(
        rules="One sweet thing per day.",
        ledger_lines=["2026-11-07 | deposit | +$20.00 | - | paid"],
        today="2026-11-07",
        request_text="a cake $77",
        parsed={"amount_usd": 77.0, "item": "cake", "category": "sweets"},
        pot_balance=20.0,
    )

    assert "POT BALANCE: $20.00" in prompt
    assert "MONTHLY CAP" not in prompt
    assert "already paid this month" not in prompt
    assert "a cake $77" in prompt


def test_pot_instruction_is_in_the_system_prompt():
    from app.llm import DECIDE_SYSTEM

    assert "pot balance is a hard limit" in DECIDE_SYSTEM
    assert "Do not do budget arithmetic" in DECIDE_SYSTEM


def test_category_normalisation():
    assert normalize_category("Food & Beverage") == "food"
    assert normalize_category("  SWEETS ") == "sweets"
    assert normalize_category("") == "other"
    assert normalize_category(None) == "other"
