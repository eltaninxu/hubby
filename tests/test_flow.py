def fund(client, amount: float = 20.0) -> dict:
    """Start a pot funding and let the (fake) PayPal return capture it."""
    response = client.post("/api/fund", json={"amount": amount})
    assert response.status_code == 200, response.text
    data = response.json()
    client.get(f"/paypal/return?token={data['order_id']}")
    return data


def ask(client, text: str) -> dict:
    response = client.post("/api/request", json={"text": text})
    assert response.status_code == 200, response.text
    return response.json()


def rules_of(db) -> str:
    return db.get_settings()["rules"]


def test_seed_pot_is_zero(client, db):
    settings = db.get_settings()
    assert "One sweet thing per day" in settings["rules"]
    assert settings["recipient_email"] == "me@example.com"
    assert len(db.all_entries()) == 3
    assert db.pot_balance() == 0.0


def test_healthz(client):
    body = client.get("/healthz").json()
    assert body["ok"] is True
    assert body["paypal"] == "sandbox"


def test_pages_render(client):
    assert client.get("/").status_code == 200
    assert client.get("/ledger").status_code == 200
    assert client.get("/settings").status_code == 200


def test_empty_pot_declines_without_the_model(client, brain, db, paypal):
    brain.extract_result = {"amount_usd": 6.0, "item": "fried chicken", "category": "food"}

    result = ask(client, "fried chicken drumstick $6")

    assert result["decision"] == "decline"
    assert result["reason"] == "Pot has $0.00, you asked for $6.00. Fund it first."
    assert result["status"] == "declined"
    assert brain.decide_calls == 0
    assert paypal.payouts == []
    assert paypal.orders == []


def test_funding_the_pot_via_paypal(client, db, paypal):
    data = fund(client, 20.0)

    assert data["approve_url"].startswith("https://paypal.test/")
    deposit = db.by_order_id(data["order_id"])
    assert deposit["kind"] == "deposit"
    assert deposit["status"] == "paid"
    assert deposit["paypal_capture_id"] == f"CAP-{data['order_id']}"
    assert db.pot_balance() == 20.0


def test_cancel_funding_leaves_pot_alone(client, db):
    response = client.post("/api/fund", json={"amount": 20})
    order_id = response.json()["order_id"]

    client.get(f"/paypal/cancel?token={order_id}")

    assert db.by_order_id(order_id)["status"] == "cancelled"
    assert db.pot_balance() == 0.0


def test_approve_releases_money_and_drops_the_pot(client, brain, db, paypal):
    fund(client, 20.0)
    brain.extract_result = {
        "amount_usd": 6.0,
        "item": "fried chicken drumstick",
        "category": "food",
    }
    brain.decide_result = {"decision": "approve", "reason": "Meals are fine.", "category": "food"}

    result = ask(client, "fried chicken drumstick $6")

    assert result["decision"] == "approve"
    assert result["status"] == "released"
    assert result["payout_item_id"] == "ITEM1"
    assert db.pot_balance() == 14.0
    assert paypal.payouts[0]["to"] == "me@example.com"
    assert paypal.payouts[0]["amount"] == 6.0
    release = db.release_for_request(result["request_id"])
    assert release["status"] == "released"
    assert release["paypal_payout_item_id"] == "ITEM1"
    assert release["paypal_transaction_id"] == "TX1"


def test_exact_pot_balance_is_allowed(client, brain, db):
    fund(client, 6.0)
    brain.extract_result = {"amount_usd": 6.0, "item": "lunch", "category": "food"}

    result = ask(client, "lunch $6")

    assert result["decision"] == "approve"
    assert result["status"] == "released"
    assert db.pot_balance() == 0.0


def test_pot_too_small_declines_with_exact_copy(client, brain, db):
    fund(client, 14.0)
    brain.extract_result = {"amount_usd": 15.0, "item": "lunch", "category": "food"}

    result = ask(client, "lunch $15")

    assert result["decision"] == "decline"
    assert result["reason"] == "Pot has $14.00, you asked for $15.00. Fund it first."
    assert brain.decide_calls == 0


def test_model_decline_is_logged(client, brain, db):
    fund(client, 20.0)
    brain.decide_result = {
        "decision": "decline",
        "reason": '"One sweet thing per day: bubble tea OR dessert, not both."',
        "category": "sweets",
    }
    brain.extract_result = {"amount_usd": 5.0, "item": "dessert", "category": "sweets"}

    result = ask(client, "dessert $5")

    assert result["decision"] == "decline"
    assert "One sweet thing per day" in result["reason"]
    assert db.pot_balance() == 20.0


