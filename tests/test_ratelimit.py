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
