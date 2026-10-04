"""Replay the scanner on historical data for one coin.

Example (the NIGHT test from the white paper):
    python backtest.py --symbol NIGHT --start 2026-09-26 --end 2026-10-03

It walks every closed 1h candle in the range, rebuilds the exact snapshot the
live scanner would have had at that moment (candles, OI history, funding,
taker flow), runs the same analysis code, and prints when it would have
alerted, with what type/score, and how the signal played out 4h / 24h later.
Note: Binance keeps only ~30 days of OI history, so keep ranges recent.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone

import aiohttp

from scanner import analysis
from scanner.config import Config
from scanner.exchanges import REGISTRY
from scanner.followup import evaluate
from scanner.models import Snapshot
from scanner.notify.templates import signal as format_signal

H = 3_600_000


def ts(d: str) -> int:
    return int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


async def oi_history(ex, c, start_ms, end_ms):
    pts, end = [], end_ms
    for _ in range(20):
        batch = await ex.oi_hist(c, 100 if ex.name == "okx" else 500, end)
        batch = [p for p in batch if p[0] <= end]
        if not batch:
            break
        pts = batch + pts
        if batch[0][0] <= start_ms:
            break
        end = batch[0][0] - 1
    return sorted(set(pts))


async def load(ex, key, start_ms, end_ms, cfg):
    contracts = [c for c in await ex.contracts() if c.key == key]
    if not contracts:
        print(f"  {ex.name}: {key} not listed")
        return None
    c = contracts[0]
    now_ms = int(time.time() * 1000)
    hist_end = min(end_ms + 25 * H, now_ms)
    n1 = (hist_end - start_ms) // H + cfg.CANDLES_1H + 2
    n4 = (hist_end - start_ms) // (4 * H) + cfg.CANDLES_4H + 2
    c1h = await ex.candles(c, "1h", int(n1), end_ms=hist_end)
    c4h = await ex.candles(c, "4h", int(n4), end_ms=hist_end)
    try:
        oi = await oi_history(ex, c, start_ms - 6 * H, end_ms)
    except Exception as e:
        print(f"  {ex.name}: OI history unavailable ({e})")
        oi = []
    print(f"  {ex.name}: {c.symbol}  1h={len(c1h)}  4h={len(c4h)}  OI points={len(oi)}")
    return {"ex": ex, "c": c, "c1h": c1h, "c4h": c4h, "oi": oi}


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", required=True, help="coin key, e.g. NIGHT")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--exchanges", default="okx,binance")
    p.add_argument("--show-alerts", action="store_true", help="print the full Telegram text")
    p.add_argument("--verbose", action="store_true", help="print every hour with RVOL >= 2")
    a = p.parse_args()

    cfg = Config.load()
    key = a.symbol.upper()
    start_ms, end_ms = ts(a.start), ts(a.end) + 24 * H
    print(f"Backtest {key}  {a.start} -> {a.end}\n")

    async with aiohttp.ClientSession() as session:
        data = {}
        for name in a.exchanges.split(","):
            ex = REGISTRY[name.strip()](session, cfg.HTTP_CONCURRENCY, cfg.HTTP_TIMEOUT)
            try:
                d = await load(ex, key, start_ms, end_ms, cfg)
            except Exception as e:
                print(f"  {name}: failed to load ({e})")
                d = None
            if d:
                data[name] = d
        if not data:
            print("No data.")
            return

        results = []
        t = start_ms
        while t <= end_ms:
            flagged = []
            for name, d in data.items():
                c1h = [c for c in d["c1h"] if c.ts + H <= t][-cfg.CANDLES_1H:]
                if not c1h or c1h[-1].ts + H != t:
                    continue
                spike = analysis.quick_spike(c1h, cfg, "1h")
                if a.verbose:
                    from scanner.indicators import rvol, price_change_pct
                    r = rvol(c1h, cfg.RVOL_LOOKBACK)
                    if r and r >= 2:
                        print(f"    {iso(t)} {name:7s} RVOL x{r:.1f} {price_change_pct(c1h[-1]):+.1f}%"
                              f"{'  <- spike' if spike else ''}")
                if not spike:
                    continue
                ex = d["ex"]
                fund = await _try(ex.funding_at(d["c"], t))
                taker = await _try(ex.taker_ratio(d["c"], t)) if name == "okx" else None
                snap = Snapshot(exchange=name, symbol=d["c"].symbol, key=key, now_ms=t, candles_1h=c1h,
                                candles_4h=[c for c in d["c4h"] if c.ts + 4 * H <= t][-cfg.CANDLES_4H:],
                                oi_hist=[p for p in d["oi"] if p[0] <= t], funding=fund,
                                taker_buy_ratio=taker, vol24h_usd=None)
                m = analysis.compute_metrics(snap, cfg)
                if m:
                    flagged.append((snap, m, d))
            if flagged:
                names = [s.exchange for s, _, _ in flagged]
                sigs = [(analysis.build_signal(s, m, cfg, names), d) for s, m, d in flagged]
                sig, d = max(sigs, key=lambda x: x[0].score)
                sd = sig.to_dict()
                sd["followup"] = {}
                for h in (4, 24):
                    r = evaluate(sd, d["c1h"], h)
                    if r:
                        sd["followup"][f"{h}h"] = r
                sd["would_alert"] = sd["score"] >= cfg.MIN_SCORE_TO_ALERT
                results.append(sd)
            t += H

    # cooldown, as live
    last = {}
    for r in results:
        prev = last.get(r["key"])
        r["cooldown_blocked"] = bool(r["would_alert"] and prev and prev[1] == r["type"]
                                     and r["candle_close_ms"] - prev[0] < cfg.COOLDOWN_HOURS * H)
        if r["would_alert"] and not r["cooldown_blocked"]:
            last[r["key"]] = (r["candle_close_ms"], r["type"])

    print(f"\n{'close (UTC)':17s} {'action':9s} {'type':17s} {'score':>5s} {'ex':>12s} {'chg':>6s} {'rvol':>6s} "
          f"{'OI1h':>6s} {'4h':>7s} {'24h':>7s}  alert")
    for r in results:
        m, fu = r["metrics"], r.get("followup") or {}
        f4 = fu.get("4h", {}).get("result_4h")
        f24 = fu.get("24h", {}).get("result_24h")
        oi = m.get("oi_chg_1h_pct")
        flag = "YES" if r["would_alert"] and not r["cooldown_blocked"] else ("cooldown" if r["cooldown_blocked"] else "-")
        act = r.get("action", "") + (f"-{r['bias']}" if r.get("action") == "WATCH" else "")
        print(f"{r['candle_close'][:16].replace('T', ' '):17s} {act:9s} {r['type']:17s} {r['score']:5d} "
              f"{'+'.join(r['exchanges']):>12s} {m['price_chg_pct']:+5.1f}% x{m['rvol_1h']:5.1f} "
              f"{(f'{oi:+.0f}%' if oi is not None else 'n/a'):>6s} "
              f"{(f'{f4:+.1f}%' if f4 is not None else '-'):>7s} {(f'{f24:+.1f}%' if f24 is not None else '-'):>7s}  {flag}")
        if a.show_alerts and flag == "YES":
            print(format_signal(r, cfg.ALERT_LANG).replace("\u200e", "") + "\n")

    alerts = [r for r in results if r["would_alert"] and not r["cooldown_blocked"]]
    print(f"\n{len(results)} spike hour(s), {len(alerts)} alert(s).")
    if alerts:
        print(f"First alert: {alerts[0]['candle_close']}  {alerts[0]['type']}  score {alerts[0]['score']}  "
              f"price {alerts[0]['price']}")
    out = f"backtest_{key}_{a.start}_{a.end}.json"
    with open(out, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"Saved {out}")


async def _try(coro):
    try:
        return await coro
    except Exception:
        return None


if __name__ == "__main__":
    asyncio.run(main())
