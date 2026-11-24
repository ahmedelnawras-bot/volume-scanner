#!/usr/bin/env python3
"""Spot ladder bot v2 (buy only) - Nour's 2026-2028 spot plan.

    python spot_ladder.py            # runs forever (PAPER mode unless LADDER_LIVE=true)
    python spot_ladder.py --status   # print plan / fills and exit
    python spot_ladder.py --selftest # offline test with a fake exchange
    python spot_ladder.py --test     # REHEARSAL: paper only, real OKX prices, fake levels 0.4%/0.8%/... under
                                     # today's BTC price, so you see the whole flow on Telegram in hours

Plan (agreed 2026-11-24)
  budget 2,590 USDT; BTC levels 53k 10% / 52k 15% / 51k 20% / 50k 25% / 49k 30% of the budget
  split per level: BTC 30, ETH 20, ADA 20, LINK 10, NEAR 10, PEPE 10 (%)
  BTC part : when BTC <= 55,000 the bot places resting LIMIT buys at the 5 levels (a quick wick still fills).
             If BTC goes back >= 65,000, unfilled limits are cancelled (USDT freed) and re-placed at 55k again.
  Alt part : when a level's BTC limit fills, the bot buys that level's ETH/ADA/LINK/NEAR/PEPE at once
             (limit IOC, at most 0.5% above the ask).
  Condition: an alt is NOT bought if it rose more than 10% in the last 24h or is not trading on OKX.
  Backup   : the money of a skipped alt goes 60% BTC / 40% ETH right away; if ETH itself fails the condition,
             that part goes to BTC.
  No money  : the bot never buys at higher prices to "catch up". If USDT is short when BTC reaches 55k, no BTC
             limits are placed (you get a PROBLEM alert, and one per level BTC touches = missed); after you add USDT
             the limits go in at the level prices only, and only while BTC is under 55k. If a level's BTC fills but
             USDT for its alts is short, the alts are bought only while BTC is within +2% of that level; above that
             you get a PROBLEM alert and they wait for BTC to come back.
  No selling - the plan leaves selling for later.

Settings (environment variables, or KEY=VALUE lines in extra.env / .env next to this file)
  LADDER_LIVE=false            true = real orders. Default false = PAPER (no keys needed, nothing is bought)
  LADDER_OKX_API_KEY / LADDER_OKX_API_SECRET / LADDER_OKX_API_PASSPHRASE
                               key created INSIDE the OKX sub-account (spoot), Read + Trade, NEVER Withdraw.
                               Live only. At start the bot checks the key: main account or withdraw -> it refuses.
  LADDER_SUBACCOUNT=spoot      name shown in messages
  LADDER_TELEGRAM_BOT_TOKEN / LADDER_TELEGRAM_CHAT_ID its own Telegram bot (falls back to TELEGRAM_BOT_TOKEN /
                               TELEGRAM_CHAT_ID); reports + /status /stop /resume
  REDIS_URL (or LADDER_REDIS_URL)  on Railway: keep the state in Redis so restarts/redeploys lose nothing
  LADDER_BUDGET=2590  LADDER_ARM=55000  LADDER_DISARM=65000  LADDER_MAX_PUMP=0.10  LADDER_MAX_SLIPPAGE=0.005
  LADDER_STATE                 default ladder_state.json (live) / ladder_state_paper.json (paper)

Safety
  - each level's alts are bought once; state is saved around every order; client order ids are fixed, so after
    a crash the bot looks an order up instead of buying twice; an alt step interrupted mid-way pauses the bot
  - total spend can never exceed LADDER_BUDGET; short USDT -> the step waits and you get one alert
  - /stop on Telegram pauses everything new (resting BTC limits stay on OKX; cancel them there if you want)
"""
import base64, hashlib, hmac, json, os, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone

LEVELS = [(53000, 0.10), (52000, 0.15), (51000, 0.20), (50000, 0.25), (49000, 0.30)]
BTC_W = 0.30
ALTS = [("ETH", 0.20), ("ADA", 0.20), ("LINK", 0.10), ("NEAR", 0.10), ("PEPE", 0.10)]
BACKUP = [("BTC", 0.60), ("ETH", 0.40)]
BASE = "https://www.okx.com"
HERE = os.path.dirname(os.path.abspath(__file__))


