import asyncio

from scanner import analysis, indicators as ind
from scanner.config import Config
from scanner.followup import daily_summary, evaluate, is_win
from scanner.models import Candle, Snapshot, normalize_base
from scanner.state import State
from scanner.telegram import format_signal

H = 3_600_000
T0 = 1_759_000_000_000 - (1_759_000_000_000 % H)


def flat_1h(n, price=0.02, vol=50_000, taker=0.5, drift=0.0):
    out, p = [], price
    for i in range(n):
        o = p
        p = p * (1 + drift)
        c = p * (1.002 if i % 2 else 0.998)
        out.append(Candle(T0 + i * H, o, max(o, c) * 1.003, min(o, c) * 0.997, c, vol, vol * taker))
        p = c
    return out


def to_4h(c1h):
    out = []
    for i in range(0, len(c1h) - len(c1h) % 4, 4):
        g = c1h[i:i + 4]
        out.append(Candle(g[0].ts, g[0].open, max(x.high for x in g), min(x.low for x in g), g[-1].close,
                          sum(x.volume_usd for x in g)))
    return out


def breakout_snapshot(funding=-0.0001, oi_jump=0.18, chg=0.068, rvol_mult=7.4):
    c = flat_1h(199)
    last = c[-1]
    o = last.close
    close = o * (1 + chg)
    c.append(Candle(last.ts + H, o, close * 1.005, o * 0.998, close, 50_000 * rvol_mult, 50_000 * rvol_mult * 0.68))
    c4 = to_4h(c)
    now = c[-1].ts + H
    oi = [(now - k * H, 1_000_000.0) for k in range(10, 0, -1)] + [(now, 1_000_000 * (1 + oi_jump))]
    return Snapshot("okx", "NIGHT-USDT-SWAP", "NIGHT", now, c, c4, [], oi, funding, None, 5_000_000)


def test_normalize():
    assert normalize_base("1000PEPE") == "PEPE"
    assert normalize_base("1MBABYDOGE") == "BABYDOGE"
    assert normalize_base("NIGHT") == "NIGHT"


def test_rvol_and_pct_b():
    c = flat_1h(21)
    c[-1].volume_usd = 150_000
    assert abs(ind.rvol(c, 20) - 3.0) < 1e-9
    assert 0 <= ind.pct_b([1.0] * 19 + [1.0]) <= 1


def test_early_breakout_like_night():
    cfg = Config()
    s = breakout_snapshot()
    assert analysis.quick_spike(s.candles_1h, cfg, "1h")
    m = analysis.compute_metrics(s, cfg)
    assert m["base_breakout"] is True
    assert m["from_7d_low_pct"] < 25
    sig = analysis.build_signal(s, m, cfg, ["okx", "binance"])
    assert sig.type == "EARLY_BREAKOUT", (sig.type, m)
    assert sig.side == "long"
    assert sig.score >= cfg.MIN_SCORE_TO_ALERT, sig.score_parts
    assert "funding_neg" in sig.notes
    assert sig.plan and sig.plan["sl"] < sig.price
    # risk sizing: size * (entry - sl) == risk
    assert abs(sig.plan["size"] * (sig.price - sig.plan["sl"]) - 20) < 1e-6
    txt = format_signal(sig.to_dict(), "en")
    assert "Early breakout | NIGHT-USDT-SWAP" in txt and "confirmed on both" in txt
    ar = format_signal(sig.to_dict(), "ar")
    assert "اختراق مبكر" in ar and "ممنوع الشورت" in ar and "مؤكدة على المنصتين" in ar


def test_matrix_types():
    cfg = Config()
    s = breakout_snapshot(oi_jump=-0.10)
    m = analysis.compute_metrics(s, cfg)
    assert analysis.classify(m, cfg)[0] == "SHORT_SQUEEZE"

    s = breakout_snapshot(chg=-0.06, oi_jump=0.12)
    m = analysis.compute_metrics(s, cfg)
    assert analysis.classify(m, cfg)[:2] == ("NEW_SHORTS", "short")

    s = breakout_snapshot(chg=-0.06, oi_jump=-0.12)
    m = analysis.compute_metrics(s, cfg)
    assert analysis.classify(m, cfg)[0] == "LONG_LIQUIDATION"


