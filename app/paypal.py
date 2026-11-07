"""Thin PayPal REST client: Orders v2 create / capture over httpx.

No SDK. Three calls only, as the SPEC asks:
  POST /v1/oauth2/token
  POST /v2/checkout/orders
  POST /v2/checkout/orders/{id}/capture
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx


class PayPalError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        name: str = "PAYPAL_ERROR",
        status: int | None = None,
        debug_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.name = name
        self.status = status
        self.debug_id = debug_id


def _error_from(response: httpx.Response, fallback: str) -> PayPalError:
    name, message, debug_id = "PAYPAL_ERROR", fallback, None
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        name = str(body.get("name") or name)
        message = str(body.get("message") or body.get("error_description") or fallback)
        debug_id = body.get("debug_id")
    return PayPalError(
        f"{name}: {message}",
        name=name,
        status=response.status_code,
        debug_id=debug_id,
    )


class PayPalClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        api_base: str,
        return_url: str,
        cancel_url: str,
        *,
        timeout: float = 20.0,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self.api_base = api_base.rstrip("/")
        self.return_url = return_url
        self.cancel_url = cancel_url
        self.timeout = timeout
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

    # -- auth ---------------------------------------------------------------
    async def _access_token(self, client: httpx.AsyncClient) -> str:
        now = time.monotonic()
        if self._token and now < self._token_expires_at:
            return self._token
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_expires_at:
                return self._token
            response = await client.post(
                f"{self.api_base}/v1/oauth2/token",
                auth=(self._client_id, self._client_secret),
                data={"grant_type": "client_credentials"},
                headers={"Accept": "application/json"},
            )
            if response.status_code != 200:
                raise _error_from(response, "could not get an access token")
            body = response.json()
            token = body.get("access_token")
            if not token:
                raise PayPalError("token response had no access_token")
            # Cache until 60s before expiry, per SPEC.
            self._token = token
            self._token_expires_at = time.monotonic() + max(
                0.0, float(body.get("expires_in", 300)) - 60.0
            )
            return token

    async def _headers(self, client: httpx.AsyncClient) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {await self._access_token(client)}",
            "Content-Type": "application/json",
        }

    # -- orders -------------------------------------------------------------
    async def create_order(self, amount_usd: float, description: str) -> tuple[str, str]:
        """Create a CAPTURE order. Returns (order_id, buyer approval url)."""
        payload: dict[str, Any] = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "amount": {
                        "currency_code": "USD",
                        "value": f"{amount_usd:.2f}",
                    },
                    "description": description[:127],
                }
            ],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "return_url": self.return_url,
                        "cancel_url": self.cancel_url,
                        "user_action": "PAY_NOW",
                    }
                }
            },
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.api_base}/v2/checkout/orders",
                json=payload,
                headers=await self._headers(client),
            )
            if response.status_code not in (200, 201):
                raise _error_from(response, "could not create the order")
            body = response.json()
        order_id = body.get("id")
        if not order_id:
            raise PayPalError("create order response had no id")
        link = self._pick_link(body)
        if not link:
            raise PayPalError(
                "create order response had no payer-action or approve link",
                name="NO_APPROVAL_LINK",
            )
        return order_id, link

    @staticmethod
    def _pick_link(body: dict[str, Any]) -> str | None:
        links = body.get("links") or []
        by_rel = {link.get("rel"): link.get("href") for link in links if isinstance(link, dict)}
        return by_rel.get("payer-action") or by_rel.get("approve")

    async def capture(self, order_id: str) -> str:
        """Capture an approved order. Returns the capture id."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.api_base}/v2/checkout/orders/{order_id}/capture",
                json={},
                headers=await self._headers(client),
            )
            try:
                body = response.json()
            except ValueError:
                body = {}
            if response.status_code >= 400:
                raise _error_from(response, "capture failed")
        if body.get("status") != "COMPLETED":
            raise PayPalError(
                f"capture did not complete (status={body.get('status')!r})",
                name="CAPTURE_NOT_COMPLETED",
                status=response.status_code,
                debug_id=body.get("debug_id"),
            )
        try:
            capture_id = body["purchase_units"][0]["payments"]["captures"][0]["id"]
        except (KeyError, IndexError, TypeError) as exc:
            raise PayPalError("capture response had no capture id") from exc
        return capture_id

    async def get_order(self, order_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                f"{self.api_base}/v2/checkout/orders/{order_id}",
                headers=await self._headers(client),
            )
            if response.status_code != 200:
                raise _error_from(response, "could not read the order")
            return response.json()


def client_from_settings(settings) -> PayPalClient:
    return PayPalClient(
        client_id=settings.paypal_client_id,
        client_secret=settings.paypal_client_secret,
        api_base=settings.paypal_api,
        return_url=settings.return_url,
        cancel_url=settings.cancel_url,
    )
