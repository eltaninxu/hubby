"""The side panel fragment, and rows where no money moved.

Display only: the pot arithmetic is in test_flow.py. What is checked here is
that the page and /fragments/sidepanel share one partial, that the fragment
catches up with the ledger, and that a +/- sign appears on exactly the rows the
balance counts.
"""

import re

import pytest

from app.db import RELEASE_MONEY_STATUSES, money_moved
from app.main import amount_view
from tests.test_flow import ask, fund


def fragment(client) -> str:
    response = client.get("/fragments/sidepanel")
    assert response.status_code == 200, response.text
    return response.text


def list_item(html: str, needle: str) -> str:
    """The one <li> that mentions `needle`."""
    found = [li for li in re.findall(r"<li\b.*?</li>", html, re.S) if needle in li]
    assert len(found) == 1, f"{len(found)} <li> mention {needle!r}"
    return found[0]


def table_row(html: str, needle: str) -> str:
    found = [tr for tr in re.findall(r"<tr\b.*?</tr>", html, re.S) if needle in tr]
    assert len(found) == 1, f"{len(found)} <tr> mention {needle!r}"
    return found[0]


def amount_cell(row: str) -> str:
    return re.search(r'<td class="num">(.*?)</td>', row, re.S).group(1)


def start_funding(client, amount: float) -> str:
    response = client.post("/api/fund", json={"amount": amount})
    assert response.status_code == 200, response.text
    return response.json()["order_id"]


# -- the fragment ------------------------------------------------------------
def test_fragment_is_html_and_not_cached(client):
    response = client.get("/fragments/sidepanel")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    html = response.text
    # the parts chat.js swaps in, by id
    for live in ('id="pot-value"', 'id="pot-totals"', 'id="recent"'):
        assert live in html
    assert "<html" not in html  # a fragment, not a page


def test_page_and_fragment_share_one_partial(client):
    fund(client, 20.0)
    ask(client, "fried chicken drumstick $6")

    side = fragment(client)
    page = client.get("/").text

    assert side.strip() in page
    assert page.count('id="pot-value"') == 1  # rendered once, not copied beside the partial


def test_fragment_catches_up_with_the_ledger(client, brain, db):
    before = fragment(client)
    assert 'data-pot="0.00"' in before

    fund(client, 20.0)
    brain.extract_result = {"amount_usd": 6.0, "item": "drumstick", "category": "food"}
    ask(client, "fried chicken drumstick, six dollars")

    after = fragment(client)
    assert 'data-pot="14.00"' in after
    assert ">$14.00</strong>" in after
    # month totals come from the same ledger the page reads (the seed rows move with the date)
    assert f"deposited this month</span><span>${db.month_deposits():.2f}" in after
    assert f"released this month</span><span>${db.month_releases():.2f}" in after
    assert db.month_deposits() >= 20.0 and db.month_releases() >= 6.0
    assert re.search(r'class="sub"><span>food</span><span>\$6\.00', after)
    # the newest rows are in Last 6, newest first: release, then its request, then the deposit
    items = re.findall(r"<li\b.*?</li>", after.split('id="recent"')[1], re.S)
    assert "fried chicken drumstick, six dollars" in items[0] and "−$6.00" in items[0]
    assert "fried chicken drumstick, six dollars" in items[1]
    assert "fund the pot $20.00" in items[2] and "+$20.00" in items[2]
    assert before != after


def test_fragment_is_the_same_numbers_as_the_pot_api(client):
    fund(client, 12.0)
    pot = client.get("/api/pot").json()["pot"]
    assert f'data-pot="{pot:.2f}"' in fragment(client)


# -- rows where no money moved -------------------------------------------------
def test_cancelled_deposit_has_no_plus_and_says_cancelled(client, db):
    order = start_funding(client, 7.0)
    client.get(f"/paypal/cancel?token={order}")
    assert db.pot_balance() == 0.0

    side = fragment(client)
    row = list_item(side, "fund the pot $7.00")
    assert "+" not in row
    assert "$7.00" in row
    assert "cancelled" in row
    assert "+$7.00" not in side

    ledger = client.get("/ledger").text
    tr = table_row(ledger, "fund the pot $7.00")
    cell = amount_cell(tr)
    assert "+" not in cell and "−" not in cell
    assert "$7.00" in cell and "cancelled" in cell
    assert "+$7.00" not in ledger
    # the status pill is still there
    assert '<span class="status cancelled">cancelled</span>' in tr


@pytest.mark.parametrize(
    "kind, status, label",
    [
        ("deposit", "pending", "awaiting payment"),
        ("deposit", "cancelled", "cancelled"),
        ("deposit", "capture_failed", "capture failed"),
        ("release", "failed", "payout failed"),
    ],
)
def test_rows_where_no_money_moved_carry_no_sign_in_both_places(client, db, kind, status, label):
    db.add_entry(
        kind=kind,
        raw_text="the row under test",
        amount_usd=3.25,
        category="other",
        decision="deposit" if kind == "deposit" else "approve",
        reason="test",
        status=status,
    )

    li = list_item(fragment(client), "the row under test")
    assert "+" not in li and "−" not in li
    assert "$3.25" in li and label in li

    cell = amount_cell(table_row(client.get("/ledger").text, "the row under test"))
    assert "+" not in cell and "−" not in cell
    assert "$3.25" in cell and label in cell


