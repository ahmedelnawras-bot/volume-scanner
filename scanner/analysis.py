"""Snapshot -> metrics -> signal type + score.  Pure functions, no I/O,
so the live scanner and the backtest share exactly the same logic."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

from . import indicators as ind
from .config import Config
from .models import Signal, Snapshot

LONG_TYPES = {"EARLY_BREAKOUT", "CONTINUATION"}
OI_DOWN_TYPES = {"SHORT_SQUEEZE", "LONG_LIQUIDATION"}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def quick_spike(candles, cfg: Config, tf: str = "1h") -> Optional[tuple[float, float]]:
    """Cheap pre-filter on one timeframe. Returns (rvol, price_chg) if it passes."""
    r = ind.rvol(candles, cfg.RVOL_LOOKBACK)
    if r is None:
        return None
    chg = ind.price_change_pct(candles[-1])
    rmin = cfg.RVOL_MIN_1H if tf == "1h" else cfg.RVOL_MIN_15M
    pmin = cfg.PRICE_MOVE_MIN if tf == "1h" else cfg.PRICE_MOVE_MIN_15M
    if r >= rmin and abs(chg) >= pmin:
        return r, chg
    return None


def compute_metrics(s: Snapshot, cfg: Config) -> Optional[dict]:
    c1h = s.candles_1h
    if len(c1h) < cfg.RVOL_LOOKBACK + 1:
        return None
    last = c1h[-1]
    price = last.close
    closes_4h = [c.close for c in s.candles_4h]

    low7, high7 = ind.range_extremes(c1h, 24 * 7)
    is_bo, base_rng, base_hi = ind.sideways_breakout(c1h, cfg.BASE_LOOKBACK_1H, cfg.BASE_RANGE_MAX_PCT)
    ma = ind.ma_position(closes_4h) if closes_4h else {}
    bb = ind.bollinger(closes_4h) if len(closes_4h) >= 20 else None

    taker = s.taker_buy_ratio if s.taker_buy_ratio is not None else ind.taker_buy_ratio(last)
    vol24 = s.vol24h_usd if s.vol24h_usd is not None else sum(c.volume_usd for c in c1h[-24:])

    extra = []
    if bb:
        extra += [bb[0], bb[1], bb[2]]
    extra += [ma.get("ma5"), ma.get("ma10"), ma.get("ma20")]
    levels = ind.support_resistance(price, c1h, s.candles_4h, [x for x in extra if x])

    candle_close_ms = last.ts + 3_600_000
    return {
        "price": price,
        "candle_close_ms": candle_close_ms,
        "rvol_1h": ind.rvol(c1h, cfg.RVOL_LOOKBACK),
        "rvol_4h": ind.rvol(s.candles_4h, cfg.RVOL_LOOKBACK) if s.candles_4h else None,
        "rvol_15m": ind.rvol(s.candles_15m, cfg.RVOL_LOOKBACK) if s.candles_15m else None,
        "price_chg_pct": ind.price_change_pct(last),
        "oi_chg_1h_pct": ind.oi_change_pct(s.oi_hist, candle_close_ms, 1),
        "oi_chg_4h_pct": ind.oi_change_pct(s.oi_hist, candle_close_ms, 4),
        "funding": s.funding,
        "taker_buy_ratio": taker,
        "pct_b_4h": ind.pct_b(closes_4h) if len(closes_4h) >= 20 else None,
        "trend_4h": ind.trend_4h(closes_4h) if closes_4h else "unknown",
        "above_ma5": ma.get("above_ma5"), "above_ma10": ma.get("above_ma10"), "above_ma20": ma.get("above_ma20"),
        "from_7d_low_pct": ind.from_low_pct(price, low7),
        "from_7d_high_pct": ind.from_high_pct(price, high7),
        "base_breakout": is_bo, "base_range_pct": base_rng, "base_high": base_hi,
        "vol24h_usd": vol24,
        "levels": levels,
    }


def classify(m: dict, cfg: Config) -> tuple[str, str, list]:
    """Returns (signal_type, side, notes)."""
    up = m["price_chg_pct"] > 0
    oi = m["oi_chg_1h_pct"]
    oi_up = oi is not None and oi > cfg.OI_FLAT_PCT
    oi_down = oi is not None and oi < -cfg.OI_FLAT_PCT
    fund = m["funding"]
    notes: list[str] = []
    if oi is None:
        notes.append("OI data unavailable")

    if up:
        pct_b = m["pct_b_4h"]
        # %B > 1 alone is normal on any breakout from a tight base, so it only
        # counts as "late" once the move is already extended off the 7d low.
        late = (m["from_7d_low_pct"] > cfg.LATE_PUMP_FROM_LOW_PCT
                or (pct_b is not None and pct_b > cfg.LATE_PUMP_PCT_B
                    and m["from_7d_low_pct"] >= cfg.EARLY_MAX_FROM_LOW_PCT))
        if late:
            notes.append("late move: do not chase the long")
            return "LATE_PUMP", "none", notes
        if oi_down:
            notes.append("shorts closing, move likely short-lived")
            return "SHORT_SQUEEZE", "none", notes
        squeezy = oi_up and fund is not None and fund < cfg.SQUEEZE_FUNDING_MAX
        if m["base_breakout"] and m["from_7d_low_pct"] < cfg.EARLY_MAX_FROM_LOW_PCT and oi_up:
            t = "EARLY_BREAKOUT"
        elif m["trend_4h"] == "up":
            t = "CONTINUATION"
        elif squeezy:
            notes.append("funding negative + OI rising: shorts crowded, do not short")
            return "SQUEEZE_RISK", "none", notes
        else:
            t = "VOLUME_SPIKE"
        if squeezy:
            notes.append("funding negative: shorts crowded, do not short")
        return t, "long", notes

    # price down
    if oi_up:
        return "NEW_SHORTS", "short", notes
    if oi_down:
        notes.append("liquidation flush, a bounce often follows")
        return "LONG_LIQUIDATION", "none", notes
    return "VOLUME_SPIKE", "short", notes


def score(m: dict, sig_type: str, cfg: Config, confirmed: bool) -> tuple[int, dict]:
    up = m["price_chg_pct"] > 0
    parts: dict[str, float] = {}

    r = m["rvol_1h"] or 0
    span = max(cfg.RVOL_FULL_SCORE - cfg.RVOL_MIN_1H, 0.1)
    parts["rvol"] = 25 * (0.4 + 0.6 * _clamp((r - cfg.RVOL_MIN_1H) / span)) if r >= cfg.RVOL_MIN_1H else 0

    oi = m["oi_chg_1h_pct"]
    want_down = sig_type in OI_DOWN_TYPES
    if oi is None or (want_down and oi >= 0) or (not want_down and oi <= 0):
        parts["oi"] = 0
    else:
        parts["oi"] = 20 * _clamp(abs(oi) / cfg.OI_FULL_SCORE_PCT)

    ctx = 0.0
    above = [m["above_ma5"], m["above_ma10"], m["above_ma20"]]
    if up:
        ctx += 10 / 3 * sum(1 for a in above if a is True)
        if m["pct_b_4h"] is not None and 0.5 <= m["pct_b_4h"] <= 1.0:
            ctx += 5
        if m["from_7d_low_pct"] < cfg.EARLY_MAX_FROM_LOW_PCT:
            ctx += 5
    else:
        ctx += 10 / 3 * sum(1 for a in above if a is False)
        if m["pct_b_4h"] is not None and 0.0 <= m["pct_b_4h"] <= 0.5:
            ctx += 5
        if m["from_7d_high_pct"] < cfg.EARLY_MAX_FROM_LOW_PCT:
            ctx += 5
    parts["context_4h"] = ctx

    parts["cross_exchange"] = 15 if confirmed else 0

    tk = m["taker_buy_ratio"]
    if tk is None:
        parts["taker"] = 0
    else:
        parts["taker"] = 10 * _clamp(((tk - 0.5) if up else (0.5 - tk)) / 0.2)

    v = m["vol24h_usd"] or 0
    lo, hi = cfg.MIN_24H_VOLUME_USD, 50_000_000
    if v <= 0:
        parts["liquidity"] = 0
    elif v >= hi:
        parts["liquidity"] = 10
    else:
        parts["liquidity"] = 3 + 7 * _clamp(math.log(max(v, lo) / lo) / math.log(hi / lo))

    pen = 0
    if sig_type == "LATE_PUMP":
        pen -= 30
    f = m["funding"]
    if f is not None and ((up and f > cfg.FUNDING_AGAINST) or (not up and f < -cfg.FUNDING_AGAINST)):
        pen -= 10
    parts["penalties"] = pen

    total = int(round(max(0, min(100, sum(parts.values())))))
    return total, {k: round(v, 1) for k, v in parts.items()}


def make_plan(m: dict, side: str, cfg: Config) -> Optional[dict]:
    if side not in ("long", "short"):
        return None
    price, lv = m["price"], m["levels"]
    buf = cfg.SL_BUFFER_PCT / 100
    if side == "long":
        sl = lv["s1"] * (1 - buf) if lv.get("s1") else price * (1 - cfg.MAX_SL_PCT / 100)
        dist = price - sl
    else:
        sl = lv["r1"] * (1 + buf) if lv.get("r1") else price * (1 + cfg.MAX_SL_PCT / 100)
        dist = sl - price
    if dist <= 0:
        return None
    size = cfg.RISK_PER_TRADE_USD / dist
    sl_pct = dist / price * 100
    return {
        "side": side, "entry": price, "sl": sl, "sl_pct": round(sl_pct, 2),
        "size": size, "notional_usd": round(size * price, 2),
        "risk_usd": cfg.RISK_PER_TRADE_USD, "wide_sl": sl_pct > cfg.MAX_SL_PCT,
    }


def build_signal(s: Snapshot, m: dict, cfg: Config, exchanges: list, timeframe: str = "1h") -> Signal:
    confirmed = len(set(exchanges)) > 1
    t, side, notes = classify(m, cfg)
    sc, parts = score(m, t, cfg, confirmed)
    plan = make_plan(m, side, cfg)
    if plan:
        # price at new highs/lows has no swing level ahead: project targets at 1.5R / 3R
        lv, dist = m["levels"], abs(plan["entry"] - plan["sl"])
        k1, k2 = ("r1", "r2") if side == "long" else ("s1", "s2")
        sgn = 1 if side == "long" else -1
        if lv.get(k1) is None:
            lv[k1] = plan["entry"] + sgn * 1.5 * dist
            lv[k2] = plan["entry"] + sgn * 3.0 * dist
            notes.append(f"no {k1.upper()} {'above' if side == 'long' else 'below'} price yet: {k1.upper()}/{k2.upper()} projected at 1.5R/3R")
        elif lv.get(k2) is None:
            lv[k2] = plan["entry"] + sgn * 3.0 * dist
    if plan and plan["wide_sl"]:
        notes.append(f"SL is wide ({plan['sl_pct']}%), size reduced accordingly")
    close_iso = datetime.fromtimestamp(m["candle_close_ms"] / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    metrics = {k: (round(v, 6) if isinstance(v, float) else v) for k, v in m.items()
               if k not in ("levels", "price", "candle_close_ms")}
    return Signal(
        id=f"{s.key}-{close_iso}", key=s.key, symbol=s.symbol, exchange=s.exchange,
        exchanges=sorted(set(exchanges)), type=t, side=side, score=sc, timeframe=timeframe,
        candle_close=close_iso, candle_close_ms=m["candle_close_ms"], price=m["price"],
        metrics=metrics, levels=m["levels"], plan=plan, notes=notes, score_parts=parts,
    )
