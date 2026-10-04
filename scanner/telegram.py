"""Telegram delivery + message formatting (layout from the white paper)."""
from __future__ import annotations

import html
import logging
import time

import aiohttp

log = logging.getLogger(__name__)


def fp(x) -> str:
    """Price with sensible significant digits."""
    if x is None:
        return "-"
    x = float(x)
    if x == 0:
        return "0"
    if abs(x) >= 1000:
        return f"{x:,.1f}"
    if abs(x) >= 1:
        return f"{x:.4f}".rstrip("0").rstrip(".")
    s = f"{x:.10f}"
    lead = len(s.split(".")[1]) - len(s.split(".")[1].lstrip("0"))
    return f"{x:.{lead + 4}f}".rstrip("0").rstrip(".")


def _pct(x, digits=1, sign=True):
    if x is None:
        return "n/a"
    return f"{x:+.{digits}f}%" if sign else f"{x:.{digits}f}%"


def _qty(x):
    if x >= 1000:
        return f"{x:,.0f}"
    if x >= 1:
        return f"{x:,.2f}"
    return f"{x:.4f}"


def format_signal(sig: dict) -> str:
    m, lv, plan = sig["metrics"], sig["levels"], sig.get("plan")
    ex = " + ".join(e.upper() for e in sig["exchanges"])
    conf = " (confirmed on both)" if len(sig["exchanges"]) > 1 else ""
    above = [n for n in ("5", "10", "20") if m.get(f"above_ma{n}")]
    below = [n for n in ("5", "10", "20") if m.get(f"above_ma{n}") is False]
    if len(above) == 3:
        ma_txt = "above MA5/10/20"
    elif len(below) == 3:
        ma_txt = "below MA5/10/20"
    else:
        ma_txt = f"above MA{'/'.join(above) or '-'}"
    funding = m.get("funding")
    f_txt = f"{funding * 100:+.3f}%" if funding is not None else "n/a"
    tk = m.get("taker_buy_ratio")
    tk_txt = f"{tk * 100:.0f}%" if tk is not None else "n/a"
    pb = m.get("pct_b_4h")
    rv4 = m.get("rvol_4h")

    lines = [
        f"<b>[{sig['type']}] {html.escape(sig['symbol'])}</b>  |  Score {sig['score']}/100",
        f"{ex}{conf}",
        "",
        f"Price {fp(sig['price'])}  ({_pct(m.get('price_chg_pct'))} {sig['timeframe']})",
        f"RVOL  1h x{m.get('rvol_1h') or 0:.1f}" + (f"  |  4h x{rv4:.1f}" if rv4 else ""),
        f"OI    {_pct(m.get('oi_chg_1h_pct'), 0)} 1h  |  Funding {f_txt}  |  Taker buy {tk_txt}",
        f"4h    {ma_txt}  |  %B {pb:.2f}" if pb is not None else f"4h    {ma_txt}",
    ]
    lines[-1] += f"  |  {_pct(m.get('from_7d_low_pct'), 0)} from 7d low"
    lines += ["", f"Levels  R1 {fp(lv.get('r1'))}  R2 {fp(lv.get('r2'))}  |  S1 {fp(lv.get('s1'))}  S2 {fp(lv.get('s2'))}"]
    if plan:
        coin = sig["key"]
        lines.append(f"Plan    {plan['side'].upper()}  SL {fp(plan['sl'])} ({plan['sl_pct']}%)  |  "
                     f"size for ${plan['risk_usd']:.0f} risk: {_qty(plan['size'])} {coin} (~${plan['notional_usd']:,.0f})")
    for n in sig.get("notes", []):
        lines.append(f"Note    {html.escape(n)}")
    return "\n".join(lines)


def format_early(key: str, symbol: str, exchange: str, rvol: float, chg: float, price: float) -> str:
    return (f"⚡ <b>EARLY 15m</b> {html.escape(symbol)} ({exchange.upper()})\n"
            f"RVOL 15m x{rvol:.1f}  |  {chg:+.1f}%  |  Price {fp(price)}\n"
            f"Watch the 1h close for confirmation.")


class Telegram:
    def __init__(self, session: aiohttp.ClientSession, token: str, chat_id: str,
                 status_chat_id: str, dry_run: bool = False):
        self.session = session
        self.token = token
        self.chat_id = chat_id
        self.status_chat_id = status_chat_id or chat_id
        self.dry_run = dry_run or not token or not chat_id
        self._last_err: dict[str, float] = {}

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
        await self._send(self.status_chat_id, f"🔴 <b>Scanner error</b>\n{html.escape(text)}")


async def _sleep(s):
    import asyncio
    await asyncio.sleep(s)
