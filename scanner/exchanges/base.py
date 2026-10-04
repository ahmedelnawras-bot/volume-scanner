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


class BaseExchange:
    name = "base"
    base_url = ""

    def __init__(self, session: aiohttp.ClientSession, concurrency: int = 8, timeout: int = 15):
        self.session = session
        self.sem = asyncio.Semaphore(concurrency)
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.request_count = 0

    async def _get(self, path: str, params: dict | None = None, retries: int = 3):
        url = self.base_url + path
        delay = 1.0
        for attempt in range(retries + 1):
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
                            log.warning("%s rate limited, sleeping %.1fs", self.name, wait)
                            await asyncio.sleep(wait)
                            delay *= 2
                            continue
                        if r.status >= 500:
                            raise ExchangeError(f"{self.name}: HTTP {r.status} on {path}")
                        data = await r.json(content_type=None)
                        if r.status >= 400:
                            raise ExchangeError(f"{self.name}: HTTP {r.status} {str(data)[:200]}")
                        return self._unwrap(data, path)
                except (GeoBlocked, RateLimited):
                    raise
                except (aiohttp.ClientError, asyncio.TimeoutError, ExchangeError, ValueError) as e:
                    if attempt == retries:
                        raise ExchangeError(f"{self.name}: {path} failed: {e}") from e
            await asyncio.sleep(delay + random.random() * 0.3)
            delay *= 2

    def _unwrap(self, data, path):
        return data