def load_env():
    for name in ("extra.env", ".env"):
        p = os.path.join(HERE, name)
        if not os.path.exists(p):
            continue
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


LOG_FILE = [os.path.join(HERE, "spot_ladder.log")]


def log(msg):
    line = f"{now()}  {msg}"
    print(line, flush=True)
    if LOG_FILE[0]:
        with open(LOG_FILE[0], "a", encoding="utf-8") as f:
            f.write(line + "\n")


def fmt(x):
    s = f"{x:.12f}".rstrip("0").rstrip(".")
    return s or "0"


def floor_to(x, step):
    return int(x / step + 1e-9) * step


# --------------------------------------------------------------------------- exchange
class OKX:
    def __init__(self, live):
        self.live = live
        # keys of the OKX SUB-ACCOUNT only - deliberately different names from the main bot's OKX_API_KEY
        self.key = os.environ.get("LADDER_OKX_API_KEY", "").strip()
        self.secret = os.environ.get("LADDER_OKX_API_SECRET", "").strip()
        self.pp = os.environ.get("LADDER_OKX_API_PASSPHRASE", "").strip()
        if live and not (self.key and self.secret and self.pp):
            raise SystemExit("LADDER_LIVE=true but LADDER_OKX_API_KEY / LADDER_OKX_API_SECRET / "
                             "LADDER_OKX_API_PASSPHRASE (sub-account key) are missing")
        self._inst = {}
        self.paper_limits = {}           # paper mode: cl_id -> [inst, px, sz, state, filled]

    def _req(self, method, path, params=None, body=None, signed=False):
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        data = json.dumps(body) if body is not None else ""
        hdr = {"Content-Type": "application/json", "User-Agent": "spot-ladder/2.0"}
        if signed:
            t = datetime.now(timezone.utc)
            ts = t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"
            sign = base64.b64encode(hmac.new(self.secret.encode(), (ts + method + path + q + data).encode(),
                                             hashlib.sha256).digest()).decode()
            hdr.update({"OK-ACCESS-KEY": self.key, "OK-ACCESS-SIGN": sign, "OK-ACCESS-TIMESTAMP": ts,
                        "OK-ACCESS-PASSPHRASE": self.pp})
        last = None
        for attempt in range(4):
            try:
                r = urllib.request.Request(BASE + path + q, data=data.encode() if data else None,
                                           headers=hdr, method=method)
                with urllib.request.urlopen(r, timeout=15) as resp:
                    out = json.loads(resp.read().decode())
                if str(out.get("code")) == "0":
                    return out.get("data", [])
                last = f"OKX {path} code {out.get('code')}: {out.get('msg')} {out.get('data')}"
                if str(out.get("code")) != "50011":
                    raise RuntimeError(last)
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
                last = f"network: {e}"
            time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(last or f"OKX {path} failed")

    def ticker(self, inst):
        """(last, ask, change over the last 24h)"""
        t = self._req("GET", "/api/v5/market/ticker", {"instId": inst})[0]
        last, op = float(t["last"]), float(t.get("open24h") or 0)
        return last, float(t["askPx"] or t["last"]), (last / op - 1) if op else 0.0

    def inst(self, inst):
        """(lotSz, minSz, tickSz, live?)"""
        if inst not in self._inst:
            d = self._req("GET", "/api/v5/public/instruments", {"instType": "SPOT", "instId": inst})
            if not d:
                return (1, float("inf"), 1, False)
            d = d[0]
            self._inst[inst] = (float(d["lotSz"]), float(d["minSz"]), float(d["tickSz"]), d.get("state") == "live")
        return self._inst[inst]

    def check_account(self, want_label):
        """Live only: refuse to run on the main account or with a withdraw-enabled key."""
        cfg = self._req("GET", "/api/v5/account/config", signed=True)[0]
        uid, main_uid = str(cfg.get("uid", "")), str(cfg.get("mainUid", ""))
        perm = str(cfg.get("perm", "")).lower()
        if not uid or uid == main_uid:
            raise SystemExit("REFUSED: this API key belongs to the MAIN OKX account. Create the key inside the "
                             f"sub-account '{want_label}' and use that.")
        if "withdraw" in perm:
            raise SystemExit("REFUSED: this API key has WITHDRAW permission. Make a key with Read + Trade only.")
        if "trade" not in perm:
            raise SystemExit("REFUSED: this API key has no TRADE permission.")
        return f"sub-account uid {uid} (main {main_uid}), key permissions: {perm}"

    def usdt(self):
        if not self.live:
            return float("inf")
        d = self._req("GET", "/api/v5/account/balance", {"ccy": "USDT"}, signed=True)
        det = d[0].get("details", []) if d else []
        return float(det[0].get("availBal") or 0) if det else 0.0

    def find(self, inst, cl_id):
        try:
            d = self._req("GET", "/api/v5/trade/order", {"instId": inst, "clOrdId": cl_id}, signed=True)
            return d[0] if d else None
        except RuntimeError as e:
            if "51603" in str(e):
                return None
            raise

    # ---- resting limit (BTC levels)
    def place_limit(self, inst, px, sz, cl_id):
        if not self.live:
            self.paper_limits.setdefault(cl_id, [inst, px, sz, "live", 0.0])
            return
        if self.find(inst, cl_id) is None:
            self._req("POST", "/api/v5/trade/order", body={"instId": inst, "tdMode": "cash", "side": "buy",
                                                             "ordType": "limit", "px": fmt(px), "sz": fmt(sz),
                                                             "clOrdId": cl_id}, signed=True)

    def limit_state(self, inst, cl_id, last_px=None):
        """(state, filled_base, avg_px). state: live / partially_filled / filled / canceled / missing"""
        if not self.live:
            o = self.paper_limits.get(cl_id)
            if not o:
                return "missing", 0.0, 0.0
            if o[3] == "live" and last_px is not None and last_px <= o[1]:
                o[3], o[4] = "filled", o[2]
            return o[3], o[4], o[1]
        o = self.find(inst, cl_id)
        if not o:
            return "missing", 0.0, 0.0
        return o.get("state"), float(o.get("accFillSz") or 0), float(o.get("avgPx") or 0)

    def cancel(self, inst, cl_id):
        if not self.live:
            o = self.paper_limits.get(cl_id)
            if o and o[3] == "live":
                o[3] = "canceled"
            return
        try:
            self._req("POST", "/api/v5/trade/cancel-order", body={"instId": inst, "clOrdId": cl_id}, signed=True)
        except RuntimeError as e:
            if "51400" not in str(e) and "51401" not in str(e) and "51402" not in str(e):   # already done
                raise

    # ---- immediate buy (alts, backup)
    def buy_ioc(self, inst, px, sz, cl_id):
        if not self.live:
            return sz, px
        if self.find(inst, cl_id) is None:
            self._req("POST", "/api/v5/trade/order", body={"instId": inst, "tdMode": "cash", "side": "buy",
                                                             "ordType": "ioc", "px": fmt(px), "sz": fmt(sz),
                                                             "clOrdId": cl_id}, signed=True)
        for _ in range(10):
            time.sleep(0.7)
            o = self.find(inst, cl_id)
            if o and o.get("state") in ("filled", "canceled", "mmp_canceled"):
                return float(o.get("accFillSz") or 0), float(o.get("avgPx") or 0)
        raise RuntimeError(f"{inst} order {cl_id} state unknown - check OKX")


