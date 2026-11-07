# Hubby

**The wallet keeper that can say no.**

You put money in Hubby's pot. After that, every time you think about buying
something, you ask Hubby first. It reads your house rules and the recent
ledger, gives a verdict with a reason that quotes the actual rule or the actual
ledger line — and only when it says yes does PayPal move that exact amount back
into your hands.

The part that matters: **the model argues, the code enforces.** The pot balance
is checked in Python before the model is ever consulted, so a "yes" from the
model cannot spend money that is not there. And the money itself never leaves
PayPal until Hubby approves.

Built for the PayPal AI Hackathon. PayPal sandbox only.

---

## How it works

```
   you ──fund the pot──▶ Hubby's PayPal account        (Orders v2: create → approve → capture)
                                │
   you ──"fried chicken $6"────▶ extract → pot check → decide
                                │           │
                       not enough money     │ rules say yes
                                │           ▼
                                ▼     Payouts: send $6.00 back to you
                           "Fund it first."
```

1. **Fund the pot.** A real PayPal order: the buyer approves, Hubby captures,
   and a `deposit` row lands in the ledger. That money now sits in Hubby's
   PayPal account.
2. **Ask.** Every purchase request goes through three gates in order:
   - *extract* — the model pulls `amount`, `item`, `category` out of the
     sentence. No price → Hubby asks "How much is it?" and stops.
   - *pot check* — plain Python: is there enough in the pot? If not, decline
     with a fixed message. The model is not called.
   - *decide* — the model reads the rules, the pot balance and the last 30 days
     of ledger, and returns `approve | decline | ask` plus a reason that has to
     point at a rule or a ledger line.
3. **Release.** On approve, Hubby calls PayPal Payouts and sends exactly that
   amount to your PayPal account, then polls the payout until it settles. The
   ledger gets a `release` row.

## Quick start

You need Docker, a PayPal **sandbox** REST app, and a sandbox **personal**
(buyer) account. Both are free.

1. **Get sandbox API keys.** Go to
   <https://developer.paypal.com/dashboard/applications/sandbox>, create (or open)
   an app, and copy the **Client ID** and **Secret**. In the app's *Features*,
   make sure **Payouts** is enabled.
2. **Create or find a sandbox personal account.** Under *Accounts*, create a
   *Personal* account (or use the default `sb-...@personal.example.com` one).
   Its email is where Hubby releases money; its password is what you use to pay
   when funding the pot.
3. **Configure.**
   ```bash
   git clone https://github.com/eltaninxu/hubby.git
   cd hubby
   cp .env.example .env
   $EDITOR .env   # PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, HUBBY_RECIPIENT_EMAIL, OPENAI_API_KEY, LLM_MODEL
   ```
   `HUBBY_RECIPIENT_EMAIL` is the sandbox personal account. `OPENAI_API_KEY`
   can be any OpenAI-compatible endpoint; set `OPENAI_BASE_URL` if it isn't
   `api.openai.com`.
4. **Run.**
   ```bash
   docker compose up --build
   ```
5. **Open** <http://localhost:8080>, and check <http://localhost:8080/healthz>.

On first start Hubby seeds the demo rules, two past releases and the deposit
that covers them, so the pot starts at $0.00 and the first request you type
gets a real answer.

## What to try

