"""Client-side rate limiting for the Last.fm API.

Last.fm's documented guidance is "approximately 5 requests per second per
originating IP", and the Terms of Service warn that sustained excess can suspend
the API key. We target 4 req/s with a small burst, enforced by one shared bucket
for the whole process so parallel requests cannot collectively exceed it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class TokenBucket:
    """An async token bucket with a steady rate and a bounded burst.

    Monotonic-clock based, so a wall-clock adjustment cannot produce a burst.
    """

    def __init__(self, rate_per_second: float, burst: int) -> None:
        if rate_per_second <= 0:
            msg = "rate_per_second must be positive"
            raise ValueError(msg)
        self._rate = rate_per_second
        self._capacity = float(max(burst, 1))
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def rate(self) -> float:
        return self._rate

    @property
    def capacity(self) -> float:
        return self._capacity

    def _refill(self, now: float) -> None:
        elapsed = max(now - self._updated, 0.0)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._updated = now

    async def acquire(self, tokens: float = 1.0) -> float:
        """Wait until ``tokens`` are available. Returns the seconds waited."""
        waited = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                self._refill(now)
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                deficit = tokens - self._tokens
                delay = deficit / self._rate
            # Sleep outside the lock so other waiters can re-check.
            await asyncio.sleep(delay)
            waited += delay

    async def run(self, operation: Callable[[], Awaitable[object]]) -> object:
        await self.acquire()
        return await operation()

    def time_until_available(self, tokens: float = 1.0) -> float:
        self._refill(time.monotonic())
        if self._tokens >= tokens:
            return 0.0
        return (tokens - self._tokens) / self._rate
