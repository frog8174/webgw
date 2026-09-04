"""Concurrency and rate limiting.

Why this is needed: upstream crawl4ai logs `work queue (per_principal=unlimited)`
at startup -- it applies no throttling of its own. Every web_fetch opens a real
browser tab, which is a heavy memory and CPU load. Without a ceiling, a handful
of concurrent requests is enough to take the upstream down.

The two layers are separate because they guard against different things:
    concurrency  how many fetches run at once -- protects upstream resources
    rate         requests per minute per caller -- stops one client from
                 consuming the whole quota
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque


class ConcurrencyLimiter:
    """Caps how many fetches run at the same time.

    Backed by asyncio.Semaphore, so requests over the limit queue up instead of
    being rejected. Fetching is inherently slow (measured 0.3-21 seconds), so
    waiting a little is far more useful to an agent than an outright failure.
    """

    def __init__(self, limit: int) -> None:
        self._sem = asyncio.Semaphore(limit) if limit > 0 else None
        self.limit = limit

    async def __aenter__(self):
        if self._sem is not None:
            await self._sem.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._sem is not None:
            self._sem.release()


class RateLimiter:
    """Per-caller request ceiling over a sliding window.

    Sliding window: timestamps of each request are kept, old ones outside the
    window are dropped, and the request is refused once the count exceeds the
    limit. More accurate than a fixed window, which lets twice the traffic
    through at a window boundary.
    """

    def __init__(self, max_requests: int, window_s: float = 60.0) -> None:
        self.max_requests = max_requests
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        if self.max_requests <= 0:
            return True
        now = time.monotonic()
        hits = self._hits[key]
        cutoff = now - self.window_s
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= self.max_requests:
            return False
        hits.append(now)
        return True

    def retry_after_s(self, key: str) -> int:
        """Seconds until quota frees up, so the caller gets a concrete wait."""
        hits = self._hits.get(key)
        if not hits:
            return 0
        return max(1, int(self.window_s - (time.monotonic() - hits[0])) + 1)

    def prune(self) -> None:
        """Drop callers with no recent activity so the dict cannot grow forever."""
        cutoff = time.monotonic() - self.window_s
        for key in [k for k, v in self._hits.items() if not v or v[-1] < cutoff]:
            self._hits.pop(key, None)
