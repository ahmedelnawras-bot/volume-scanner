"""OKX public market data (USDT-margined linear swaps). No API keys."""
from __future__ import annotations

import time

from ..models import Candle, Contract, normalize_base
from .base import BaseExchange, ExchangeError, RateLimited

BAR = {"15m": "15m", "1h": "1H", "4h": "4H"}
TF_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}


class OKX(BaseExchange):
    name = "okx"
    base_url = "https://www.okx.com"
    RATE_LIMITS = [
        ("/api/v5/rubik/", 2, 1.0),              # 5 / 2s
        ("/api/v5/market/history-candles", 8, 1.0),  # 20 / 2s
        ("/api/v5/market/candles", 16, 1.0),     # 40 / 2s
        ("/api/v5/public/funding-rate", 8, 1.0), # 20 / 2s
        ("", 8, 1.0),
    ]

    def _unwrap(self, data, path):
        code = str(data.get("code", "0"))
        if code == "0":
            return data.get("data", [])
        if code in ("50011", "50061"):
            raise RateLimited(f"okx rate limited: {data.get('msg')}")
        raise ExchangeError(f"okx {path}: code {code} {data.get('msg')}")

    # -- universe --------------------------------------------------------
    async def contracts(self) -> list[Contract]:
        rows = await self._get("/api/v5/public/instruments", {"instType": "SWAP"})
        out = []
        for r in rows:
            if r.get("settleCcy") != "USDT" or r.get("ctType") != "linear" or r.get("state") != "live":
                continue
            base = r["instId"].split("-")[0]
            out.append(Contract(
                exchange=self.name, symbol=r["instId"], key=normalize_base(base),
                list_time_ms=int(r.get("listTime") or 0),
                ct_val=float(r.get("ctVal") or 1),
                multiplier=_mult(base),
            ))
        return out

    async def volumes_24h(self) -> dict:
        rows = await self._get("/api/v5/market/tickers", {"instType": "SWAP"})
        out = {}
        for r in rows:
            last = float(r.get("last") or 0)
            vol_base = float(r.get("volCcy24h") or 0)  # SWAP: base-coin volume
            out[r["instId"]] = (vol_base * last, last)
        return out

    async def oi_all(self, prices: dict) -> dict:
        rows = await self._get("/api/v5/public/open-interest", {"instType": "SWAP"})
        out = {}
        for r in rows:
            usd = r.get("oiUsd")
            if usd not in (None, ""):
                out[r["instId"]] = float(usd)
            else:
                px = prices.get(r["instId"], (0, 0))[1]
                out[r["instId"]] = float(r.get("oiCcy") or 0) * px
        return out

    # -- per-contract ----------------------------------------------------
    async def candles(self, c: Contract, tf: str, limit: int, end_ms: int | None = None,
                      now_ms: int | None = None) -> list[Candle]:
        rows: list = []
        if end_ms is None:
            rows = await self._get("/api/v5/market/candles",
                                   {"instId": c.symbol, "bar": BAR[tf], "limit": str(min(limit + 1, 300))})
        else:
            cursor = end_ms + 1
            while len(rows) < limit + 1:
                batch = await self._get("/api/v5/market/history-candles",
                                        {"instId": c.symbol, "bar": BAR[tf], "after": str(cursor), "limit": "100"})
                if not batch:
                    break
                rows.extend(batch)
                cursor = int(batch[-1][0])
        candles = []
        for r in reversed(rows):  # OKX is newest-first
            if len(r) > 8 and r[8] == "0":
                continue  # unconfirmed (still open)
            ts = int(r[0])
            if end_ms is not None and ts + TF_MS[tf] > end_ms + 1:
                continue
            close = float(r[4])
            vol_usd = float(r[5]) * c.ct_val * close
            candles.append(Candle(ts, float(r[1]), float(r[2]), float(r[3]), close, vol_usd))
        return candles[-limit:]

    async def oi_hist(self, c: Contract, limit: int = 30, end_ms: int | None = None) -> list:
        params = {"instId": c.symbol, "period": "1H", "limit": str(min(limit, 100))}
        if end_ms:
            params["end"] = str(end_ms)
        rows = await self._get("/api/v5/rubik/stat/contracts/open-interest-history", params)
        return sorted((int(r[0]), float(r[3])) for r in rows)

    async def funding(self, c: Contract) -> float | None:
        rows = await self._get("/api/v5/public/funding-rate", {"instId": c.symbol})
        return float(rows[0]["fundingRate"]) if rows else None

    async def funding_at(self, c: Contract, ts_ms: int) -> float | None:
        rows = await self._get("/api/v5/public/funding-rate-history",
                               {"instId": c.symbol, "after": str(ts_ms + 1), "limit": "1"})
        return float(rows[0]["fundingRate"]) if rows else None

    async def taker_ratio(self, c: Contract, end_ms: int | None = None) -> float | None:
        """Taker buy share of the last closed hour."""
        params = {"instId": c.symbol, "period": "1H", "limit": "3", "unit": "2"}
        if end_ms:
            params["end"] = str(end_ms)
        rows = await self._get("/api/v5/rubik/stat/taker-volume-contract", params)
        rows = sorted(rows, key=lambda r: int(r[0]))
        # keep only buckets whose hour has fully closed
        cutoff = end_ms if end_ms else int(time.time() * 1000)
        closed = [r for r in rows if int(r[0]) + 3_600_000 <= cutoff + 1]
        if not closed:
            return None
        sell, buy = float(closed[-1][1]), float(closed[-1][2])
        return buy / (buy + sell) if buy + sell > 0 else None


def _mult(base: str) -> float:
    for p, m in (("1000000", 1e6), ("100000", 1e5), ("10000", 1e4), ("1000", 1e3)):
        if base.startswith(p):
            return m
    return 1.0
