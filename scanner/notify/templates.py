"""Turn data into Telegram HTML text. Pure functions: no I/O, no config.

Layout of a signal (headline first, always):
    <b>🟢 LONG | SYMBOL</b>          what to do
    ⚠️ counter-trend (if any)        the one warning that must not be missed
    type · score / exchanges / candle
    📌 plan     (LONG / SHORT only)   entry, SL, TP, size
    📊 reading                        why
    🧱 levels   (warnings / watch)
    📝 notes
"""
from __future__ import annotations

import html

from . import fmt
from .terms import T


def _note(code: str, t: dict) -> str:
    key, _, arg = code.partition(":")
    notes = t["notes"]
    if key == "counter_trend":
        return notes["counter_trend"].format(arg=t["trend"].get(arg, arg))
    if key == "projected":
        return notes["projected_long" if arg == "long" else "projected_short"]
    if key == "wide_sl":
        return notes["wide_sl"].format(arg=arg)
    return notes.get(key, html.escape(code))


def _headline(sig: dict, t: dict) -> str:
    action = sig.get("action") or "WATCH"
    key = f"WATCH_{sig.get('bias') or 'long'}" if action == "WATCH" else action
    label = t["action"][key]
    if sig["type"] == "LONG_LIQUIDATION" and action == "LONG":
        label += f" ({t['bounce']})"
    return f"<b>{label} | {html.escape(sig['symbol'])}</b>"


def _ma(m: dict, t: dict) -> str:
    above = [n for n in ("5", "10", "20") if m.get(f"above_ma{n}")]
    below = [n for n in ("5", "10", "20") if m.get(f"above_ma{n}") is False]
    if len(above) == 3:
        return t["ma_above"]
    if len(below) == 3:
        return t["ma_below"]
    return t["ma_mixed"].format(list="/".join(above)) if above else t["ma_none"]


def _levels(a, b, t: dict) -> str:
    vals = [fmt.price(x) for x in (a, b) if x]
    return " · ".join(vals) if vals else t["none_near"]


def _rel(level, entry) -> str:
    return fmt.pct((level - entry) / entry * 100) if level and entry else ""


def signal(sig: dict, lang: str = "ar", tz_name: str = "Africa/Cairo") -> str:
    t = T(lang)
    m, lv, plan = sig["metrics"], sig.get("levels") or {}, sig.get("plan")
    notes = sig.get("notes") or []
    exs = " + ".join(e.upper() for e in sig["exchanges"])

    out = [_headline(sig, t)]
    out += [_note(n, t) for n in notes if n.startswith("counter_trend")]
    out += [
        f"{t['type'].get(sig['type'], sig['type'])} · {t['score']} <b>{fmt.ltr(str(sig['score']) + '/100')}</b>",
        (t["confirmed"] if len(sig["exchanges"]) > 1 else t["single"]).format(exs=exs),
        t["candle"].format(window=fmt.candle_window(sig["candle_close_ms"], tz_name)),
    ]

    if plan:
        entry = plan["entry"]
        long_ = plan["side"] == "long"
        tp1, tp2 = (lv.get("r1"), lv.get("r2")) if long_ else (lv.get("s1"), lv.get("s2"))
        proj = t["projected"] if any(n.startswith("projected") for n in notes) else ""
        pl = t["plan_lines"]
        out += ["", t["plan_h"],
                pl["entry"].format(entry=fmt.price(entry)),
                pl["sl"].format(sl=fmt.price(plan["sl"]), sl_pct=_rel(plan["sl"], entry))]
        if tp1:
            out.append(pl["tp1"].format(tp1=fmt.price(tp1), tp1_pct=_rel(tp1, entry), proj=proj))
        if tp2:
            out.append(pl["tp2"].format(tp2=fmt.price(tp2), tp2_pct=_rel(tp2, entry), proj=proj))
        out.append(pl["size"].format(qty=fmt.qty(plan["size"]), coin=html.escape(sig["key"]),
                                     notional=fmt.usd(plan["notional_usd"]), risk=fmt.usd(plan["risk_usd"])))

    pb = m.get("pct_b_4h")
    values = {
        "rvol": fmt.mult(m.get("rvol_1h")),
        "chg": fmt.pct(m.get("price_chg_pct")),
        "oi": fmt.pct(m.get("oi_chg_1h_pct"), 0),
        "funding": fmt.funding(m.get("funding")),
        "taker": fmt.ratio(m.get("taker_buy_ratio")),
        "ma": _ma(m, t),
        "pctb": fmt.ltr(f"{pb:.2f}") if pb is not None else "n/a",
        "from_low": fmt.pct(m.get("from_7d_low_pct"), 0),
    }
    out += ["", t["read_h"]] + [line.format(**values) for line in t["read_lines"]]

    if not plan:
        out += [
            "",
            t["lv_h"],
            f"{t['res']}: {_levels(lv.get('r1'), lv.get('r2'), t)}",
            f"{t['sup']}: {_levels(lv.get('s1'), lv.get('s2'), t)}",
        ]

    rest = [n for n in notes if not n.startswith("counter_trend")]
    if rest:
        out += ["", t["notes_h"]] + [f"• {_note(n, t)}" for n in rest]
    return _join(out, lang)


