#!/usr/bin/env python3
"""Record the demo video: one tab, a real browser, captions burned into the page.

    HUBBY_BASE=https://hubby.royola.dev \
    PAYPAL_SANDBOX_PERSONAL_EMAIL=... PAYPAL_SANDBOX_PERSONAL_PASSWORD=... \
    .venv/bin/python scripts/record_demo.py

    HUBBY_BASE=http://127.0.0.1:8080 .venv/bin/python scripts/record_demo.py --dry

Shot list and captions: video.md (captions are final copy). Preconditions: the
service is up and the database is fresh (pot $0.00); this script checks the pot
and stops if it is not empty. It never starts, stops or resets the service.

Writes evidence/demo-video/hubby-demo.webm, report.md, and shots/ (one PNG per
caption or card, as it appeared). Exits 1 if the video runs over 2:55 or a card
did not render, 2 if a precondition fails.

--dry: for a local service with fake keys (LLM_STUB=1 gives verdicts without a
model). Skips the PayPal funding, the payout wait and the Activity page, so it
only checks layout, captions and pacing; it prints the dry length plus an
estimate of what the skipped PayPal parts add.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
import time
from datetime import datetime

from playwright.async_api import Page, async_playwright

from acceptance import login_if_needed

BASE = os.environ.get("HUBBY_BASE", "http://localhost:8080").rstrip("/")
OUT = pathlib.Path("evidence/demo-video")
LIMIT_S = 175  # 2:55
VIEW = {"width": 1280, "height": 720}

KEY_DELAY_MS = 60
CAPTION_MIN_S = 3.5
VERDICT_HOLD_S = 4.0
RELEASE_HOLD_S = 5.0
# What the skipped PayPal parts take in a real run (11/07 sandbox: payout ~17.5 s).
DRY_ESTIMATE_S = {"funding": 40, "payout": 18, "activity": 30}

CAPTIONS = {
    "1": "Everyone gave agents a wallet this year. In our house the agent holds the money, and she has to ask.",
    "2": "Empty pot. Declined by plain Python. The model was never asked.",
    "3": "Fund the pot: a real PayPal order. Create, buyer approves, capture.",
    "3b": "$20.00 now sits in Hubby's PayPal account.",
    "4": "Same question. This time there is money in the pot.",
    "4b": "Approved. Hubby calls PayPal Payouts and sends exactly $6.00 back to her.",
    "5": "Declined. It quotes her own rule and yesterday's ledger line. No money moves.",
    "6": "No price, no decision. It asks first.",
    "6b-decline": "One sweet thing a day. She already had one.",
    "6b-approve": "Then it decides, with the price.",
    "7": "The hard cap is the real balance.",
    "8": "One ledger: deposits in, releases out, every decline with its reason.",
    "9": "And in her PayPal account: $6.00, from Hubby.",
}
TITLE_CARD = ("Hubby", "the wallet keeper that can say no.",
              "PayPal sandbox · Orders in, Payouts out")
END_CARD = ("The model argues. The code enforces. PayPal moves the money.",
            "Not a wallet for agents. A wallet an agent keeps for you.",
            "github.com/eltaninxu/hubby")

# Runs in every document of the context, PayPal's included: keeps one caption bar
# at the bottom, re-attached after any navigation or body re-render. On Hubby's
# own pages window.open navigates this tab (PayPal pages asked for in English), so
# funding stays in one video, and the chat card is shortened to end above the bar.
INIT_JS = """
(() => {
  if (window.__hubbyCaptionReady) return;
  window.__hubbyCaptionReady = true;
  const isHubby = location.origin === %(origin)s;
  let text = "";
  function ensure() {
    let bar = document.getElementById("hubby-caption-bar");
    if (!text) { if (bar) bar.remove(); if (isHubby) fitAboveBar(0); return; }
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "hubby-caption-bar";
      Object.assign(bar.style, {
        position: "fixed", left: "0", right: "0", bottom: "0", zIndex: "2147483647",
        background: "rgba(10, 10, 12, 0.86)", color: "#fff", padding: "14px",
        font: "22px/1.35 system-ui, -apple-system, 'Segoe UI', sans-serif",
        textAlign: "center", pointerEvents: "none", boxSizing: "border-box",
      });
      document.documentElement.appendChild(bar);
    }
    if (bar.textContent !== text) bar.textContent = text;
    if (isHubby) fitAboveBar(bar.offsetHeight);
  }
  function fitAboveBar(h) {
    if (document.body) document.body.style.paddingBottom = h + "px";
    const chat = document.querySelector(".chat");
    if (!chat) return;
    const top = chat.getBoundingClientRect().top + window.scrollY;
    const height = Math.max(240, window.innerHeight - h - top - 12);
    if (chat.style.height !== height + "px") {
      Object.assign(chat.style, { minHeight: "0", maxHeight: "none", height: height + "px" });
      const list = document.getElementById("messages");
      if (list) list.scrollTop = list.scrollHeight;
    }
  }
  window.__setCaption = (t) => { text = t || ""; ensure(); };
  async function start() {
    try { text = (await window.__hubbyCaption()) || ""; } catch (e) {}
    ensure();
    setInterval(ensure, 300);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
  if (isHubby) {
    window.open = (url) => { location.assign(englishPayPal(url)); return null; };
  }
  function englishPayPal(url) {
    const u = new URL(url, location.href);
    if (u.hostname.endsWith("paypal.com")) u.searchParams.set("locale.x", "en_US");
    return u.toString();
  }
})();
"""

CARD_HTML = """<!doctype html><html><body style="margin:0;height:100vh;display:flex;
align-items:center;justify-content:center;background:#111214;color:#fff;
font-family:system-ui,-apple-system,'Segoe UI',sans-serif;text-align:center">
<div>%s</div></body></html>"""


class Recorder:
    def __init__(self, page: Page, state: dict, dry: bool) -> None:
        self.page = page
        self.state = state
        self.dry = dry
        self.t0 = time.monotonic()
        self.caption_at = 0.0
        self.steps: list[tuple[str, float, str]] = []
        self.decisions: list[tuple[str, str, str]] = []
        self.skipped: list[str] = []
        self.failures: list[str] = []
        self.shots = 0

    def now(self) -> float:
        return time.monotonic() - self.t0

    def step(self, name: str, note: str = "") -> None:
        at = self.now()
        self.steps.append((name, at, note))
        print(f"  {at:6.1f}s  {name}{'  ' + note if note else ''}", flush=True)

    async def hold(self, seconds: float) -> None:
        await self.page.wait_for_timeout(int(seconds * 1000))

    async def caption(self, key: str) -> None:
        """Change the caption (a CAPTIONS key, or "" for none), but never before the
        last one has had CAPTION_MIN_S; save how the page looks with it."""
        text = CAPTIONS[key] if key else ""
        wait = CAPTION_MIN_S - (time.monotonic() - self.caption_at)
        if self.caption_at and wait > 0:
            await self.hold(wait)
        self.state["caption"] = text
        await self.page.evaluate("t => window.__setCaption && window.__setCaption(t)", text)
        self.caption_at = time.monotonic()
        if key:
            await self.page.wait_for_timeout(350)  # the chat card refits on the next tick
            await self.shot(key)

    async def shot(self, name: str) -> None:
        self.shots += 1
        await self.page.screenshot(path=str(OUT / "shots" / f"{self.shots:02d}-{name}.png"))

    async def card(self, name: str, lines: tuple[str, ...], seconds: float, big_first: bool) -> None:
        """A local full-screen card, on a blank page so nothing left over (PayPal's
        scripts) can draw over it; fails the run if its first line is not on screen."""
        await self.caption("")
        await self.page.goto("about:blank")
        parts = []
        for i, line in enumerate(lines):
            size = 64 if (big_first and i == 0) else (26 if i < 2 else 20)
            weight = 700 if (big_first and i == 0) else 400
            parts.append(f'<p style="margin:14px 40px;font-size:{size}px;font-weight:{weight}">{line}</p>')
        await self.page.set_content(CARD_HTML % "".join(parts))
        try:
            await self.page.get_by_text(lines[0], exact=True).wait_for(timeout=5_000)
        except Exception:  # noqa: BLE001
            self.failures.append(f"{name} card did not render")
        await self.shot(name)
        await self.hold(seconds)

    async def ask(self, text: str) -> dict:
        """Type like a person, send, and wait for the server's answer to be drawn."""
        box = self.page.locator("#text")
        await box.click()
        await box.press_sequentially(text, delay=KEY_DELAY_MS)
        async with self.page.expect_response(
            lambda r: r.url.endswith("/api/request"), timeout=180_000
        ) as info:
            await box.press("Enter")
        response = await info.value
        data = await response.json()
        await self.page.wait_for_function("() => !document.getElementById('text').disabled")
        decision = data.get("decision") or ("error: " + str(data.get("error", ""))[:80])
        self.decisions.append((text, decision, str(data.get("status", ""))))
        return data


