"""The only object the rest of the bot uses to talk to the user.

engine.py / main.py call `notifier.signal(sig)`, `notifier.error(...)` etc.
They never build text and never touch Telegram directly, so wording,
language, channels or a second destination can change here without
touching scan logic.
"""
from __future__ import annotations

import time

from . import templates
from .telegram_client import TelegramClient


class Notifier:
    def __init__(self, client: TelegramClient, chat_id: str, status_chat_id: str = "",
                 lang: str = "ar", tz_name: str = "Africa/Cairo"):
        self.client = client
        self.chat_id = chat_id
        self.status_chat_id = status_chat_id or chat_id
        self.lang = lang if lang in ("ar", "en") else "ar"
        self.tz_name = tz_name
        self._last_err: dict[str, float] = {}

    @property
    def dry_run(self) -> bool:
        return self.client.dry_run or not self.chat_id

    # -- trading channel ---------------------------------------------------
    async def signal(self, sig: dict) -> bool:
        return await self.client.send(self.chat_id, templates.signal(sig, self.lang, self.tz_name))

    async def early(self, symbol: str, exchange: str, rvol: float, chg: float, price: float) -> bool:
        return await self.client.send(self.chat_id, templates.early(symbol, exchange, rvol, chg, price, self.lang))

    # -- status channel ----------------------------------------------------
    async def started(self, exchanges: list, memory: bool) -> bool:
        return await self.client.send(self.status_chat_id, templates.started(exchanges, memory, self.lang))

    async def heartbeat(self, stats: dict, exchanges: list) -> bool:
        return await self.client.send(self.status_chat_id, templates.heartbeat(stats, exchanges, self.lang))

    async def daily_summary(self, data: dict) -> bool:
        return await self.client.send(self.status_chat_id, templates.daily_summary(data, self.lang))

    async def error(self, key: str, ar: str, en: str | None = None, throttle_s: int = 1800) -> bool:
        """Immediate, but the same error key at most once per `throttle_s`."""
        now = time.time()
        if now - self._last_err.get(key, 0) < throttle_s:
            return False
        self._last_err[key] = now
        text = ar if self.lang == "ar" or en is None else en
        return await self.client.send(self.status_chat_id, templates.error(text, self.lang))
