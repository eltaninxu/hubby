#!/usr/bin/env python3
"""Acceptance run for SPEC section 9, driven by a real browser and real PayPal sandbox.

Runs against an already-running Hubby instance (container or local dev server):

    HUBBY_BASE=http://localhost:8086 \
    PAYPAL_SANDBOX_PERSONAL_EMAIL=... PAYPAL_SANDBOX_PERSONAL_PASSWORD=... \
    HUBBY_ACCEPT_DB=.dev/data/hubby.db \
    .venv/bin/python scripts/acceptance.py

Writes screenshots and report.md into evidence/acceptance/.
Nothing here is needed to run the app; it is how we prove the app works.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sqlite3
import sys
import time
from datetime import datetime, timezone

import httpx
from playwright.async_api import Page, async_playwright

BASE = os.environ.get("HUBBY_BASE", "http://localhost:8086").rstrip("/")
DB_PATH = pathlib.Path(os.environ.get("HUBBY_ACCEPT_DB", ".dev/data/hubby.db"))
OUT = pathlib.Path("evidence/acceptance")
STATE = OUT / "paypal_state.json"

RESULTS: list[tuple[str, bool, str]] = []


def check(item: str, ok: bool, detail: str) -> None:
    RESULTS.append((item, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {item}: {detail}")


def db_rows(limit: int = 5) -> list[sqlite3.Row]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM ledger ORDER BY id DESC LIMIT ?", (limit,)))
    finally:
        conn.close()


def month_paid() -> float:
    conn = sqlite3.connect(DB_PATH)
    try:
        total, = conn.execute(
            "SELECT COALESCE(SUM(amount_usd), 0) FROM ledger"
            " WHERE status = 'paid' AND paid_at LIKE ?",
            (datetime.now(timezone.utc).strftime("%Y-%m") + "%",),
        ).fetchone()
    finally:
        conn.close()
    return round(float(total), 2)


async def shot(page: Page, name: str) -> None:
    await page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)


async def say(page: Page, text: str) -> str:
    """Type a request into the chat and wait until the answer lands."""
    await page.fill("#text", text)
    await page.press("#text", "Enter")
    await page.wait_for_function(
        "() => !document.getElementById('text').disabled", timeout=120_000
    )
    await page.wait_for_timeout(300)
    return await page.locator(".msg.hubby").last.inner_text()


async def login_if_needed(page: Page) -> None:
    if not await page.locator("#email").count():
        return
    await page.fill("#email", os.environ["PAYPAL_SANDBOX_PERSONAL_EMAIL"])
    if await page.locator("#btnNext").count():
        await page.click("#btnNext")
        await page.wait_for_timeout(3_500)
    await page.fill("#password", os.environ["PAYPAL_SANDBOX_PERSONAL_PASSWORD"])
    await page.click("#btnLogin")
    await page.wait_for_timeout(9_000)


async def pay_with_paypal(page: Page, context, name: str) -> str:
    """Click the PayPal button, log in as the sandbox buyer, pay, come back."""
    async with context.expect_page() as popup_info:
        await page.locator("a.pay").last.click()
    popup = await popup_info.value
    await popup.wait_for_load_state("domcontentloaded")
    await popup.wait_for_timeout(3_500)
    await login_if_needed(popup)
    await shot(popup, f"{name}-paypal")

    for selector in (
        "#payment-submit-btn",
        "button:has-text('完成购物')",
        "button:has-text('Complete Purchase')",
        "button:has-text('Agree & Continue')",
    ):
        try:
            await popup.click(selector, timeout=5_000)
            break
        except Exception:
            continue
    await popup.wait_for_url("**/*flash=*", timeout=60_000)
    final_url = popup.url
    await popup.close()
    return final_url


async def cancel_at_paypal(page: Page, context, name: str) -> str:
    async with context.expect_page() as popup_info:
        await page.locator("a.pay").last.click()
    popup = await popup_info.value
    await popup.wait_for_load_state("domcontentloaded")
    await popup.wait_for_timeout(3_500)
    await login_if_needed(popup)
    for text in ("取消并返回Test Store", "Cancel and return to Test Store"):
        try:
            await popup.get_by_text(text, exact=False).first.click(timeout=4_000)
            break
        except Exception:
            continue
    await popup.wait_for_url("**/*flash=*", timeout=60_000)
    final_url = popup.url
    await popup.close()
    return final_url


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    personal_ready = bool(
        os.environ.get("PAYPAL_SANDBOX_PERSONAL_EMAIL")
        and os.environ.get("PAYPAL_SANDBOX_PERSONAL_PASSWORD")
    )

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as http:
        health = (await http.get("/healthz")).json()
        home = await http.get("/")
        check(
            "1. app is up (/healthz + home page)",
            health.get("ok") is True and home.status_code == 200,
            f"healthz={health} home={home.status_code}",
        )

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()
        await page.goto(BASE + "/", wait_until="networkidle")
        await page.wait_for_timeout(500)
        await shot(page, "01-home")

        # 2. bubble tea gets declined and cites yesterday's cup
        reply = await say(page, "bubble tea $4.50")
        row = db_rows(1)[0]
        check(
            "2. bubble tea declined, citing the last cup and 3 days",
            row["decision"] == "decline"
            and row["status"] == "declined"
            and "3 days" in row["reason"]
            and "2026-11-0" in row["reason"],
            row["reason"][:120],
        )
        await shot(page, "02-bubble-tea")

        # 3. fried chicken approved, paid for real
        if personal_ready:
            reply = await say(page, "fried chicken drumstick $6")
            approve_button = await page.locator("a.pay").count()
            check(
                "3a. fried chicken approved with a PayPal button",
                "approve" in reply.lower() and approve_button > 0,
                reply[:110].replace("\n", " | "),
            )
            await shot(page, "03-chicken-approved")
            try:
                url = await pay_with_paypal(page, context, "03-pay")
                await page.goto(BASE + "/", wait_until="networkidle")
                paid_row = db_rows(1)[0]
                check(
                    "3b. buyer paid, capture landed, ledger is paid",
                    paid_row["status"] == "paid"
                    and bool(paid_row["paypal_capture_id"])
                    and month_paid() == 11.70,
                    f"capture={paid_row['paypal_capture_id']} month=${month_paid():.2f}",
                )
                await shot(page, "03-paid")
            except Exception as exc:  # noqa: BLE001
                check("3b. buyer paid, capture landed, ledger is paid", False, str(exc)[:200])
        else:
            check("3. fried chicken approved and paid", False, "no sandbox buyer credentials")

        # 4. "a cake" asks for the price and never builds an order
        reply = await say(page, "a cake")
        row = db_rows(1)[0]
        check(
            "4. 'a cake' asks how much, no order",
            row["decision"] == "ask"
            and "How much" in row["reason"]
            and row["paypal_order_id"] is None,
            row["reason"][:80],
        )
        await shot(page, "04-cake-ask")

        # 5. hard cap, straight from code
        await page.goto(BASE + "/settings", wait_until="networkidle")
        await page.fill("input[name=monthly_cap_usd]", "7")
        await page.click("button[type=submit]")
        await page.wait_for_timeout(800)
        await page.goto(BASE + "/", wait_until="networkidle")
        reply = await say(page, "lunch $8")
        row = db_rows(1)[0]
        expected = f"Hard cap: ${month_paid():.2f} of $7 used this month"
        check(
            "5. hard cap declines with fixed code text",
            row["decision"] == "decline" and row["reason"] == expected,
            f"got {row['reason']!r}",
        )
        await shot(page, "05-hard-cap")

        # back to a sane cap so the next check tests the rules, not the cap
        await page.goto(BASE + "/settings", wait_until="networkidle")
        await page.fill("input[name=monthly_cap_usd]", "120")
        await page.click("button[type=submit]")
        await page.wait_for_timeout(800)
        await page.goto(BASE + "/", wait_until="networkidle")

        # 6. one sweet thing per day
        reply = await say(page, "dessert $5")
        row = db_rows(1)[0]
        check(
            "6. dessert declined citing one sweet thing per day",
            row["decision"] == "decline" and "One sweet thing per day" in row["reason"],
            row["reason"][:110],
        )
        await shot(page, "06-dessert")

        # 7. buyer cancels at PayPal
        if personal_ready:
            reply = await say(page, "coffee $3")
            before = month_paid()
            try:
                url = await cancel_at_paypal(page, context, "07-cancel")
                await page.goto(BASE + "/", wait_until="networkidle")
                row = db_rows(1)[0]
                check(
                    "7. cancel leaves the row cancelled and the month total unchanged",
                    row["status"] == "cancelled" and month_paid() == before,
                    f"status={row['status']} month=${month_paid():.2f}",
                )
                await shot(page, "07-cancelled")
            except Exception as exc:  # noqa: BLE001
                check(
                    "7. cancel leaves the row cancelled and the month total unchanged",
                    False,
                    str(exc)[:200],
                )
        else:
            check("7. cancel flow", False, "no sandbox buyer credentials")

        # ledger evidence
        await page.goto(BASE + "/ledger", wait_until="networkidle")
        await shot(page, "08-ledger")

        await browser.close()

    # 9. no secrets in the repo
    markers = ("s" + "k-", "BAA" + "2", "EL" + "A" * 60)
    hits: list[str] = []
    for path in pathlib.Path(".").rglob("*"):
        if any(
            part in {".git", ".venv", ".dev", "data", "evidence", "__pycache__"}
            for part in path.parts
        ):
            continue
        if not path.is_file() or path.suffix in {".png", ".db", ".whl"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(marker in text for marker in markers):
            hits.append(str(path))
    check("9. no key material in the repo", not hits, f"hits={hits}" if hits else "clean")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# Acceptance run",
        "",
        f"- when: {stamp}",
        f"- target: {BASE}",
        f"- result: **{passed}/{len(RESULTS)} passed**",
        "",
        "| item | result | detail |",
        "|---|---|---|",
    ]
    for item, ok, detail in RESULTS:
        lines.append(f"| {item} | {'PASS' if ok else 'FAIL'} | {detail.replace('|', '/')} |")
    (OUT / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\n{passed}/{len(RESULTS)} passed -> {OUT / 'report.md'}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
