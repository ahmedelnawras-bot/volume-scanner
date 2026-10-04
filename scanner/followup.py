"""Signal follow-up (4h / 24h) and daily-summary statistics. Pure functions."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Optional


def direction(sig: dict) -> int:
    side = sig.get("side") if sig.get("side") in ("long", "short") else sig.get("bias")
    if side == "long":
        return 1
    if side == "short":
        return -1
    return 1 if sig["metrics"].get("price_chg_pct", 0) >= 0 else -1


def evaluate(sig: dict, candles_after: list, hours: int) -> Optional[dict]:
    """candles_after: closed 1h candles starting at the signal candle close."""
    window = [c for c in candles_after
              if sig["candle_close_ms"] <= c.ts < sig["candle_close_ms"] + hours * 3_600_000]
    if not window:
        return None
    d = direction(sig)
    p0 = sig["price"]
    lv = sig.get("levels") or {}
    plan = sig.get("plan") or {}
    target = lv.get("r1") if d > 0 else lv.get("s1")
    stop = plan.get("sl") or (lv.get("s1") if d > 0 else lv.get("r1"))

    first_hit = None
    for c in window:
        hit_t = target is not None and ((c.high >= target) if d > 0 else (c.low <= target))
        hit_s = stop is not None and ((c.low <= stop) if d > 0 else (c.high >= stop))
        if hit_t and hit_s:
            first_hit = "sl"  # same candle: assume the worse case
            break
        if hit_t:
            first_hit = "r1"
            break
        if hit_s:
            first_hit = "sl"
            break

    hi = max(c.high for c in window)
    lo = min(c.low for c in window)
    fav = (hi - p0) / p0 * 100 if d > 0 else (p0 - lo) / p0 * 100
    adv = (p0 - lo) / p0 * 100 if d > 0 else (hi - p0) / p0 * 100
    res = (window[-1].close - p0) / p0 * 100
    return {
        "max_favorable_pct": round(fav, 2),
        "max_adverse_pct": round(adv, 2),
        "hit_r1": first_hit == "r1",
        "hit_sl": first_hit == "sl",
        "first_hit": first_hit,
        f"result_{hours}h": round(res, 2),
        "candles": len(window),
    }


def is_win(sig: dict) -> Optional[bool]:
    fu = sig.get("followup") or {}
    f = fu.get("24h") or fu.get("4h")
    if not f:
        return None
    if f.get("first_hit") == "r1":
        return True
    if f.get("first_hit") == "sl":
        return False
    res = f.get("result_24h", f.get("result_4h", 0))
    return res * direction(sig) > 0


def summary_stats(last_24h: list[dict], last_7d: list[dict]) -> dict:
    """Numbers for the daily summary; wording lives in scanner.notify."""
    def perf(s):
        fu = s.get("followup") or {}
        f = fu.get("24h") or fu.get("4h") or {}
        r = f.get("result_24h", f.get("result_4h"))
        return None if r is None else r * direction(s)

    scored = sorted(((perf(s), s) for s in last_24h if perf(s) is not None),
                    key=lambda x: x[0], reverse=True)
    stats = defaultdict(lambda: [0, 0])
    for s in last_7d:
        w = is_win(s)
        if w is None:
            continue
        stats[s["type"]][1] += 1
        stats[s["type"]][0] += int(w)
    return {
        "count": len(last_24h),
        "by_type": Counter(s["type"] for s in last_24h).most_common(),
        "best": [(s["key"], s["type"], p) for p, s in scored[:3]],
        "worst": [(s["key"], s["type"], p) for p, s in scored[-3:][::-1]],
        "win_rate": [(t, w, n) for t, (w, n) in sorted(stats.items(), key=lambda x: -x[1][1])],
    }
