"""Telegram Bot API transport only: send text, retry, dry-run. No wording here."""
from __future__ import annotations

import asyncio
import logging

import aiohttp

log = logging.getLogger(__name__)

MAX_LEN = 4000  # Telegram hard limit is 4096


class TelegramClient:
    def __init__(self, session: aiohttp.ClientSession, token: str, dry_run: bool = False):
        self.session = session
        self.token = token
        self.dry_run = dry_run or not token
        self.sent = 0
        self.failed = 0

    async def send(self, chat_id: str, text: str) -> bool:
        if len(text) > MAX_LEN:
            text = text[:MAX_LEN - 3] + "..."
        if self.dry_run or not chat_id:
            print("\n----- TELEGRAM (dry run) -----\n" + text.replace("‎", "")
                  + "\n------------------------------")
            return True
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        for _ in range(3):
            try:
                async with self.session.post(url, json=payload,
                                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status == 429:
                        data = await r.json(content_type=None)
                        await asyncio.sleep(data.get("parameters", {}).get("retry_after", 3))
                        continue
                    if r.status >= 400:
                        body = (await r.text())[:300]
                        log.error("telegram %s: %s", r.status, body)
                        if r.status == 400 and "parse" in body.lower():
                            payload.pop("parse_mode", None)  # bad HTML: resend as plain text
                            continue
                        self.failed += 1
                        return False
                    self.sent += 1
                    return True
            except Exception as e:  # never let Telegram kill a scan
                log.error("telegram send failed: %s", e)
                await asyncio.sleep(2)
        self.failed += 1
        return False
