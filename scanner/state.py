"""State that lives between scans: cooldowns, OI snapshots, signal log,
contract cache.  Redis in production, in-memory dict when REDIS_URL is empty."""
from __future__ import annotations

import json
import time
from typing import Optional

PREFIX = "vscan:"


class MemoryBackend:
    def __init__(self):
        self.kv: dict = {}
        self.exp: dict = {}
        self.z: dict = {}

    async def get(self, k):
        if k in self.exp and self.exp[k] < time.time():
            self.kv.pop(k, None)
            self.exp.pop(k, None)
        return self.kv.get(k)

    async def set(self, k, v, ex: Optional[int] = None):
        self.kv[k] = v
        if ex:
            self.exp[k] = time.time() + ex
        else:
            self.exp.pop(k, None)

    async def zadd(self, k, mapping: dict):
        self.z.setdefault(k, {}).update(mapping)

    async def zrangebyscore(self, k, lo, hi):
        items = sorted(self.z.get(k, {}).items(), key=lambda x: x[1])
        return [m for m, s in items if lo <= s <= hi]

    async def zremrangebyscore(self, k, lo, hi):
        z = self.z.get(k, {})
        for m in [m for m, s in z.items() if lo <= s <= hi]:
            del z[m]

    async def ping(self):
        return True

    async def close(self):
        pass


class State:
    def __init__(self, backend):
        self.r = backend

    @classmethod
    async def connect(cls, url: str) -> "State":
        if not url:
            return cls(MemoryBackend())
        import redis.asyncio as redis
        client = redis.from_url(url, decode_responses=True)
        await client.ping()
        return cls(client)

    @property
    def is_memory(self) -> bool:
        return isinstance(self.r, MemoryBackend)

    async def _gj(self, k):
        v = await self.r.get(PREFIX + k)
        return json.loads(v) if v else None

    async def _sj(self, k, obj, ex=None):
        await self.r.set(PREFIX + k, json.dumps(obj, default=str), ex=ex)

    # -- cooldown --------------------------------------------------------
    async def in_cooldown(self, key: str, sig_type: str, hours: float) -> bool:
        last = await self._gj(f"cooldown:{key}")
        if not last:
            return False
        if last.get("type") != sig_type:
            return False  # classification changed -> allowed through
        return time.time() - last["ts"] < hours * 3600

    async def mark_alert(self, key: str, sig_type: str, hours: float):
        await self._sj(f"cooldown:{key}", {"ts": time.time(), "type": sig_type},
                       ex=int(hours * 3600) + 60)

    # -- signals ---------------------------------------------------------
    async def save_signal(self, sig: dict):
        await self._sj(f"signal:{sig['id']}", sig, ex=60 * 86400)
        await self.r.zadd(PREFIX + "signals", {sig["id"]: sig["candle_close_ms"]})

    async def get_signal(self, sid: str) -> Optional[dict]:
        return await self._gj(f"signal:{sid}")

    async def signals_between(self, start_ms: int, end_ms: int) -> list[dict]:
        ids = await self.r.zrangebyscore(PREFIX + "signals", start_ms, end_ms)
        out = []
        for sid in ids:
            s = await self.get_signal(sid)
            if s:
                out.append(s)
        return out

    async def prune_signals(self, older_than_ms: int):
        await self.r.zremrangebyscore(PREFIX + "signals", 0, older_than_ms)

    # -- OI snapshots (backup when history endpoint fails) ----------------
    async def add_oi_snapshot(self, exchange: str, values: dict, ts_ms: int, keep_hours: int = 48):
        if not values:
            return
        snap = await self._gj(f"oi:{exchange}") or {}
        cutoff = ts_ms - keep_hours * 3_600_000
        for sym, oi in values.items():
            pts = [p for p in snap.get(sym, []) if p[0] >= cutoff]
            pts.append([ts_ms, oi])
            snap[sym] = pts
        await self._sj(f"oi:{exchange}", snap)

    async def oi_snapshots(self, exchange: str, symbol: str) -> list:
        snap = await self._gj(f"oi:{exchange}") or {}
        return [tuple(p) for p in snap.get(symbol, [])]

    # -- contracts cache ---------------------------------------------------
    async def save_contracts(self, exchange: str, rows: list[dict]):
        await self._sj(f"contracts:{exchange}", {"ts": time.time(), "rows": rows})

    async def load_contracts(self, exchange: str, max_age_h: float = 6) -> Optional[list[dict]]:
        v = await self._gj(f"contracts:{exchange}")
        if v and time.time() - v["ts"] < max_age_h * 3600:
            return v["rows"]
        return None

    # -- misc flags --------------------------------------------------------
    async def get_flag(self, name: str):
        return await self._gj(f"flag:{name}")

    async def set_flag(self, name: str, value, ex: Optional[int] = None):
        await self._sj(f"flag:{name}", value, ex=ex)

    async def close(self):
        try:
            await self.r.close()
        except Exception:
            pass
