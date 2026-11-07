"""FastAPI app: chat, pot, ledger, settings, and both PayPal legs."""

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

from app.budget import money, pot_covers, pot_refusal_reason
from app.config import ConfigError, get_settings
from app.db import Database, today_iso
from app.llm import LLMError, HubbyBrain
from app.paypal import PayPalError, client_from_settings, payout_error_message
from app.ratelimit import RateLimiter

log = logging.getLogger("hubby")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["money"] = money

# "3", "$3", "about 3.50", "3 dollars" -- an answer to "how much is it?"
FOLLOWUP_PRICE = re.compile(
    r"^\s*(?:it'?s|it is|about|around|maybe|only|just)?\s*\$?\s*"
    r"\d+(?:[.,]\d{1,2})?\s*(?:usd|dollars?|bucks?)?\s*$",
    re.IGNORECASE,
)
PENDING_TTL_SECONDS = 15 * 60
FUND_MAX_USD = 500.0


def _seed_enabled() -> bool:
    return os.environ.get("HUBBY_SEED", "1").strip().lower() not in {"0", "false", "no"}


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


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


async def _maintenance(app: FastAPI) -> None:
    settings = app.state.settings
    while True:
        await asyncio.sleep(settings.maintenance_seconds)
        try:
            db: Database = app.state.db
            if (
                settings.demo_reset
                and db.hours_since_last_entry() >= settings.demo_reset_after_hours
            ):
                db.reset_demo(settings.recipient_email)
                log.info("demo reset: pot and ledger back to seeded state")
            elif db.refresh_seed_dates():
                log.info("seed dates refreshed")
        except Exception:  # noqa: BLE001
            log.exception("maintenance failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        settings = get_settings()
    except ConfigError as exc:
        raise RuntimeError(f"Cannot start Hubby: {exc}") from exc

    db = Database(settings.db_path)
    db.init(recipient_email=settings.recipient_email, seed=_seed_enabled())

    if settings.demo_reset and db.hours_since_last_entry() >= settings.demo_reset_after_hours:
        db.reset_demo(settings.recipient_email)
        log.info("demo reset on boot")
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
            "fund": settings.rate_fund,
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
    amount = money(entry.get("amount_usd"))
    kind = entry.get("kind") or "request"
    if kind == "deposit":
        return f"{stamp} | deposit | +{amount} | - | {entry.get('status')}"
    if kind == "release":
        category = entry.get("category") or "other"
        return f"{stamp} | {entry.get('raw_text', '')} | -{amount} | {category} | {entry.get('status')}"
    category = entry.get("category") or "other"
    return f"{stamp} | {entry.get('raw_text', '')} | {amount} | {category} | {entry.get('status')}"


def _pot_view(db: Database) -> dict[str, Any]:
    return {
        "pot": db.pot_balance(),
        "deposits": db.month_deposits(),
        "releases": db.month_releases(),
        "categories": db.category_totals(),
    }


def _rate_limited(request: Request, scope: str) -> JSONResponse | None:
    allowed, retry_after = request.app.state.limiter.allow(scope, _client_ip(request))
    if allowed:
        return None
    return JSONResponse(
        {"error": f"Too many requests from this address. Try again in {retry_after}s."},
        status_code=429,
        headers={"Retry-After": str(retry_after)},
    )


# -- pages -----------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def chat_page(request: Request, flash: str | None = None):
    db: Database = request.app.state.db
    response = templates.TemplateResponse(
        request,
        "chat.html",
        {"flash": flash, "pot": _pot_view(db), "recent": db.recent(6)},
    )
    if not request.cookies.get("hubby_sid"):
        response.set_cookie(
            "hubby_sid", uuid.uuid4().hex, httponly=True, samesite="lax", max_age=86_400
        )
    return response


@app.get("/ledger", response_class=HTMLResponse)
async def ledger_page(request: Request, flash: str | None = None):
    db: Database = request.app.state.db
    return templates.TemplateResponse(
        request,
        "ledger.html",
        {"flash": flash, "pot": _pot_view(db), "entries": db.all_entries()},
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, flash: str | None = None):
    db: Database = request.app.state.db
    return templates.TemplateResponse(
        request, "settings.html", {"flash": flash, "settings": db.get_settings()}
    )


@app.post("/settings")
async def save_settings(
    request: Request,
    rules: str = Form(""),
    recipient_email: str = Form(""),
):
    allowed, retry_after = request.app.state.limiter.allow("settings", _client_ip(request))
    if not allowed:
        return _flash_redirect(
            f"Too many rule changes from this address. Try again in {retry_after}s."
        )

    db: Database = request.app.state.db
    email = recipient_email.strip()
    if "@" not in email or "." not in email.split("@")[-1]:
        return _flash_redirect("Recipient email does not look like an email address.")
    if not rules.strip():
        return _flash_redirect("The rules cannot be empty.")
    db.save_settings(rules.strip(), email)
    return _flash_redirect("Saved.")


@app.get("/api/pot")
async def api_pot(request: Request):
    return {"pot": request.app.state.db.pot_balance()}


# -- funding the pot -------------------------------------------------------
@app.post("/api/fund")
async def api_fund(request: Request):
    limited = _rate_limited(request, "fund")
    if limited:
        return limited

    payload: dict[str, Any] = {}
    try:
        payload = await request.json()
    except ValueError:
        pass
    try:
        amount = round(float(payload.get("amount")), 2)
    except (TypeError, ValueError):
        return JSONResponse({"error": "How much do you want to put in the pot?"}, status_code=400)
    if amount <= 0 or amount > FUND_MAX_USD:
        return JSONResponse(
            {"error": f"Funding amount must be between $0.01 and ${FUND_MAX_USD:.0f}."},
            status_code=400,
        )

    state = request.app.state
    try:
        order_id, approve_url = await state.paypal.create_order(amount, "Fund the pot")
    except PayPalError as exc:
        return JSONResponse({"error": f"PayPal refused: {exc}"}, status_code=502)

    state.db.add_entry(
        kind="deposit",
        raw_text=f"fund the pot {money(amount)}",
        amount_usd=amount,
        category="other",
        decision="deposit",
        reason="Fund the pot",
        status="pending",
        paypal_order_id=order_id,
    )
    return {"approve_url": approve_url, "order_id": order_id, "pot": state.db.pot_balance()}


@app.get("/paypal/return")
async def paypal_return(request: Request, token: str = ""):
    db: Database = request.app.state.db
    entry = db.by_order_id(token) if token else None
    if not entry:
        return _flash_redirect("PayPal came back with an order I don't know about.")

    if entry["status"] == "paid":
        return _flash_redirect(
            f"This pot funding is already complete ({money(entry.get('amount_usd'))})."
        )
    if entry["status"] != "pending":
        return _flash_redirect(f"That funding is {entry['status']}; nothing changed.")

    try:
        capture_id = await request.app.state.paypal.capture(token)
    except PayPalError as exc:
        db.mark_deposit(entry["id"], "capture_failed")
        return _flash_redirect(f"Funding failed: {exc}")

    db.mark_deposit(entry["id"], "paid", capture_id)
    return _flash_redirect(
        f"Pot funded +{money(entry.get('amount_usd'))} · capture {capture_id[:10]}…"
    )


@app.get("/paypal/cancel")
async def paypal_cancel(request: Request, token: str = ""):
    db: Database = request.app.state.db
    entry = db.by_order_id(token) if token else None
    if entry and entry["status"] == "pending":
        db.mark_deposit(entry["id"], "cancelled")
        return _flash_redirect("Funding cancelled. The pot is unchanged.")
    return _flash_redirect("Cancelled. The pot is unchanged.")


# -- spending requests -----------------------------------------------------
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
    result["pot"] = state.db.pot_balance()
    return JSONResponse(result)


async def _handle_purchase(state: Any, text: str) -> dict[str, Any]:
    db: Database = state.db
    brain: HubbyBrain = state.brain

    # 1. facts out of the sentence
    parsed = await brain.extract(text)
    amount = parsed.get("amount_usd")
    item = parsed.get("item") or text
    category = parsed.get("category") or "other"

    # 2. no price -> ask, never move money
    if amount is None:
        reason = "How much is it?"
        db.add_entry(
            kind="request",
            raw_text=text,
            amount_usd=None,
            category=category,
            decision="ask",
            reason=reason,
            status="ask",
        )
        return {
            "decision": "ask",
            "reason": reason,
            "amount": None,
            "item": item,
            "category": category,
            "status": "ask",
        }

    # 3. the pot is the hard limit, in code, before the model gets a say
    covered, balance = pot_covers(db, float(amount))
    if not covered:
        reason = pot_refusal_reason(balance, float(amount))
        db.add_entry(
            kind="request",
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
        }

    # 4. the model argues
    settings_row = db.get_settings()
    verdict = await brain.decide(
        rules=settings_row.get("rules", ""),
        ledger_lines=[_ledger_line(entry) for entry in db.since(30)],
        today=today_iso(),
        request_text=text,
        parsed=parsed,
        pot_balance=balance,
    )

    # 5. decline / ask stop here
    if verdict["decision"] in {"decline", "ask"}:
        status = "declined" if verdict["decision"] == "decline" else "ask"
        db.add_entry(
            kind="request",
            raw_text=text,
            amount_usd=float(amount),
            category=verdict["category"],
            decision=verdict["decision"],
            reason=verdict["reason"],
            status=status,
        )
        return {
            "decision": verdict["decision"],
            "reason": verdict["reason"],
            "amount": float(amount),
            "item": item,
            "category": verdict["category"],
            "status": status,
        }

    # 6. approved -> put the money back in the user's hands
    recipient = settings_row.get("recipient_email") or state.settings.recipient_email
    request_id, release_id = db.add_request_with_release(
        raw_text=text,
        amount_usd=float(amount),
        category=verdict["category"],
        reason=verdict["reason"],
    )
    sender_batch_id = f"hubby-{release_id}-{int(time.time())}"
    try:
        batch_id = await state.paypal.create_payout(
            recipient_email=recipient,
            amount_usd=float(amount),
            note=item,
            subject=f"Hubby released {money(amount)}",
            sender_batch_id=sender_batch_id,
        )
    except PayPalError as exc:
        db.mark_release(release_id, status="failed", reason=f"{exc.name}: {exc}")
        return {
            "decision": "approve",
            "reason": verdict["reason"],
            "amount": float(amount),
            "item": item,
            "category": verdict["category"],
            "status": "failed",
            "error": f"Payout failed: {exc}",
            "request_id": request_id,
        }

    status, payout = await state.paypal.wait_for_payout(
        batch_id,
        attempts=state.settings.payout_poll_attempts,
        interval=state.settings.payout_poll_interval,
    )
    item_id = (payout or {}).get("payout_item_id")
    transaction_id = (payout or {}).get("transaction_id")
    if status == "released":
        db.mark_release(
            release_id,
            status="released",
            batch_id=batch_id,
            item_id=item_id,
            transaction_id=transaction_id,
            reason=f"released to {recipient}",
            mark_paid_at=True,
        )
    elif status == "failed":
        db.mark_release(
            release_id,
            status="failed",
            batch_id=batch_id,
            reason=payout_error_message(payout or {}) or "payout failed",
        )
    elif status == "unclaimed":
        db.mark_release(
            release_id,
            status="unclaimed",
            batch_id=batch_id,
            reason="recipient has no PayPal account for that email",
        )
    else:
        db.mark_release(
            release_id,
            status="pending",
            batch_id=batch_id,
            reason="PayPal is still processing this payout",
        )

    result: dict[str, Any] = {
        "decision": "approve",
        "reason": verdict["reason"],
        "amount": float(amount),
        "item": item,
        "category": verdict["category"],
        "status": status,
        "payout_batch_id": batch_id,
        "request_id": request_id,
    }
    if item_id:
        result["payout_item_id"] = item_id
    if status == "failed":
        result["error"] = payout_error_message(payout or {}) or "payout failed"
    return result


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
