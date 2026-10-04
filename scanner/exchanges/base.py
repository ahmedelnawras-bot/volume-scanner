"""Shared async HTTP plumbing: concurrency cap, retries, rate-limit backoff."""
from __future__ import annotations

import asyncio
import logging
import random

import aiohttp

log = logging.getLogger(__name__)


class ExchangeError(Exception):
    pass


class GeoBlocked(ExchangeError):
    """Binance returns 451 from restricted regions (e.g. US Railway regions)."""


class RateLimited(ExchangeError):
    pass


class Pacer:
    """Spaces requests so each bucket stays under `count` calls per `period` s."""

    def __init__(self, count: int, period: float):
        self.interval = period / count
        self.next_at = 0.0
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            if self.next_at > now:
                await asyncio.sleep(self.next_at - now)
                now = loop.time()
            self.next_at = now + self.interval


class BaseExchange:
    name = "base"
    base_url = ""
    # (path prefix, max calls, per seconds) — first match wins, ~80% of the
    # exchange's published limit to leave headroom. Override per exchange.
    RATE_LIMITS: list = [("", 10, 1.0)]

    def __init__(self, session: aiohttp.ClientSession, concurrency: int = 8, timeout: int = 15):
        self.session = session
        self.sem = asyncio.Semaphore(concurrency)
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.request_count = 0
        self.rate_limited = 0
        self._pacers = {p: Pacer(n, t) for p, n, t in self.RATE_LIMITS}

    def _pacer(self, path: str) -> Pacer:
        for prefix, _, _ in self.RATE_LIMITS:
            if path.startswith(prefix):
                return self._pacers[prefix]
        return self._pacers[self.RATE_LIMITS[-1][0]]

    async def _get(self, path: str, params: dict | None = None, retries: int = 3):
        url = self.base_url + path
        delay = 1.0
        for attempt in range(retries + 1):
            await self._pacer(path).wait()
            async with self.sem:
                try:
                    self.request_count += 1
                    async with self.session.get(url, params=params, timeout=self.timeout) as r:
                        if r.status == 451:
                            raise GeoBlocked(f"{self.name}: HTTP 451 (region blocked)")
                        if r.status in (418, 429):
                            wait = float(r.headers.get("Retry-After", delay * 2))
                            if attempt == retries:
                                raise RateLimited(f"{self.name}: rate limited on {path}")
                            self.rate_limited += 1
                            log.debug("%s rate limited on %s, sleeping %.1fs", self.name, path, wait)
                            await asyncio.sleep(wait)
                            delay *= 2
                            continue
                        if r.status >= 500:
                            raise ExchangeError(f"{self.name}: HTTP {r.status} on {path}")
                        data = await r.json(content_type=None)
                        if r.status >= 400:
                            raise ExchangeError(f"{self.name}: HTTP {r.status} {str(data)[:200]}")
                        return self._unwrap(data, path)
                except GeoBlocked:
                    raise
                except RateLimited:
                    self.rate_limited += 1
                    if attempt == retries:
                        raise
                    await asyncio.sleep(delay * 2)
                    delay *= 2
                    continue
                except (aiohttp.ClientError, asyncio.TimeoutError, ExchangeError, ValueError) as e:
                    if attempt == retries:
                        raise ExchangeError(f"{self.name}: {path} failed: {e}") from e
            await asyncio.sleep(delay + random.random() * 0.3)
            delay *= 2

    def _unwrap(self, data, path):
        return data
