"""All tunable settings in one place.

Every value can be overridden with an environment variable of the same name
(e.g. RVOL_MIN_1H=2.5) so thresholds change on Railway without touching code.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields


def _env(name: str, default):
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(float(raw))
    if isinstance(default, float):
        return float(raw)
    if isinstance(default, (list, tuple)):
        return [x.strip().upper() if name != "EXCHANGES" else x.strip().lower()
                for x in raw.split(",") if x.strip()]
    return raw


@dataclass
class Config:
    # --- exchanges / universe -------------------------------------------
    EXCHANGES: list = field(default_factory=lambda: ["okx", "binance"])
    WATCHLIST: list = field(default_factory=list)   # always scanned, skip liquidity filter
    BLACKLIST: list = field(default_factory=list)   # never scanned
    MIN_24H_VOLUME_USD: float = 2_000_000
    MIN_LISTING_AGE_DAYS: int = 7

    # --- spike detection -------------------------------------------------
    RVOL_LOOKBACK: int = 20
    RVOL_MIN_1H: float = 3.0
    RVOL_MIN_15M: float = 4.0
    RVOL_FULL_SCORE: float = 10.0       # RVOL at which the RVOL component is maxed
    PRICE_MOVE_MIN: float = 2.0         # % move of the 1h candle
    PRICE_MOVE_MIN_15M: float = 1.5     # % move of the 15m candle (early warning)

    # --- structure -------------------------------------------------------
    BASE_LOOKBACK_1H: int = 24          # candles that form the "sideways base"
    BASE_RANGE_MAX_PCT: float = 12.0    # base is sideways if its range <= this
    EARLY_MAX_FROM_LOW_PCT: float = 25.0
    LATE_PUMP_FROM_LOW_PCT: float = 100.0
    LATE_PUMP_PCT_B: float = 1.0
    OI_FULL_SCORE_PCT: float = 15.0     # OI change (1h, %) at which OI component is maxed
    OI_FLAT_PCT: float = 0.5            # |OI change| below this counts as flat

    # --- funding ---------------------------------------------------------
    FUNDING_AGAINST: float = 0.0005     # +0.05% on a long (or -0.05% on a short) = crowded
    SQUEEZE_FUNDING_MAX: float = 0.0    # funding below this with price & OI up -> squeeze flag

    # --- alerting --------------------------------------------------------
    MIN_SCORE_TO_ALERT: int = 60
    COOLDOWN_HOURS: float = 2.0
    RISK_PER_TRADE_USD: float = 20.0
    SL_BUFFER_PCT: float = 0.8          # SL placed this % beyond S1/R1
    MAX_SL_PCT: float = 10.0            # flag plans whose SL is wider than this

    # --- scheduling ------------------------------------------------------
    HOURLY_SCAN_MINUTE: int = 1         # run at HH:01
    LIGHT_SCAN_EVERY_MIN: int = 15
    ENABLE_LIGHT_SCAN: bool = True
    DAILY_SUMMARY_HOUR_CAIRO: int = 9
    HEARTBEAT_HOURS: int = 6
    SCAN_SLOW_SECONDS: int = 180
    TIMEZONE: str = "Africa/Cairo"

    # --- http ------------------------------------------------------------
    HTTP_CONCURRENCY: int = 8
    HTTP_TIMEOUT: int = 15
    CANDLES_1H: int = 200               # 7d low/high needs 168
    CANDLES_4H: int = 60
    CANDLES_15M: int = 40

    # --- infra -----------------------------------------------------------
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""          # signals
    TELEGRAM_STATUS_CHAT_ID: str = ""   # heartbeat / errors (defaults to TELEGRAM_CHAT_ID)
    REDIS_URL: str = ""                 # empty -> in-memory state (local testing)
    DRY_RUN: bool = False               # print alerts instead of sending
    LOG_LEVEL: str = "INFO"

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        for f in fields(cls):
            setattr(cfg, f.name, _env(f.name, getattr(cfg, f.name)))
        if not cfg.TELEGRAM_STATUS_CHAT_ID:
            cfg.TELEGRAM_STATUS_CHAT_ID = cfg.TELEGRAM_CHAT_ID
        return cfg
