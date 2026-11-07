#!/usr/bin/env python3
"""PayPal sandbox smoke test -- the gate before anything else.

Step 1, create:  get a token, create a $1.00 CAPTURE order, print the buyer link.
Step 2, capture: after the buyer approves, capture the order and print the id.
Step 3, payout:  send $1.00 out of the pot to the sandbox personal account.

    python scripts/paypal_smoke.py                  # create + print link
    python scripts/paypal_smoke.py --capture ID     # capture an approved order
    python scripts/paypal_smoke.py --show ID        # inspect an order
    python scripts/paypal_smoke.py --payout 1.00    # Payouts: send money out

Reads the same env as the app: PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET
(the PAYPAL_SANDBOX_* names also work), plus an optional PAYPAL_RETURN_BASE.
Never prints secrets.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import ConfigError, load_settings  # noqa: E402
from app.paypal import PayPalError, client_from_settings  # noqa: E402


def step(message: str) -> None:
    print(f"  -> {message}")


async def create(amount: float) -> str:
    settings = load_settings(require_llm=False)
    client = client_from_settings(settings)

    step(f"environment: {settings.paypal_api} (return {settings.return_url})")
    step("asking PayPal for an access token ...")
    order_id, url = await client.create_order(amount, "Hubby smoke test")
    step(f"token ok, order created: {order_id}")
    print()
    print(f"  amount        ${amount:.2f} USD")
    print(f"  order id      {order_id}")
    print(f"  approve link  {url}")
    print()
    print("  Open the link as the sandbox *personal* buyer and pay.")
    print(f"  Then run:  python scripts/paypal_smoke.py --capture {order_id}")
    return order_id


async def capture(order_id: str) -> None:
    settings = load_settings(require_llm=False)
    client = client_from_settings(settings)

    step(f"capturing {order_id} ...")
    try:
        capture_id = await client.capture(order_id)
    except PayPalError as exc:
        step(f"capture FAILED: {exc}")
        if exc.status == 422:
            print(
                "      422 usually means the buyer never approved this order, "
                "or it was already captured."
            )
        raise SystemExit(1)
    print()
    print("  status        COMPLETED")
    print(f"  capture id    {capture_id}")
    print(f"  order id      {order_id}")
    print()
    print("  Day 1 gate: PASSED. The money path works end to end.")


async def payout(amount: float) -> None:
    settings = load_settings(require_llm=False)
    client = client_from_settings(settings)
    recipient = settings.recipient_email or os.environ.get("PAYPAL_SANDBOX_PERSONAL_EMAIL", "")
    if not recipient:
        print(
            "config error: set HUBBY_RECIPIENT_EMAIL (or PAYPAL_SANDBOX_PERSONAL_EMAIL)",
            file=sys.stderr,
        )
        raise SystemExit(2)

    step(f"sending ${amount:.2f} from the pot to {recipient} ...")
    batch = f"hubby-smoke-{int(time.time())}"
    batch_id = await client.create_payout(
        recipient_email=recipient,
        amount_usd=amount,
        note="Hubby payout smoke test",
        subject=f"Hubby payout smoke {amount:.2f}",
        sender_batch_id=batch,
    )
    step(f"payout batch created: {batch_id} (polling up to 80s)")
    status, item = await client.wait_for_payout(batch_id, attempts=40, interval=2.0)
    print()
    print(f"  status          {status}")
    print(f"  payout batch    {batch_id}")
    if item:
        print(f"  payout item     {item.get('payout_item_id')}")
        print(f"  transaction id  {item.get('transaction_id')}")
    if status == "released":
        print()
        print("  Payouts gate: PASSED. Money can leave the pot.")
    else:
        print()
        print("  Payouts gate: not released yet. Check the sandbox account balance")
        print("  and whether Payouts is enabled for the app (Royola's dashboard).")


async def show(order_id: str) -> None:
    settings = load_settings(require_llm=False)
    client = client_from_settings(settings)
    order = await client.get_order(order_id)
    print(f"  status   {order.get('status')}")
    captures = (
        order.get("purchase_units", [{}])[0].get("payments", {}).get("captures", [])
    )
    for capture in captures:
        print(f"  capture  {capture.get('id')}  {capture.get('status')}")


def main() -> None:
    parser = argparse.ArgumentParser(description="PayPal sandbox smoke test")
    parser.add_argument("--amount", type=float, default=1.0)
    parser.add_argument("--capture", metavar="ORDER_ID")
    parser.add_argument("--show", metavar="ORDER_ID")
    parser.add_argument("--payout", type=float, metavar="USD")
    args = parser.parse_args()

    try:
        if args.payout:
            asyncio.run(payout(args.payout))
        elif args.capture:
            asyncio.run(capture(args.capture))
        elif args.show:
            asyncio.run(show(args.show))
        else:
            asyncio.run(create(args.amount))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except PayPalError as exc:
        print(f"paypal error: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
