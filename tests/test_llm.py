from app.llm import build_decide_prompt, normalize_category


def test_decide_prompt_has_no_budget_numbers():
    prompt = build_decide_prompt(
        rules="One sweet thing per day.",
        ledger_lines=["2026-11-07 | Pepsi | $1.20 | sweets | paid"],
        today="2026-11-07",
        request_text="a cake $77",
        parsed={"amount_usd": 77.0, "item": "cake", "category": "sweets"},
    )

    assert "MONTHLY CAP" not in prompt
    assert "already paid" not in prompt
    assert "$120" not in prompt
    assert "$11.70" not in prompt
    assert "a cake $77" in prompt


def test_budget_instruction_is_in_the_system_prompt():
    from app.llm import DECIDE_SYSTEM

    assert "Never compute" in DECIDE_SYSTEM
    assert "cap" in DECIDE_SYSTEM


def test_category_normalisation():
    assert normalize_category("Food & Beverage") == "food"
    assert normalize_category("  SWEETS ") == "sweets"
    assert normalize_category("") == "other"
    assert normalize_category(None) == "other"
