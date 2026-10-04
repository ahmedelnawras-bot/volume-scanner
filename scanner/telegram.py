"""Telegram delivery + message formatting (layout from the white paper)."""
from __future__ import annotations

import logging
import time

import aiohttp

log = logging.getLogger(__name__)


# formatting lives in messages.py; re-exported for older imports
from .messages import format_signal, fp  # noqa: E402,F401
from . import messages  # noqa: E402

__all__ = ["Telegram", "format_signal", "fp"]


class Telegram:
    def __init__(self, session: aiohttp.ClientSession, token: str, chat_id: str,
                 status_chat_id: str, dry_run: bool = False, lang: str = "ar"):
        self.session = session
        self.token = token
        self.chat_id = chat_id
        self.status_chat_id = status_chat_id or chat_id
        self.dry_run = dry_run or not token or not chat_id
        self._last_err: dict[str, float] = {}
        self.lang = lang

    async def _send(self, chat_id: str, text: str):
        if self.dry_run:
            print("\n----- TELEGRAM (dry run) -----\n" + text + "\n------------------------------")
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        for attempt in range(3):
            try:
                async with self.session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status == 429:
                        data = await r.json(content_type=None)
                        wait = data.get("parameters", {}).get("retry_after", 3)
                        await _sleep(wait)
                        continue
                    if r.status >= 400:
                        log.error("telegram %s: %s", r.status, (await r.text())[:300])
                    return
            except Exception as e:  # never let Telegram kill a scan
                log.error("telegram send failed: %s", e)
                await _sleep(2)

    async def signal(self, text: str):
        await self._send(self.chat_id, text)

    async def status(self, text: str):
        await self._send(self.status_chat_id, text)

    async def error(self, key: str, text: str, throttle_s: int = 1800):
        """Immediate error alert, but the same error key at most once per 30 min."""
        now = time.time()
        if now - self._last_err.get(key, 0) < throttle_s:
            return
        self._last_err[key] = now
        await self._send(self.status_chat_id, messages.error(text, self.lang))


async def _sleep(s):
    import asyncio
    await asyncio.sleep(s)
