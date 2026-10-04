"""Binance USDT-M futures public market data. No API keys."""
from __future__ import annotations

import time

from ..models import Candle, Contract, normalize_base
from .base import BaseExchange
from .okx import _mult

TF_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}


class Binance(BaseExchange):
    name = "binance"
    base_url = "https://fapi.binance.com"
    RATE_LIMITS = [
        ("/futures/data/", 3, 1.0),   # 1000 / 5min per IP
        ("", 15, 1.0),                # 2400 weight / min, klines weigh 2
    ]

    async def contracts(self) -> list[Contract]:
        info = await self._get("/fapi/v1/exchangeInfo")
        out = []
        for s in info.get("symbols", []):
            if (s.get("contractType") != "PERPETUAL" or s.get("quoteAsset") != "USDT"
                    or s.get("status") != "TRADING"):
                continue
            base = s["baseAsset"]
            out.append(Contract(
                exchange=self.name, symbol=s["symbol"], key=normalize_base(base),
                list_time_ms=int(s.get("onboardDate") or 0), multiplier=_mult(base),
            ))
        return out

    async def volumes_24h(self) -> dict:
        rows = await self._get("/fapi/v1/ticker/24hr")
        return {r["symbol"]: (float(r.get("quoteVolume") or 0), float(r.get("lastPrice") or 0)) for r in rows}

    async def funding_all(self) -> dict:
        rows = await self._get("/fapi/v1/premiumIndex")
        return {r["symbol"]: float(r.get("lastFundingRate") or 0) for r in rows}

    async def oi_all(self, prices: dict) -> dict:
        return {}  # no bulk endpoint; openInterestHist is used instead

    async def candles(self, c: Contract, tf: str, limit: int, end_ms: int | None = None,
                      now_ms: int | None = None) -> list[Candle]:
        params = {"symbol": c.symbol, "interval": tf, "limit": str(min(limit + 1, 1500))}
        if end_ms is not None:
            params["endTime"] = str(end_ms)
        rows = await self._get("/fapi/v1/klines", params)
        cutoff = end_ms if end_ms is not None else (now_ms or int(time.time() * 1000))
        out = []
        for r in rows:
            if int(r[6]) >= cutoff:  # candle not closed yet
                continue
            out.append(Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                              float(r[7]), float(r[10])))
        return out[-limit:]

    async def oi_hist(self, c: Contract, limit: int = 30, end_ms: int | None = None) -> list:
        params = {"symbol": c.symbol, "period": "1h", "limit": str(min(limit, 500))}
        if end_ms:
            params["endTime"] = str(end_ms)
        rows = await self._get("/futures/data/openInterestHist", params)
        return sorted((int(r["timestamp"]), float(r["sumOpenInterestValue"])) for r in rows)

    async def funding(self, c: Contract) -> float | None:
        r = await self._get("/fapi/v1/premiumIndex", {"symbol": c.symbol})
        return float(r.get("lastFundingRate") or 0)

    async def funding_at(self, c: Contract, ts_ms: int) -> float | None:
        rows = await self._get("/fapi/v1/fundingRate",
                               {"symbol": c.symbol, "endTime": str(ts_ms), "limit": "1"})
        return float(rows[-1]["fundingRate"]) if rows else None

    async def taker_ratio(self, c: Contract, end_ms: int | None = None) -> float | None:
        return None  # taken from the kline's taker-buy column
