"""FastAPI app: chat, ledger, settings, and the PayPal return legs."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.budget import assess_cap, cap_label, hard_cap_reason, money, reserved_reason
from app.config import ConfigError, get_settings
from app.db import Database, today_iso
from app.llm import LLMError, HubbyBrain
from app.paypal import PayPalError, client_from_settings
from app.ratelimit import RateLimiter

log = logging.getLogger("hubby")

# "3", "$3", "about 3.50", "3 dollars" -- an answer to "how much is it?"
FOLLOWUP_PRICE = re.compile(
    r"^\s*(?:it'?s|it is|about|around|maybe|only|just)?\s*\$?\s*"
    r"\d+(?:[.,]\d{1,2})?\s*(?:usd|dollars?|bucks?)?\s*$",
    re.IGNORECASE,
)
PENDING_TTL_SECONDS = 15 * 60

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["money"] = money
templates.env.globals["cap_label"] = cap_label


def _seed_enabled() -> bool:
    return os.environ.get("HUBBY_SEED", "1").strip().lower() not in {"0", "false", "no"}


def _session_id(request: Request) -> str:
    return request.cookies.get("hubby_sid") or "anon"


def _remember_question(state: Any, session: str, text: str) -> None:
    state.pending[session] = {"text": text, "at": time.monotonic()}
    cutoff = time.monotonic() - PENDING_TTL_SECONDS
    for key in [k for k, v in state.pending.items() if v["at"] < cutoff]:
        state.pending.pop(key, None)


def _take_question(state: Any, session: str) -> dict[str, Any] | None:
    pending = state.pending.get(session)
    if not pending:
        return None
    if time.monotonic() - pending["at"] > PENDING_TTL_SECONDS:
        state.pending.pop(session, None)
        return None
    return pending


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _maintenance(app: FastAPI) -> None:
    """Keep the demo honest: slide seed dates forward, reset an abandoned demo."""
    settings = app.state.settings
    while True:
        await asyncio.sleep(settings.maintenance_seconds)
        try:
            db: Database = app.state.db
            if (
                settings.demo_reset
                and db.hours_since_last_entry() >= settings.demo_reset_after_hours
            ):
                db.reset_demo(settings.monthly_cap_usd)
                log.info("demo reset: household returned to seeded state")
            elif db.refresh_seed_dates():
                log.info("seed dates refreshed")
        except Exception:  # noqa: BLE001 - a background janitor must not die
            log.exception("maintenance failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        settings = get_settings()
    except ConfigError as exc:
        raise RuntimeError(f"Cannot start Hubby: {exc}") from exc

    db = Database(settings.db_path)
    db.init(settings.monthly_cap_usd, seed=_seed_enabled())

    if settings.demo_reset and db.hours_since_last_entry() >= settings.demo_reset_after_hours:
        db.reset_demo(settings.monthly_cap_usd)
        log.info("demo reset on boot: household returned to seeded state")
    else:
        db.refresh_seed_dates()

    app.state.settings = settings
    app.state.db = db
    app.state.brain = HubbyBrain(settings)
    app.state.paypal = client_from_settings(settings)
    app.state.pending = {}
    app.state.limiter = RateLimiter(
        {
            "api": settings.rate_api,
            "settings": settings.rate_settings,
            "global": settings.rate_global,
        }
    )

    maintenance = asyncio.create_task(_maintenance(app))
    try:
        yield
    finally:
        maintenance.cancel()


app = FastAPI(title="Hubby", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# -- helpers ---------------------------------------------------------------
def _flash_redirect(message: str) -> RedirectResponse:
    return RedirectResponse(url=f"/?flash={quote(message)}", status_code=303)


def _ledger_line(entry: dict[str, Any]) -> str:
    stamp = (entry.get("paid_at") or entry.get("created_at") or "")[:10]
    category = entry.get("category") or "other"
    return (
        f"{stamp} | {entry.get('raw_text', '')} | {money(entry.get('amount_usd'))}"
        f" | {category} | {entry.get('status')}"
    )


def _budget_view(db: Database) -> dict[str, Any]:
    row = db.get_settings()
    used = db.month_paid_usd()
    pending = db.month_pending_usd()
    cap = float(row.get("monthly_cap_usd") or 0)
    percent = min(100.0, round(used / cap * 100, 1)) if cap > 0 else 0.0
    return {
        "used": used,
        "pending": pending,
        "cap": cap,
        "percent": percent,
        "over": used > cap,
        "categories": db.category_totals(),
    }


# -- pages -----------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def chat_page(request: Request, flash: str | None = None):
    db: Database = request.app.state.db
    response = templates.TemplateResponse(
        request,
        "chat.html",
        {"flash": flash, "budget": _budget_view(db), "recent": db.recent(5)},
    )
    if not request.cookies.get("hubby_sid"):
        response.set_cookie(
            "hubby_sid",
            uuid.uuid4().hex,
            httponly=True,
            samesite="lax",
            max_age=86_400,
        )
    return response


@app.get("/ledger", response_class=HTMLResponse)
async def ledger_page(request: Request, flash: str | None = None):
    db: Database = request.app.state.db
    return templates.TemplateResponse(
        request,
        "ledger.html",
        {"flash": flash, "budget": _budget_view(db), "entries": db.all_entries()},
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, flash: str | None = None):
    db: Database = request.app.state.db
    return templates.TemplateResponse(
        request, "settings.html", {"flash": flash, "settings": db.get_settings()}
    )


@app.post("/settings")
async def save_settings(request: Request, rules: str = Form(""), monthly_cap_usd: str = Form("")):
    allowed, retry_after = request.app.state.limiter.allow("settings", _client_ip(request))
    if not allowed:
        return _flash_redirect(
            f"Too many rule changes from this address. Try again in {retry_after}s."
        )

    db: Database = request.app.state.db
    try:
        cap = round(float(monthly_cap_usd.strip()), 2)
    except ValueError:
        return _flash_redirect("Monthly cap must be a number.")
    if cap <= 0:
        return _flash_redirect("Monthly cap must be greater than zero.")
    if not rules.strip():
        return _flash_redirect("The rules cannot be empty.")
    db.save_settings(rules.strip(), cap)
    return _flash_redirect(f"Saved. Monthly cap is now {cap_label(cap)}.")


# -- purchase flow ---------------------------------------------------------
@app.post("/api/request")
async def api_request(request: Request):
    limiter: RateLimiter = request.app.state.limiter
    allowed, retry_after = limiter.allow("api", _client_ip(request))
    if not allowed:
        return JSONResponse(
            {"error": f"Too many requests from this address. Try again in {retry_after}s."},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )
    allowed, retry_after = limiter.allow("global", "household")
    if not allowed:
        return JSONResponse(
            {"error": "Hubby is handling a lot right now. Try again in a minute."},
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    payload: dict[str, Any] = {}
    try:
        payload = await request.json()
    except ValueError:
        pass
    text = str(payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "Say what you want to buy."}, status_code=400)

    state = request.app.state
    session = _session_id(request)
    asked = _take_question(state, session)
    if asked and FOLLOWUP_PRICE.match(text):
        # "How much is it?" -> "3" means "bubble tea 3", not a new mystery item.
        text = f"{asked['text']} {text}".strip()
        state.pending.pop(session, None)

    try:
        result = await _handle_purchase(state, text)
    except LLMError as exc:
        return JSONResponse(
            {"error": f"Hubby can't reach its brain right now: {exc}"}, status_code=502
        )
    except PayPalError as exc:
        return JSONResponse({"error": f"PayPal refused: {exc}"}, status_code=502)

    if result["decision"] == "ask":
        _remember_question(state, session, text)
    else:
        state.pending.pop(session, None)
    return JSONResponse(result)


async def _handle_purchase(state: Any, text: str) -> dict[str, Any]:
    db: Database = state.db
    brain: HubbyBrain = state.brain

    # 1. facts out of the sentence
    parsed = await brain.extract(text)
    amount = parsed.get("amount_usd")
    item = parsed.get("item") or text
    category = parsed.get("category") or "other"

    # 2. no price -> ask, never build an order
    if amount is None:
        reason = "How much is it?"
        entry_id = db.add_entry(
            raw_text=text,
            amount_usd=None,
            category=category,
            decision="ask",
            reason=reason,
            status="declined",
        )
        return {
            "decision": "ask",
            "reason": reason,
            "amount": None,
            "item": item,
            "category": category,
            "status": "declined",
            "ledger_id": entry_id,
        }

    # 3. hard cap, in code, before the model gets a say. Pending orders reserve
    #    budget too, otherwise two approved-but-unpaid orders can overshoot.
    settings_row = db.get_settings()
    cap = float(settings_row.get("monthly_cap_usd") or 0)
    check = assess_cap(db, float(amount), cap)
    if check.over_paid or check.over_reserved:
        reason = (
            hard_cap_reason(check.paid, cap)
            if check.over_paid
            else reserved_reason(check.committed, cap)
        )
        entry_id = db.add_entry(
            raw_text=text,
            amount_usd=float(amount),
            category=category,
            decision="decline",
            reason=reason,
            status="declined",
        )
        return {
            "decision": "decline",
            "reason": reason,
            "amount": float(amount),
            "item": item,
            "category": category,
            "status": "declined",
            "ledger_id": entry_id,
        }

    # 4. the model argues
    verdict = await brain.decide(
        rules=settings_row.get("rules", ""),
        ledger_lines=[_ledger_line(entry) for entry in db.since(30)],
        today=today_iso(),
        request_text=text,
        parsed=parsed,
    )

    # 5. decline / ask stop here
    if verdict["decision"] in {"decline", "ask"}:
        entry_id = db.add_entry(
            raw_text=text,
            amount_usd=float(amount),
            category=verdict["category"],
            decision=verdict["decision"],
            reason=verdict["reason"],
            status="declined",
        )
        return {
            "decision": verdict["decision"],
            "reason": verdict["reason"],
            "amount": float(amount),
            "item": item,
            "category": verdict["category"],
            "status": "declined",
            "ledger_id": entry_id,
        }

    # 6. approved -> PayPal order -> pending row -> buyer link
    order_id, approve_url = await state.paypal.create_order(float(amount), text)
    entry_id = db.add_entry(
        raw_text=text,
        amount_usd=float(amount),
        category=verdict["category"],
        decision="approve",
        reason=verdict["reason"],
        status="pending",
        paypal_order_id=order_id,
    )
    return {
        "decision": "approve",
        "reason": verdict["reason"],
        "amount": float(amount),
        "item": item,
        "category": verdict["category"],
        "status": "pending",
        "approve_url": approve_url,
        "order_id": order_id,
        "ledger_id": entry_id,
    }


# -- paypal return legs ----------------------------------------------------
@app.get("/paypal/return")
async def paypal_return(request: Request, token: str = ""):
    db: Database = request.app.state.db
    entry = db.by_order_id(token) if token else None
    if not entry:
        return _flash_redirect("PayPal came back with an order I don't know about.")

    if entry["status"] == "paid":
        tip = f" · capture {entry['paypal_capture_id']}" if entry.get("paypal_capture_id") else ""
        return _flash_redirect(f"This one is already paid ({money(entry.get('amount_usd'))}){tip}.")

    if entry["status"] != "pending":
        return _flash_redirect(f"That order is {entry['status']}; nothing was captured.")

    # Second cap check: the rules may have changed between approval and payment.
    settings_row = db.get_settings()
    cap = float(settings_row.get("monthly_cap_usd") or 0)
    amount = float(entry.get("amount_usd") or 0)
    paid_now = db.month_paid_usd()
    if round(paid_now + amount, 2) > round(cap, 2):
        reason = hard_cap_reason(paid_now, cap) + " — refused at capture; authorization left uncaptured"
        db.mark_capture_failed(entry["id"], reason)
        return _flash_redirect(
            f"Capture refused. {hard_cap_reason(paid_now, cap)} — nothing was charged."
        )

    try:
        capture_id = await request.app.state.paypal.capture(token)
    except PayPalError as exc:
        db.mark_capture_failed(entry["id"], f"{exc.name}: {exc}")
        return _flash_redirect(f"Capture failed: {exc}")

    db.mark_paid(entry["id"], capture_id)
    return _flash_redirect(
        f"Paid {money(entry.get('amount_usd'))} · capture {capture_id[:10]}…"
    )


@app.get("/paypal/cancel")
async def paypal_cancel(request: Request, token: str = ""):
    db: Database = request.app.state.db
    entry = db.by_order_id(token) if token else None
    if entry and entry["status"] == "pending":
        db.mark_cancelled(entry["id"])
        return _flash_redirect("Cancelled at PayPal. Nothing was charged.")
    return _flash_redirect("Cancelled. Nothing was charged.")


@app.get("/healthz")
async def healthz(request: Request):
    db: Database = request.app.state.db
    settings_row = db.get_settings()
    return {
        "ok": bool(settings_row),
        "paypal": request.app.state.settings.paypal_env,
        "llm": request.app.state.settings.llm_model,
        "db": "ok" if settings_row else "missing",
    }
