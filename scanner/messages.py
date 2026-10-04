"""Every user-facing Telegram text, in Arabic (default) or English.

ALERT_LANG=ar|en picks the language. Technical terms (OI, Funding, RVOL, SL,
MA, %B) stay in English on purpose: that is how they read on the charts.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

LRM = "‎"  # keeps "+6.8%" / "-0.010%" from flipping inside Arabic lines


def ltr(s) -> str:
    return f"{LRM}{s}{LRM}"


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


def pct(x, digits=1, sign=True) -> str:
    if x is None:
        return "n/a"
    return ltr(f"{x:+.{digits}f}%" if sign else f"{x:.{digits}f}%")


def qty(x) -> str:
    if x >= 1000:
        return f"{x:,.0f}"
    if x >= 1:
        return f"{x:,.2f}"
    return f"{x:.4f}"


def candle_window(close_ms: int, tz_name: str) -> str:
    try:
        tz = ZoneInfo(tz_name)
        o = datetime.fromtimestamp(close_ms / 1000 - 3600, tz=timezone.utc).astimezone(tz)
        c = datetime.fromtimestamp(close_ms / 1000, tz=timezone.utc).astimezone(tz)
        return ltr(f"{o:%H:%M}-{c:%H:%M}")
    except Exception:
        return ""


# ----------------------------------------------------------------- vocab
TYPES = {
    "ar": {
        "EARLY_BREAKOUT": ("🚀", "اختراق مبكر"),
        "CONTINUATION": ("📈", "استمرار ترند"),
        "SQUEEZE_RISK": ("⚠️", "خطر Squeeze"),
        "SHORT_SQUEEZE": ("💥", "Short squeeze"),
        "NEW_SHORTS": ("🔻", "شورتات جديدة"),
        "LONG_LIQUIDATION": ("🩸", "تصفية لونجات"),
        "LATE_PUMP": ("🛑", "بامب متأخر"),
        "VOLUME_SPIKE": ("🔊", "فوليوم غير طبيعي"),
    },
    "en": {
        "EARLY_BREAKOUT": ("🚀", "Early breakout"),
        "CONTINUATION": ("📈", "Continuation"),
        "SQUEEZE_RISK": ("⚠️", "Squeeze risk"),
        "SHORT_SQUEEZE": ("💥", "Short squeeze"),
        "NEW_SHORTS": ("🔻", "New shorts"),
        "LONG_LIQUIDATION": ("🩸", "Long liquidation"),
        "LATE_PUMP": ("🛑", "Late pump"),
        "VOLUME_SPIKE": ("🔊", "Volume spike"),
    },
}

TREND = {"ar": {"up": "صاعد", "down": "هابط"}, "en": {"up": "up", "down": "down"}}
SIDE = {"ar": {"long": "LONG 🟢", "short": "SHORT 🔴"}, "en": {"long": "LONG 🟢", "short": "SHORT 🔴"}}


def note_text(code: str, lang: str) -> str:
    key, _, arg = code.partition(":")
    ar = lang == "ar"
    if key == "counter_trend":
        tr = TREND[lang].get(arg, arg)
        return (f"⚠️ <b>عكس الترند:</b> ترند الـ 4h {tr}، اعتبرها صفقة تصحيح"
                if ar else f"⚠️ <b>Counter-trend:</b> 4h trend is {tr}, treat as a pullback trade")
    if key == "projected":
        if arg == "long":
            return ("مفيش مقاومة فوق السعر: R1/R2 محسوبين على 1.5R و 3R"
                    if ar else "No resistance above price: R1/R2 projected at 1.5R / 3R")
        return ("مفيش دعم تحت السعر: S1/S2 محسوبين على 1.5R و 3R"
                if ar else "No support below price: S1/S2 projected at 1.5R / 3R")
    if key == "wide_sl":
        return (f"الـ SL واسع ({ltr(arg + '%')})، والحجم اتقلل على قده"
                if ar else f"SL is wide ({arg}%), size reduced accordingly")
    table = {
        "oi_na": ("مفيش داتا OI للعملة دي", "OI data unavailable"),
        "late": ("الحركة متأخرة: متطاردش اللونج", "Late move: do not chase the long"),
        "short_squeeze": ("الشورتات بتقفل: الحركة غالبًا قصيرة", "Shorts closing: move likely short-lived"),
        "squeeze_risk": ("الـ Funding سالب والـ OI طالع: الشورتات متزاحمة، ممنوع الشورت",
                         "Funding negative + OI rising: shorts crowded, do not short"),
        "funding_neg": ("الـ Funding سالب: الشورتات متزاحمة، ممنوع الشورت",
                        "Funding negative: shorts crowded, do not short"),
        "liq_flush": ("تصفية لونجات: غالبًا بيتبعها ارتداد", "Liquidation flush: a bounce often follows"),
    }
    if key in table:
        return table[key][0 if ar else 1]
    return html.escape(code)  # older free-text notes


# ----------------------------------------------------------------- signal
def format_signal(sig: dict, lang: str = "ar", tz_name: str = "Africa/Cairo") -> str:
    return _signal_ar(sig, tz_name) if lang == "ar" else _signal_en(sig, tz_name)


def _ma_state(m: dict, lang: str) -> str:
    above = [n for n in ("5", "10", "20") if m.get(f"above_ma{n}")]
    below = [n for n in ("5", "10", "20") if m.get(f"above_ma{n}") is False]
    if lang == "ar":
        if len(above) == 3:
            return "فوق MA5/10/20"
        if len(below) == 3:
            return "تحت MA5/10/20"
        return f"فوق MA{'/'.join(above)}" if above else "تحت المتوسطات"
    if len(above) == 3:
        return "above MA5/10/20"
    if len(below) == 3:
        return "below MA5/10/20"
    return f"above MA{'/'.join(above) or '-'}"


def _common(sig):
    m = sig["metrics"]
    f = m.get("funding")
    tk = m.get("taker_buy_ratio")
    return (m, sig["levels"], sig.get("plan"),
            ltr(f"{f * 100:+.3f}%") if f is not None else "n/a",
            ltr(f"{tk * 100:.0f}%") if tk is not None else "n/a",
            m.get("pct_b_4h"), m.get("rvol_4h"))


def _signal_ar(sig: dict, tz_name: str) -> str:
    m, lv, plan, f_txt, tk_txt, pb, rv4 = _common(sig)
    emoji, name = TYPES["ar"].get(sig["type"], ("🔔", sig["type"]))
    notes = sig.get("notes", [])
    head_notes = [n for n in notes if n.startswith("counter_trend")]
    other_notes = [n for n in notes if not n.startswith("counter_trend")]
    exs = " + ".join(e.upper() for e in sig["exchanges"])
    conf = " ✅ مؤكدة على المنصتين" if len(sig["exchanges"]) > 1 else ""

    lines = [f"{emoji} <b>{name} | {html.escape(sig['symbol'])}</b>"]
    lines += [note_text(n, "ar") for n in head_notes]
    lines += [
        f"السكور: <b>{ltr(str(sig['score']) + '/100')}</b> · {exs}{conf}",
        f"الشمعة: {candle_window(sig['candle_close_ms'], tz_name)} بتوقيت القاهرة",
        "",
        f"💰 السعر: <b>{fp(sig['price'])}</b> ({pct(m.get('price_chg_pct'))} في الساعة)",
        f"📊 الفوليوم: {ltr('x' + format(m.get('rvol_1h') or 0, '.1f'))} من المتوسط"
        + (f" · 4h {ltr('x' + format(rv4, '.1f'))}" if rv4 else ""),
        f"📈 OI: {pct(m.get('oi_chg_1h_pct'), 0)} في الساعة · Funding: {f_txt} · Taker buy: {tk_txt}",
        f"🕓 4h: {_ma_state(m, 'ar')}"
        + (f" · %B {ltr(format(pb, '.2f'))}" if pb is not None else "")
        + f" · {pct(m.get('from_7d_low_pct'), 0)} من قاع 7 أيام",
        "",
        f"🎯 المقاومات: R1 {fp(lv.get('r1'))} · R2 {fp(lv.get('r2'))}",
        f"🛡 الدعوم: S1 {fp(lv.get('s1'))} · S2 {fp(lv.get('s2'))}",
    ]
    if plan:
        lines += [
            "",
            f"📝 الخطة: <b>{SIDE['ar'][plan['side']]}</b> · SL {fp(plan['sl'])} ({ltr(str(plan['sl_pct']) + '%')})",
            f"الحجم لمخاطرة {ltr('$' + format(plan['risk_usd'], '.0f'))}: "
            f"{ltr(qty(plan['size']))} {html.escape(sig['key'])} (~{ltr('$' + format(plan['notional_usd'], ',.0f'))})",
        ]
    if other_notes:
        lines.append("")
        lines += [f"• {note_text(n, 'ar')}" for n in other_notes]
    return "\n".join(lines)


def _signal_en(sig: dict, tz_name: str) -> str:
    m, lv, plan, f_txt, tk_txt, pb, rv4 = _common(sig)
    emoji, name = TYPES["en"].get(sig["type"], ("🔔", sig["type"]))
    notes = sig.get("notes", [])
    head = [n for n in notes if n.startswith("counter_trend")]
    rest = [n for n in notes if not n.startswith("counter_trend")]
    conf = " (confirmed on both)" if len(sig["exchanges"]) > 1 else ""
    lines = [f"{emoji} <b>{name} | {html.escape(sig['symbol'])}</b>  |  Score {sig['score']}/100"]
    lines += [note_text(n, "en") for n in head]
    lines += [
        " + ".join(e.upper() for e in sig["exchanges"]) + conf,
        f"Candle {candle_window(sig['candle_close_ms'], tz_name)}",
        "",
        f"Price {fp(sig['price'])}  ({pct(m.get('price_chg_pct'))} 1h)",
        f"RVOL  1h x{m.get('rvol_1h') or 0:.1f}" + (f"  |  4h x{rv4:.1f}" if rv4 else ""),
        f"OI    {pct(m.get('oi_chg_1h_pct'), 0)} 1h  |  Funding {f_txt}  |  Taker buy {tk_txt}",
        f"4h    {_ma_state(m, 'en')}" + (f"  |  %B {pb:.2f}" if pb is not None else "")
        + f"  |  {pct(m.get('from_7d_low_pct'), 0)} from 7d low",
        "",
        f"Levels  R1 {fp(lv.get('r1'))}  R2 {fp(lv.get('r2'))}  |  S1 {fp(lv.get('s1'))}  S2 {fp(lv.get('s2'))}",
    ]
    if plan:
        lines.append(f"Plan    {plan['side'].upper()}  SL {fp(plan['sl'])} ({plan['sl_pct']}%)  |  "
                     f"size for ${plan['risk_usd']:.0f} risk: {qty(plan['size'])} {sig['key']} "
                     f"(~${plan['notional_usd']:,.0f})")
    lines += [f"Note    {note_text(n, 'en')}" for n in rest]
    return "\n".join(lines)


# ----------------------------------------------------------------- other messages
def early(symbol: str, exchange: str, rvol: float, chg: float, price: float, lang: str = "ar") -> str:
    if lang == "ar":
        return (f"⚡ <b>إنذار مبكر 15m | {html.escape(symbol)}</b> ({exchange.upper()})\n"
                f"الفوليوم: {ltr(f'x{rvol:.1f}')} من المتوسط · الحركة: {pct(chg)} · السعر: {fp(price)}\n"
                f"استنى قفل شمعة الساعة للتأكيد.")
    return (f"⚡ <b>EARLY 15m | {html.escape(symbol)}</b> ({exchange.upper()})\n"
            f"RVOL 15m x{rvol:.1f}  |  {chg:+.1f}%  |  Price {fp(price)}\n"
            f"Watch the 1h close for confirmation.")


def started(exchanges: list, memory: bool, lang: str = "ar") -> str:
    exs = " + ".join(e.upper() for e in exchanges)
    if lang == "ar":
        st = "ذاكرة مؤقتة (بتتمسح مع الـ restart)" if memory else "Redis"
        return f"🟢 <b>الـ Volume Scanner اشتغل</b>\nالمنصات: {exs}\nحفظ الحالة: {st}"
    return f"🟢 Volume Scanner started ({exs}), state: {'memory' if memory else 'redis'}"


def heartbeat(stats: dict, exchanges: list, lang: str = "ar") -> str:
    took = stats.get("took_s", "n/a")
    rows = []
    for name in exchanges:
        s = stats.get(name, {})
        if lang == "ar":
            rows.append(f"{name.upper()}: {s.get('eligible', 0)} من {s.get('contracts', 0)} عقد · "
                        f"{s.get('spikes', 0)} spikes")
        else:
            rows.append(f"{name.upper()}: {s.get('eligible', 0)}/{s.get('contracts', 0)} scanned, "
                        f"{s.get('spikes', 0)} spikes")
    if lang == "ar":
        return "💓 <b>الـ Scanner شغال</b>\n" + "\n".join(rows) + f"\nمدة آخر scan: {ltr(str(took) + 's')}"
    return "💓 <b>Heartbeat</b>\n" + "\n".join(rows) + f"\nLast scan: {took}s"


def error(text: str, lang: str = "ar") -> str:
    head = "🔴 <b>مشكلة في الـ Scanner</b>" if lang == "ar" else "🔴 <b>Scanner error</b>"
    return f"{head}\n{html.escape(text)}"
