"""End-to-end hourly scan against fake exchanges (no network)."""
import asyncio
import time

from scanner import engine
from scanner.config import Config
from scanner.models import Candle, Contract
from scanner.state import State
from tests.test_analysis import H, flat_1h, to_4h


def _series(spike: bool, now_ms: int):
    c = flat_1h(200)
    shift = (now_ms - now_ms % H) - (c[-1].ts + H)
    for x in c:
        x.ts += shift
    if spike:
        last = c[-1]
        c[-1] = Candle(last.ts, last.open, last.open * 1.075, last.open * 0.998, last.open * 1.07,
                       400_000, 280_000)
    return c


class FakeEx:
    def __init__(self, name):
        self.name = name
        self.request_count = 0
        self.rate_limited = 0
        self.now = int(time.time() * 1000)

    async def contracts(self):
        sym = {"okx": "{}-USDT-SWAP", "binance": "{}USDT"}[self.name]
        return [Contract(self.name, sym.format(k), k, 0) for k in ("NIGHT", "FLAT", "TINY")]

    async def volumes_24h(self):
        sym = {"okx": "{}-USDT-SWAP", "binance": "{}USDT"}[self.name]
        return {sym.format("NIGHT"): (5e6, 0.02), sym.format("FLAT"): (9e6, 0.02), sym.format("TINY"): (1e5, 0.02)}

    async def oi_all(self, prices):
        return {}

    async def candles(self, c, tf, limit, end_ms=None, now_ms=None):
        s = _series(c.key == "NIGHT", self.now)
        return s if tf == "1h" else (to_4h(s) if tf == "4h" else s[-40:])

    async def oi_hist(self, c, limit=30, end_ms=None):
        n = self.now - self.now % H
        return [(n - k * H, 1e6) for k in range(10, 0, -1)] + [(n, 1.2e6)]

    async def funding(self, c):
        return -0.0001

    async def taker_ratio(self, c, end_ms=None):
        return None


class FakeTG:
    def __init__(self):
        self.signals, self.errors = [], []

    async def signal(self, t):
        self.signals.append(t)

    async def status(self, t):
        pass

    async def error(self, k, t, throttle_s=0):
        self.errors.append(t)


def test_hourly_scan_end_to_end(monkeypatch):
    monkeypatch.setattr(engine, "REGISTRY", {"okx": lambda *a: FakeEx("okx"), "binance": lambda *a: FakeEx("binance")})

    async def run():
        st = await State.connect("")
        tg = FakeTG()
        sc = engine.Scanner(Config(), None, st, tg)
        sent = await sc.hourly_scan()
        assert not tg.errors, tg.errors
        assert len(sent) == 1, [s["key"] for s in sent]
        sig = sent[0]
        assert sig["key"] == "NIGHT" and sig["exchanges"] == ["binance", "okx"]
        assert sig["score_parts"]["cross_exchange"] == 15
        assert sc.last_stats["okx"]["eligible"] == 2  # TINY filtered by liquidity
        assert "[" in tg.signals[0] and "NIGHT" in tg.signals[0]
        # second scan in the same hour: cooldown blocks the repeat
        assert await sc.hourly_scan() == []
        await sc.followups()
        print("\n" + tg.signals[0])

    asyncio.run(run())


def test_pacer_spacing():
    from scanner.exchanges.base import Pacer

    async def run():
        p = Pacer(10, 1.0)  # 10/s -> 0.1s apart
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        for _ in range(6):
            await p.wait()
        return loop.time() - t0

    took = asyncio.run(run())
    assert 0.45 <= took < 0.8, took
