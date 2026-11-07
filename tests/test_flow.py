from app.paypal import PayPalError


def ask(client, text: str) -> dict:
    response = client.post("/api/request", json={"text": text})
    assert response.status_code == 200, response.text
    return response.json()


def rules_of(db) -> str:
    return db.get_settings()["rules"]


def test_seed_writes_rules_and_two_receipts(client, db):
    settings = db.get_settings()
    assert "One sweet thing per day" in settings["rules"]
    assert settings["monthly_cap_usd"] == 120
    assert db.month_paid_usd() == 5.70
    assert len(db.all_entries()) == 2


def test_healthz(client):
    body = client.get("/healthz").json()
    assert body["ok"] is True
    assert body["paypal"] == "sandbox"
    assert body["db"] == "ok"


def test_pages_render(client):
    assert client.get("/").status_code == 200
    ledger = client.get("/ledger")
    assert ledger.status_code == 200
    assert "bubble tea" in ledger.text


def test_missing_amount_asks_and_never_orders(client, brain, db, paypal):
    brain.extract_result = {"amount_usd": None, "item": "cake", "category": "food"}

    result = ask(client, "a cake")

    assert result["decision"] == "ask"
    assert result["reason"] == "How much is it?"
    assert result["amount"] is None
    assert paypal.orders == []
    assert db.all_entries()[0]["status"] == "declined"
    assert db.all_entries()[0]["paypal_order_id"] is None


def test_hard_cap_declines_without_calling_the_model(client, brain, db):
    client.post("/settings", data={"rules": rules_of(db), "monthly_cap_usd": "7"})
    brain.extract_result = {"amount_usd": 8.0, "item": "lunch", "category": "food"}

    result = ask(client, "lunch $8")

    assert result["decision"] == "decline"
    assert result["reason"] == "Hard cap: $5.70 of $7 used this month"
    assert brain.decide_calls == 0
    assert db.month_paid_usd() == 5.70


def test_exactly_at_cap_is_allowed(client, brain, db):
    client.post("/settings", data={"rules": rules_of(db), "monthly_cap_usd": "10.70"})
    brain.extract_result = {"amount_usd": 5.0, "item": "lunch", "category": "food"}

    result = ask(client, "lunch $5")

    assert result["decision"] == "approve"
    assert brain.decide_calls == 1


def test_model_decline_is_logged(client, brain, db):
    brain.decide_result = {
        "decision": "decline",
        "reason": '"One sweet thing per day: bubble tea OR dessert, not both."',
        "category": "sweets",
    }
    brain.extract_result = {"amount_usd": 5.0, "item": "dessert", "category": "sweets"}

    result = ask(client, "dessert $5")

    assert result["decision"] == "decline"
    assert "One sweet thing per day" in result["reason"]
    assert db.all_entries()[0]["status"] == "declined"


def test_approve_creates_pending_order_then_return_pays(client, brain, db, paypal):
    brain.extract_result = {
        "amount_usd": 6.0,
        "item": "fried chicken drumstick",
        "category": "food",
    }
    brain.decide_result = {"decision": "approve", "reason": "Meals are fine.", "category": "food"}

    result = ask(client, "fried chicken drumstick $6")

    assert result["decision"] == "approve"
    assert result["status"] == "pending"
    assert result["approve_url"].startswith("https://paypal.test/")
    assert paypal.orders[0]["amount"] == 6.0
    assert db.by_order_id(result["order_id"])["status"] == "pending"

    client.get(f"/paypal/return?token={result['order_id']}")

    row = db.by_order_id(result["order_id"])
    assert row["status"] == "paid"
    assert row["paypal_capture_id"] == f"CAP-{result['order_id']}"
    assert db.month_paid_usd() == 11.70


def test_return_is_idempotent(client, brain, db):
    brain.decide_result = {"decision": "approve", "reason": "ok", "category": "food"}
    result = ask(client, "lunch $6")

    client.get(f"/paypal/return?token={result['order_id']}")
    first = db.by_order_id(result["order_id"])["paypal_capture_id"]
    client.get(f"/paypal/return?token={result['order_id']}")

    row = db.by_order_id(result["order_id"])
    assert row["status"] == "paid"
    assert row["paypal_capture_id"] == first
    assert db.month_paid_usd() == 11.70


def test_capture_failure_marks_row(client, brain, db, paypal):
    brain.decide_result = {"decision": "approve", "reason": "ok", "category": "food"}
    result = ask(client, "lunch $6")

    async def boom(order_id: str) -> str:
        raise PayPalError("CAPTURE_NOT_COMPLETED: nope", name="CAPTURE_NOT_COMPLETED")

    paypal.capture = boom
    client.get(f"/paypal/return?token={result['order_id']}")

    row = db.by_order_id(result["order_id"])
    assert row["status"] == "capture_failed"
    assert "CAPTURE_NOT_COMPLETED" in row["reason"]
    assert db.month_paid_usd() == 5.70


def test_cancel_leaves_month_total_alone(client, brain, db):
    brain.decide_result = {"decision": "approve", "reason": "ok", "category": "drink"}
    result = ask(client, "coffee $3")

    client.get(f"/paypal/cancel?token={result['order_id']}")

    assert db.by_order_id(result["order_id"])["status"] == "cancelled"
    assert db.month_paid_usd() == 5.70


