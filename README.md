# Hubby

**The wallet keeper that can say no.**

You give Hubby your house rules in plain language and a monthly cap. After that,
every time someone wants to buy something they ask Hubby first. Hubby reads the
rules and the last 30 days of receipts, gives a verdict with a reason that quotes
the actual rule or the actual receipt, and — when the answer is yes — actually
takes the money through PayPal.

The part that matters: **the model argues, the code enforces.** The monthly cap
is checked in Python before the model is ever consulted, so a "yes" from the
model cannot blow the budget.

Built for the PayPal AI Hackathon. PayPal sandbox only.

---

## How it works

One request goes through four steps, in this order:

1. **Extract** — the model pulls `amount`, `item`, `category` out of the sentence.
   No price → Hubby asks "How much is it?" and stops.
2. **Hard cap** — plain Python: `this month's paid total + this amount > cap`?
   If yes, decline with a fixed message. The model is not called.
3. **Decide** — the model reads your rules, the last 30 days of ledger and
   today's date, and returns `approve | decline | ask` plus a one-or-two-sentence
   reason that has to point at a rule or a receipt.
4. **Pay** — on approve, Hubby creates a PayPal Orders v2 order, shows the buyer
   a "Pay with PayPal" button, and captures on return. Captured → `paid`;
   buyer cancelled → `cancelled`.

```
           ┌────────────┐   no price   ┌───────────────────┐
 request ─▶│  extract   │─────────────▶│ ask "How much?"   │
           └─────┬──────┘              └───────────────────┘
                 │ amount
           ┌─────▼──────┐   over cap   ┌───────────────────┐
           │  hard cap  │─────────────▶│ decline, code only│
           └─────┬──────┘              └───────────────────┘
                 │ under cap
           ┌─────▼──────┐  decline/ask ┌───────────────────┐
           │   decide   │─────────────▶│ log it, done      │
           └─────┬──────┘              └───────────────────┘
                 │ approve
           ┌─────▼──────┐   PayPal     ┌───────────────────┐
           │ create order│─────────────▶│ buyer pays → capture│
           └────────────┘               └───────────────────┘
```

## Quick start

You need Docker, a PayPal **sandbox** REST app, and a sandbox **personal**
(buyer) account. Both are free.

1. **Get sandbox API keys.** Go to
   <https://developer.paypal.com/dashboard/applications/sandbox>, create (or open)
   an app, and copy the **Client ID** and **Secret**.
2. **Create or find a sandbox personal account.** On the same dashboard, under
   *Accounts*, create a *Personal* account (or use the default `sb-...@personal.example.com`
   one) and note its email and password. You'll log in with it when paying.
3. **Configure.**
   ```bash
   git clone https://github.com/eltaninxu/hubby.git
   cd hubby
   cp .env.example .env
   $EDITOR .env          # fill PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, OPENAI_API_KEY, LLM_MODEL
   ```
   `OPENAI_API_KEY` can be any OpenAI-compatible endpoint; set `OPENAI_BASE_URL`
   if it isn't `api.openai.com`.
4. **Run.**
   ```bash
   docker compose up --build
   ```
5. **Open** <http://localhost:8080>, and check <http://localhost:8080/healthz>.

On first start Hubby seeds the demo rules and two receipts (yesterday's bubble
tea, today's Pepsi) so the first thing you type gets a real answer.

## What to try

| Type this | What should happen |
|---|---|
| `bubble tea $4.50` | **decline** — the rule says once every 3 days, and the ledger shows yesterday's cup |
| `fried chicken drumstick $6` | **approve** → a PayPal button → pay with the sandbox buyer account → back on `/` with "Paid $6.00 · capture …" |
| `a cake` | **ask** — "How much is it?", no order created |
| `bubble tea`, then `3` | the follow-up is read as the price of the same request, not a new one |
| Settings: set cap to `7`, then `lunch $8` | **decline** with `Hard cap: $11.70 of $7 used this month` — and the reason contains no model text |
| `dessert $5` | **decline** — "One sweet thing per day", citing today's Pepsi |
| Pay, then click *Cancel* on PayPal | ledger row becomes `cancelled`, month total unchanged |

The `/ledger` page shows every request: time, original words, amount, category,
verdict, reason, PayPal order/capture id, status — plus this month's total
against the cap and per-category subtotals.

## The hard cap

`app/budget.py` is deliberately tiny and boring:

```python
used = sum of this month's paid amounts
over = round(used + amount, 2) > round(cap, 2)
reason = f"Hard cap: ${used:.2f} of ${cap:g} used this month"
```

Nothing in the model's answer can move that number. The verdict call's
`amount_usd` is thrown away on purpose; the amount from the extraction call is
the only one that counts.

## Configuration

All settings live in `.env` (see `.env.example`).

| Variable | Required | Meaning |
|---|---|---|
| `PAYPAL_ENV` | yes | Only `sandbox` is supported |
| `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` | yes | Sandbox REST app credentials |
| `PAYPAL_RETURN_BASE` | yes | Public base URL PayPal returns to, e.g. `http://localhost:8080` |
| `OPENAI_API_KEY` | yes | Key for any OpenAI-compatible endpoint |
| `OPENAI_BASE_URL` | no | e.g. `http://localhost:4000/v1` for LiteLLM, or an Ollama `/v1` |
| `LLM_MODEL` | yes | A chat model your endpoint actually serves; it must support tool calling (e.g. `deepseek-chat` on LiteLLM) |
| `MONTHLY_CAP_USD` | no | Seed cap on first run (default `120`); after that use `/settings` |
| `HUBBY_PORT` | no | Host port for compose (default `8080`) |
| `HUBBY_DB` | no | SQLite path (default `data/hubby.db`) |
| `HUBBY_SEED` | no | `0` to start with an empty ledger |

The app refuses to start if a required value is missing, and tells you which one.

## Running without Docker

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
set -a; . ./.env; set +a
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080
```

## Using a local model gateway

If you already run LiteLLM (or anything else that speaks the OpenAI API) in
Docker, join its network so the container can reach it by service name:

```bash
docker compose -f docker-compose.yml -f docker-compose.with-llm-gateway.yml up -d
```

Then in `.env`:

```
OPENAI_BASE_URL=http://litellm:4000/v1
LLM_MODEL=deepseek-chat
```

and set `LLM_GATEWAY_NETWORK` to the network your gateway is on
(default `litellm_default`).

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
```

The tests use a fake model and a fake PayPal client, so they never touch the
network or spend sandbox money.

## Project layout

```
app/
  main.py       routes: chat, ledger, settings, /api/request, PayPal return/cancel
  llm.py        two model calls (extract, decide) over any OpenAI-compatible API
  budget.py     the hard cap — the only place money limits are enforced
  paypal.py     Orders v2 create/capture over httpx, token cache
  db.py         sqlite schema, seed, queries
  templates/    Jinja2 pages
  static/       one stylesheet, one small script
scripts/
  paypal_smoke.py   Day-1 smoke test: create a $1 order, capture it
tests/          pytest, offline
```

## Design notes and limits

- Single household, single user — no accounts, no auth. Don't put it on the open
  internet with real money.
- Sandbox only, and the code refuses to start otherwise.
- No webhooks: capture happens on the browser's return. An abandoned order stays
  `pending` and is shown as such in the ledger.
- No payouts, refunds, subscriptions or disputes. Orders create → approve →
  capture, nothing else.
- The chat transcript is not persisted (the ledger is). Reloading `/` starts a
  fresh conversation; every request is still in `/ledger`.
- The rules are plain text fed to the model as-is. Only the monthly cap is
  structured.

## License

MIT — see [LICENSE](LICENSE).
