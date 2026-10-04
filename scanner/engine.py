"""Scan orchestration: universe -> pre-filter -> enrich candidates -> signals."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict

from . import analysis
from .config import Config
from .exchanges import REGISTRY, GeoBlocked, RateLimited
from .followup import evaluate, summary_stats
from .models import Contract, Snapshot
from .state import State
from .notify import Notifier

log = logging.getLogger(__name__)
H = 3_600_000


class Scanner:
    def __init__(self, cfg: Config, session, state: State, notifier: Notifier):
        self.cfg = cfg
        self.state = state
        self.notify = notifier
        self.ex = {n: REGISTRY[n](session, cfg.HTTP_CONCURRENCY, cfg.HTTP_TIMEOUT)
                   for n in cfg.EXCHANGES if n in REGISTRY}
        self.eligible: dict[str, list[Contract]] = {}
        self.last_stats: dict = {}

    # ------------------------------------------------------------------ universe
    async def contracts(self, name: str) -> list[Contract]:
        cached = await self.state.load_contracts(name)
        if cached:
            rows = [Contract(**r) for r in cached]
        else:
            rows = await self.ex[name].contracts()
            await self.state.save_contracts(name, [asdict(c) for c in rows])
        min_list = time.time() * 1000 - self.cfg.MIN_LISTING_AGE_DAYS * 86_400_000
        return [c for c in rows
                if c.key not in self.cfg.BLACKLIST
                and (c.list_time_ms == 0 or c.list_time_ms <= min_list or c.key in self.cfg.WATCHLIST)]

    async def _safe(self, coro, default=None):
        try:
            return await coro
        except GeoBlocked:
            raise
        except RateLimited as e:
            log.info("gave up after rate limits: %s", e)
            return default
        except Exception as e:
            log.debug("call failed: %s", e)
            return default

    # ------------------------------------------------------------------ hourly scan
    async def scan_exchange(self, name: str, now_ms: int) -> list[tuple[Snapshot, dict]]:
        ex = self.ex[name]
        cfg = self.cfg
        contracts = await self.contracts(name)
        vols = await ex.volumes_24h()

        oi_now = await self._safe(ex.oi_all(vols), {})
        if oi_now:
            hour_ms = now_ms - now_ms % H
            await self.state.add_oi_snapshot(name, oi_now, hour_ms)

        eligible = [c for c in contracts
                    if c.key in cfg.WATCHLIST or vols.get(c.symbol, (0, 0))[0] >= cfg.MIN_24H_VOLUME_USD]
        self.eligible[name] = eligible

        c1h = await asyncio.gather(*[self._safe(ex.candles(c, "1h", cfg.CANDLES_1H, now_ms=now_ms), [])
                                     for c in eligible])
        candidates = []
        for c, candles in zip(eligible, c1h):
            if candles and analysis.quick_spike(candles, cfg, "1h"):
                # stale data guard: last closed candle must be the one that just closed
                if candles[-1].ts + H >= now_ms - H:
                    candidates.append((c, candles))
        log.info("%s: %d contracts, %d eligible, %d spikes", name, len(contracts), len(eligible), len(candidates))

        funding_bulk = {}
        if candidates and hasattr(ex, "funding_all"):
            funding_bulk = await self._safe(ex.funding_all(), {})

        async def enrich(c: Contract, candles):
            c4h, oi_hist, fund, taker = await asyncio.gather(
                self._safe(ex.candles(c, "4h", cfg.CANDLES_4H, now_ms=now_ms), []),
                self._safe(ex.oi_hist(c, 30), []),
                self._safe(ex.funding(c)) if c.symbol not in funding_bulk else _const(funding_bulk[c.symbol]),
                self._safe(ex.taker_ratio(c)),
            )
            if not oi_hist:
                oi_hist = await self.state.oi_snapshots(name, c.symbol)
            snap = Snapshot(exchange=name, symbol=c.symbol, key=c.key, now_ms=now_ms,
                            candles_1h=candles, candles_4h=c4h, oi_hist=oi_hist, funding=fund,
                            taker_buy_ratio=taker, vol24h_usd=vols.get(c.symbol, (0, 0))[0])
            m = analysis.compute_metrics(snap, cfg)
            return (snap, m) if m else None

        results = await asyncio.gather(*[enrich(c, k) for c, k in candidates])
        self.last_stats[name] = {"contracts": len(contracts), "eligible": len(eligible),
                                 "spikes": len(candidates), "requests": ex.request_count,
                                 "rate_limited": ex.rate_limited}
        if ex.rate_limited:
            log.info("%s: %d rate-limit retries this scan", name, ex.rate_limited)
        ex.rate_limited = 0
        return [r for r in results if r]

    async def hourly_scan(self) -> list[dict]:
        t0 = time.time()
        now_ms = int(t0 * 1000)
        per_ex: dict[str, list] = {}

        async def run(name):
            try:
                per_ex[name] = await self.scan_exchange(name, now_ms)
            except GeoBlocked as e:
                await self.notify.error(
                    f"451:{name}",
                    f"{name.upper()} حاجبة المنطقة (HTTP 451). انقل السيرفر لـ region في أوروبا أو آسيا.",
                    f"{e}. Move the Railway service to an EU/Asia region.")
                per_ex[name] = []
            except Exception as e:
                log.exception("scan failed on %s", name)
                await self.notify.error(f"scan:{name}", f"{name.upper()} مش بترد: {e}",
                                        f"{name.upper()} not responding: {e}")
                per_ex[name] = []

        await asyncio.gather(*[run(n) for n in self.ex])
        sent = await self._emit(per_ex)

        took = time.time() - t0
        self.last_stats["took_s"] = round(took, 1)
        self.last_stats["at"] = now_ms
        await self.state.set_flag("last_scan", self.last_stats)
        if took > self.cfg.SCAN_SLOW_SECONDS:
            await self.notify.error("slow", f"الـ scan خد {took:.0f} ثانية (الحد {self.cfg.SCAN_SLOW_SECONDS})",
                                    f"Scan took {took:.0f}s (limit {self.cfg.SCAN_SLOW_SECONDS}s)")
        log.info("hourly scan done in %.1fs, %d alerts", took, len(sent))
        return sent

    async def _emit(self, per_ex: dict[str, list]) -> list[dict]:
        by_key: dict[str, list] = {}
        for name, items in per_ex.items():
            for snap, m in items:
                by_key.setdefault(snap.key, []).append((snap, m))

        sent = []
        for key, items in by_key.items():
            exchanges = [s.exchange for s, _ in items]
            sigs = [analysis.build_signal(s, m, self.cfg, exchanges) for s, m in items]
            best = max(sigs, key=lambda x: x.score)
            sig = best.to_dict()
            if sig["score"] < self.cfg.MIN_SCORE_TO_ALERT:
                continue
            if await self.state.in_cooldown(key, sig["type"], self.cfg.COOLDOWN_HOURS):
                continue
            await self.notify.signal(sig)
            await self.state.mark_alert(key, sig["type"], self.cfg.COOLDOWN_HOURS)
            await self.state.save_signal(sig)
            sent.append(sig)
        return sent

    # ------------------------------------------------------------------ 15m light scan
    async def light_scan(self) -> int:
        now_ms = int(time.time() * 1000)
        n = 0
        for name, ex in self.ex.items():
            eligible = self.eligible.get(name)
            if not eligible:
                continue
            try:
                rows = await asyncio.gather(*[self._safe(ex.candles(c, "15m", self.cfg.CANDLES_15M, now_ms=now_ms), [])
                                              for c in eligible])
            except Exception as e:
                await self.notify.error(f"light:{name}", f"مسح الـ 15m فشل على {name.upper()}: {e}",
                                        f"15m scan failed on {name.upper()}: {e}")
                continue
            for c, candles in zip(eligible, rows):
                hit = candles and analysis.quick_spike(candles, self.cfg, "15m")
                if not hit:
                    continue
                if await self.state.in_cooldown(f"{c.key}:15m", "EARLY_15M", self.cfg.COOLDOWN_HOURS):
                    continue
                await self.notify.early(c.symbol, name, hit[0], hit[1], candles[-1].close)
                await self.state.mark_alert(f"{c.key}:15m", "EARLY_15M", self.cfg.COOLDOWN_HOURS)
                n += 1
        return n

    # ------------------------------------------------------------------ follow-ups
    async def followups(self):
        now_ms = int(time.time() * 1000)
        sigs = await self.state.signals_between(now_ms - 26 * H, now_ms)
        for sig in sigs:
            fu = sig.get("followup") or {}
            age_h = (now_ms - sig["candle_close_ms"]) / H
            todo = [h for h in (4, 24) if age_h >= h and f"{h}h" not in fu]
            if not todo:
                continue
            ex = self.ex.get(sig["exchange"])
            if not ex:
                continue
            c = Contract(exchange=sig["exchange"], symbol=sig["symbol"], key=sig["key"])
            if sig["exchange"] == "okx":
                cached = {x.symbol: x for x in await self.contracts("okx")}
                c = cached.get(sig["symbol"], c)
            candles = await self._safe(ex.candles(c, "1h", min(int(age_h) + 3, 200), now_ms=now_ms), [])
            for h in todo:
                r = evaluate(sig, candles, h)
                if r:
                    fu[f"{h}h"] = r
            sig["followup"] = fu or None
            await self.state.save_signal(sig)
        await self.state.prune_signals(now_ms - 30 * 86_400_000)

    async def send_daily_summary(self):
        now_ms = int(time.time() * 1000)
        last24 = await self.state.signals_between(now_ms - 24 * H, now_ms)
        last7 = await self.state.signals_between(now_ms - 7 * 24 * H, now_ms)
        await self.notify.daily_summary(summary_stats(last24, last7))

    async def heartbeat(self):
        st = await self.state.get_flag("last_scan") or {}
        await self.notify.heartbeat(st, list(self.ex))


async def _const(v):
    return v
