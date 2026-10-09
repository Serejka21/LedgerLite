"""Bonus 2: at most N POST requests per minute per API key (sliding window).

In-memory and per-process: correct for the single-worker setup here. With several
workers/replicas this would move to Redis (e.g. a sorted set per key).
"""

from __future__ import annotations

import hashlib
import math
import time
from collections import deque
from collections.abc import Callable

from starlette.types import ASGIApp, Receive, Scope, Send

from app.errors import error_response

EXEMPT_PREFIXES = ("/v1/webhooks/",)
WINDOW_SECONDS = 60.0


class SlidingWindowLimiter:
    def __init__(
        self,
        limit: int,
        window: float = WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window = window
        self.clock = clock
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str) -> float | None:
        """Registers a request. Returns None if allowed, else seconds to wait."""
        now = self.clock()
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return hits[0] + self.window - now
        hits.append(now)
        return None


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp, limiter: SlidingWindowLimiter) -> None:
        self.app = app
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or scope["path"].startswith(EXEMPT_PREFIXES)
        ):
            await self.app(scope, receive, send)
            return

        api_key = dict(scope["headers"]).get(b"x-api-key")
        if not api_key:  # will be rejected with 401 by the endpoint
            await self.app(scope, receive, send)
            return

        bucket = hashlib.sha256(api_key).hexdigest()
        retry_after = self.limiter.hit(bucket)
        if retry_after is None:
            await self.app(scope, receive, send)
            return

        seconds = max(1, math.ceil(retry_after))
        response = error_response(
            429,
            "rate_limited",
            "Too many requests",
            {"limit_per_minute": self.limiter.limit, "retry_after_seconds": seconds},
            headers={"Retry-After": str(seconds)},
        )
        await response(scope, receive, send)
