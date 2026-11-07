import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.config import reset_settings_cache
from app.paypal import PayPalError


class FakeBrain:
    """Deterministic stand-in for HubbyBrain."""

    def __init__(self) -> None:
        self.extract_result = {"amount_usd": 6.0, "item": "thing", "category": "food"}
        self.extract_results: dict[str, dict] = {}
        self.decide_result = {
            "decision": "approve",
            "reason": "Meals are fine.",
            "category": "food",
        }
        self.extract_calls = 0
        self.decide_calls = 0
        self.last_decide_kwargs: dict = {}

    async def extract(self, text: str) -> dict:
        self.extract_calls += 1
        return dict(self.extract_results.get(text, self.extract_result))

    async def decide(self, **kwargs) -> dict:
        self.decide_calls += 1
        self.last_decide_kwargs = kwargs
        return dict(self.decide_result)


class FakePayPal:
    """Records orders and payouts; never talks to PayPal."""

    def __init__(self) -> None:
        self.orders: list[dict] = []
        self.captured: list[str] = []
        self.payouts: list[dict] = []
        self.payout_result: tuple[str, dict] = (
            "released",
            {"payout_item_id": "ITEM1", "transaction_id": "TX1"},
        )

    async def create_order(self, amount: float, description: str) -> tuple[str, str]:
        order_id = f"ORDER{len(self.orders) + 1}"
        self.orders.append({"id": order_id, "amount": amount, "description": description})
        return order_id, f"https://paypal.test/checkoutnow?token={order_id}"

    async def capture(self, order_id: str) -> str:
        if order_id.startswith("FAIL"):
            raise PayPalError(
                "CAPTURE_NOT_COMPLETED: nothing to capture",
                name="CAPTURE_NOT_COMPLETED",
                status=422,
            )
        self.captured.append(order_id)
        return f"CAP-{order_id}"

    async def create_payout(
        self,
        *,
        recipient_email: str,
        amount_usd: float,
        note: str,
        subject: str,
        sender_batch_id: str,
    ) -> str:
        batch_id = f"BATCH{len(self.payouts) + 1}"
        self.payouts.append(
            {
                "batch": batch_id,
                "to": recipient_email,
                "amount": amount_usd,
                "note": note,
                "subject": subject,
                "sender_batch_id": sender_batch_id,
            }
        )
        return batch_id

    async def wait_for_payout(self, batch_id: str, *, attempts: int, interval: float):
        return self.payout_result


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYPAL_ENV", "sandbox")
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "test-client")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("PAYPAL_RETURN_BASE", "http://testserver")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("HUBBY_RECIPIENT_EMAIL", "me@example.com")
    monkeypatch.setenv("HUBBY_SETTINGS_OPEN", "1")
    monkeypatch.setenv("HUBBY_DB", str(tmp_path / "hubby.db"))
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_STUB", raising=False)
    reset_settings_cache()

    with TestClient(main_module.app) as test_client:
        test_client.app.state.brain = FakeBrain()
        test_client.app.state.paypal = FakePayPal()
        yield test_client

    reset_settings_cache()


@pytest.fixture
def brain(client) -> FakeBrain:
    return client.app.state.brain


@pytest.fixture
def paypal(client) -> FakePayPal:
    return client.app.state.paypal


@pytest.fixture
def db(client):
    return client.app.state.db
