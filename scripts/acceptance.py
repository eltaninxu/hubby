#!/usr/bin/env python3
"""Acceptance run for SPEC v2 section 9, with a real browser and PayPal sandbox.

    HUBBY_BASE=https://hubby.royola.dev \
    PAYPAL_SANDBOX_PERSONAL_EMAIL=... PAYPAL_SANDBOX_PERSONAL_PASSWORD=... \
    HUBBY_ACCEPT_DB=data/hubby.db \
    .venv/bin/python scripts/acceptance.py

The demo database must be fresh (pot $0.00). Writes screenshots and report.md
into evidence/acceptance-v2/.

Not automated here, and stated in the report:
  * item 1's clean `git clone` + `compose up` (see scripts/clone_check.sh)
  * item 8's human read-through of the README
  * item 10's restart-idempotency (needs a restart mid-run)
  * the personal account's Activity page (manual glance)
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx
from playwright.async_api import Page, async_playwright

BASE = os.environ.get("HUBBY_BASE", "http://localhost:8080").rstrip("/")
DB_PATH = pathlib.Path(os.environ.get("HUBBY_ACCEPT_DB", "data/hubby.db"))
OUT = pathlib.Path("evidence/acceptance-v2")

RESULTS: list[tuple[str, bool, str]] = []

# Assembled at runtime so this file never contains key-looking text.
SECRET_RE = re.compile("s" + "k-" + r"|EL[A-Za-z0-9_-]{60,}|BAA" + "2")
SECRET_RE_HISTORY = re.compile(
    "s" + "k-" + r"[A-Za-z0-9_-]{20,}|EL[A-Za-z0-9_-]{60,}|BAA" + "2" + r"[A-Za-z0-9_-]{20,}"
)


def check(item: str, ok: bool, detail: str) -> None:
    RESULTS.append((item, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {item}: {detail}")


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def rows(where: str = "", args: tuple = ()) -> list[sqlite3.Row]:
    with db_conn() as conn:
        return list(conn.execute(f"SELECT * FROM ledger {where}", args))


def pot() -> float:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN kind='deposit' AND status='paid' THEN amount_usd END),0)"
            " - COALESCE(SUM(CASE WHEN kind='release' AND status IN ('released','pending')"
            " THEN amount_usd END),0) AS b FROM ledger"
        ).fetchone()
    return round(float(row["b"]), 2)


def seed_day(name: str) -> str | None:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT paid_at FROM ledger WHERE raw_text = ? AND reason = 'seed' ORDER BY id LIMIT 1",
            (name,),
        ).fetchone()
    return row["paid_at"][:10] if row else None


async def shot(page: Page, name: str) -> None:
    await page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)


async def say(page: Page, text: str, timeout: int = 120_000) -> str:
    await page.fill("#text", text)
    await page.press("#text", "Enter")
    await page.wait_for_function(
        "() => !document.getElementById('text').disabled", timeout=timeout
    )
    await page.wait_for_timeout(400)
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


async def wait_for_pot(page: Page, expected: float, timeout: float = 180) -> bool:
    """The page reloads itself while funding settles; survive the navigation."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            text = (await page.locator("#pot-value").inner_text()).strip()
            if text == f"${expected:.2f}":
                return True
        except Exception:  # noqa: BLE001 - page may be reloading
            pass
        await page.wait_for_timeout(1_000)
    return False


