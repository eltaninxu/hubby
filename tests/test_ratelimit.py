from app.ratelimit import RateLimiter, parse_limit


def test_parse_limit():
    assert parse_limit("40/600", default_limit=1, default_window=1) == (40, 600)
    assert parse_limit("junk", default_limit=5, default_window=10) == (5, 10)
    assert parse_limit("0/60", default_limit=5, default_window=10) == (5, 10)


def test_limiter_blocks_after_limit():
    limiter = RateLimiter({"api": (2, 60)})

    assert limiter.allow("api", "1.2.3.4")[0] is True
    assert limiter.allow("api", "1.2.3.4")[0] is True
    allowed, retry_after = limiter.allow("api", "1.2.3.4")

    assert allowed is False
    assert retry_after >= 1
    assert limiter.allow("api", "5.6.7.8")[0] is True


def test_daily_budget_caps_the_day():
    from app.ratelimit import DailyBudget

    budget = DailyBudget(2)
    assert budget.take() == (True, 1)
    assert budget.take() == (True, 0)
    assert budget.take() == (False, 0)
    assert DailyBudget(0).take()[0] is True


def test_client_ip_uses_the_last_proxy_hop():
    from types import SimpleNamespace

    from app.main import _client_ip

    spoofed = SimpleNamespace(
        headers={"x-forwarded-for": "9.9.9.9, 203.0.113.7"}, client=None
    )
    assert _client_ip(spoofed) == "203.0.113.7"

    direct = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
    assert _client_ip(direct) == "127.0.0.1"