# --------------------------------------------------------------------------- telegram
class TG:
    def __init__(self):
        # its own Telegram bot first (LADDER_TELEGRAM_*), the old bot's variables only as a fallback
        # .strip(): a pasted value often carries a hidden newline/space, which breaks the Telegram URL
        self.tok = (os.environ.get("LADDER_TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
        self.chat = (os.environ.get("LADDER_TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID", "")).strip()
        self.offset = None

    def send(self, text):
        log("TG: " + text.replace("\n", " | "))
        if not (self.tok and self.chat):
            return
        try:
            body = urllib.parse.urlencode({"chat_id": self.chat, "text": text}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{self.tok}/sendMessage", body, timeout=15).read()
        except Exception as e:
            log("telegram send failed: " + str(e).replace(self.tok, "<token>"))   # never print the token

    def commands(self):
        if not (self.tok and self.chat):
            return []
        try:
            p = {"timeout": 0}
            if self.offset is not None:
                p["offset"] = self.offset
            with urllib.request.urlopen(f"https://api.telegram.org/bot{self.tok}/getUpdates?" +
                                        urllib.parse.urlencode(p), timeout=15) as r:
                ups = json.loads(r.read().decode()).get("result", [])
        except Exception:
            return []
        out = []
        for u in ups:
            self.offset = u["update_id"] + 1
            m = u.get("message") or {}
            if str(m.get("chat", {}).get("id")) == str(self.chat):
                out.append((m.get("text") or "").strip().split("@")[0].lower())
        return out


# --------------------------------------------------------------------------- state
class State:
    """Saved to a JSON file, or to Redis when redis_url is given (Railway wipes files on every restart)."""

    def __init__(self, path, redis_url=""):
        self.path = path
        self.key = "spot_ladder:" + os.path.basename(path)
        self.r = None
        self.d = {"levels": {}, "fills": {}, "spent": 0.0, "paused": False, "alerts": {}, "arm_round": 0,
                  "armed": False}
        if redis_url:
            import redis                                   # pip install redis (only needed on Railway)
            self.r = redis.from_url(redis_url, decode_responses=True)
            raw = self.r.get(self.key)
            if raw:
                self.d.update(json.loads(raw))
        elif os.path.exists(path):
            self.d.update(json.load(open(path, encoding="utf-8")))

    def lv(self, lv):
        return self.d["levels"].setdefault(str(lv), {"btc": "waiting", "alts": "waiting", "btc_id": "",
                                                    "btc_qty": 0.0, "btc_cost": 0.0})

    def save(self):
        if self.r is not None:
            self.r.set(self.key, json.dumps(self.d))
            return
        tmp = self.path + ".tmp"
        json.dump(self.d, open(tmp, "w", encoding="utf-8"), indent=1)
        os.replace(tmp, self.path)

    def add_fill(self, coin, qty, cost):
        f = self.d["fills"].setdefault(coin, {"qty": 0.0, "cost": 0.0})
        f["qty"] += qty
        f["cost"] += cost
        self.d["spent"] += cost


class Bot:
    def __init__(self, ex, st, tg, budget=2590.0, arm=55000.0, disarm=65000.0, max_pump=0.10, slip=0.005,
                 alt_tol=0.02):
        self.ex, self.st, self.tg = ex, st, tg
        self.alt_tol = alt_tol
        self.budget, self.arm, self.disarm, self.max_pump, self.slip = budget, arm, disarm, max_pump, slip

    # ---- summary
    def summary(self, btc=None):
        st = self.st
        lines = [f"Spot ladder ({'LIVE' if self.ex.live else 'PAPER'}) budget {self.budget:,.0f} USDT, "
                 f"spent {st.d['spent']:,.2f}"]
        if btc:
            state = "BTC limits placed" if st.d["armed"] else f"limits go in at {self.arm:,.0f}"
            lines.append(f"BTC {btc:,.0f} - {state}")
        for lv, pct in LEVELS:
            s = st.lv(lv)
            lines.append(f"  {lv:,} ({pct * self.budget:,.0f}$): BTC {s['btc']}, alts {s['alts']}")
        for coin, f in st.d["fills"].items():
            if f["qty"] > 0:
                lines.append(f"  {coin}: {fmt(round(f['qty'], 8))} @ avg {f['cost'] / f['qty']:.6g} "
                             f"(cost {f['cost']:,.2f})")
        if st.d.get("paused"):
            lines.append("PAUSED - send /resume to continue")
        return "\n".join(lines)

    def once_alert(self, key, text):
        if self.st.d["alerts"].get(key) != text:
            self.st.d["alerts"][key] = text
            self.st.save()
            self.tg.send(text)

    # ---- BTC limits
    def btc_step(self, btc):
        st, ex = self.st, self.ex
        open_lv = [(lv, pct) for lv, pct in LEVELS if st.lv(lv)["btc"] in ("waiting", "placed")]
        if not st.d["armed"] and btc <= self.arm and open_lv:
            need = sum(pct * self.budget * BTC_W for lv, pct in open_lv if st.lv(lv)["btc"] == "waiting")
            if st.d["spent"] + need > self.budget * 1.001:
                self.once_alert("btc_budget", "BTC limits not placed: budget would be exceeded")
                return
            if ex.usdt() < need * 0.995:
                st.d["unfunded"] = True
                self.once_alert("btc_bal", f"PROBLEM: BTC {btc:,.0f} is under {self.arm:,.0f} but USDT balance is "
                                           f"less than the {need:,.2f} needed for the BTC limit orders. Nothing placed. "
                                           f"Add USDT - orders will only ever be placed AT the level prices.")
                for lv, pct in open_lv:
                    if btc <= lv:
                        self.once_alert(f"miss{lv}", f"PROBLEM: BTC touched {lv:,} with no order (no USDT). "
                                                     f"This level was missed. It will NOT be bought at a higher "
                                                     f"price later - only if BTC comes back to {lv:,} after you add USDT.")
                return
            if st.d.pop("unfunded", False):
                self.tg.send("USDT found - placing the BTC limit orders now, at the level prices only.")
            st.d["arm_round"] += 1
            lot, mn, tick, _ = ex.inst("BTC-USDT")
            placed = []
            for lv, pct in open_lv:
                s = st.lv(lv)
                if s["btc"] != "waiting":
                    continue
                sz = floor_to(pct * self.budget * BTC_W / lv, lot)
                if sz < mn:
                    s["btc"], s["alts"] = "skipped (size)", s["alts"]
                    continue
                s["btc_id"] = f"B{lv}r{st.d['arm_round']}"
                s["btc"] = "placing"
                st.save()
                ex.place_limit("BTC-USDT", lv, sz, s["btc_id"])
                s["btc"] = "placed"
                st.save()
                placed.append(f"{lv:,} x {fmt(sz)} BTC")
            st.d["armed"] = True
            st.save()
            self.tg.send(f"BTC {btc:,.0f} <= {self.arm:,.0f}: limit buys placed on OKX\n  " + "\n  ".join(placed))
            return
        if not st.d["armed"]:
            return
        # check fills
        for lv, pct in LEVELS:
            s = st.lv(lv)
            if s["btc"] != "placed":
                continue
            state, q, avg = ex.limit_state("BTC-USDT", s["btc_id"], btc)
            if state == "filled":
                s["btc"], s["btc_qty"], s["btc_cost"] = "filled", q, q * avg
                st.add_fill("BTC", q, q * avg)
                st.save()
                self.tg.send(f"BTC limit {lv:,} FILLED: {fmt(round(q, 8))} BTC @ {avg:,.1f} = {q * avg:,.2f} USDT")
            elif state in ("canceled", "missing", "mmp_canceled"):
                s["btc"] = "waiting"
                if q > 0:
                    s["btc"], s["btc_qty"], s["btc_cost"] = "filled (partial)", q, q * avg
                    st.add_fill("BTC", q, q * avg)
                st.save()
                self.tg.send(f"BTC limit {lv:,} is no longer on OKX ({state}). "
                             + ("Partial fill kept." if q > 0 else "It will be placed again next time."))
        # disarm above the cancel price
        if btc >= self.disarm:
            for lv, pct in LEVELS:
                s = st.lv(lv)
                if s["btc"] != "placed":
                    continue
                ex.cancel("BTC-USDT", s["btc_id"])
                state, q, avg = ex.limit_state("BTC-USDT", s["btc_id"], None)
                if q > 0:
                    s["btc"], s["btc_qty"], s["btc_cost"] = "filled (partial)", q, q * avg
                    st.add_fill("BTC", q, q * avg)
                else:
                    s["btc"] = "waiting"
                st.save()
            st.d["armed"] = False
            st.save()
            self.tg.send(f"BTC back at {btc:,.0f} (>= {self.disarm:,.0f}): unfilled BTC limits cancelled, USDT freed. "
                         f"They go back in at {self.arm:,.0f}.")

    # ---- alts for a filled level
    def buy_now(self, coin, usd, tag):
        """IOC buys up to `usd`; returns (qty, cost)."""
        inst = f"{coin}-USDT"
        got_q, got_c = 0.0, 0.0
        for attempt in range(3):
            left = usd - got_c
            lot, mn, tick, _ = self.ex.inst(inst)
            _, ask, _ = self.ex.ticker(inst)
            px = floor_to(ask * (1 + self.slip), tick) or ask
            sz = floor_to(left / px, lot)
            if sz < mn:
                break
            q, avg = self.ex.buy_ioc(inst, px, sz, f"{tag}{coin}{attempt}"[:32])
            got_q += q
            got_c += q * avg
            if left - q * avg < max(1.0, usd * 0.02):
                break
        return got_q, got_c

    def ok_to_buy(self, coin):
        lot, mn, tick, live = self.ex.inst(f"{coin}-USDT")
        if not live:
            return False, "not trading on OKX"
        _, _, chg = self.ex.ticker(f"{coin}-USDT")
        if chg > self.max_pump:
            return False, f"+{100 * chg:.0f}% in 24h"
        return True, ""

    def alt_step(self, lv, pct, btc=None):
        st = self.st
        s = st.lv(lv)
        if s["alts"] == "in progress":
            st.d["paused"] = True
            st.save()
            self.tg.send(f"Alt buying for level {lv:,} was interrupted earlier. Paused - check OKX orders, then set "
                         f"levels.{lv}.alts to done or waiting in {os.path.basename(st.path)} and send /resume.")
            return
        frac = 1.0
        if s["btc"] == "filled (partial)":
            target = pct * self.budget * BTC_W
            frac = min(1.0, s["btc_cost"] / target) if target else 0.0
        need = pct * self.budget * (1 - BTC_W) * frac
        if st.d["spent"] + need > self.budget * 1.001:
            s["alts"] = "skipped (budget)"
            st.save()
            self.tg.send(f"Level {lv:,} alts skipped: budget would be exceeded")
            return
        ceiling = lv * (1 + self.alt_tol)
        if btc is not None and btc > ceiling:
            self.once_alert(f"alt_far{lv}", f"PROBLEM: level {lv:,} alts were not bought in time (no USDT) and BTC is "
                                            f"now {btc:,.0f}, above {ceiling:,.0f}. They will NOT be bought at today's "
                                            f"prices. They will be bought only if BTC comes back to {ceiling:,.0f} or lower "
                                            f"(and USDT is there).")
            return
        if self.ex.usdt() < need * 0.995:
            self.once_alert(f"alt_bal{lv}", f"PROBLEM: BTC level {lv:,} filled but USDT balance is less than the "
                                            f"{need:,.2f} needed for the alts. Add USDT now - the alts are bought only "
                                            f"while BTC stays at or under {ceiling:,.0f}.")
            return
        s["alts"] = "in progress"
        st.save()
        report = [f"Level {lv:,}: buying alts for {need:,.0f} USDT ({'LIVE' if self.ex.live else 'PAPER'})"]
        backup_usd = 0.0
        for coin, w in ALTS:
            usd = pct * self.budget * w * frac
            ok, why = self.ok_to_buy(coin)
            if not ok:
                backup_usd += usd
                report.append(f"  {coin}: SKIPPED ({why}) -> {usd:,.2f} to backup")
                continue
            q, c = self.buy_now(coin, usd, f"A{lv}")
            st.add_fill(coin, q, c)
            st.save()
            miss = "" if c >= usd * 0.97 else f"  (only {c:,.2f} of {usd:,.2f} filled)"
            report.append(f"  {coin}: {fmt(round(q, 8))} @ {c / q if q else 0:.6g} = {c:,.2f}{miss}")
        if backup_usd > 0:
            eth_ok, _ = self.ok_to_buy("ETH")
            parts = [("BTC", backup_usd * (1.0 if not eth_ok else BACKUP[0][1]))]
            if eth_ok:
                parts.append(("ETH", backup_usd * BACKUP[1][1]))
            for coin, usd in parts:
                q, c = self.buy_now(coin, usd, f"X{lv}")
                st.add_fill(coin, q, c)
                st.save()
                report.append(f"  backup {coin}: {fmt(round(q, 8))} @ {c / q if q else 0:.6g} = {c:,.2f}")
            if not eth_ok:
                report.append("  (ETH also failed the condition - backup all to BTC)")
        s["alts"] = "done"
        st.save()
        self.tg.send("\n".join(report))

    def tick(self, btc):
        if self.st.d.get("paused"):
            return
        self.btc_step(btc)
        for lv, pct in LEVELS:
            s = self.st.lv(lv)
            if s["btc"].startswith("filled") and s["alts"] in ("waiting", "in progress"):
                self.alt_step(lv, pct, btc)
                if self.st.d.get("paused"):
                    return


def main():
    load_env()
    live = os.environ.get("LADDER_LIVE", "false").lower() == "true"
    g = lambda k, d: float(os.environ.get(k, d))
    test = "--test" in sys.argv
    if test:
        # rehearsal: PAPER only, levels just under today's price so the whole flow runs within hours/days
        live = False
        ex0 = OKX(False)
        p = ex0.ticker("BTC-USDT")[0]
        step = float(os.environ.get("LADDER_TEST_STEP", "0.004"))
        LEVELS[:] = [(round(p * (1 - step * (k + 1)), 1), pct) for k, (_, pct) in enumerate(LEVELS)]
        arm, disarm = p * 1.001, p * 1.03
        state_name = "ladder_state_test.json"
        if os.path.exists(os.path.join(HERE, state_name)):
            os.remove(os.path.join(HERE, state_name))
    else:
        arm, disarm = g("LADDER_ARM", 55000), g("LADDER_DISARM", 65000)
        state_name = "ladder_state.json" if live else "ladder_state_paper.json"
    redis_url = os.environ.get("LADDER_REDIS_URL") or os.environ.get("REDIS_URL", "")
    st = State(os.path.join(HERE, os.environ.get("LADDER_STATE", state_name) if not test else state_name),
               "" if test else redis_url)
    log(f"state saved in {'Redis ' + st.key if st.r is not None else st.path}")
    ex, tg = OKX(live), TG()
    acct = ""
    if live:
        acct = ex.check_account(os.environ.get("LADDER_SUBACCOUNT", "spoot"))
        log("account check OK: " + acct)
    bot = Bot(ex, st, tg, g("LADDER_BUDGET", 2590), arm, disarm,
              g("LADDER_MAX_PUMP", 0.10), g("LADDER_MAX_SLIPPAGE", 0.005))
    if "--status" in sys.argv:
        print(bot.summary(ex.ticker("BTC-USDT")[0]))
        return
    mode = "TEST REHEARSAL (paper, fake levels " + ", ".join(f"{lv:,.0f}" for lv, _ in LEVELS) + ")" if test \
        else ("LIVE" if live else "PAPER - no real orders")
    tg.send(f"Spot ladder v2 started ({mode})." + (f"\nOKX {os.environ.get('LADDER_SUBACCOUNT', 'spoot')}: {acct}" if acct else "") + "\n" +
            bot.summary(ex.ticker("BTC-USDT")[0]))
    last_daily = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    while True:
        try:
            for cmd in tg.commands():
                if cmd == "/stop":
                    st.d["paused"] = True; st.save(); tg.send("Paused. /resume to continue.")
                elif cmd == "/resume":
                    st.d["paused"] = False; st.save(); tg.send("Resumed.")
                elif cmd == "/status":
                    tg.send(bot.summary(ex.ticker("BTC-USDT")[0]))
            btc = ex.ticker("BTC-USDT")[0]
            bot.tick(btc)
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if datetime.now(timezone.utc).hour >= 6 and last_daily != day:      # ~08:00 Cairo
                last_daily = day
                tg.send(bot.summary(btc))
        except Exception as e:
            log(f"error: {e}")
            bot.once_alert("last_err", f"Spot ladder error: {str(e)[:200]}")
        time.sleep(60)


# --------------------------------------------------------------------------- self test
def selftest():
    import tempfile
    LOG_FILE[0] = None

    class Fake(OKX):
        def __init__(self):
            super().__init__(False)
            self.px = {"BTC": 62000.0, "ETH": 2000, "ADA": 0.4, "LINK": 12, "NEAR": 2.5, "PEPE": 0.000008}
            self.chg = {}
            self.dead = set()
            self.iocs = []

        def ticker(self, inst):
            c = inst[:-5]
            p = self.px[c]
            return p, p * 1.0005, self.chg.get(c, 0.0)

        def inst(self, inst):
            c = inst[:-5]
            base = {"BTC": (1e-8, 1e-5, 0.1), "PEPE": (1, 100000, 1e-9)}.get(c, (1e-6, 1e-4, 1e-4))
            return base + (c not in self.dead,)

        def buy_ioc(self, inst, px, sz, cl):
            self.iocs.append(cl)
            return sz, px

        bal = float("inf")

        def usdt(self):
            return self.bal

    d = tempfile.mkdtemp()
    tg = TG(); tg.tok = ""
    ex = Fake()
    st = State(os.path.join(d, "s.json"))
    bot = Bot(ex, st, tg)

    def go(p):
        ex.px["BTC"] = p
        bot.tick(p)

    go(62000); assert not st.d["armed"]
    go(55000); assert st.d["armed"] and all(st.lv(lv)["btc"] == "placed" for lv, _ in LEVELS)
    ex.chg["ADA"] = 0.15                         # ADA pumping -> backup
    go(52900)                                    # wick through 53k
    assert st.lv(53000)["btc"] == "filled" and st.lv(53000)["alts"] == "done"
    assert st.lv(52000)["btc"] == "placed"
    assert "ADA" not in st.d["fills"], "ADA must be skipped"
    eth_after_53 = st.d["fills"]["ETH"]["cost"]
    assert abs(eth_after_53 - (259 * 0.20 + 259 * 0.20 * 0.40)) < 1.5, eth_after_53
    go(66000)                                    # bounce above 65k -> cancel
    assert not st.d["armed"] and st.lv(52000)["btc"] == "waiting"
    ex.chg["ADA"] = 0.0
    ex.chg["ETH"] = 0.12; ex.dead.add("NEAR")    # ETH pumping + NEAR halted -> all backup to BTC
    go(54000); assert st.d["armed"]
    go(51900)
    assert st.lv(52000)["alts"] == "done" and st.lv(51000)["btc"] == "placed"
    ex.chg = {}; ex.dead = set()
    go(48000)
    assert all(st.lv(lv)["btc"] == "filled" and st.lv(lv)["alts"] == "done" for lv, _ in LEVELS), st.d["levels"]
    assert st.d["spent"] <= 2590 * 1.001 and st.d["spent"] > 2590 * 0.97, st.d["spent"]
    ids = ex.iocs
    assert len(ids) == len(set(ids)), "duplicate client ids"
    # restart safety: interrupted alt step pauses
    st.lv(53000)["alts"] = "in progress"
    bot.alt_step(53000, 0.10)
    assert st.d["paused"]
    # budget cap
    st2 = State(os.path.join(d, "s2.json")); st2.d["spent"] = 2500
    b2 = Bot(ex, st2, tg)
    s = st2.lv(49000); s["btc"] = "filled"
    b2.alt_step(49000, 0.30)
    assert st2.lv(49000)["alts"].startswith("skipped"), "budget cap"
    # ---- no money scenarios
    sent = []
    tg3 = TG(); tg3.tok = ""; tg3.send = lambda t: sent.append(t)
    ex3 = Fake(); ex3.bal = 0.0
    st3 = State(os.path.join(d, "s3.json")); b3 = Bot(ex3, st3, tg3)
    def go3(p):
        ex3.px["BTC"] = p; b3.tick(p)
    go3(52800)                                     # under 53k, no USDT
    assert not st3.d["armed"] and any("touched 53,000" in t for t in sent), sent
    go3(56500); ex3.bal = 5000.0; go3(56500)        # bounced, money added -> must NOT place/buy
    assert not st3.d["armed"] and "BTC" not in st3.d["fills"], "bought after bounce!"
    go3(54500)                                     # back under 55k -> limits at level prices only
    assert st3.d["armed"] and "BTC" not in st3.d["fills"], "54.5k must not fill a 53k limit"
    ex3.bal = 0.0; go3(52950)                      # 53k fills, but no USDT for alts
    assert st3.lv(53000)["btc"] == "filled" and st3.lv(53000)["alts"] == "waiting"
    assert any("USDT balance is less" in t for t in sent)
    go3(54500); ex3.bal = 5000.0; go3(54500)       # bounced above 53k+2%, money added -> no alt buy
    assert st3.lv(53000)["alts"] == "waiting" and "ADA" not in st3.d["fills"], "alts bought at higher price!"
    assert any("will NOT be bought at today" in t for t in sent)
    go3(53800)                                     # back within +2% -> alts bought
    assert st3.lv(53000)["alts"] == "done" and "ADA" in st3.d["fills"]
    print()
    print(bot.summary(48000))
    print("\nSELFTEST OK: limits placed at 55k, wick fill, alts bought once, ADA pump -> 60/40 BTC/ETH backup, "
          "ETH pump -> backup all BTC, halted coin skipped, cancel at 65k and re-place at 55k, budget cap, crash pause, NO-MONEY: missed level alerted, no buy after bounce, limits only at levels, alts wait for BTC <= level+2%")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