def _join(lines: list, lang: str, keep_first_ltr: bool = True) -> str:
    """Arabic: mark every line after the headline right-to-left (RLM), so lines
    that start with an emoji, a number or an English term still align right.
    The headline keeps its natural left-to-right order: LONG/SHORT reads first."""
    if lang != "ar":
        return "\n".join(lines)
    head, rest = (lines[:1], lines[1:]) if keep_first_ltr else ([], lines)
    return "\n".join(head + [fmt.RLM + ln if ln else ln for ln in rest])


def early(symbol: str, exchange: str, rvol: float, chg: float, px: float, lang: str = "ar") -> str:
    t = T(lang)
    side = "WATCH_long" if chg >= 0 else "WATCH_short"
    lines = [f"<b>{t['action'][side]} | {html.escape(symbol)}</b>",
             f"{t['type']['EARLY_15M']} · {exchange.upper()}",
             *t["early_body"].format(rvol=fmt.mult(rvol), chg=fmt.pct(chg), price=fmt.price(px)).split("\n")]
    return _join(lines, lang)


def started(exchanges: list, memory: bool, lang: str = "ar") -> str:
    t = T(lang)
    text = t["started"].format(exs=" + ".join(e.upper() for e in exchanges),
                               state=t["state_memory"] if memory else t["state_redis"])
    return _join(text.split("\n"), lang, keep_first_ltr=False)


def heartbeat(stats: dict, exchanges: list, lang: str = "ar") -> str:
    t = T(lang)
    rows = [t["hb_row"].format(ex=n.upper(), eligible=stats.get(n, {}).get("eligible", 0),
                               contracts=stats.get(n, {}).get("contracts", 0),
                               spikes=stats.get(n, {}).get("spikes", 0)) for n in exchanges]
    took = stats.get("took_s")
    return _join([t["hb_h"], *rows, t["hb_took"].format(took=fmt.ltr(f"{took}s") if took else "n/a")],
                 lang, keep_first_ltr=False)


def error(text: str, lang: str = "ar") -> str:
    return _join([T(lang)["err_h"], html.escape(text)], lang, keep_first_ltr=False)


def daily_summary(data: dict, lang: str = "ar") -> str:
    """data comes from followup.summary_stats()."""
    t = T(lang)

    def tn(x):
        return t["type"].get(x, x)

    out = [t["sum_h"], ""]
    if not data["count"]:
        out.append(t["sum_none"])
    else:
        out.append(t["sum_count"].format(n=data["count"]))
        out += [f"• {tn(k)}: {n}" for k, n in data["by_type"]]
        if data["best"]:
            out += ["", t["sum_best"]] + [f"• {tn(ty)}: {k} {fmt.pct(p)}" for k, ty, p in data["best"]]
            out += [t["sum_worst"]] + [f"• {tn(ty)}: {k} {fmt.pct(p)}" for k, ty, p in data["worst"]]
    if data["win_rate"]:
        out += ["", t["sum_win"]]
        out += [f"• {tn(ty)}: {fmt.ltr(f'{w}/{n}')} ({fmt.pct(w / n * 100, 0, sign=False)})"
                for ty, w, n in data["win_rate"]]
    return _join(out, lang, keep_first_ltr=False)
