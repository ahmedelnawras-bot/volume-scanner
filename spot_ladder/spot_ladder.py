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
  LADDER_TARGETS=80000,100000,115000   BTC prices that trigger a one-time "target reached" alert (no selling)
  LADDER_HEARTBEAT_URL         optional ping URL (e.g. healthchecks.io) hit every 5 min; it alerts you if pings stop
  LADDER_TELEGRAM_BOT_TOKEN / LADDER_TELEGRAM_CHAT_ID its own Telegram bot (falls back to TELEGRAM_BOT_TOKEN /
                               TELEGRAM_CHAT_ID); reports, buttons, summaries at 08:00 and 20:00 Cairo
  REDIS_URL (or LADDER_REDIS_URL)  on Railway: keep the state in Redis so restarts/redeploys lose nothing
  LADDER_BUDGET=2590  LADDER_ARM=55000  LADDER_DISARM=65000  LADDER_MAX_PUMP=0.10  LADDER_MAX_SLIPPAGE=0.005
  LADDER_STATE                 default ladder_state.json (live) / ladder_state_paper.json (paper)

Safety
  - each level's alts are bought once; state is saved around every order; client order ids are fixed, so after
    a crash the bot looks an order up instead of buying twice; an alt step interrupted mid-way pauses the bot
  - total spend can never exceed LADDER_BUDGET; short USDT -> the step waits and you get one alert
  - /stop on Telegram pauses everything new (resting BTC limits stay on OKX; cancel them there if you want)
