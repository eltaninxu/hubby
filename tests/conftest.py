import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.config import reset_settings_cache
from app.paypal import PayPalError


class FakeBrain:
    """Deterministic stand-in for HubbyBrain."""

    def __init__(self) -> None:
        self.extract_result = {"amount_usd": 6.0, "item": "thing", "category": "food"}
        self.decide_result = {
            "decision": "approve",
            "reason": 'Meals are fine.',
            "category": "food",
        }
        self.extract_calls = 0
        self.decide_calls = 0

    async def extract(self, text: str) -> dict:
        self.extract_calls += 1
        return dict(self.extract_result)

    async def decide(self, **kwargs) -> dict:
        self.decide_calls += 1
        return dict(self.decide_result)


class FakePayPal:
    """Records orders; never talks to PayPal."""

    def __init__(self) -> None:
        self.orders: list[dict] = []
        self.captured: list[str] = []

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


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYPAL_ENV", "sandbox")
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "test-client")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("PAYPAL_RETURN_BASE", "http://testserver")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
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


def ask(client, text: str) -> dict:
    response = client.post("/api/request", json={"text": text})
    assert response.status_code == 200, response.text
    return response.json()


def rules_of(db) -> str:
    return db.get_settings()["rules"]