def test_missing_amount_asks_and_never_pays(client, brain, db, paypal):
    fund(client, 20.0)
    brain.extract_result = {"amount_usd": None, "item": "cake", "category": "food"}

    result = ask(client, "a cake")

    assert result["decision"] == "ask"
    assert result["reason"] == "How much is it?"
    assert result["status"] == "ask"
    assert paypal.payouts == []
    assert db.pot_balance() == 20.0


def test_pending_payout_still_counts_against_the_pot(client, brain, db, paypal):
    fund(client, 20.0)
    paypal.payout_result = ("pending", {"transaction_status": "PENDING"})
    brain.extract_result = {"amount_usd": 6.0, "item": "lunch", "category": "food"}

    result = ask(client, "lunch $6")

    assert result["status"] == "pending"
    assert db.pot_balance() == 14.0
    assert db.release_for_request(result["request_id"])["status"] == "pending"


def test_failed_payout_does_not_eat_the_pot(client, brain, db, paypal):
    fund(client, 20.0)
    paypal.payout_result = ("failed", {"errors": {"message": "INSUFFICIENT_FUNDS"}})
    brain.extract_result = {"amount_usd": 6.0, "item": "lunch", "category": "food"}

    result = ask(client, "lunch $6")

    assert result["decision"] == "approve"
    assert result["status"] == "failed"
    assert "INSUFFICIENT_FUNDS" in result["error"]
    assert db.pot_balance() == 20.0
    release = db.release_for_request(result["request_id"])
    assert release["status"] == "failed"
    assert "INSUFFICIENT_FUNDS" in release["reason"]


def test_unclaimed_payout_is_recorded(client, brain, db, paypal):
    fund(client, 20.0)
    paypal.payout_result = ("unclaimed", {"transaction_status": "UNCLAIMED"})
    brain.extract_result = {"amount_usd": 6.0, "item": "lunch", "category": "food"}

    result = ask(client, "lunch $6")

    assert result["status"] == "unclaimed"
    assert db.release_for_request(result["request_id"])["status"] == "unclaimed"


def test_recipient_can_change_in_settings(client, brain, db, paypal):
    fund(client, 20.0)
    client.post(
        "/settings",
        data={"rules": rules_of(db), "recipient_email": "someone-else@example.com"},
    )
    brain.extract_result = {"amount_usd": 6.0, "item": "lunch", "category": "food"}

    ask(client, "lunch $6")

    assert paypal.payouts[0]["to"] == "someone-else@example.com"


def test_settings_validation(client, db):
    client.post("/settings", data={"rules": rules_of(db), "recipient_email": "not-an-email"})
    assert db.get_settings()["recipient_email"] == "me@example.com"

    client.post("/settings", data={"rules": "", "recipient_email": "a@b.co"})
    assert db.get_settings()["recipient_email"] == "me@example.com"

    client.post("/settings", data={"rules": "Be nice.", "recipient_email": "a@b.co"})
    assert db.get_settings()["recipient_email"] == "a@b.co"
    assert db.get_settings()["rules"] == "Be nice."


def test_price_followup_completes_the_question(client, brain, db):
    fund(client, 20.0)
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


def test_empty_request_is_rejected(client):
    response = client.post("/api/request", json={"text": "   "})
    assert response.status_code == 400


def test_fund_amount_is_validated(client, paypal):
    assert client.post("/api/fund", json={"amount": 0}).status_code == 400
    assert client.post("/api/fund", json={"amount": -5}).status_code == 400
    assert client.post("/api/fund", json={"amount": "abc"}).status_code == 400
    assert paypal.orders == []


def test_pot_api(client, db):
    fund(client, 20.0)
    assert client.get("/api/pot").json()["pot"] == 20.0


def test_seed_dates_refresh_to_today(client, db):
    from datetime import datetime, timedelta, timezone

    old = "2020-01-01T09:00:00+00:00"
    with db.connect() as conn:
        conn.execute(
            "UPDATE ledger SET created_at = ?, paid_at = ? WHERE reason = 'seed'",
            (old, old),
        )

    assert db.refresh_seed_dates() is True

    days = {row["raw_text"]: row["created_at"][:10] for row in db.all_entries()}
    now = datetime.now(timezone.utc)
    assert days["bubble tea"] == (now.date() - timedelta(days=1)).isoformat()
    assert days["Pepsi"] == now.date().isoformat()
    assert db.refresh_seed_dates() is False


def test_demo_reset_restores_the_seeded_pot(client, db):
    fund(client, 20.0)
    db.save_settings("Changed rules.", "x@y.co")

    db.reset_demo("me@example.com")

    assert db.get_settings()["rules"].startswith("One sweet thing per day")
    assert db.get_settings()["recipient_email"] == "me@example.com"
    assert len(db.all_entries()) == 3
    assert db.pot_balance() == 0.0
