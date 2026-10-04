"""Exchange-neutral data shapes shared by every module."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Optional

_MULT_PREFIX = re.compile(r"^(1000000|100000|10000|1000|1M)(?=[A-Z])")


def normalize_base(base: str) -> str:
    """One key per coin across exchanges: 1000PEPE / PEPE -> PEPE."""
    base = base.upper()
    return _MULT_PREFIX.sub("", base)


@dataclass
class Candle:
    ts: int                 # open time, ms UTC
    open: float
    high: float
    low: float
    close: float
    volume_usd: float       # quote volume
    taker_buy_usd: Optional[float] = None


@dataclass
class Contract:
    exchange: str
    symbol: str             # exchange symbol: NIGHT-USDT-SWAP / NIGHTUSDT
    key: str                # normalized coin key: NIGHT
    list_time_ms: int = 0
    ct_val: float = 1.0     # OKX contract size in base coin
    multiplier: float = 1.0 # 1000PEPE -> 1000


@dataclass
class Snapshot:
    """Everything the analysis needs for one coin on one exchange at one time."""
    exchange: str
    symbol: str
    key: str
    now_ms: int
    candles_1h: list
    candles_4h: list = field(default_factory=list)
    candles_15m: list = field(default_factory=list)
    oi_hist: list = field(default_factory=list)     # [(ts_ms, oi_usd)], oldest first
    funding: Optional[float] = None
    taker_buy_ratio: Optional[float] = None        # overrides candle taker data if set
    vol24h_usd: Optional[float] = None


@dataclass
class Signal:
    id: str
    key: str
    symbol: str
    exchange: str
    exchanges: list
    type: str
    side: str               # long / short / none
    score: int
    timeframe: str
    candle_close: str
    candle_close_ms: int
    price: float
    metrics: dict
    levels: dict
    plan: Optional[dict]
    notes: list
    score_parts: dict
    followup: Optional[dict] = None

    def to_dict(self) -> dict:
        return asdict(self)
