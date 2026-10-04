"""Number / time formatting shared by all templates."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

LRM = "‎"  # keeps "+6.8%" / "-0.010%" from flipping inside Arabic (RTL) lines


def ltr(s) -> str:
    return f"{LRM}{s}{LRM}"


def price(x) -> str:
    """Price with sensible significant digits."""
    if x is None:
        return "-"
    x = float(x)
    if x == 0:
        return "0"
    if abs(x) >= 1000:
        return ltr(f"{x:,.1f}")
    if abs(x) >= 1:
        return ltr(f"{x:.4f}".rstrip("0").rstrip("."))
    s = f"{x:.10f}"
    lead = len(s.split(".")[1]) - len(s.split(".")[1].lstrip("0"))
    return ltr(f"{x:.{lead + 4}f}".rstrip("0").rstrip("."))


def pct(x, digits: int = 1, sign: bool = True) -> str:
    if x is None:
        return "n/a"
    return ltr(f"{x:+.{digits}f}%" if sign else f"{x:.{digits}f}%")


def mult(x) -> str:
    return ltr(f"x{x:.1f}") if x is not None else "n/a"


def usd(x, decimals: int = 0) -> str:
    return ltr(f"${x:,.{decimals}f}")


def qty(x: float) -> str:
    if x >= 1000:
        return ltr(f"{x:,.0f}")
    if x >= 1:
        return ltr(f"{x:,.2f}")
    return ltr(f"{x:.4f}")


def funding(x) -> str:
    return ltr(f"{x * 100:+.3f}%") if x is not None else "n/a"


def ratio(x) -> str:
    return ltr(f"{x * 100:.0f}%") if x is not None else "n/a"


def candle_window(close_ms: int, tz_name: str) -> str:
    try:
        tz = ZoneInfo(tz_name)
        o = datetime.fromtimestamp(close_ms / 1000 - 3600, tz=timezone.utc).astimezone(tz)
        c = datetime.fromtimestamp(close_ms / 1000, tz=timezone.utc).astimezone(tz)
        return ltr(f"{o:%H:%M}-{c:%H:%M}")
    except Exception:
        return ""