async def pay_on_paypal(page: Page) -> None:
    """The PayPal approve page, in this tab: log in as the sandbox buyer and pay."""
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_timeout(3_500)
    await login_if_needed(page)
    for selector in (
        "#payment-submit-btn",
        "button:has-text('完成购物')",
        "button:has-text('Complete Purchase')",
    ):
        try:
            await page.click(selector, timeout=5_000)
            break
        except Exception:  # noqa: BLE001 - PayPal's page varies by locale
            continue
    await page.wait_for_url("**/?flash=*", timeout=90_000)


async def show_activity(rec: Recorder) -> bool:
    page = rec.page
    await page.goto("https://www.sandbox.paypal.com/myaccount/activities/?locale.x=en_US")
    await page.wait_for_timeout(3_000)
    await login_if_needed(page)
    await page.get_by_text("6.00").first.wait_for(timeout=15_000)
    await rec.shot("9-activity")
    await rec.hold(CAPTION_MIN_S + 1)
    return True


async def run(dry: bool) -> int:
    (OUT / "shots").mkdir(parents=True, exist_ok=True)
    if not dry and not (
        os.environ.get("PAYPAL_SANDBOX_PERSONAL_EMAIL")
        and os.environ.get("PAYPAL_SANDBOX_PERSONAL_PASSWORD")
    ):
        print("needs PAYPAL_SANDBOX_PERSONAL_EMAIL and PAYPAL_SANDBOX_PERSONAL_PASSWORD (or --dry)")
        return 2

    state = {"caption": ""}
    async with async_playwright() as p:
        # System Chrome by default, like acceptance.py; set HUBBY_CHROME_CHANNEL=""
        # to use Playwright's bundled Chromium instead.
        channel = os.environ.get("HUBBY_CHROME_CHANNEL", "chrome") or None
        browser = await p.chromium.launch(channel=channel, headless=True)
        context = await browser.new_context(
            viewport=VIEW,
            record_video_dir=str(OUT / "raw"),
            record_video_size=VIEW,
            bypass_csp=True,
            locale="en-US",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        start_pot = (await (await context.request.get(BASE + "/api/pot")).json())["pot"]
        if start_pot != 0.0:
            print(f"pot is ${start_pot:.2f}, not $0.00: start from a fresh database")
            await context.close()
            await browser.close()
            return 2

        await context.expose_binding("__hubbyCaption", lambda source: state["caption"])
        await context.add_init_script(INIT_JS % {"origin": json.dumps(BASE)})
        page = await context.new_page()
        rec = Recorder(page, state, dry)
        print(f"recording against {BASE}{' (dry)' if dry else ''}")

        rec.step("T title card")
        await rec.card("T-title", TITLE_CARD, 4, big_first=True)

        rec.step("1 home")
        await page.goto(BASE + "/", wait_until="networkidle")
        await rec.caption("1")
        await rec.hold(5)

        rec.step("2 empty pot")
        await rec.ask("fried chicken drumstick $6")
        await rec.caption("2")
        await rec.hold(VERDICT_HOLD_S)

        rec.step("3 fund the pot")
        await rec.caption("3")
        if dry:
            rec.skipped.append("3 PayPal funding (dry)")
        else:
            await page.fill("#fund-amount", "20")
            await page.click("#fund button[type=submit]")
            await pay_on_paypal(page)
            rec.step("3b back home")
            await page.wait_for_load_state("networkidle")
            await rec.caption("3b")
            await rec.hold(CAPTION_MIN_S)

        rec.step("4 approve and release")
        await rec.caption("4")  # stays up through the payout wait
        data = await rec.ask("fried chicken drumstick $6")
        rec.step("4b verdict", f"{data.get('decision')} / {data.get('status')}")
        await rec.caption("4b")
        await rec.hold(RELEASE_HOLD_S)
        pot = float(data.get("pot") or 0)

        rec.step("5 bubble tea")
        data = await rec.ask("bubble tea $4.50")
        await rec.caption("5")
        await rec.hold(VERDICT_HOLD_S)
        pot = float(data.get("pot", pot) or 0)

        rec.step("6 a cake")
        await rec.ask("a cake")
        await rec.caption("6")
        await rec.hold(VERDICT_HOLD_S)
        data = await rec.ask("$5")
        rec.step("6b price given", data.get("decision", ""))
        await rec.caption("6b-decline" if data.get("decision") == "decline" else "6b-approve")
        await rec.hold(VERDICT_HOLD_S)
        pot = float(data.get("pot", pot) or 0)

        over = int(pot) + 1  # $15 when the pot is $14.00
        rec.step("7 over the pot", f"lunch ${over} (pot ${pot:.2f})")
        await rec.ask(f"lunch ${over}")
        await rec.caption("7")
        await rec.hold(VERDICT_HOLD_S)

        rec.step("8 ledger")
        await page.goto(BASE + "/ledger", wait_until="networkidle")
        await rec.caption("8")
        await rec.hold(6)

        if dry:
            rec.skipped.append("9 PayPal Activity page (dry)")
        else:
            rec.step("9 PayPal activity")
            await rec.caption("9")  # before leaving the ledger; every PayPal page re-applies it
            try:
                await asyncio.wait_for(show_activity(rec), timeout=30)
            except Exception as exc:  # noqa: BLE001 - optional shot, skip on anything
                rec.skipped.append(f"9 PayPal Activity page: {type(exc).__name__}")

        rec.step("E end card")
        await rec.card("E-end", END_CARD, 6, big_first=False)
        total = rec.now()
        rec.step("total", f"{total:.1f}s")

        video = page.video
        await context.close()
        await browser.close()
        raw = pathlib.Path(await video.path())
        final = OUT / ("hubby-demo-dry.webm" if dry else "hubby-demo.webm")
        raw.replace(final)
        raw.parent.rmdir()  # only Playwright's one file was in it

    estimate = total + (sum(DRY_ESTIMATE_S.values()) if dry else 0)
    write_report(rec, total, estimate, final, dry)
    print(f"\nvideo: {final}  length {total:.1f}s"
          + (f"  (real run estimate ~{estimate:.0f}s)" if dry else ""))
    if estimate > LIMIT_S:
        print(f"over the {LIMIT_S}s limit")
        return 1
    if rec.failures:
        print("failed: " + "; ".join(rec.failures))
        return 1
    return 0


def write_report(rec: Recorder, total: float, estimate: float, video: pathlib.Path, dry: bool) -> None:
    lines = [
        f"# Demo recording{' (dry)' if dry else ''}",
        "",
        f"- when: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- target: {BASE}",
        f"- video: `{video}`",
        f"- length: {total:.1f}s (limit {LIMIT_S}s)"
        + (f"; real-run estimate with PayPal parts ~{estimate:.0f}s" if dry else ""),
        "",
        "| step | at | note |",
        "|---|---|---|",
    ]
    lines += [f"| {name} | {at:.1f}s | {note} |" for name, at, note in rec.steps]
    lines += ["", "Verdicts the service returned:", ""]
    lines += [f"- `{text}` → {decision} ({status})" for text, decision, status in rec.decisions]
    lines += ["", "Failed: " + ("; ".join(rec.failures) or "nothing.")]
    lines += ["", "Skipped:" if rec.skipped else "Skipped: nothing."]
    lines += [f"- {item}" for item in rec.skipped]
    (OUT / ("report-dry.md" if dry else "report.md")).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry", action="store_true", help="local service, fake keys; no PayPal")
    args = parser.parse_args()
    return asyncio.run(run(args.dry))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
