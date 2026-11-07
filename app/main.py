"""FastAPI app: chat, ledger, settings, and the PayPal return legs."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.budget import cap_label, exceeds_cap, hard_cap_reason, money
from app.config import ConfigError, get_settings
from app.db import Database, today_iso
from app.llm import LLMError, HubbyBrain
from app.paypal import PayPalError, client_from_settings

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["money"] = money
templates.env.globals["cap_label"] = cap_label


def _seed_enabled() -> bool:
    return os.environ.get("HUBBY_SEED", "1").strip().lower() not in {"0", "false", "no"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        settings = get_settings()
    except ConfigError as exc:
        raise RuntimeError(f"Cannot start Hubby: {exc}") from exc

    db = Database(settings.db_path)
    db.init(settings.monthly_cap_usd, seed=_seed_enabled())
    app.state.settings = settings
    app.state.db = db
    app.state.brain = HubbyBrain(settings)
    app.state.paypal = client_from_settings(settings)
    yield


app = FastAPI(title="Hubby", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# -- helpers ---------------------------------------------------------------
def _flash_redirect(message: str) -> RedirectResponse:
    return RedirectResponse(url=f"/?flash={quote(message)}", status_code=303)


def _ledger_line(entry: dict[str, Any]) -> str:
    stamp = (entry.get("paid_at") or entry.get("created_at") or "")[:10]
    category = entry.get("category") or "other"
    return f"{stamp} | {entry.get('raw_text', '')} | {money(entry.get('amount_usd'))} | {category} | {entry.get('status')}"


def _budget_view(db: Database) -> dict[str, Any]:
    row = db.get_settings()
    used = db.month_paid_usd()
    cap = float(row.get("monthly_cap_usd") or 0)
    percent = min(100.0, round(used / cap * 100, 1)) if cap > 0 else 0.0
    return {
        "used": used,
        "cap": cap,
        "percent": percent,
        "over": used > cap,
        "categories": db.category_totals(),
    }


# -- pages -----------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def chat_page(request: Request, flash: str | None = None):
    db: Database = request.app.state.db
    return templates.TemplateResponse(
        request,
        "chat.html",
        {"flash": flash, "budget": _budget_view(db), "recent": db.recent(5)},
    )


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
    payload: dict[str, Any] = {}
    try:
        payload = await request.json()
    except ValueError:
        pass
    text = str(payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "Say what you want to buy."}, status_code=400)

    state = request.app.state
    try:
        result = await _handle_purchase(state, text)
    except LLMError as exc:
        return JSONResponse(
            {"error": f"Hubby can't reach its brain right now: {exc}"}, status_code=502
        )
    except PayPalError as exc:
        return JSONResponse({"error": f"PayPal refused: {exc}"}, status_code=502)
    return JSONResponse(result)


async def _handle_purchase(state: Any, text: str) -> dict[str, Any]:
    db: Database = state.db
    brain: HubbyBrain = state.brain

    # 1. facts out of the sentence
    parsed = await brain.extract(text)
    amount = parsed.get("amount_usd")
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
            "category": category,
            "status": "declined",
            "ledger_id": entry_id,
        }

    # 3. hard cap, in code, before the model gets a say
    settings_row = db.get_settings()
    cap = float(settings_row.get("monthly_cap_usd") or 0)
    over, used = exceeds_cap(db, float(amount), cap)
    if over:
        reason = hard_cap_reason(used, cap)
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
        monthly_cap=cap,
        month_used=used,
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
