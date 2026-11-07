"""A tiny fixed-window rate limiter.

The hosted demo is public and has no accounts (by design), so the two doors
that cost money or change the rules get a per-address limit. In-memory is fine:
one process, one household.
"""

from __future__ import annotations

import time
from collections import defaultdict


def parse_limit(spec: str, *, default_limit: int, default_window: int) -> tuple[int, int]:
    """'40/600' -> (40, 600). Falls back to the defaults on junk."""
    try:
        limit_text, window_text = spec.split("/", 1)
        limit, window = int(limit_text), int(window_text)
        if limit > 0 and window > 0:
            return limit, window
    except (ValueError, AttributeError):
        pass
    return default_limit, default_window


class RateLimiter:
    def __init__(self, limits: dict[str, tuple[int, int]]) -> None:
        self._limits = limits
        self._hits: dict[tuple[str, str], list[float]] = defaultdict(list)

    def allow(self, scope: str, key: str) -> tuple[bool, int]:
        """Returns (allowed, retry_after_seconds)."""
        limit, window = self._limits.get(scope, (0, 0))
        if limit <= 0:
            return True, 0
        now = time.monotonic()
        bucket = self._hits[(scope, key)]
        cutoff = now - window
        bucket[:] = [stamp for stamp in bucket if stamp > cutoff]
        if len(bucket) >= limit:
            retry_after = max(1, int(bucket[0] + window - now) + 1)
            return False, retry_after
        bucket.append(now)
        return True, 0
