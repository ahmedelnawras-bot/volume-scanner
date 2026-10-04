"""Pure indicator math on lists of Candle (oldest first). No I/O."""
from __future__ import annotations

import math
from statistics import mean, pstdev
from typing import Optional

from .models import Candle


def rvol(candles: list[Candle], lookback: int = 20) -> Optional[float]:
    """Last candle volume / average of the `lookback` candles before it."""
    if len(candles) < lookback + 1:
        return None
    base = [c.volume_usd for c in candles[-lookback - 1:-1]]
    avg = mean(base)
    if avg <= 0:
        return None
    return candles[-1].volume_usd / avg


def price_change_pct(c: Candle) -> float:
    return (c.close - c.open) / c.open * 100 if c.open else 0.0


def sma(values: list[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def bollinger(closes: list[float], n: int = 20, k: float = 2.0):
    if len(closes) < n:
        return None
    window = closes[-n:]
    mid = mean(window)
    sd = pstdev(window)
    return mid - k * sd, mid, mid + k * sd


def pct_b(closes: list[float], n: int = 20, k: float = 2.0) -> Optional[float]:
    bb = bollinger(closes, n, k)
    if not bb:
        return None
    lo, _, hi = bb
    if hi == lo:
        return 0.5
    return (closes[-1] - lo) / (hi - lo)


def range_extremes(candles: list[Candle], hours: int, tf_hours: float = 1) -> tuple:
    n = max(1, int(hours / tf_hours))
    window = candles[-n:]
    return min(c.low for c in window), max(c.high for c in window)


def from_low_pct(price: float, low: float) -> float:
    return (price - low) / low * 100 if low else 0.0


def from_high_pct(price: float, high: float) -> float:
    return (high - price) / high * 100 if high else 0.0


def sideways_breakout(candles: list[Candle], lookback: int, max_range_pct: float):
    """Was there a tight base over the previous `lookback` candles, and did the
    last candle close above it?  Returns (is_breakout, base_range_pct, base_high)."""
    if len(candles) < lookback + 1:
        return False, None, None
    base = candles[-lookback - 1:-1]
    lo = min(c.low for c in base)
    hi = max(c.high for c in base)
    rng = (hi - lo) / lo * 100 if lo else 999
    last = candles[-1]
    return (rng <= max_range_pct and last.close > hi), rng, hi


def trend_4h(closes: list[float]) -> str:
    m5, m10, m20 = sma(closes, 5), sma(closes, 10), sma(closes, 20)
    if None in (m5, m10, m20):
        return "unknown"
    p = closes[-1]
    if p > m5 > m10 > m20 or (p > m20 and m5 > m20 and m10 > m20):
        return "up"
    if p < m5 < m10 < m20 or (p < m20 and m5 < m20 and m10 < m20):
        return "down"
    return "flat"


def ma_position(closes: list[float]) -> dict:
    p = closes[-1]
    out = {}
    for n in (5, 10, 20):
        m = sma(closes, n)
        out[f"ma{n}"] = m
        out[f"above_ma{n}"] = (p > m) if m is not None else None
    return out


def oi_change_pct(oi_hist: list, now_ms: int, hours: int) -> Optional[float]:
    """% change of OI over the last `hours`, using points at or before now."""
    pts = [p for p in oi_hist if p[0] <= now_ms]
    if len(pts) < 2:
        return None
    latest_ts, latest = pts[-1]
    target = latest_ts - hours * 3_600_000
    past = [p for p in pts if p[0] <= target]
    if not past:
        return None
    prev = past[-1][1]
    if prev <= 0:
        return None
    return (latest - prev) / prev * 100


def taker_buy_ratio(c: Candle) -> Optional[float]:
    if c.taker_buy_usd is None or c.volume_usd <= 0:
        return None
    return c.taker_buy_usd / c.volume_usd


def swing_points(candles: list[Candle], left: int = 2, right: int = 2) -> tuple[list, list]:
    highs, lows = [], []
    for i in range(left, len(candles) - right):
        c = candles[i]
        if all(c.high >= candles[j].high for j in range(i - left, i + right + 1) if j != i):
            highs.append(c.high)
        if all(c.low <= candles[j].low for j in range(i - left, i + right + 1) if j != i):
            lows.append(c.low)
    return highs, lows


def _cluster(levels: list[float], tol_pct: float = 0.5) -> list[float]:
    out: list[float] = []
    for lv in sorted(levels):
        if out and abs(lv - out[-1]) / out[-1] * 100 <= tol_pct:
            out[-1] = (out[-1] + lv) / 2
        else:
            out.append(lv)
    return out


def support_resistance(price: float, c1h: list[Candle], c4h: list[Candle], extra: list[float]) -> dict:
    """Two nearest resistances above and supports below price."""
    h1, l1 = swing_points(c1h[-120:])
    h4, l4 = swing_points(c4h)
    levels = _cluster([x for x in h1 + l1 + h4 + l4 + extra if x and x > 0 and math.isfinite(x)])
    above = [x for x in levels if x > price * 1.002]
    below = [x for x in reversed(levels) if x < price * 0.998]
    return {
        "r1": above[0] if len(above) > 0 else None,
        "r2": above[1] if len(above) > 1 else None,
        "s1": below[0] if len(below) > 0 else None,
        "s2": below[1] if len(below) > 1 else None,
    }
