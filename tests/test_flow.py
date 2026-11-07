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