def test_paid_deposit_keeps_its_plus_sign(client, db):
    fund(client, 20.0)

    li = list_item(fragment(client), "fund the pot $20.00")
    assert "+$20.00" in li
    assert "no-money" not in li

    cell = amount_cell(table_row(client.get("/ledger").text, "fund the pot $20.00"))
    assert cell.strip() == "+$20.00"


def test_released_release_keeps_its_minus_sign(client, brain, db):
    fund(client, 20.0)
    ask(client, "fried chicken drumstick $6")
    release = [e for e in db.all_entries() if e["kind"] == "release" and e["reason"] != "seed"][0]
    assert release["status"] == "released"

    li = [x for x in re.findall(r"<li\b.*?</li>", fragment(client), re.S) if "−$6.00" in x]
    assert len(li) == 1 and "no-money" not in li[0]

    cells = [
        amount_cell(tr)
        for tr in re.findall(r"<tr\b.*?</tr>", client.get("/ledger").text, re.S)
        if 'class="kind release"' in tr and "drumstick" in tr
    ]
    assert [c.strip() for c in cells] == ["−$6.00"]


@pytest.mark.parametrize("status", ["pending", "unclaimed"])
def test_a_payout_still_in_flight_keeps_its_minus_sign(client, brain, paypal, status):
    # these already left the pot (see test_flow), so they still read as money out
    paypal.payout_result = (status, {})
    fund(client, 20.0)
    ask(client, "fried chicken drumstick $6")

    assert client.get("/api/pot").json()["pot"] == 14.0
    assert "−$6.00" in fragment(client)
    ledger = client.get("/ledger").text
    cells = [
        amount_cell(tr).strip()
        for tr in re.findall(r"<tr\b.*?</tr>", ledger, re.S)
        if 'class="kind release"' in tr and "drumstick" in tr
    ]
    assert cells == ["−$6.00"]


def test_failed_payout_row_has_no_minus_sign(client, brain, paypal, db):
    paypal.payout_result = ("failed", {})
    fund(client, 20.0)
    ask(client, "fried chicken drumstick $6")
    assert db.pot_balance() == 20.0

    side = fragment(client)
    assert "−$6.00" not in side
    release = [
        li for li in re.findall(r"<li\b.*?</li>", side, re.S) if "payout failed" in li
    ]
    assert len(release) == 1 and "$6.00" in release[0]


def test_request_rows_are_unchanged(client, db):
    ask(client, "fried chicken drumstick $6")  # declined: the pot is empty

    li = list_item(fragment(client), "fried chicken drumstick $6")
    assert re.search(r'mini-amount">\s*\$6\.00\s*<', li)
    assert "no-money" not in li


# -- display and arithmetic cannot disagree --------------------------------------
KINDS_AND_STATUSES = [
    ("deposit", "pending"),
    ("deposit", "paid"),
    ("deposit", "cancelled"),
    ("deposit", "capture_failed"),
    ("deposit", "never-heard-of-it"),
    ("release", "pending"),
    ("release", "released"),
    ("release", "unclaimed"),
    ("release", "failed"),
    ("release", "never-heard-of-it"),
]


@pytest.mark.parametrize("kind, status", KINDS_AND_STATUSES)
def test_sign_appears_exactly_when_the_balance_counts_the_row(client, db, kind, status):
    before = db.pot_balance()
    db.add_entry(
        kind=kind,
        raw_text="parity row",
        amount_usd=3.0,
        category="other",
        decision="deposit" if kind == "deposit" else "approve",
        reason="test",
        status=status,
    )
    moved_in_sql = db.pot_balance() != before

    entry = [e for e in db.all_entries() if e["raw_text"] == "parity row"][0]
    view = amount_view(entry)
    assert money_moved(kind, status) is moved_in_sql
    assert bool(view["sign"]) is moved_in_sql
    assert (view["label"] is None) is moved_in_sql


def test_only_the_counted_release_statuses_reach_the_month_totals(client, db):
    # month_releases uses the same release statuses as the balance and as money_moved
    before = db.month_releases()
    for status in ("released", "pending", "unclaimed", "failed"):
        db.add_entry(
            kind="release",
            raw_text=f"month {status}",
            amount_usd=1.0,
            category="other",
            decision="approve",
            reason="test",
            status=status,
        )
    assert set(RELEASE_MONEY_STATUSES) == {"released", "pending", "unclaimed"}
    assert round(db.month_releases() - before, 2) == 3.0  # "failed" is the fourth, not counted