def test_unknown_order_token_is_ignored(client, db):
    client.get("/paypal/return?token=NOT-A-REAL-ORDER")
    assert db.month_paid_usd() == 5.70


def test_settings_validation(client, db):
    client.post("/settings", data={"rules": rules_of(db), "monthly_cap_usd": "0"})
    assert db.get_settings()["monthly_cap_usd"] == 120

    client.post("/settings", data={"rules": "", "monthly_cap_usd": "50"})
    assert db.get_settings()["monthly_cap_usd"] == 120

    client.post("/settings", data={"rules": "Be nice.", "monthly_cap_usd": "7.5"})
    assert db.get_settings()["monthly_cap_usd"] == 7.5
    assert db.get_settings()["rules"] == "Be nice."


def test_empty_request_is_rejected(client):
    response = client.post("/api/request", json={"text": "   "})
    assert response.status_code == 400


def test_pending_orders_reserve_the_cap(client, brain, db, paypal):
    """Two approved-but-unpaid orders must not both fit under the cap."""
    brain.extract_result = {"amount_usd": 60.0, "item": "party pizza", "category": "food"}
    brain.decide_result = {"decision": "approve", "reason": "Meals are fine.", "category": "food"}

    first = ask(client, "party pizza for the team $60 because farewell lunch")
    second = ask(client, "catering tray $60 because the offsite is today")

    assert first["decision"] == "approve"
    assert second["decision"] == "decline"
    assert second["reason"] == "Hard cap: $65.70 of $120 committed this month"
    assert len(paypal.orders) == 1
    assert db.month_paid_usd() == 5.70


def test_capture_refused_when_cap_lowered_after_approval(client, brain, db, paypal):
    brain.decide_result = {"decision": "approve", "reason": "ok", "category": "food"}
    result = ask(client, "lunch $6")
    assert result["status"] == "pending"

    client.post("/settings", data={"rules": rules_of(db), "monthly_cap_usd": "7"})
    client.get(f"/paypal/return?token={result['order_id']}")

    row = db.by_order_id(result["order_id"])
    assert row["status"] == "capture_failed"
    assert row["reason"].startswith("Hard cap: $5.70 of $7 used this month")
    assert "refused at capture" in row["reason"]
    assert paypal.captured == []
    assert db.month_paid_usd() == 5.70


def test_seed_dates_refresh_to_today(client, db):
    from datetime import datetime, timedelta, timezone

    old = "2020-01-01T09:00:00+00:00"
    with db.connect() as conn:
        conn.execute(
            "UPDATE ledger SET created_at = ?, paid_at = ? WHERE reason LIKE 'Seeded example:%'",
            (old, old),
        )

    assert db.refresh_seed_dates() is True

    days = {row["raw_text"]: row["created_at"][:10] for row in db.all_entries()}
    now = datetime.now(timezone.utc)
    assert days["bubble tea"] == (now.date() - timedelta(days=1)).isoformat()
    assert days["Pepsi"] == now.date().isoformat()
    assert db.refresh_seed_dates() is False


def test_demo_reset_restores_the_seeded_household(client, db):
    db.save_settings("Changed rules.", 7)
    db.add_entry(
        raw_text="junk",
        amount_usd=1.0,
        category="other",
        decision="approve",
        reason="junk",
        status="paid",
    )

    db.reset_demo(120)

    assert db.get_settings()["rules"].startswith("One sweet thing per day")
    assert db.get_settings()["monthly_cap_usd"] == 120
    assert len(db.all_entries()) == 2
    assert db.month_paid_usd() == 5.70


def test_price_followup_completes_the_question(client, brain, db):
    """'How much is it?' then '$3' must resolve the original request, not re-ask."""
    brain.extract_results = {
        "bubble tea": {"amount_usd": None, "item": "bubble tea", "category": "drink"},
        "bubble tea $3": {"amount_usd": 3.0, "item": "bubble tea", "category": "drink"},
    }
    brain.decide_result = {
        "decision": "decline",
        "reason": "Bubble tea is once every 3 days.",
        "category": "drink",
    }

    first = ask(client, "bubble tea")
    second = ask(client, "$3")

    assert first["decision"] == "ask"
    assert second["decision"] == "decline"
    latest = db.all_entries()[0]
    assert latest["raw_text"] == "bubble tea $3"
    assert latest["amount_usd"] == 3.0


def test_unrelated_request_does_not_absorb_the_question(client, brain, db):
    brain.extract_results = {
        "bubble tea": {"amount_usd": None, "item": "bubble tea", "category": "drink"},
        "coffee $3": {"amount_usd": 3.0, "item": "coffee", "category": "drink"},
    }
    brain.decide_result = {"decision": "approve", "reason": "ok", "category": "drink"}

    ask(client, "bubble tea")
    ask(client, "coffee $3")

    assert db.all_entries()[0]["raw_text"] == "coffee $3"
    assert db.all_entries()[0]["amount_usd"] == 3.0