"""
import base64, hashlib, hmac, html, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone

LEVELS = [(53000, 0.10), (52000, 0.15), (51000, 0.20), (50000, 0.25), (49000, 0.30)]
BTC_W = 0.30
ALTS = [("ETH", 0.20), ("ADA", 0.20), ("LINK", 0.10), ("NEAR", 0.10), ("PEPE", 0.10)]
BACKUP = [("BTC", 0.60), ("ETH", 0.40)]
BASE = "https://www.okx.com"
# Telegram buttons under the chat box (a button press arrives as its text)
RLM = "\u200f"


def iso(x):
    """Keep a Latin word / number left-to-right inside an Arabic line."""
    return "\u200e" + str(x) + "\u200e"      # LRM ... LRM: works in every Telegram client


# ------------------------------------------------------------------ Telegram texts (Nour's wording, 2026-11-25)
# {name} = a value filled in by the bot. Latin words (BTC, USDT, OKX, ETH) and percentages are kept
# left-to-right automatically inside the Arabic lines.
TXT = {
    "btn_status": "📊 عرض الملخص",
    "btn_stop": "⏸ إيقاف الشراء",
    "btn_resume": "▶️ استئناف الشراء",
    "t_summary": "📊 ملخص خطة الشراء المتدرّج",
    "t_morning": "☀️ ملخص الصباح",
    "t_evening": "🌙 ملخص المساء",
    "t_after_fill": "📊 ملخص الخطة بعد الشراء عند مستوى {level}",
    "t_start": "🚀 بدأ بوت الشراء المتدرّج العمل",
    "t_account_ok": "🔐 تم التحقق من الحساب الفرعي {account}. مفتاح الربط لا يتيح السحب.",
    "mode_live": "🟢 الوضع الفعلي — ينفّذ البوت أوامر شراء حقيقية على OKX",
    "mode_paper": "🧪 الوضع التجريبي — يحاكي البوت الشراء دون تنفيذ صفقات حقيقية",
    "btc_now": "₿ سعر BTC الآن: {price}",
    "orders_open": "📌 أوامر شراء BTC مفتوحة وتنتظر وصول السعر إلى مستويات الشراء",
    "cancel_above": "↩️ يلغي البوت الأوامر المفتوحة إذا ارتفع BTC فوق {disarm} (الفارق عن السعر الحالي: {pct})",
    "arm_at": "🎯 يضع البوت أوامر شراء BTC عند وصول السعر إلى {arm} (الفارق عن السعر الحالي: {pct})",
    "next_level": "📍 أقرب مستوى شراء: {level} (الفارق عن السعر الحالي: {pct})",
    "budget": "💰 إجمالي ميزانية الخطة: {amount}",
    "spent": "إجمالي المشتريات حتى الآن: {amount} ({pct} من الميزانية)",
    "left": "المتبقي من الميزانية: {amount}",
    "levels_head": "🪜 مستويات الشراء",
    "level_line": "{icon} المستوى {level} | المبلغ {amount} | {state}",
    "st_waiting": "السعر لم يصل إلى المستوى بعد",
    "st_placed": "أمر شراء BTC مفتوح",
    "st_placing": "البوت يرسل أمر الشراء الآن",
    "st_done": "اكتمل شراء BTC والعملات البديلة",
    "st_alts_running": "اكتمل شراء BTC، والبوت يشتري العملات البديلة الآن",
    "st_alts_pending": "اكتمل شراء BTC، وشراء العملات البديلة معلّق",
    "st_partial": "نُفّذ جزء من أمر شراء BTC",
    "pf_head": "🧾 المحفظة الحالية",
    "pf_line": "• {coin} | تكلفة الشراء: {cost} | متوسط سعر الشراء: {avg} | السعر الآن: {price} | الربح/الخسارة: {pct}",
    "pf_total": "📈 إجمالي المحفظة | تكلفة الشراء: {cost} | القيمة الآن: {value} | الربح/الخسارة: {pnl} ({pct})",
    "pf_empty": "🧾 المحفظة فارغة. لم ينفّذ البوت أي عملية شراء حتى الآن.",
    "bal_total": "🏦 إجمالي رصيد الحساب الفرعي: {amount}",
    "bal_usdt": "• USDT: {qty} | المتاح: {free} | المحجوز للأوامر المفتوحة: {frozen}",
    "bal_coin": "• {coin}: {qty} | القيمة التقريبية: {amount}",
    "bal_empty": "• لا يوجد رصيد في الحساب الفرعي",
    "bal_ok": "✔️ رصيد USDT يكفي لإكمال الخطة. المبلغ المطلوب: {amount}",
    "bal_short": "⚠️ رصيد USDT أقل من المطلوب لإكمال الخطة. الرصيد: {have} | المطلوب: {need} | المبلغ الناقص: {gap} USDT",
    "bal_paper": "🏦 رصيد الحساب غير متاح في الوضع التجريبي لأن مفتاح OKX غير مربوط",
    "bal_paper_need": "المطلوب لإكمال الخطة: {amount} USDT",
    "bal_error": "🏦 تعذّر جلب رصيد الحساب من OKX الآن",
    "rules": "⚙️ بعد تنفيذ أمر شراء BTC عند أي مستوى، يشتري البوت العملات البديلة المخصصة لهذا المستوى. "
             "إذا ارتفع سعر إحدى العملات بأكثر من {pct} خلال آخر ٢٤ ساعة، يستبعدها البوت ويوزّع المبلغ المخصص لها: "
             "60% لشراء BTC و40% لشراء ETH. البوت ينفّذ عمليات شراء فقط.",
    "paused": "⏸ الشراء متوقف مؤقتًا. للمتابعة، اضغط «▶️ استئناف الشراء».",
    "updated": "🕒 آخر تحديث: {time} بتوقيت القاهرة",
    "placed_head": "📌 وضع البوت أوامر شراء BTC على OKX",
    "placed_reason": "وصل سعر BTC إلى {price}، وهو أقل من سعر التفعيل {arm}.",
    "placed_line": "• المستوى {level} | الكمية: {qty} BTC | قيمة الأمر: {amount}",
    "placed_cancel": "يلغي البوت الأوامر التي لم تُنفّذ تلقائيًا إذا ارتفع BTC فوق {disarm}.",
    "filled_head": "✅ تم تنفيذ أمر الشراء عند مستوى {level}",
    "filled_body": "اشترى البوت {qty} BTC بسعر {price}، بإجمالي {amount}.",
    "filled_next": "يشتري البوت الآن العملات البديلة المخصصة لهذا المستوى…",
    "disarm_head": "↩️ ارتفع BTC فوق سعر الإلغاء {disarm}. السعر الآن: {price}",
    "disarm_body": "أُلغيت الأوامر التي لم تُنفّذ، وأصبح رصيد USDT المحجوز لها متاحًا من جديد.",
    "disarm_next": "يضع البوت أوامر الشراء من جديد عندما يعود السعر إلى {arm}.",
    "gone_head": "⚠️ أمر شراء BTC عند مستوى {level} لم يعد مفتوحًا على OKX",
    "gone_partial": "نُفّذ جزء من الأمر، وأُضيفت الكمية المنفّذة إلى المشتريات.",
    "gone_again": "عند التفعيل التالي، يضع البوت أمر الشراء من جديد.",
    "alts_head": "🛒 مشتريات العملات البديلة عند مستوى {level}",
    "alts_amount": "المبلغ المخصص للشراء: {amount}",
    "alt_bought": "✅ {coin} | الكمية المشتراة: {qty} | سعر الشراء: {price} | الإجمالي: {amount}",
    "alt_partial": "⚠️ شراء جزئي — قيمة الشراء {amount} من أصل {wanted}",
    "alt_skipped": "⏭ استبعد البوت {coin}. السبب: {reason}. خُصّص مبلغها ({amount}) للشراء البديل.",
    "why_pump": "ارتفاع سعرها بنسبة {pct} خلال آخر ٢٤ ساعة",
    "why_halted": "توقّف تداولها على OKX",
    "backup_bought": "🔁 شراء بديل | {coin} | الكمية: {qty} | سعر الشراء: {price} | الإجمالي: {amount}",
    "backup_all_btc": "ℹ️ لم يستوفِ ETH شرط الشراء أيضًا، فخصّص البوت المبلغ البديل بالكامل لشراء BTC.",
    "nofund_head": "🚨 رصيد USDT لا يكفي لوضع أوامر شراء BTC",
    "nofund_body": "وصل BTC إلى {price}، وهو أقل من سعر التفعيل {arm}. وضع أوامر الشراء يحتاج إلى {amount} USDT، والرصيد أقل من ذلك.",
    "nofund_next": "لم يضع البوت أي أمر. حوّل USDT إلى الحساب الفرعي ليضع البوت أوامر الشراء عند أسعار المستويات المحددة فقط.",
    "missed_head": "🚨 وصل BTC إلى مستوى {level} دون تنفيذ الشراء",
    "missed_body": "وصل السعر إلى هذا المستوى قبل توفّر رصيد USDT الكافي، فلم يكن هناك أمر شراء مفتوح.",
    "missed_next": "بعد إضافة الرصيد، ينتظر البوت عودة BTC إلى {level} للشراء. لن يشتري بسعر أعلى.",
    "funded": "✔️ رصيد USDT كافٍ الآن. يضع البوت أوامر شراء BTC عند أسعار المستويات المحددة فقط.",
    "altfund_head": "🚨 رصيد USDT لا يكفي لشراء العملات البديلة",
    "altfund_body": "تم شراء BTC عند مستوى {level}. شراء العملات البديلة يحتاج إلى {amount} USDT، والرصيد المتاح أقل من ذلك.",
    "altfund_next": "حوّل USDT إلى الحساب الفرعي الآن. يمكن للبوت شراء العملات البديلة ما دام سعر BTC عند {ceiling} أو أقل.",
    "altfar_head": "🚨 لم ينفّذ البوت شراء العملات البديلة لمستوى {level}",
    "altfar_body": "لم يتوفر رصيد USDT وقت الشراء. ارتفع BTC الآن إلى {price} وتجاوز الحد المحدد للشراء ({ceiling}).",
    "altfar_next": "ينتظر البوت عودة BTC إلى {ceiling} أو أقل. عندها يشتري العملات البديلة إذا كان الرصيد كافيًا.",
    "broken_head": "🚨 توقّف شراء العملات البديلة لمستوى {level} قبل اكتماله",
    "broken_body": "أوقف البوت الشراء مؤقتًا لتجنّب تكرار الشراء. راجع الأوامر على OKX وصحّح حالة التنفيذ في البوت، ثم اضغط «▶️ استئناف الشراء».",
    "budget_btc": "⚠️ لم يضع البوت أوامر شراء BTC لأن قيمتها تتجاوز الميزانية المحددة.",
    "budget_alts": "⚠️ لم يشترِ البوت العملات البديلة لمستوى {level} لأن تكلفة الشراء تتجاوز الميزانية المحددة.",
    "error": "⚠️ حدث خطأ في البوت: {error}",
    "stopped": "⏸ تم إيقاف الشراء مؤقتًا. لن يرسل البوت أي أوامر شراء جديدة حتى تضغط «▶️ استئناف الشراء».",
    "stopped_note": "📌 أوامر شراء BTC المفتوحة على OKX تظل نشطة، وقد تُنفّذ أثناء الإيقاف المؤقت.",
    "resumed": "▶️ تم استئناف الشراء.",
    # --- added 2026-11-25: bot down, approaching the trigger, API key, targets
    "down_head": "⚠️ كان البوت متوقفًا عن العمل",
    "down_body": "توقّف البوت من {since} حتى {until} (مدة التوقف: {minutes} دقيقة).",
    "down_next": "عاد البوت للعمل الآن. راجع الملخص للتأكد من حالة الأوامر.",
    "near_head": "⚡ اقترب سعر BTC من سعر التفعيل",
    "near_body": "سعر BTC الآن {price}، ويبعد {pct} عن سعر التفعيل {arm}.",
    "near_ok": "✔️ رصيد USDT جاهز: {have} من أصل {need} مطلوبة لإكمال الخطة.",
    "near_short": "⚠️ رصيد USDT غير كافٍ: {have} من أصل {need} مطلوبة. حوّل المبلغ الناقص ({gap} USDT) قبل وصول السعر.",
    "near_paper": "الوضع التجريبي: لا يوجد رصيد حقيقي للتحقق منه.",
    "key_head": "🔑 مفتاح OKX لا يعمل",
    "key_body": "رفضت OKX مفتاح الربط ({reason}). قد يكون انتهى بعد ١٤ يومًا من عدم النشاط، أو حُذف، أو تغيّرت بياناته.",
    "key_next": "أنشئ مفتاحًا جديدًا للحساب الفرعي بصلاحيتي القراءة والتداول فقط، وضعه في Railway. لن يضع البوت أي أوامر حتى يعود المفتاح للعمل.",
    "key_back": "✔️ عاد مفتاح OKX للعمل، واستأنف البوت المراقبة.",
    "refused_head": "⛔ رفض البوت التشغيل الفعلي حمايةً للحساب",
    "refused_body": "السبب: {reason}",
    "target_head": "🎯 وصل BTC إلى {target}",
    "target_body": "هذا أحد أهداف الخطة. البوت لا يبيع تلقائيًا، والقرار لك.",
}
BTN_STATUS, BTN_STOP, BTN_RESUME = TXT["btn_status"], TXT["btn_stop"], TXT["btn_resume"]
KEYBOARD = {"keyboard": [[{"text": BTN_STATUS}], [{"text": BTN_STOP}, {"text": BTN_RESUME}]],
            "resize_keyboard": True, "is_persistent": True}
BUTTONS = {BTN_STATUS: "/status", BTN_STOP: "/stop", BTN_RESUME: "/resume",
           "📊 الملخص": "/status"}               # old button label, in case Telegram still shows it
RAW_KEYS = {"icon", "state", "reason"}          # values that are Arabic text, not numbers/Latin
_LATIN = re.compile(r"(?<![{A-Za-z0-9_])(BTC|USDT|OKX|ETH|\d+%)(?![A-Za-z0-9_}])")


def _ltr(text):
    return _LATIN.sub(lambda m: iso(m.group(1)), text)


def T(key, **kw):
    """One Telegram line from TXT (HTML-escaped), right-to-left, Latin words and numbers kept left-to-right."""
    tpl = _ltr(html.escape(TXT[key], quote=False))
    vals = {k: (v if k in RAW_KEYS else iso(html.escape(str(v), quote=False))) for k, v in kw.items()}
    return RLM + tpl.format(**vals)


# ---- message layout (Telegram HTML): bold title, quoted body, items "first part" bold + " · " between parts
def B(x):
    return f"<b>{x}</b>"


def I(x):
    return f"<i>{x}</i>"


def quote(lines, expandable=False):
    """Plain lines (old-bot style: one fact per line). Only the long rules text is a collapsible quote."""
    lines = [l for l in lines if l is not None]
    if not lines:
        return ""
    if expandable:
        return "<blockquote expandable>" + "\n".join(lines) + "</blockquote>"
    return "\n".join(lines)


def item(line, split=False):
    """'• BTC | a | b' -> '• <b>BTC</b> · a · b' (split=True puts the details on a second line)."""
    parts = line.split(" | ")
    if len(parts) == 1:
        return line
    head = parts[0]
    lead = head[:len(head) - len(head.lstrip(RLM + "•✅📌⏳🟡⛔🔁 "))]
    head_txt = head[len(lead):]
    first = lead + B(head_txt) if head_txt else lead
    rest = " | ".join(parts[1:])
    return first + ("\n" + RLM + "      " + rest if split else " | " + rest)


def coin_block(line, pnl):
    """Portfolio: '• BTC | cost | avg | now | pnl' -> 3 short lines with a green/red dot."""
    parts = line.split(" | ")
    if len(parts) < 5:
        return item(line, split=True)
    dot = "🟢" if pnl >= 0 else "🔴"
    name = parts[0].replace("•", "").strip(RLM + " ")
    pad = "\n" + RLM + "      "
    return f"{RLM}{dot} {B(name)} | {parts[4]}{pad}{parts[1]} | {parts[2]}{pad}{parts[3]}"


def card(head, lines=(), expandable=False):
    """Alert layout: bold first line, the rest in a quote block."""
    lines = [l for l in lines if l]
    return B(head) + ("\n\n" + "\n".join(lines) if lines else "")


def bar(frac, n=10):
    k = max(0, min(n, round(frac * n)))
    return "▰" * k + "▱" * (n - k)


def money(x, d=0):
    return f"{x:,.{d}f}$"


def pct(x, sign=True):
    return f"{x:+.1f}%" if sign else f"{x:.0f}%"
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


def cairo_dt(ts=None):
    utc = datetime.fromtimestamp(ts, timezone.utc) if ts else datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return utc.astimezone(ZoneInfo("Africa/Cairo"))
    except Exception:
        from datetime import timedelta
        return utc + timedelta(hours=2)


def cairo_now():                 # uses iso(), defined below; only called at run time
    t = cairo_dt()
    return iso(t.strftime("%Y-%m-%d %H:%M")) + " (بتوقيت القاهرة)"


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
class KeyProblem(RuntimeError):
    """OKX refused the API key (expired, deleted, wrong secret/passphrase, IP...)."""


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

    AUTH_CODES = {"50100", "50101", "50102", "50103", "50104", "50105", "50106", "50107", "50110", "50111",
                  "50112", "50113", "50114", "50119", "50120", "50121", "50122"}

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
                if str(out.get("code")) in self.AUTH_CODES:
                    raise KeyProblem(f"{out.get('code')} {out.get('msg')}")
                if str(out.get("code")) != "50011":
                    raise RuntimeError(last)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403) and signed:
                    try:
                        j = json.loads(e.read().decode())
                        raise KeyProblem(f"{j.get('code')} {j.get('msg')}")
                    except (ValueError, AttributeError):
                        raise KeyProblem(f"HTTP {e.code}")
                last = f"network: {e}"
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

    def balances(self):
        """Live only: {'total_usd': x, 'coins': [(ccy, qty, usd_value, avail, frozen)]}; None in paper mode."""
        if not self.live:
            return None
        d = self._req("GET", "/api/v5/account/balance", signed=True)
        if not d:
            return {"total_usd": 0.0, "coins": []}
        f = lambda v: float(v) if v not in (None, "") else 0.0
        coins = []
        for x in d[0].get("details", []):
            qty = f(x.get("eq") or x.get("cashBal"))
            if qty <= 0:
                continue
            coins.append((x.get("ccy", "?"), qty, f(x.get("eqUsd")), f(x.get("availBal")), f(x.get("frozenBal"))))
        coins.sort(key=lambda c: -c[2])
        return {"total_usd": f(d[0].get("totalEq")), "coins": coins}

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
        log("TG: " + re.sub(r"</?[a-z][^>]*>", "", text).replace("\n", " | "))
        if not (self.tok and self.chat):
            return
        try:
            data = {"chat_id": self.chat, "text": text, "parse_mode": "HTML",
                    "link_preview_options": json.dumps({"is_disabled": True}),
                    "reply_markup": json.dumps(KEYBOARD)}
            url = f"https://api.telegram.org/bot{self.tok}/sendMessage"
            try:
                urllib.request.urlopen(url, urllib.parse.urlencode(data).encode(), timeout=15).read()
            except urllib.error.HTTPError as e:
                if e.code != 400:
                    raise
                data.pop("parse_mode")                       # formatting rejected -> send plain text
                data["text"] = html.unescape(re.sub(r"</?[a-z][^>]*>", "", text))
                urllib.request.urlopen(url, urllib.parse.urlencode(data).encode(), timeout=15).read()
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
                t = (m.get("text") or "").strip()
                out.append(BUTTONS.get(t, t.split("@")[0].lower()))
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
    def summary(self, btc=None, title=None):
        st, live = self.st, self.ex.live
        spent = st.d["spent"]
        out = [B(RLM + (title or TXT["t_summary"])), I(T("mode_live" if live else "mode_paper"))]
        if btc:
            q = [T("orders_open"), T("cancel_above", disarm=money(self.disarm), pct=pct(100 * (self.disarm / btc - 1)))] \
                if st.d["armed"] else [T("arm_at", arm=money(self.arm), pct=pct(100 * (self.arm / btc - 1)))]
            nxt = [lv for lv, _ in LEVELS if not st.lv(lv)["btc"].startswith("filled")]
            if nxt:
                q.append(T("next_level", level=money(nxt[0]), pct=pct(100 * (nxt[0] / btc - 1))))
            out += ["", B(T("btc_now", price=money(btc))), quote(q)]
        frac = spent / self.budget if self.budget else 0
        out += ["", B(T("budget", amount=money(self.budget))),
                quote([T("spent", amount=money(spent), pct=pct(100 * frac, False)),
                       T("left", amount=money(self.budget - spent)),
                       RLM + bar(frac) + " " + iso(pct(100 * frac, False))])]
        icons = {"waiting": "⏳", "placed": "📌", "placing": "📌", "filled": "✅", "filled (partial)": "🟡"}
        rows = []
        for lv, p in LEVELS:
            s_ = st.lv(lv)
            b, a = s_["btc"], s_["alts"]
            if b == "filled":
                state = TXT[{"done": "st_done", "in progress": "st_alts_running"}.get(a, "st_alts_pending")]
            elif b == "filled (partial)":
                state = TXT["st_partial"]
            elif b in ("waiting", "placed", "placing"):
                state = TXT["st_" + b]
            else:
                state = b
            rows.append(item(T("level_line", icon=icons.get(b, "⛔"), level=money(lv), amount=money(p * self.budget),
                                state=_ltr(html.escape(state, quote=False)))))
        out += ["", B(T("levels_head")), quote(rows)]
        held = [(c, f) for c, f in st.d["fills"].items() if f["qty"] > 0]
        out.append("")
        if held:
            rows = []
            tot_cost = tot_val = 0.0
            for coin, f in held:
                avg = f["cost"] / f["qty"]
                try:
                    px = self.ex.ticker(f"{coin}-USDT")[0]
                except Exception:
                    px = avg
                val = f["qty"] * px
                tot_cost += f["cost"]
                tot_val += val
                rows.append(coin_block(T("pf_line", coin=coin, cost=money(f["cost"]), avg=f"{avg:,.6g}",
                                         price=f"{px:,.6g}", pct=pct(100 * (val / f["cost"] - 1))), val - f["cost"]))
            rows.append("")
            rows.append(item(T("pf_total", cost=money(tot_cost), value=money(tot_val), pnl=f"{tot_val - tot_cost:+,.0f}$",
                               pct=pct(100 * (tot_val / tot_cost - 1))), split=True))
            out += [B(T("pf_head")), quote(rows)]
        else:
            out.append(B(T("pf_empty")))
        bal = self.balance_lines()
        out += ["", B(bal[0]), quote([item(x) for x in bal[1:]])]
        out += ["", quote([T("rules", pct=pct(100 * self.max_pump, False))], expandable=True)]
        if st.d.get("paused"):
            out.append(B(T("paused")))
        out.append(I(T("updated", time=cairo_dt().strftime("%Y-%m-%d %H:%M"))))
        return "\n".join(x for x in out if x is not None)

    def balance_lines(self):
        need = max(0.0, self.budget - self.st.d["spent"])
        try:
            b = self.ex.balances()
        except Exception:
            return [T("bal_error")]
        if b is None:
            return [T("bal_paper"), T("bal_paper_need", amount=f"{need:,.0f}")]
        L = [T("bal_total", amount=money(b["total_usd"], 2))]
        usdt_free = usdt_frozen = 0.0
        for ccy, qty, usd_v, avail, frozen in b["coins"]:
            if ccy == "USDT":
                usdt_free, usdt_frozen = avail, frozen
                L.append(T("bal_usdt", qty=f"{qty:,.2f}", free=f"{avail:,.2f}", frozen=f"{frozen:,.2f}"))
            else:
                L.append(T("bal_coin", coin=ccy, qty=fmt(round(qty, 8)), amount=money(usd_v, 2)))
        if not b["coins"]:
            L.append(T("bal_empty"))
        have = usdt_free + usdt_frozen
        if have + 0.5 < need:
            L.append(T("bal_short", have=f"{have:,.0f}", need=f"{need:,.0f}", gap=f"{need - have:,.0f}"))
        else:
            L.append(T("bal_ok", amount=f"{need:,.0f}"))
        return L

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
                self.once_alert("btc_budget", T("budget_btc"))
                return
            if ex.usdt() < need * 0.995:
                st.d["unfunded"] = True
                self.once_alert("btc_bal", card(T("nofund_head"), [
                    T("nofund_body", price=money(btc), arm=money(self.arm), amount=f"{need:,.2f}"),
                    T("nofund_next")]))
                for lv, pct in open_lv:
                    if btc <= lv:
                        self.once_alert(f"miss{lv}", card(T("missed_head", level=money(lv)), [
                            T("missed_body"), T("missed_next", level=money(lv))]))
                return
            if st.d.pop("unfunded", False):
                self.tg.send(B(T("funded")))
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
                placed.append(T("placed_line", level=money(lv), qty=fmt(sz), amount=money(sz * lv, 2)))
            st.d["armed"] = True
            st.save()
            msg = [T("placed_head") + ("" if ex.live else " (وضع تجريبي، دون أوامر حقيقية)")]
            msg += [T("placed_reason", price=money(btc), arm=money(self.arm))] + placed
            msg += [T("placed_cancel", disarm=money(self.disarm))]
            self.tg.send(card(msg[0], [msg[1]] + [item(x) for x in msg[2:-1]] + [I(msg[-1])]))
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
                self.tg.send(card(T("filled_head", level=money(lv)), [
                    T("filled_body", qty=fmt(round(q, 8)), price=money(avg, 1), amount=money(q * avg, 2)),
                    I(T("filled_next"))]))
            elif state in ("canceled", "missing", "mmp_canceled"):
                s["btc"] = "waiting"
                if q > 0:
                    s["btc"], s["btc_qty"], s["btc_cost"] = "filled (partial)", q, q * avg
                    st.add_fill("BTC", q, q * avg)
                st.save()
                self.tg.send(card(T("gone_head", level=money(lv)), [T("gone_partial" if q > 0 else "gone_again")]))
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
            self.tg.send(card(T("disarm_head", disarm=money(self.disarm), price=money(btc)),
                              [T("disarm_body"), T("disarm_next", arm=money(self.arm))]))

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
            return False, _LATIN.sub(lambda m: iso(m.group(1)), TXT["why_halted"])
        _, _, chg = self.ex.ticker(f"{coin}-USDT")
        if chg > self.max_pump:
            return False, TXT["why_pump"].format(pct=iso(f"{100 * chg:+.0f}%"))
        return True, ""

    def alt_step(self, lv, pct, btc=None):
        st = self.st
        s = st.lv(lv)
        if s["alts"] == "in progress":
            st.d["paused"] = True
            st.save()
            self.tg.send(card(T("broken_head", level=money(lv)), [T("broken_body")]))
            return
        frac = 1.0
        if s["btc"] == "filled (partial)":
            target = pct * self.budget * BTC_W
            frac = min(1.0, s["btc_cost"] / target) if target else 0.0
        need = pct * self.budget * (1 - BTC_W) * frac
        if st.d["spent"] + need > self.budget * 1.001:
            s["alts"] = "skipped (budget)"
            st.save()
            self.tg.send(T("budget_alts", level=money(lv)))
            return
        ceiling = lv * (1 + self.alt_tol)
        if btc is not None and btc > ceiling:
            self.once_alert(f"alt_far{lv}", card(T("altfar_head", level=money(lv)), [
                T("altfar_body", price=money(btc), ceiling=money(ceiling)),
                T("altfar_next", ceiling=money(ceiling))]))
            return
        if self.ex.usdt() < need * 0.995:
            self.once_alert(f"alt_bal{lv}", card(T("altfund_head"), [T("altfund_body", level=money(lv), amount=f"{need:,.2f}"),
                T("altfund_next", ceiling=money(ceiling))]))
            return
        s["alts"] = "in progress"
        st.save()
        report = [T("alts_head", level=money(lv)) + ("" if self.ex.live else " (تجريبي)"),
                  T("alts_amount", amount=money(need, 2))]
        backup_usd = 0.0
        for coin, w in ALTS:
            usd = pct * self.budget * w * frac
            ok, why = self.ok_to_buy(coin)
            if not ok:
                backup_usd += usd
                report.append(T("alt_skipped", coin=coin, reason=why, amount=money(usd, 2)))
                continue
            q, c = self.buy_now(coin, usd, f"A{lv}")
            st.add_fill(coin, q, c)
            st.save()
            report.append(T("alt_bought", coin=coin, qty=fmt(round(q, 8)), price=f"{c / q if q else 0:,.6g}",
                            amount=money(c, 2)))
            if c < usd * 0.97:
                report.append(T("alt_partial", amount=money(c, 2), wanted=money(usd, 2)))
        if backup_usd > 0:
            eth_ok, _ = self.ok_to_buy("ETH")
            parts = [("BTC", backup_usd * (1.0 if not eth_ok else BACKUP[0][1]))]
            if eth_ok:
                parts.append(("ETH", backup_usd * BACKUP[1][1]))
            for coin, usd in parts:
                q, c = self.buy_now(coin, usd, f"X{lv}")
                st.add_fill(coin, q, c)
                st.save()
                report.append(T("backup_bought", coin=coin, qty=fmt(round(q, 8)),
                                price=f"{c / q if q else 0:,.6g}", amount=money(c, 2)))
            if not eth_ok:
                report.append(T("backup_all_btc"))
        s["alts"] = "done"
        st.save()
        self.tg.send(card(report[0], [report[1]] + [item(x, split=True) for x in report[2:]]))
        try:
            self.tg.send(self.summary(btc, T("t_after_fill", level=money(lv))[1:]))
        except Exception as e:
            log(f"summary after fill failed: {e}")

    def near_check(self, btc):
        """One alert when BTC comes within 3% of the trigger; re-armed after it goes 5% above."""
        st = self.st
        if st.d["armed"] or btc > self.arm * 1.05:
            if st.d.get("near_sent"):
                st.d["near_sent"] = False
                st.save()
            return
        if btc <= self.arm * 1.03 and not st.d.get("near_sent"):
            st.d["near_sent"] = True
            st.save()
            need = max(0.0, self.budget - st.d["spent"])
            lines = [T("near_body", price=money(btc), pct=pct(100 * (self.arm / btc - 1)), arm=money(self.arm))]
            if not self.ex.live:
                lines.append(T("near_paper"))
            else:
                try:
                    have = self.ex.usdt()
                    lines.append(T("near_ok", have=f"{have:,.0f}", need=f"{need:,.0f}") if have + 0.5 >= need else
                                 T("near_short", have=f"{have:,.0f}", need=f"{need:,.0f}", gap=f"{need - have:,.0f}"))
                except Exception:
                    lines.append(T("bal_error"))
            self.tg.send(card(T("near_head"), lines))

    def target_check(self, btc, targets):
        st = self.st
        hit = st.d.setdefault("targets_hit", [])
        held = any(f["qty"] > 0 for f in st.d["fills"].values())
        for tgt in targets:
            if btc >= tgt and tgt not in hit and held:
                hit.append(tgt)
                st.save()
                self.tg.send(card(T("target_head", target=money(tgt)), [T("target_body")]))
                self.tg.send(self.summary(btc, T("target_head", target=money(tgt))[1:]))

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
    key_ok = True
    key_err = ""
    if live:
        try:
            acct = ex.check_account(os.environ.get("LADDER_SUBACCOUNT", "spoot"))
            log("account check OK: " + acct)
        except KeyProblem as e:
            key_ok, key_err = False, str(e)
            log(f"API key refused: {e}")
        except SystemExit as e:                      # main-account key or withdraw permission: refuse, but say why
            log(str(e))
            tg.send(card(T("refused_head"), [T("refused_body", reason=str(e)[:200])]))
            while True:
                time.sleep(3600)
    bot = Bot(ex, st, tg, g("LADDER_BUDGET", 2590), arm, disarm,
              g("LADDER_MAX_PUMP", 0.10), g("LADDER_MAX_SLIPPAGE", 0.005))
    if "--status" in sys.argv:
        print(bot.summary(ex.ticker("BTC-USDT")[0]))
        return
    mode = "TEST REHEARSAL (paper, fake levels " + ", ".join(f"{lv:,.0f}" for lv, _ in LEVELS) + ")" if test \
        else ("LIVE" if live else "PAPER - no real orders")
    head = TXT["t_start"] + (" (تجربة بمستويات افتراضية)" if test else "")
    if acct:
        log("OKX " + os.environ.get("LADDER_SUBACCOUNT", "spoot") + ": " + acct)
        head += "\n" + T("t_account_ok", account=os.environ.get("LADDER_SUBACCOUNT", "spoot"))
    tg.send(bot.summary(ex.ticker("BTC-USDT")[0], head))
    # was the bot down? (last heartbeat saved in the state)
    now_ts = time.time()
    last_alive = st.d.get("alive")
    if last_alive and now_ts - last_alive > 600:
        since = cairo_dt(last_alive).strftime("%Y-%m-%d %H:%M")
        until = cairo_dt().strftime("%Y-%m-%d %H:%M")
        tg.send(card(T("down_head"), [T("down_body", since=since, until=until,
                                         minutes=f"{(now_ts - last_alive) / 60:,.0f}"), T("down_next")]))
    st.d["alive"] = now_ts
    st.save()

    def key_alert(err):
        tg.send(card(T("key_head"), [T("key_body", reason=err[:120]), T("key_next")]))

    if not key_ok:
        key_alert(key_err)
    targets = [float(x) for x in os.environ.get("LADDER_TARGETS", "80000,100000,115000").split(",") if x.strip()]
    heartbeat_url = os.environ.get("LADDER_HEARTBEAT_URL", "").strip()
    last_key_check = last_beat = 0.0
    # scheduled summaries, Cairo time; what was already sent is kept in the state (survives restarts)
    SCHEDULE = [(8, "morning", TXT["t_morning"]), (20, "evening", TXT["t_evening"])]
    sent = st.d.setdefault("summaries_sent", {})
    t0 = cairo_dt()
    for hour, key, _ in SCHEDULE:            # don't fire a summary for a slot that already passed before start
        if t0.hour >= hour:
            sent.setdefault(key, t0.strftime("%Y-%m-%d"))
    def handle_commands():
        for cmd in tg.commands():
            if cmd == "/stop":
                st.d["paused"] = True; st.save()
                tg.send(card(T("stopped"), [T("stopped_note")]))
            elif cmd == "/resume":
                st.d["paused"] = False; st.save()
                tg.send(B(T("resumed")))
            elif cmd in ("/status", "/start"):
                tg.send(bot.summary(ex.ticker("BTC-USDT")[0]))

    while True:
        try:
            handle_commands()
            btc = ex.ticker("BTC-USDT")[0]
            nowt = time.time()
            st.d["alive"] = nowt
            st.save()
            if live and (not key_ok or nowt - last_key_check > 1800):    # key check every 30 min
                last_key_check = nowt
                try:
                    ex.usdt()
                    if not key_ok:
                        key_ok = True
                        tg.send(B(T("key_back")))
                except KeyProblem as e:
                    if key_ok:
                        key_ok = False
                        key_alert(str(e))
            if heartbeat_url and nowt - last_beat > 300:                 # external "bot is alive" ping
                last_beat = nowt
                try:
                    urllib.request.urlopen(heartbeat_url, timeout=10).read()
                except Exception as e:
                    log(f"heartbeat failed: {e}")
            if key_ok:
                bot.near_check(btc)
                bot.tick(btc)
                bot.target_check(btc, targets)

            t = cairo_dt()
            day = t.strftime("%Y-%m-%d")
            for hour, key, title in SCHEDULE:
                if t.hour >= hour and sent.get(key) != day:
                    sent[key] = day
                    st.save()
                    tg.send(bot.summary(btc, title))
        except KeyProblem as e:
            log(f"API key refused: {e}")
            if key_ok:
                key_ok = False
                key_alert(str(e))
        except Exception as e:
            log(f"error: {e}")
            bot.once_alert("last_err", T("error", error=str(e)[:200]))
        for _ in range(12):                 # price every 60 s, buttons answered within ~5 s
            time.sleep(5)
            try:
                handle_commands()
            except Exception as e:
                log(f"command error: {e}")


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
    assert not st3.d["armed"] and any("دون تنفيذ الشراء" in t and "53,000" in t for t in sent), sent
    go3(56500); ex3.bal = 5000.0; go3(56500)        # bounced, money added -> must NOT place/buy
    assert not st3.d["armed"] and "BTC" not in st3.d["fills"], "bought after bounce!"
    go3(54500)                                     # back under 55k -> limits at level prices only
    assert st3.d["armed"] and "BTC" not in st3.d["fills"], "54.5k must not fill a 53k limit"
    ex3.bal = 0.0; go3(52950)                      # 53k fills, but no USDT for alts
    assert st3.lv(53000)["btc"] == "filled" and st3.lv(53000)["alts"] == "waiting"
    assert any("لا يكفي لشراء العملات البديلة" in t for t in sent)
    go3(54500); ex3.bal = 5000.0; go3(54500)       # bounced above 53k+2%, money added -> no alt buy
    assert st3.lv(53000)["alts"] == "waiting" and "ADA" not in st3.d["fills"], "alts bought at higher price!"
    assert any("ينتظر البوت عودة" in t for t in sent)
    go3(53800)                                     # back within +2% -> alts bought
    assert st3.lv(53000)["alts"] == "done" and "ADA" in st3.d["fills"]
    print()
    print(bot.summary(48000))
    print("\nSELFTEST OK: limits placed at 55k, wick fill, alts bought once, ADA pump -> 60/40 BTC/ETH backup, "
          "ETH pump -> backup all BTC, halted coin skipped, cancel at 65k and re-place at 55k, budget cap, crash pause, NO-MONEY: missed level alerted, no buy after bounce, limits only at levels, alts wait for BTC <= level+2%")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
