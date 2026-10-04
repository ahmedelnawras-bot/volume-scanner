"""Entry point and scheduler.

  python -m scanner            run forever (Railway)
  python -m scanner --once     one hourly scan + follow-ups, then exit
  python -m scanner --light    one 15m scan (needs a prior hourly scan in the same run)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp

from .config import Config
from .engine import Scanner
from .state import State
from . import messages
from .telegram import Telegram

log = logging.getLogger("scanner")


async def build(cfg: Config):
    session = aiohttp.ClientSession(headers={"User-Agent": "volume-scanner/1.0"})
    state = await State.connect(cfg.REDIS_URL)
    tg = Telegram(session, cfg.TELEGRAM_BOT_TOKEN, cfg.TELEGRAM_CHAT_ID,
                  cfg.TELEGRAM_STATUS_CHAT_ID, cfg.DRY_RUN, cfg.ALERT_LANG)
    return session, state, tg, Scanner(cfg, session, state, tg)


async def run_forever(cfg: Config):
    session, state, tg, sc = await build(cfg)
    tz = ZoneInfo(cfg.TIMEZONE)
    await tg.status(messages.started(list(sc.ex), state.is_memory, cfg.ALERT_LANG))
    # scan once at boot so the universe and the light scan are ready
    await _guard(tg, "boot", sc.hourly_scan())
    last_hour = last_light = None
    try:
        while True:
            now = datetime.now(tz)
            utc_hour = int(time.time() // 3600)
            minute = now.minute

            if minute == cfg.HOURLY_SCAN_MINUTE and last_hour != utc_hour:
                last_hour = utc_hour
                await _guard(tg, "hourly", sc.hourly_scan())
                await _guard(tg, "followup", sc.followups())

                if utc_hour % cfg.HEARTBEAT_HOURS == 0:
                    await _guard(tg, "heartbeat", sc.heartbeat())

            elif (cfg.ENABLE_LIGHT_SCAN and minute != cfg.HOURLY_SCAN_MINUTE
                  and minute % cfg.LIGHT_SCAN_EVERY_MIN == cfg.HOURLY_SCAN_MINUTE
                  and last_light != (utc_hour, minute)):
                last_light = (utc_hour, minute)
                await _guard(tg, "light", sc.light_scan())

            if now.hour == cfg.DAILY_SUMMARY_HOUR_CAIRO and minute >= 5:
                day = now.strftime("%Y-%m-%d")
                if await state.get_flag("summary_day") != day:
                    await state.set_flag("summary_day", day, ex=3 * 86400)
                    await _guard(tg, "summary", sc.send_daily_summary())

            await asyncio.sleep(20)
    finally:
        await session.close()
        await state.close()


async def _guard(tg: Telegram, name: str, coro):
    try:
        return await coro
    except Exception as e:
        log.exception("%s job failed", name)
        await tg.error(f"job:{name}", f"{name} job failed / فشل: {e}")


async def run_once(cfg: Config, light: bool = False):
    session, state, tg, sc = await build(cfg)
    try:
        sent = await sc.hourly_scan()
        await sc.followups()
        print(f"\nHourly scan: {len(sent)} alert(s). Stats: {sc.last_stats}")
        if light:
            n = await sc.light_scan()
            print(f"15m scan: {n} early alert(s)")
    finally:
        await session.close()
        await state.close()


def cli():
    p = argparse.ArgumentParser(description="OKX + Binance 1h volume scanner")
    p.add_argument("--once", action="store_true", help="run one scan and exit")
    p.add_argument("--light", action="store_true", help="with --once: also run the 15m scan")
    p.add_argument("--dry-run", action="store_true", help="print alerts instead of sending")
    a = p.parse_args()

    cfg = Config.load()
    if a.dry_run:
        cfg.DRY_RUN = True
    logging.basicConfig(level=cfg.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("settings file: %s", cfg.ENV_FILE or "none found (.env)")
    if cfg.DRY_RUN or not cfg.TELEGRAM_BOT_TOKEN or not cfg.TELEGRAM_CHAT_ID:
        missing = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not getattr(cfg, k)]
        log.warning("Telegram DRY RUN - alerts print here only. Missing: %s",
                    ", ".join(missing) or "none (DRY_RUN is on)")
    else:
        tok = cfg.TELEGRAM_BOT_TOKEN
        log.info("Telegram ON -> chat %s (token %s...%s)", cfg.TELEGRAM_CHAT_ID, tok[:4], tok[-3:])
        if cfg.TELEGRAM_CHAT_ID == tok.split(":")[0]:
            log.error("TELEGRAM_CHAT_ID is the bot's own id - use YOUR id from @userinfobot")
    if a.once:
        asyncio.run(run_once(cfg, a.light))
    else:
        asyncio.run(run_forever(cfg))


if __name__ == "__main__":
    cli()
