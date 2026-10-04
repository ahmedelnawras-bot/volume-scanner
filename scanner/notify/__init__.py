"""Everything user-facing lives in this package.

  terms.py            wording per language (edit this to change how alerts read)
  fmt.py              number / time formatting
  templates.py        data -> Telegram HTML text (pure functions)
  telegram_client.py  Bot API transport (send, retry, dry-run)
  notifier.py         Notifier: the single entry point engine/main use
"""
from __future__ import annotations

from .notifier import Notifier
from .telegram_client import TelegramClient


def build_notifier(cfg, session) -> Notifier:
    client = TelegramClient(session, cfg.TELEGRAM_BOT_TOKEN, cfg.DRY_RUN)
    return Notifier(client, cfg.TELEGRAM_CHAT_ID, cfg.TELEGRAM_STATUS_CHAT_ID,
                    cfg.ALERT_LANG, cfg.TIMEZONE)


__all__ = ["Notifier", "TelegramClient", "build_notifier"]