def test_late_pump_penalised():
    cfg = Config()
    c = flat_1h(150, price=0.01, drift=0.008)  # ~3x over the window
    last = c[-1]
    c.append(Candle(last.ts + H, last.close, last.close * 1.1, last.close, last.close * 1.08, 500_000, 350_000))
    s = Snapshot("binance", "XUSDT", "X", c[-1].ts + H, c, to_4h(c), [], [], 0.0001, None, 9_000_000)
    m = analysis.compute_metrics(s, cfg)
    t, side, _ = analysis.classify(m, cfg)
    assert t == "LATE_PUMP" and side == "none"
    sc, parts = analysis.score(m, t, cfg, False)
    assert parts["penalties"] <= -30


def test_followup_and_summary():
    cfg = Config()
    s = breakout_snapshot()
    sig = analysis.build_signal(s, analysis.compute_metrics(s, cfg), cfg, ["okx"]).to_dict()
    p = sig["price"]
    r1 = sig["levels"]["r1"] or p * 1.05
    after = [Candle(sig["candle_close_ms"] + i * H, p, max(p * 1.01, r1 * 1.001) if i == 2 else p * 1.01,
                    p * 0.995, p * 1.03, 1, None) for i in range(30)]
    f4 = evaluate(sig, after, 4)
    assert f4["hit_r1"] and f4["result_4h"] > 0
    sig["followup"] = {"4h": f4, "24h": evaluate(sig, after, 24)}
    assert is_win(sig) is True
    txt = daily_summary([sig], [sig], "en")
    assert "Early breakout" in txt and "Win rate" in txt
    assert "نسبة النجاح" in daily_summary([sig], [sig], "ar")


def test_state_cooldown_memory():
    async def run():
        st = await State.connect("")
        assert not await st.in_cooldown("NIGHT", "EARLY_BREAKOUT", 2)
        await st.mark_alert("NIGHT", "EARLY_BREAKOUT", 2)
        assert await st.in_cooldown("NIGHT", "EARLY_BREAKOUT", 2)
        assert not await st.in_cooldown("NIGHT", "SQUEEZE_RISK", 2)  # type changed
        await st.save_signal({"id": "a", "candle_close_ms": 10})
        assert len(await st.signals_between(0, 20)) == 1
    asyncio.run(run())


def test_sl_floor_rune_case():
    """RUNE live case: S1 just under price gave a 1% stop inside the spike candle."""
    cfg = Config()
    m = {"price": 0.7844, "levels": {"s1": 0.7827, "s2": 0.7721, "r1": 0.7924, "r2": 0.8091}, "atr_1h": 0.009}
    plan = analysis.make_plan(m, "long", cfg)
    assert plan["sl_pct"] >= cfg.MIN_SL_PCT
    assert abs(plan["sl"] - (0.7844 - 0.0118)) < 1e-3  # max(1.5% = 0.0118, ATR 0.009)
    assert abs(plan["size"] * (plan["entry"] - plan["sl"]) - 20) < 1e-6


def test_dotenv_txt_and_empty_env(tmp_path, monkeypatch):
    from scanner.config import Config
    monkeypatch.chdir(tmp_path)
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\n")
    (tmp_path / ".env.txt").write_text("﻿TELEGRAM_BOT_TOKEN=111:abc\nTELEGRAM_CHAT_ID=5523\n", encoding="utf-8")
    cfg = Config.load()
    assert cfg.TELEGRAM_BOT_TOKEN == "111:abc" and cfg.TELEGRAM_CHAT_ID == "5523"
    assert ".env.txt" in cfg.ENV_FILE