| Step | Type this | What should happen |
|---|---|---|
| 1 | `fried chicken drumstick $6` | **decline** — `Pot has $0.00, you asked for $6.00. Fund it first.` The model was not asked |
| 2 | *Fund the pot* `20` → PayPal button → pay as the sandbox buyer | back on `/`: `Pot funded +$20.00 · capture …`, pot shows `$20.00` |
| 3 | `fried chicken drumstick $6` | **approve** → `Released $6.00 to you · payout …`; pot `$14.00`; log into the personal account and the $6.00 is in *Activity* (the payout arrives from the business account's display name — rename it in the sandbox dashboard if you want it to read "Hubby") |
| 4 | `bubble tea $4.50` | **decline** — the rule says once every 3 days, and the ledger shows yesterday's cup |
| 5 | `a cake` | **ask** — "How much is it?", no money moves |
| 6 | `lunch $15` | **decline** — `Pot has $14.00, you asked for $15.00. Fund it first.` |
| 7 | Fund the pot, then cancel on PayPal | the deposit becomes `cancelled`; the pot does not change |
| 8 | `/ledger` | deposit, release and declined rows together, with the pot balance on top |

## The pot

`app/budget.py` is deliberately tiny and boring:

```python
balance = sum(deposit rows with status paid)
        - sum(release rows with status released, pending or unclaimed)
```

Money leaves the pot the moment Hubby asks PayPal to send it: `pending` and
`unclaimed` count, not just `released`. Only a `failed` payout leaves the pot
alone. If PayPal later returns an unclaimed payout, Hubby does not reconcile it
automatically. Nothing the model says can move that number; the verdict call's
`amount_usd` is thrown away on purpose.

## Configuration

All settings live in `.env` (see `.env.example`).

| Variable | Required | Meaning |
|---|---|---|
| `PAYPAL_ENV` | yes | Only `sandbox` is supported |
| `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` | yes | Sandbox REST app credentials |
| `PAYPAL_RETURN_BASE` | yes | Public base URL PayPal returns to, e.g. `http://localhost:8080` |
| `HUBBY_RECIPIENT_EMAIL` | yes | PayPal email approved money is released to (the sandbox personal account) |
| `OPENAI_API_KEY` | yes | Key for any OpenAI-compatible endpoint |
| `OPENAI_BASE_URL` | no | e.g. `http://localhost:4000/v1` for LiteLLM, or an Ollama `/v1` |
| `LLM_MODEL` | yes | A chat model your endpoint serves and that supports tool calling (e.g. `deepseek-chat`) |
| `HUBBY_PORT` | no | Host port for compose (default `8080`) |
| `HUBBY_DB` | no | SQLite path (default `data/hubby.db`) |
| `HUBBY_SEED` | no | `0` to start with an empty ledger |
| `HUBBY_DEMO_RESET` | no | `1` to reset an idle demo back to the seeded state |
| `PAYOUT_POLL_ATTEMPTS` / `PAYOUT_POLL_INTERVAL` | no | How long to wait for a payout to settle (default 8 × 1.5s) |

The app refuses to start if a required value is missing, and tells you which one.
The recipient email can also be changed later on `/settings`.

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
(default `hubby_edge`).

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
```

The tests use a fake model and a fake PayPal client (including fake payouts), so
they never touch the network or move sandbox money.

## Project layout

```
app/
  main.py       routes: chat, pot, ledger, settings, PayPal return/cancel
  llm.py        two model calls (extract, decide) over any OpenAI-compatible API
  budget.py     the pot: the only budget, enforced in Python
  paypal.py     Orders create/capture and Payouts send/poll, over httpx
  db.py         sqlite schema (request / deposit / release), seed, queries
  templates/    Jinja2 pages
  static/       one stylesheet, one small script
scripts/
  paypal_smoke.py   token + $1 order, capture, and a $1 payout, in three steps
tests/            pytest, offline
```

## Design notes and limits

- Single household, single user — no accounts, no auth. Don't put it on the open
  internet with real money.
- Sandbox only, and the code refuses to start otherwise.
- The pot is computed from the ledger. It can differ from the real PayPal
  balance because of fees, refunds or sandbox quirks; no balance API is polled.
- `failed` payouts do not reduce the pot; `pending`, `released` and `unclaimed`
  ones do, because the money has already left the sending account. A payout
  PayPal returns after 30 days is not reconciled automatically.
- No webhooks: pot funding is captured on the browser's return; payouts are
  polled synchronously for a bounded window, then left `pending` if PayPal is
  slow. A payout is never re-sent automatically, and the same release row is
  never paid twice.
- No refunds, subscriptions or disputes. Orders in, Payouts out, nothing else.
- The rules are plain text fed to the model as-is. There is no monthly cap
  number: the cap is the pot.
- The chat transcript is not persisted (the ledger is).

## License

MIT — see [LICENSE](LICENSE).