async def fund_pot(page: Page, context, amount: float, *, cancel: bool = False) -> str:
    """Use the UI: fill the fund form, pay (or cancel) in the PayPal popup."""
    await page.fill("#fund-amount", str(amount))
    async with context.expect_page() as popup_info:
        await page.click("#fund button[type=submit]")
    popup = await popup_info.value
    await popup.wait_for_load_state("domcontentloaded")
    await popup.wait_for_timeout(3_500)
    await login_if_needed(popup)

    if cancel:
        for text in ("取消并返回Test Store", "Cancel and return to Test Store"):
            try:
                await popup.get_by_text(text, exact=False).first.click(timeout=4_000)
                break
            except Exception:
                continue
    else:
        for selector in (
            "#payment-submit-btn",
            "button:has-text('完成购物')",
            "button:has-text('Complete Purchase')",
        ):
            try:
                await popup.click(selector, timeout=5_000)
                break
            except Exception:
                continue
    await popup.wait_for_url("**/*flash=*", timeout=90_000)
    flash = await popup.locator(".flash").inner_text()
    await popup.close()
    return flash


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    personal_ready = bool(
        os.environ.get("PAYPAL_SANDBOX_PERSONAL_EMAIL")
        and os.environ.get("PAYPAL_SANDBOX_PERSONAL_PASSWORD")
    )

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as http:
        health = (await http.get("/healthz")).json()
        home = await http.get("/")
        start_pot = (await http.get("/api/pot")).json()["pot"]
        check(
            "0. fresh demo database (pot is empty)",
            start_pot == 0.0,
            f"pot={start_pot}",
        )
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

        # 2. empty pot: the code says no, the model is not asked
        await say(page, "fried chicken drumstick $6")
        row = rows("WHERE kind='request' ORDER BY id DESC LIMIT 1")[0]
        check(
            "2. empty pot declines with the fixed text, before the model",
            row["decision"] == "decline"
            and row["reason"] == "Pot has $0.00, you asked for $6.00. Fund it first."
            and pot() == 0.0,
            row["reason"],
        )
        await shot(page, "02-empty-pot")

        if not personal_ready:
            check("3. fund the pot via PayPal", False, "no sandbox buyer credentials")
        else:
            # 3. fund the pot with a real PayPal order
            try:
                flash = await fund_pot(page, context, 20)
                funded = await wait_for_pot(page, 20.0)
                deposit = rows("WHERE kind='deposit' ORDER BY id DESC LIMIT 1")[0]
                check(
                    "3. fund the pot: paid, captured, pot is $20.00",
                    "Pot funded +$20.00" in flash
                    and funded
                    and deposit["status"] == "paid"
                    and bool(deposit["paypal_capture_id"]),
                    f"{flash[:60]} · pot={pot():.2f}",
                )
                await shot(page, "03-funded")
            except Exception as exc:  # noqa: BLE001
                check("3. fund the pot via PayPal", False, str(exc)[:200])

            # 4. approve releases money with a real payout
            try:
                reply = await say(page, "fried chicken drumstick $6", timeout=180_000)
                await page.wait_for_timeout(500)
                release = rows("WHERE kind='release' AND reason != 'seed' ORDER BY id DESC LIMIT 1")
                request = rows("WHERE kind='request' ORDER BY id DESC LIMIT 1")[0]
                check(
                    "4a. approve released the money (payout id, released row)",
                    bool(release)
                    and release[0]["status"] == "released"
                    and bool(release[0]["paypal_payout_item_id"])
                    and request["status"] == "approved",
                    f"{(release[0]['paypal_payout_item_id'] if release else None)}",
                )
                check(
                    "4b. the pot dropped to $14.00",
                    pot() == 14.0,
                    f"pot={pot():.2f}",
                )
                check(
                    "4c. the chat shows the release",
                    "Released $6.00 to you" in reply,
                    reply[:100].replace("\n", " | "),
                )
                await shot(page, "04-released")
            except Exception as exc:  # noqa: BLE001
                check("4a. approve released the money", False, str(exc)[:200])

            # 5. rules still decline, with the rule and the ledger line quoted
            last_cup = seed_day("bubble tea")
            await say(page, "bubble tea $4.50")
            row = rows("WHERE kind='request' ORDER BY id DESC LIMIT 1")[0]
            check(
                "5. bubble tea declined citing the last cup and 3 days",
                row["decision"] == "decline"
                and "3 day" in row["reason"]
                and last_cup is not None
                and last_cup in row["reason"],
                row["reason"][:110],
            )
            await shot(page, "05-bubble-tea")

            # 6. a question is not a payment
            await say(page, "a cake")
            row = rows("WHERE kind='request' ORDER BY id DESC LIMIT 1")[0]
            check(
                "6. 'a cake' asks how much, no payout",
                row["decision"] == "ask" and "How much" in row["reason"],
                row["reason"][:60],
            )
            await shot(page, "06-cake-ask")

            # 7. the pot is the hard limit
            await say(page, "lunch $15")
            row = rows("WHERE kind='request' ORDER BY id DESC LIMIT 1")[0]
            check(
                "7. over-pot request declines with the fixed text",
                row["decision"] == "decline"
                and row["reason"] == "Pot has $14.00, you asked for $15.00. Fund it first.",
                row["reason"],
            )
            await shot(page, "07-pot-limit")

            # 7b. cancelling a funding leaves the pot alone
            try:
                before = pot()
                flash = await fund_pot(page, context, 5, cancel=True)
                deposit = rows("WHERE kind='deposit' ORDER BY id DESC LIMIT 1")[0]
                check(
                    "7b. cancelled funding: deposit cancelled, pot unchanged",
                    deposit["status"] == "cancelled" and pot() == before and "cancelled" in flash.lower(),
                    f"{flash[:50]} · pot={pot():.2f}",
                )
                await shot(page, "07b-cancel-funding")
            except Exception as exc:  # noqa: BLE001
                check("7b. cancelled funding", False, str(exc)[:200])

            # 7c. a payout to an email with no PayPal account is not "released"
            try:
                settings = {}
                async with httpx.AsyncClient(base_url=BASE, timeout=30) as http:
                    rules = ""
                    with db_conn() as conn:
                        row = conn.execute("SELECT rules FROM settings WHERE id=1").fetchone()
                        rules = row["rules"] if row else ""
                    bogus = f"nobody-{int(time.time())}@example.com"
                    await http.post(
                        "/settings", data={"rules": rules, "recipient_email": bogus}
                    )
                reply = await say(page, "gum $1", timeout=180_000)
                release = rows("WHERE kind='release' ORDER BY id DESC LIMIT 1")[0]
                check(
                    "7c. payout to a dead email is unclaimed or pending, never released",
                    release["status"] in {"unclaimed", "pending"},
                    release["status"],
                )
                # put the real recipient back
                async with httpx.AsyncClient(base_url=BASE, timeout=30) as http:
                    await http.post(
                        "/settings",
                        data={
                            "rules": rules,
                            "recipient_email": os.environ["PAYPAL_SANDBOX_PERSONAL_EMAIL"],
                        },
                    )
                await shot(page, "07c-unclaimed")
            except Exception as exc:  # noqa: BLE001
                check("7c. payout to a dead email", False, str(exc)[:200])

            # 10. one release row per approved request, no double payouts
            with db_conn() as conn:
                dupes = conn.execute(
                    "SELECT request_id, COUNT(*) n FROM ledger WHERE kind='release'"
                    " AND request_id IS NOT NULL GROUP BY request_id HAVING n > 1"
                ).fetchall()
            check(
                "10. every approved request has at most one release row",
                not dupes,
                f"duplicates={[dict(d) for d in dupes]}",
            )

        await page.goto(BASE + "/ledger", wait_until="networkidle")
        await shot(page, "08-ledger")
        await browser.close()

    # 9. no secrets in the working tree or anywhere in git history
    tree_hits: list[str] = []
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], capture_output=True, text=True, timeout=60
    ).stdout.split("\0")
    for name in tracked:
        if not name or pathlib.Path(name).suffix in {".png", ".db", ".whl"}:
            continue
        try:
            text = pathlib.Path(name).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if SECRET_RE.search(text):
            tree_hits.append(name)

    history_hits: list[str] = []
    try:
        log = subprocess.run(
            ["git", "log", "-p", "--all"], capture_output=True, text=True, timeout=120
        ).stdout
        for line in log.splitlines():
            if SECRET_RE_HISTORY.search(line):
                history_hits.append(line.strip()[:120])
                break
    except Exception:  # noqa: BLE001
        history_hits.append("(could not read git history)")

    check(
        "9. no key material in the repo (tree + history)",
        not tree_hits and not history_hits,
        f"tree={tree_hits} history={history_hits}" if (tree_hits or history_hits) else "clean",
    )

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# Acceptance run (v2: Hubby holds the money)",
        "",
        f"- when: {stamp}",
        f"- target: {BASE}",
        f"- result: **{passed}/{len(RESULTS)} automated checks passed**",
        "",
        "| check | result | detail |",
        "|---|---|---|",
    ]
    for item, ok, detail in RESULTS:
        lines.append(f"| {item} | {'PASS' if ok else 'FAIL'} | {detail.replace('|', '/')} |")
    lines += [
        "",
        "Not automated here:",
        "- **item 1 (clean clone + compose up)** — `scripts/clone_check.sh`,",
        "- **item 8 (README walk-through by a human)** — still needs a person who has",
        "  never seen this project,",
        "- **item 10 restart half** — no payout is re-sent after a restart because nothing",
        "  re-sends payouts at all; the duplicate-row check above is the automated half,",
        "- **personal account Activity page** — the payout transaction ids are the evidence;",
        "  a glance at the sandbox buyer's Activity is manual.",
    ]
    (OUT / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\n{passed}/{len(RESULTS)} passed -> {OUT / 'report.md'}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
