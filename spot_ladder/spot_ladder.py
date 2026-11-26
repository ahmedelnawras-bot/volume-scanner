#!/usr/bin/env python3
"""Spot ladder bot v2 (buy only) - Nour's 2026-2028 spot plan.

    python spot_ladder.py            # runs forever (PAPER mode unless LADDER_LIVE=true)
    python spot_ladder.py --status   # print plan / fills and exit
    python spot_ladder.py --selftest # offline test with a fake exchange
    python spot_ladder.py --test     # REHEARSAL: paper only, real OKX prices, fake levels 0.4%/0.8%/... under
                                     # today's BTC price, so you see the whole flow on Telegram in hours

Plan (agreed 2026-11-24)
  plan size = spent + USDT in the sub-account, min 500 / max 4,000 (agreed 2026-11-25): under 500 no orders
             (alert); above 4,000 the rest stays USDT. Set when the limits go in; if more USDT arrives while limits
             are open, the unfilled limits are re-placed bigger at the SAME prices; all available cash is split over
             the levels not bought yet by their weights.
  BTC levels 53.4k 10% / 52.6k 15% / 51k 20% / 50.15k 25% / 49.2k 30% of the plan
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
  Plan B (button, agreed 2026-11-26): after a confirmation, cancels the open Plan A limits (verified; if a cancel
             cannot be confirmed the switch stops), fixes the current BTC price as the start, buys the first
             unfinished level's BTC share at once (IOC) + its alts, and places the other levels at
             start x (original level / first unfinished original level). Same weights, alt rules, plan size.
             Cancel above start x 65/55, re-place back at the start. Saved; pressing again changes nothing.
             "Back to Plan A" button (confirmation) restores the Plan A prices for the levels not bought.
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
  LADDER_BASE_BUDGET=2600 (shown on every message)  LADDER_MIN_BUDGET=500  LADDER_MAX_BUDGET=4000  LADDER_PAPER_BALANCE=2590 (paper only)  LADDER_ARM=55000  LADDER_DISARM=65000  LADDER_MAX_PUMP=0.10  LADDER_MAX_SLIPPAGE=0.005
  LADDER_STATE                 default ladder_state.json (live) / ladder_state_paper.json (paper)

Safety
  - each level's alts are bought once; state is saved around every order; client order ids are fixed, so after
    a crash the bot looks an order up instead of buying twice; an alt step interrupted mid-way pauses the bot
  - total spend can never exceed LADDER_BUDGET; short USDT -> the step waits and you get one alert
  - /stop on Telegram pauses everything new (resting BTC limits stay on OKX; cancel them there if you want)
"""
import base64, hashlib, hmac, html, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone

# levels sit just ABOVE the supports so a bounce off the support still fills them (agreed 2026-11-25):
# 53,400 over the Jul-2024 low 53,269 | 52,600 over the Sep-2024 low 52,529 | 51,000 unchanged |
# 50,150 over the round 50,000 | 49,200 over the Aug-2024 low 48,935 and the round 49,000
LEVELS = [(53400, 0.10), (52600, 0.15), (51000, 0.20), (50150, 0.25), (49200, 0.30)]
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
    "btn_status": "📊 الملخص",
    "btn_stop": "⏸ إيقاف",
    "btn_resume": "▶️ تشغيل",
    "btn_on_now": "🟢 مفعّل ✓",
    "btn_off_now": "⏸ متوقف ✓",
    "status_on": "🟢 الشراء مفعّل",
    "status_off": "⏸ الشراء متوقف مؤقتًا",
    "btn_plan_b": "🔄 التحويل إلى Plan B",
    "btn_plan_a": "↩️ الرجوع إلى Plan A",
    "label_a": " · Plan A",
    "label_b": " · Plan B (من {start})",
    "active_a": "🗂 الخطة النشطة: Plan A — مستويات ثابتة فوق الدعوم",
    "active_b": "🗂 الخطة النشطة: Plan B — سعر البداية {start} (منذ {time})",
    "b_confirm": "هل تريد التحويل إلى Plan B؟",
    "b_confirm_note": "سيثبّت البوت سعر BTC لحظة الضغط على «نعم» (الآن نحو {price}) كأول مستوى شراء، ويشتري حصة BTC لهذا المستوى فورًا ثم العملات البديلة. تُلغى أوامر Plan A المفتوحة أولًا.",
    "b_confirm_avg": "متوسط شراء BTC المتوقع للمستويات المتبقية: نحو {avg_b} (في Plan A: {avg_a}).",
    "b_confirm_paused": "الشراء متوقف الآن، وسيُشغَّل تلقائيًا مع التحويل.",
    "yes_b": "نعم، حوّل",
    "yes_a": "نعم، ارجع",
    "b_done_head": "🔄 تم التحويل إلى Plan B",
    "b_done_body": "سعر البداية: {start}. المستويات الجديدة:",
    "b_level_line": "• {px} | بدلًا من {orig} | المبلغ {amount}",
    "b_level_done": "• {orig} | تم الشراء ضمن Plan A",
    "b_instant": "⚡ اشترى البوت {qty} BTC فورًا بسعر {price}، بإجمالي {amount}. يشتري الآن العملات البديلة لهذا المستوى.",
    "b_instant_partial": "⚠️ نُفّذ جزء من الشراء الفوري ({amount} من {wanted}). اعتُبر المستوى منفّذًا جزئيًا.",
    "b_instant_none": "⚠️ لم يُنفَّذ الشراء الفوري. سيضع البوت أمر شراء محددًا عند {price}.",
    "b_nofund": "⚠️ رصيد USDT المتاح لا يكفي للشراء الفوري. سيضع البوت أمر الشراء عند {price} بعد توفّر الرصيد.",
    "b_failed_head": "🚨 تعذّر التحويل إلى Plan B",
    "b_failed_body": "لم يتمكن البوت من التأكد من إلغاء أمر شراء BTC عند {level}. لم يرسل البوت أي أمر جديد، وما زالت الخطة النشطة Plan A.",
    "b_failed_next": "راجع الأوامر المفتوحة على OKX، ثم اختر «🔄 التحويل إلى Plan B» من القائمة مرة أخرى.",
    "a_failed_head": "🚨 تعذّر الرجوع إلى Plan A",
    "a_failed_body": "لم يتمكن البوت من التأكد من إلغاء أمر شراء BTC عند {level}. ما زالت الخطة النشطة Plan B.",
    "a_failed_next": "راجع الأوامر المفتوحة على OKX، ثم اختر «↩️ الرجوع إلى Plan A» من القائمة مرة أخرى.",
    "b_min": "⚠️ لا يمكن التحويل: رصيد USDT أقل من الحد الأدنى للخطة ({min}).",
    "b_all_done": "ℹ️ اكتملت كل مستويات الشراء، ولا يوجد ما يُحوَّل.",
    "b_already": "أنت على Plan B بالفعل منذ {time}، بسعر بداية {start}. لن يُعاد حساب المستويات.",
    "b_resumed": "▶️ تم تشغيل الشراء مع التحويل.",
    "a_confirm": "هل تريد الرجوع إلى Plan A؟",
    "a_confirm_note": "تُلغى أوامر Plan B المفتوحة، وتعود المستويات التي لم تُنفّذ إلى أسعار Plan A، ويضع البوت أوامرها عند وصول BTC إلى {arm}. المشتريات السابقة تبقى محسوبة.",
    "a_done": "↩️ تمت العودة إلى Plan A. يضع البوت أوامر المستويات المتبقية عند وصول BTC إلى {arm}.",
    "a_already": "أنت على Plan A بالفعل.",
    "plan_cancelled": "لم يتغيّر شيء، وما زالت الخطة النشطة {pname}.",
    "switch_broken": "⚠️ انقطع تحويل الخطة قبل اكتماله. لم تتغيّر الخطة النشطة. اختر التحويل من القائمة مرة أخرى إذا أردت.",
    "base_line": "💼 الميزانية الأساسية {base} | حجم الخطة الآن {plan} | {diff}",
    "base_low": "💼 الميزانية الأساسية {base} | الرصيد أقل من الحد الأدنى للخطة ({min})",
    "diff_below": "أقل بـ {pct}",
    "diff_above": "أعلى بـ {pct}",
    "diff_above_max": "أعلى بـ {pct} (الحد الأقصى)",
    "diff_same": "مطابق للخطة ✅",
    "stop_confirm": "هل تريد إيقاف الشراء مؤقتًا؟",
    "stop_confirm_note": "لن يرسل البوت أوامر شراء جديدة حتى تختار «▶️ تشغيل الشراء» من القائمة.",
    "yes_stop": "نعم، أوقف",
    "cancel": "إلغاء",
    "stop_cancelled": "لم يتغيّر شيء، والشراء ما زال مفعّلًا.",
    "already_on": "الشراء مفعّل بالفعل.",
    "already_off": "الشراء متوقف بالفعل. للتشغيل اختر «▶️ تشغيل الشراء» من القائمة.",
    "t_summary": "📊 ملخص خطة الشراء المتدرّج",
    "t_morning": "☀️ ملخص الصباح",
    "t_evening": "🌙 ملخص المساء",
    "t_after_fill": "📊 ملخص الخطة بعد الشراء عند مستوى {level}",
    "t_start": "🚀 بدأ بوت الشراء المتدرّج العمل",
    "t_account_ok": "🔐 تم التحقق من الحساب الفرعي {account}. مفتاح الربط لا يتيح السحب.",
    "mode_live": "🟢 الوضع الفعلي — ينفّذ البوت أوامر شراء حقيقية على OKX",
    "mode_paper": "🧪 الوضع التجريبي — يحاكي البوت الشراء دون تنفيذ صفقات حقيقية",
    "btc_now": "🟠 سعر BTC الآن: {price}",
    "btc_24h": "{dot} التغيّر خلال آخر ٢٤ ساعة: {pct}",
    "orders_open": "📌 أوامر شراء BTC مفتوحة وتنتظر وصول السعر إلى مستويات الشراء",
    "cancel_above": "↩️ يلغي البوت الأوامر المفتوحة إذا ارتفع BTC فوق {disarm} (الفارق عن السعر الحالي: {pct})",
    "arm_at": "🎯 يضع البوت أوامر شراء BTC عند وصول السعر إلى {arm} (الفارق عن السعر الحالي: {pct})",
    "next_level": "📍 أقرب مستوى شراء: {level} (الفارق عن السعر الحالي: {pct})",
    "budget": "💰 حجم الخطة: {amount}",
    "budget_est": "💰 حجم الخطة المتوقع حسب الرصيد الحالي: {amount}",
    "budget_low": "💰 رصيد USDT الحالي أقل من الحد الأدنى للخطة ({min})",
    "budget_rule": "يُحسب حجم الخطة من رصيد USDT في الحساب الفرعي، بحد أدنى {min} وحد أقصى {max}.",
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
    "bal_ok": "✔️ رصيد USDT كافٍ. حجم الخطة: {amount}",
    "bal_short": "⚠️ رصيد USDT أقل من الحد الأدنى للخطة | الرصيد: {have} | الحد الأدنى: {need} | المبلغ الناقص: {gap} USDT",
    "bal_extra": "ℹ️ الرصيد يزيد عن الحد الأقصى للخطة، والمبلغ الزائد ({amount}) لن يُستخدم.",
    "bal_paper": "🏦 رصيد الحساب غير متاح في الوضع التجريبي لأن مفتاح OKX غير مربوط",
    "bal_paper_need": "المطلوب لإكمال الخطة: {amount} USDT",
    "bal_error": "🏦 تعذّر جلب رصيد الحساب من OKX الآن",
    "rules": "⚙️ بعد تنفيذ أمر شراء BTC عند أي مستوى، يشتري البوت العملات البديلة المخصصة لهذا المستوى. "
             "إذا ارتفع سعر إحدى العملات بأكثر من {pct} خلال آخر ٢٤ ساعة، يستبعدها البوت ويوزّع المبلغ المخصص لها: "
             "60% لشراء BTC و40% لشراء ETH. البوت ينفّذ عمليات شراء فقط.",
    "paused": "⏸ الشراء متوقف مؤقتًا. للمتابعة، اختر «▶️ تشغيل الشراء» من القائمة.",
    "updated": "🕒 آخر تحديث: {time} بتوقيت القاهرة",
    "placed_head": "📌 وضع البوت أوامر شراء BTC على OKX",
    "placed_reason": "وصل سعر BTC إلى {price}، وهو أقل من سعر التفعيل {arm}.",
    "placed_plan": "حجم الخطة: {amount}، محسوبًا من رصيد USDT في الحساب الفرعي.",
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
    "broken_body": "أوقف البوت الشراء مؤقتًا لتجنّب تكرار الشراء. راجع الأوامر على OKX وصحّح حالة التنفيذ في البوت، ثم اختر «▶️ تشغيل الشراء» من القائمة.",
    "budget_btc": "⚠️ لم يضع البوت أوامر شراء BTC لأن قيمتها تتجاوز الميزانية المحددة.",
    "budget_alts": "⚠️ لم يشترِ البوت العملات البديلة لمستوى {level} لأن تكلفة الشراء تتجاوز الميزانية المحددة.",
    "error": "⚠️ حدث خطأ في البوت: {error}",
    "stopped": "⏸ تم إيقاف الشراء مؤقتًا. لن يرسل البوت أي أوامر شراء جديدة حتى تختار «▶️ تشغيل الشراء» من القائمة.",
    "stopped_note": "📌 أوامر شراء BTC المفتوحة على OKX تظل نشطة، وقد تُنفّذ أثناء الإيقاف المؤقت.",
    "resumed": "▶️ تم استئناف الشراء.",
    # --- added 2026-11-25: bot down, approaching the trigger, API key, targets
    "down_head": "⚠️ كان البوت متوقفًا عن العمل",
    "down_body": "توقّف البوت من {since} حتى {until} (مدة التوقف: {minutes} دقيقة).",
    "down_next": "عاد البوت للعمل الآن. راجع الملخص للتأكد من حالة الأوامر.",
    "near_head": "⚡ اقترب سعر BTC من سعر التفعيل",
    "near_body": "سعر BTC الآن {price}، ويبعد {pct} عن سعر التفعيل {arm}.",
    "near_ok": "✔️ رصيد USDT جاهز: {have}. حجم الخطة سيكون {need}.",
    "near_short": "⚠️ رصيد USDT أقل من الحد الأدنى للخطة: {have} من أصل {need}. حوّل {gap} USDT على الأقل قبل وصول السعر.",
    "min_head": "🚨 رصيد USDT أقل من الحد الأدنى للخطة",
    "min_body": "وصل BTC إلى {price}، وهو أقل من سعر التفعيل {arm}. الرصيد المتاح للخطة {have} USDT، والحد الأدنى {min} USDT.",
    "min_next": "لم يضع البوت أي أمر. حوّل {gap} USDT على الأقل إلى الحساب الفرعي، وسيضع البوت الأوامر عند أسعار المستويات المحددة فقط.",
    "resized_head": "🔄 زاد رصيد USDT، فكبّر البوت أوامر الشراء المفتوحة",
    "resized_body": "حجم الخطة الآن {amount}. الأسعار كما هي، والذي تغيّر هو الكميات فقط.",
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
BTN_ON_NOW, BTN_OFF_NOW = TXT["btn_on_now"], TXT["btn_off_now"]
BTN_PLAN_B, BTN_PLAN_A = TXT["btn_plan_b"], TXT["btn_plan_a"]


def keyboard(paused, plan="A"):
    """3 small buttons in one row (the current mode carries a ✓) + one plan button under them."""
    row = [BTN_STATUS, BTN_OFF_NOW, BTN_RESUME] if paused else [BTN_STATUS, BTN_STOP, BTN_ON_NOW]
    return {"keyboard": [[{"text": x} for x in row], [{"text": BTN_PLAN_A if plan == "B" else BTN_PLAN_B}]],
            "resize_keyboard": True, "is_persistent": True}


KEYBOARD = keyboard(False)
BUTTONS = {BTN_STATUS: "/status", BTN_STOP: "/stop", BTN_RESUME: "/resume",
           BTN_ON_NOW: "/mode", BTN_OFF_NOW: "/mode", BTN_PLAN_B: "/planb", BTN_PLAN_A: "/plana",               # pressing the ✓ button just reports the mode
           # older labels, in case Telegram still shows an old keyboard
           "📊 عرض الملخص": "/status", "⏸ إيقاف الشراء": "/stop", "▶️ استئناف الشراء": "/resume"}
RAW_KEYS = {"icon", "state", "reason", "dot", "diff", "pname"}          # values that are Arabic text, not numbers/Latin
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


def item_lines(line):
    """'head | a | b' -> head on its own line, then each part on its own indented line."""
    parts = line.split(" | ")
    if len(parts) == 1:
        return line
    return parts[0] + "".join("\n" + RLM + "      " + x for x in parts[1:])


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

    def usdt_total(self):
        """Live: all USDT in the sub-account (free + held by open orders). Paper: None."""
        if not self.live:
            return None
        d = self._req("GET", "/api/v5/account/balance", {"ccy": "USDT"}, signed=True)
        det = d[0].get("details", []) if d else []
        return float(det[0].get("eq") or det[0].get("cashBal") or 0) if det else 0.0

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
        self.paused_fn = None                    # set in main(): lambda -> True when buying is paused
        self.plan_fn = None                      # set in main(): lambda -> "base budget vs plan" line
        self.label_fn = None                     # set in main(): lambda -> (plan name, label text)

    def status_line(self):
        if self.paused_fn is None:
            return ""
        label = ""
        if self.label_fn is not None:
            try:
                label = self.label_fn()[1]
            except Exception:
                label = ""
        line = B(T("status_off" if self.paused_fn() else "status_on") + label)
        if self.plan_fn is not None:
            try:
                line += "\n" + self.plan_fn()
            except Exception as e:
                log(f"plan line failed: {e}")
        return line

    def send(self, text, inline=None):
        """Every message starts with the buying status. inline = [[(label, callback_data), ...]] buttons."""
        sl = self.status_line()
        if sl:
            text = sl + "\n\n" + text
        log("TG: " + re.sub(r"</?[a-z][^>]*>", "", text).replace("\n", " | "))
        if not (self.tok and self.chat):
            return
        try:
            data = {"chat_id": self.chat, "text": text, "parse_mode": "HTML",
                    "link_preview_options": json.dumps({"is_disabled": True}),
                    "reply_markup": json.dumps(
                        {"inline_keyboard": [[{"text": t, "callback_data": c} for t, c in r] for r in inline]}
                        if inline else {"remove_keyboard": True})}   # buttons live in the "Menu" list now
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

    MENU = [("status", "📊 عرض الملخص"), ("stop", "⏸ إيقاف الشراء"), ("resume", "▶️ تشغيل الشراء"),
            ("planb", "🔄 التحويل إلى Plan B"), ("plana", "↩️ الرجوع إلى Plan A")]

    def set_menu(self):
        """The commands list behind Telegram's "Menu" button (next to the message box) - replaces the big buttons."""
        if not (self.tok and self.chat):
            return
        base = f"https://api.telegram.org/bot{self.tok}/"
        try:
            urllib.request.urlopen(base + "setMyCommands", urllib.parse.urlencode({"commands": json.dumps(
                [{"command": c, "description": d} for c, d in self.MENU], ensure_ascii=False)}).encode(),
                timeout=15).read()
            urllib.request.urlopen(base + "setChatMenuButton", urllib.parse.urlencode({
                "chat_id": self.chat, "menu_button": json.dumps({"type": "commands"})}).encode(), timeout=15).read()
        except Exception as e:
            log("telegram menu failed: " + str(e).replace(self.tok, "<token>"))

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
            q = u.get("callback_query")
            if q:                                              # inline button (yes / cancel)
                try:
                    urllib.request.urlopen(f"https://api.telegram.org/bot{self.tok}/answerCallbackQuery",
                                           urllib.parse.urlencode({"callback_query_id": q["id"]}).encode(),
                                           timeout=15).read()
                except Exception:
                    pass
                if str((q.get("message") or {}).get("chat", {}).get("id")) == str(self.chat):
                    out.append("cb:" + str(q.get("data", "")))
                continue
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
    def __init__(self, ex, st, tg, paper_cash=2590.0, arm=55000.0, disarm=65000.0, max_pump=0.10, slip=0.005,
                 alt_tol=0.02, min_plan=500.0, max_plan=4000.0):
        self.ex, self.st, self.tg = ex, st, tg
        self.alt_tol = alt_tol
        self.paper_cash, self.arm, self.disarm, self.max_pump, self.slip = paper_cash, arm, disarm, max_pump, slip
        self.min_plan, self.max_plan = min_plan, max_plan
        self.resize_every, self._last_resize = 300, 0.0
        self.base_budget = 2600.0                       # the plan's original size, shown on every message
        self._pl_cache = (0.0, None)

    # ---- Plan A / Plan B
    def is_b(self):
        return self.st.d.get("plan_name") == "B"

    def px(self, lv):
        """Price of a level in the active plan (Plan B keeps the same level slots at new prices)."""
        if self.is_b():
            return self.st.d.get("b_px", {}).get(str(lv), lv)
        return lv

    def arm_px(self):
        return self.st.d["b_start"] if self.is_b() else self.arm

    def disarm_px(self):
        return self.st.d["b_start"] * self.disarm / self.arm if self.is_b() else self.disarm

    def label(self):
        if self.is_b():
            return "B", T("label_b", start=money(self.st.d["b_start"])).lstrip(RLM)
        return "A", T("label_a").lstrip(RLM)

    def avg_px(self, prices):
        """Average BTC price if every level in `prices` [(price, weight)] fills."""
        w = sum(p for _, p in prices)
        return w / sum(p / x for x, p in prices) if prices else 0.0

    def cancel_open(self):
        """Cancel every resting BTC limit and record any fill. Returns '' or the level that could not be confirmed."""
        st, ex = self.st, self.ex
        for lv, pct in LEVELS:
            s = st.lv(lv)
            if s["btc"] not in ("placed", "placing"):
                continue
            ok = False
            for attempt in range(4):
                try:
                    ex.cancel("BTC-USDT", s["btc_id"])
                    state, q, avg = ex.limit_state("BTC-USDT", s["btc_id"], None)
                except Exception as e:
                    log(f"cancel check {lv}: {e}")
                    state, q, avg = "unknown", 0.0, 0.0
                if state in ("canceled", "filled", "missing", "mmp_canceled"):
                    ok = True
                    break
                time.sleep(1.0 if ex.live else 0)
            if not ok:
                return money(self.px(lv))
            if state == "filled":
                s["btc"] = "filled"
            elif q > 0:
                s["btc"] = "filled (partial)"
            else:
                s["btc"] = "waiting"
            if q > 0:
                s["btc_qty"], s["btc_cost"] = q, q * avg
                st.add_fill("BTC", q, q * avg)
            st.save()
        st.d["armed"] = False
        st.save()
        return ""

    def switch_to_b(self, btc):
        st, ex = self.st, self.ex
        if self.is_b():
            self.tg.send(T("b_already", time=st.d.get("b_time", ""), start=money(st.d["b_start"])))
            return
        base = next((lv for lv, _ in LEVELS if st.lv(lv)["btc"] != "filled"), None)
        if base is None:
            self.tg.send(T("b_all_done"))
            return
        if self.plan_now()[0] < self.min_plan:
            self.tg.send(T("b_min", min=money(self.min_plan)))
            return
        st.d["switching"] = "B"
        st.save()
        bad = self.cancel_open()
        if bad:
            st.d.pop("switching", None)
            st.save()
            self.tg.send(card(T("b_failed_head"), [T("b_failed_body", level=bad), T("b_failed_next")]))
            return
        base = next((lv for lv, _ in LEVELS if st.lv(lv)["btc"] != "filled"), None)   # a cancel may have filled it
        if base is None:
            st.d.pop("switching", None)
            st.save()
            self.tg.send(T("b_all_done"))
            return
        start = round(ex.ticker("BTC-USDT")[0], 1)
        st.d["switch_n"] = st.d.get("switch_n", 0) + 1
        st.d.update(plan_name="B", b_start=start, b_base=base, b_time=cairo_dt().strftime("%Y-%m-%d %H:%M"),
                    b_px={str(lv): round(start * lv / base, 1) for lv, _ in LEVELS if lv <= base})
        if st.d.get("paused"):
            st.d["paused"] = False
            self.tg.send(B(T("b_resumed")))
        # money for every level not bought yet, same rule as Plan A (all available USDT, by weights)
        plan, alloc = self.plan_now()
        st.d["plan"] = plan
        for lv, a in alloc.items():
            st.lv(lv)["alloc"] = a
        st.d.pop("switching", None)
        st.save()
        rows = []
        for lv, p in LEVELS:
            if lv > base:
                rows.append(T("b_level_done", orig=money(lv)))
            else:
                rows.append(T("b_level_line", px=money(self.px(lv)), orig=money(lv), amount=money(self.alloc_of(lv, p))))
        self.tg.send(card(T("b_done_head"), [T("b_done_body", start=money(start))] + [item(x) for x in rows]))
        self.instant_buy(base)
        now_px = ex.ticker("BTC-USDT")[0]
        self.place_open(now_px, forced=True)
        self.tick(now_px)

    def instant_buy(self, lv):
        """Plan B, first level: buy its BTC share now (IOC, at most `slip` above the ask)."""
        st, ex = self.st, self.ex
        s = st.lv(lv)
        pct = dict(LEVELS)[lv]
        resume = s["btc"] == "buying"
        if not resume:
            part = s["btc"] == "filled (partial)"
            s["prev_qty"] = s.get("btc_qty", 0.0) if part else 0.0
            s["prev_cost"] = s.get("btc_cost", 0.0) if part else 0.0
        usd = self.alloc_of(lv, pct) * BTC_W - s["prev_cost"]
        if usd < 1:
            s["btc"] = "filled"
            st.save()
            return
        if not resume and ex.usdt() < usd * 0.995:
            self.tg.send(T("b_nofund", price=money(self.px(lv))))
            return
        s["btc"] = "buying"
        st.save()
        q, c = self.buy_now("BTC", usd, f"P{st.d.get('switch_n', 1)}L{lv}")
        if q <= 0:
            s["btc"] = "filled (partial)" if s["prev_qty"] else "waiting"
            st.save()
            self.tg.send(T("b_instant_none", price=money(self.px(lv))))
            return
        st.add_fill("BTC", q, c)
        s["btc_qty"], s["btc_cost"] = s["prev_qty"] + q, s["prev_cost"] + c
        s["btc"] = "filled" if c >= usd * 0.97 else "filled (partial)"
        st.save()
        msg = [T("b_instant", qty=fmt(round(q, 8)), price=money(c / q, 1), amount=money(c, 2))]
        if s["btc"] == "filled (partial)":
            msg.append(T("b_instant_partial", amount=money(c, 2), wanted=money(usd, 2)))
        self.tg.send(card(T("filled_head", level=money(self.px(lv))), msg))

    def switch_to_a(self):
        st = self.st
        if not self.is_b():
            self.tg.send(T("a_already"))
            return
        bad = self.cancel_open()
        if bad:
            self.tg.send(card(T("a_failed_head"), [T("a_failed_body", level=bad), T("a_failed_next")]))
            return
        st.d.setdefault("b_history", []).append(
            {"start": st.d.get("b_start"), "time": st.d.get("b_time"), "base": st.d.get("b_base")})
        for k in ("b_start", "b_px", "b_base", "b_time"):
            st.d.pop(k, None)
        st.d["plan_name"] = "A"
        for lv, _ in LEVELS:
            if st.lv(lv)["btc"] == "waiting":
                st.lv(lv).pop("alloc", None)
        st.save()
        self.tg.send(B(T("a_done", arm=money(self.arm))))

    # ---- plan size: from the USDT in the sub-account, clamped to [min_plan, max_plan]
    def cash_total(self):
        v = self.ex.usdt_total()
        if v is None:                                    # paper: a pretend balance
            return max(0.0, self.paper_cash - self.st.d["spent"])
        return v

    def alloc_of(self, lv, pct):
        """Money for one level (BTC + alts). Set when its limit is placed; before that: share of the plan."""
        a = self.st.lv(lv).get("alloc")
        return a if a else pct * self.st.d.get("plan", self.paper_cash)

    def alt_need(self, lv, pct):
        s = self.st.lv(lv)
        a = self.alloc_of(lv, pct)
        frac = 1.0
        if s["btc"] == "filled (partial)":
            target = a * BTC_W
            frac = min(1.0, s["btc_cost"] / target) if target else 0.0
        done = sum(s.get("alt_done", {}).values())
        return max(0.0, a * (1 - BTC_W) * frac - done), frac

    def plan_line(self):
        """'base 2,600 | plan now X | below/above by Y%' - every message, right under the status line."""
        st = self.st
        if st.d["armed"]:
            plan = st.d.get("plan", st.d["spent"])
        else:
            ts, cached = self._pl_cache
            if cached is None or time.time() - ts > 60:
                cached = self.plan_now()[0]
                self._pl_cache = (time.time(), cached)
            plan = cached
        base = money(self.base_budget)
        if plan < self.min_plan:
            return T("base_low", base=base, min=money(self.min_plan))
        d = 100 * (plan / self.base_budget - 1)
        if abs(d) < 1:
            diff = T("diff_same")
        elif d < 0:
            diff = T("diff_below", pct=f"{-d:.1f}%")
        else:
            diff = T("diff_above_max" if plan >= self.max_plan - 0.5 else "diff_above", pct=f"{d:.1f}%")
        return T("base_line", base=base, plan=money(plan), diff=diff.lstrip(RLM))

    def plan_now(self):
        """(plan size, {level: money}) for the levels not bought yet - all available USDT, split by their weights."""
        st = self.st
        spent = st.d["spent"]
        pending = sum(self.alt_need(lv, p)[0] for lv, p in LEVELS
                      if st.lv(lv)["btc"].startswith("filled") and st.lv(lv)["alts"] in ("waiting", "in progress"))
        plan = min(self.max_plan, spent + self.cash_total())
        open_lv = [(lv, p) for lv, p in LEVELS if st.lv(lv)["btc"] in ("waiting", "placed", "placing")]
        wsum = sum(p for _, p in open_lv)
        avail = max(0.0, plan - spent - pending)
        return plan, {lv: avail * p / wsum for lv, p in open_lv} if wsum else {}

    # ---- summary
    def summary(self, btc=None, title=None, foot=()):
        """Important numbers first; mode / plan details / start notes at the end."""
        st, live = self.st, self.ex.live
        spent = st.d["spent"]
        out = [B(RLM + (title or TXT["t_summary"]))]
        if btc:
            q = [T("orders_open"), T("cancel_above", disarm=money(self.disarm_px()), pct=pct(100 * (self.disarm_px() / btc - 1)))] \
                if st.d["armed"] else [T("arm_at", arm=money(self.arm_px()), pct=pct(100 * (self.arm_px() / btc - 1)))]
            nxt = [lv for lv, _ in LEVELS if not st.lv(lv)["btc"].startswith("filled")]
            if nxt:
                q.append(T("next_level", level=money(self.px(nxt[0])), pct=pct(100 * (self.px(nxt[0]) / btc - 1))))
            out += ["", self.btc_line(btc), quote(q)]
        if st.d["armed"] or spent > 0:
            plan, head = st.d.get("plan", spent), T("budget", amount=money(st.d.get("plan", spent)))
        else:
            try:
                plan = self.plan_now()[0]
            except Exception:
                plan = st.d.get("plan", self.min_plan)
            head = T("budget_est", amount=money(plan)) if plan >= self.min_plan else \
                T("budget_low", min=money(self.min_plan))
        frac = spent / plan if plan else 0
        out += ["", B(head),
                quote([T("spent", amount=money(spent), pct=pct(100 * frac, False)),
                       T("left", amount=money(max(0.0, plan - spent))),
                       RLM + bar(frac) + " " + iso(pct(100 * frac, False))])]
        show_plan = max(plan, self.min_plan)
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
            rows.append(item(T("level_line", icon=icons.get(b, "⛔"), level=money(self.px(lv)), amount=money(s_.get("alloc") or p * show_plan),
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
        out += ["", B(bal[0]), quote([item_lines(x) for x in bal[1:]])]
        out += ["", quote([T("rules", pct=pct(100 * self.max_pump, False))], expandable=True)]
        out += ["", I(T("mode_live" if live else "mode_paper")),
                I(T("active_b", start=money(st.d["b_start"]), time=st.d.get("b_time", "")) if self.is_b()
                  else T("active_a"))]
        out.append(I(T("budget_rule", min=money(self.min_plan), max=money(self.max_plan))))
        out += [I(x) for x in foot]
        out.append(I(T("updated", time=cairo_dt().strftime("%Y-%m-%d %H:%M"))))
        return "\n".join(x for x in out if x is not None)

    def btc_line(self, btc):
        """Price line that stands out: bold, price in a code box, 24h change with a green/red dot."""
        p = money(btc)
        line = B(T("btc_now", price=p)).replace(iso(p), "<u>" + iso(p) + "</u>")   # bold + underlined price
        try:
            chg = 100 * self.ex.ticker("BTC-USDT")[2]
            line += "\n" + T("btc_24h", dot="🟢" if chg >= 0 else "🔴", pct=pct(chg))
        except Exception:
            pass
        return line

    def balance_lines(self):
        spent = self.st.d["spent"]
        try:
            b = self.ex.balances()
        except Exception:
            return [T("bal_error")]
        if b is None:
            return [T("bal_paper"), T("bal_paper_need", amount=f"{max(0.0, self.paper_cash - spent):,.0f}")]
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
        if spent + have + 0.5 < self.min_plan:
            need = self.min_plan - spent
            L.append(T("bal_short", have=f"{have:,.0f}", need=f"{need:,.0f}", gap=f"{need - have:,.0f}"))
        else:
            L.append(T("bal_ok", amount=money(min(self.max_plan, spent + have))))
            if spent + have > self.max_plan + 0.5:
                L.append(T("bal_extra", amount=money(spent + have - self.max_plan)))
        return L

    def once_alert(self, key, text):
        if self.st.d["alerts"].get(key) != text:
            self.st.d["alerts"][key] = text
            self.st.save()
            self.tg.send(text)

    # ---- BTC limits
    def btc_step(self, btc):
        st = self.st
        if not st.d["armed"] and btc <= self.arm_px():
            self.place_open(btc)
            return
        self.watch(btc)

    def place_open(self, btc, forced=False):
        """Place resting BTC limits for every level not bought yet (Plan A: at 55k; Plan B: right away)."""
        st, ex = self.st, self.ex
        open_lv = [(lv, pct) for lv, pct in LEVELS if st.lv(lv)["btc"] in ("waiting", "placed")]
        if st.d["armed"] or not open_lv:
            return
        if True:
            plan, alloc = self.plan_now()
            if plan < self.min_plan:
                st.d["unfunded"] = True
                have = max(0.0, plan - st.d["spent"])
                self.once_alert("btc_min", card(T("min_head"), [
                    T("min_body", price=money(btc), arm=money(self.arm_px()), have=f"{have:,.0f}",
                      min=f"{self.min_plan:,.0f}"),
                    T("min_next", gap=f"{self.min_plan - plan:,.0f}")]))
                for lv, pct in open_lv:
                    if btc <= self.px(lv):
                        self.once_alert(f"miss{lv}", card(T("missed_head", level=money(self.px(lv))), [
                            T("missed_body"), T("missed_next", level=money(self.px(lv)))]))
                return
            need = sum(alloc[lv] * BTC_W for lv, pct in open_lv if st.lv(lv)["btc"] == "waiting")
            if ex.usdt() < need * 0.995:
                st.d["unfunded"] = True
                self.once_alert("btc_bal", card(T("nofund_head"), [
                    T("nofund_body", price=money(btc), arm=money(self.arm_px()), amount=f"{need:,.2f}"),
                    T("nofund_next")]))
                for lv, pct in open_lv:
                    if btc <= self.px(lv):
                        self.once_alert(f"miss{lv}", card(T("missed_head", level=money(self.px(lv))), [
                            T("missed_body"), T("missed_next", level=money(self.px(lv)))]))
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
                s["alloc"] = alloc[lv]
                sz = floor_to(alloc[lv] * BTC_W / self.px(lv), lot)
                if sz < mn:
                    s["btc"], s["alts"] = "skipped (size)", s["alts"]
                    continue
                s["btc_id"] = f"B{lv}r{st.d['arm_round']}"
                s["btc"] = "placing"
                st.save()
                ex.place_limit("BTC-USDT", self.px(lv), sz, s["btc_id"])
                s["btc"] = "placed"
                st.save()
                placed.append(T("placed_line", level=money(self.px(lv)), qty=fmt(sz), amount=money(sz * self.px(lv), 2)))
            st.d["armed"] = True
            st.d["plan"] = plan
            self._last_resize = time.time()
            st.save()
            msg = [T("placed_head") + ("" if ex.live else " (وضع تجريبي، دون أوامر حقيقية)")]
            reason = T("placed_plan", amount=money(plan)) if forced else \
                T("placed_reason", price=money(btc), arm=money(self.arm_px())) + "\n" + T("placed_plan", amount=money(plan))
            msg += [reason] + placed
            msg += [T("placed_cancel", disarm=money(self.disarm_px()))]
            if placed:
                self.tg.send(card(msg[0], [msg[1]] + [item(x) for x in msg[2:-1]] + [I(msg[-1])]))

    def watch(self, btc):
        st, ex = self.st, self.ex
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
                self.tg.send(card(T("filled_head", level=money(self.px(lv))), [
                    T("filled_body", qty=fmt(round(q, 8)), price=money(avg, 1), amount=money(q * avg, 2)),
                    I(T("filled_next"))]))
            elif state in ("canceled", "missing", "mmp_canceled"):
                s["btc"] = "waiting"
                if q > 0:
                    s["btc"], s["btc_qty"], s["btc_cost"] = "filled (partial)", q, q * avg
                    st.add_fill("BTC", q, q * avg)
                st.save()
                self.tg.send(card(T("gone_head", level=money(self.px(lv))), [T("gone_partial" if q > 0 else "gone_again")]))
        # more USDT arrived while orders are open -> bigger orders at the SAME prices
        if btc < self.disarm_px() and time.time() - self._last_resize >= self.resize_every:
            self._last_resize = time.time()
            self.resize()
        # disarm above the cancel price
        if btc >= self.disarm_px():
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
            self.tg.send(card(T("disarm_head", disarm=money(self.disarm_px()), price=money(btc)),
                              [T("disarm_body"), T("disarm_next", arm=money(self.arm_px()))]))

    def resize(self):
        st, ex = self.st, self.ex
        plan, alloc = self.plan_now()
        grow = [(lv, alloc[lv]) for lv, _ in LEVELS if st.lv(lv)["btc"] == "placed" and lv in alloc
                and alloc[lv] > st.lv(lv).get("alloc", 0) * 1.05 and alloc[lv] - st.lv(lv).get("alloc", 0) >= 10]
        if not grow:
            return
        extra = sum((a - st.lv(lv).get("alloc", 0)) * BTC_W for lv, a in grow)
        if ex.usdt() < extra * 0.995:
            return
        lot, mn, tick, _ = ex.inst("BTC-USDT")
        lines = []
        for lv, a in grow:
            s = st.lv(lv)
            ex.cancel("BTC-USDT", s["btc_id"])
            state, q, avg = ex.limit_state("BTC-USDT", s["btc_id"], None)
            if q > 0:                                   # it (partly) filled meanwhile: keep that, don't re-place
                s["btc"], s["btc_qty"], s["btc_cost"] = ("filled" if state == "filled" else "filled (partial)"), q, q * avg
                st.add_fill("BTC", q, q * avg)
                st.save()
                continue
            sz = floor_to(a * BTC_W / self.px(lv), lot)
            s["resize_n"] = s.get("resize_n", 0) + 1
            s["btc_id"] = f"B{lv}r{st.d['arm_round']}u{s['resize_n']}"
            s["btc"], s["alloc"] = "placing", a
            st.save()
            ex.place_limit("BTC-USDT", self.px(lv), sz, s["btc_id"])
            s["btc"] = "placed"
            st.save()
            lines.append(item(T("placed_line", level=money(self.px(lv)), qty=fmt(sz), amount=money(sz * self.px(lv), 2))))
        st.d["plan"] = plan
        st.save()
        if lines:
            self.tg.send(card(T("resized_head"), [T("resized_body", amount=money(plan))] + lines))

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
            self.tg.send(card(T("broken_head", level=money(self.px(lv))), [T("broken_body")]))
            return
        need, frac = self.alt_need(lv, pct)
        if st.d["spent"] + need > self.max_plan * 1.001:
            s["alts"] = "skipped (budget)"
            st.save()
            self.tg.send(T("budget_alts", level=money(self.px(lv))))
            return
        ceiling = self.px(lv) * (1 + self.alt_tol)
        if btc is not None and btc > ceiling:
            self.once_alert(f"alt_far{lv}", card(T("altfar_head", level=money(self.px(lv))), [
                T("altfar_body", price=money(btc), ceiling=money(ceiling)),
                T("altfar_next", ceiling=money(ceiling))]))
            return
        if self.ex.usdt() < need * 0.995:
            self.once_alert(f"alt_bal{lv}", card(T("altfund_head"), [T("altfund_body", level=money(self.px(lv)), amount=f"{need:,.2f}"),
                T("altfund_next", ceiling=money(ceiling))]))
            return
        s["alts"] = "in progress"
        st.save()
        report = [T("alts_head", level=money(self.px(lv))) + ("" if self.ex.live else " (تجريبي)"),
                  T("alts_amount", amount=money(need, 2))]
        backup_usd = 0.0
        done = s.setdefault("alt_done", {})                 # per coin: money already bought / sent to backup
        for coin, w in ALTS:
            usd = self.alloc_of(lv, pct) * w * frac - done.get(coin, 0.0)
            if usd < 0.5:
                continue
            ok, why = self.ok_to_buy(coin)
            if not ok:
                backup_usd += usd
                done[coin] = done.get(coin, 0.0) + usd
                st.save()
                report.append(T("alt_skipped", coin=coin, reason=why, amount=money(usd, 2)))
                continue
            q, c = self.buy_now(coin, usd, f"A{lv}")
            st.add_fill(coin, q, c)
            done[coin] = done.get(coin, 0.0) + c
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
            self.tg.send(self.summary(btc, T("t_after_fill", level=money(self.px(lv)))[1:]))
        except Exception as e:
            log(f"summary after fill failed: {e}")

    def near_check(self, btc):
        """One alert when BTC comes within 3% of the trigger; re-armed after it goes 5% above."""
        st = self.st
        if st.d["armed"] or btc > self.arm_px() * 1.05:
            if st.d.get("near_sent"):
                st.d["near_sent"] = False
                st.save()
            return
        if btc <= self.arm_px() * 1.03 and not st.d.get("near_sent"):
            st.d["near_sent"] = True
            st.save()
            lines = [T("near_body", price=money(btc), pct=pct(100 * (self.arm_px() / btc - 1)), arm=money(self.arm_px()))]
            if not self.ex.live:
                lines.append(T("near_paper"))
            else:
                try:
                    have = self.ex.usdt_total()
                    plan = min(self.max_plan, st.d["spent"] + have)
                    need = max(0.0, self.min_plan - st.d["spent"])
                    lines.append(T("near_ok", have=f"{have:,.0f}", need=money(plan)) if plan >= self.min_plan else
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
        if self.st.d.pop("switching", None):
            self.st.save()
            self.tg.send(T("switch_broken"))
        for lv, _ in LEVELS:
            if self.st.lv(lv)["btc"] == "buying":           # restart during the Plan B instant buy: same client ids,
                self.instant_buy(lv)                       # so OKX returns the order already sent - no double buy
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
    tg.paused_fn = lambda: bool(st.d.get("paused"))   # every message starts with the buying status
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
    bot = Bot(ex, st, tg, g("LADDER_PAPER_BALANCE", g("LADDER_BUDGET", 2590)), arm, disarm,
              g("LADDER_MAX_PUMP", 0.10), g("LADDER_MAX_SLIPPAGE", 0.005),
              min_plan=g("LADDER_MIN_BUDGET", 500), max_plan=g("LADDER_MAX_BUDGET", 4000))
    bot.base_budget = g("LADDER_BASE_BUDGET", 2600)
    tg.plan_fn = bot.plan_line                        # 2nd line of every message: base budget vs plan now
    tg.label_fn = bot.label                           # active plan in the status line
    tg.set_menu()
    if "--status" in sys.argv:
        print(bot.summary(ex.ticker("BTC-USDT")[0]))
        return
    mode = "TEST REHEARSAL (paper, fake levels " + ", ".join(f"{lv:,.0f}" for lv, _ in LEVELS) + ")" if test \
        else ("LIVE" if live else "PAPER - no real orders")
    foot = [RLM + TXT["t_start"] + (" (تجربة بمستويات افتراضية)" if test else "")]
    if acct:
        log("OKX " + os.environ.get("LADDER_SUBACCOUNT", "spoot") + ": " + acct)
        foot.append(T("t_account_ok", account=os.environ.get("LADDER_SUBACCOUNT", "spoot")))
    tg.send(bot.summary(ex.ticker("BTC-USDT")[0], foot=foot))
    # was the bot down? (last heartbeat saved in the state)
    now_ts = time.time()
    last_alive = st.d.get("alive")
    if last_alive and now_ts - last_alive > 1800:                       # down more than 30 min
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
                if st.d.get("paused"):
                    tg.send(T("already_off"))
                else:                                   # ask first: a stray tap must not pause the plan
                    tg.send(card(T("stop_confirm"), [T("stop_confirm_note")]),
                            inline=[[(TXT["yes_stop"], "stop_yes"), (TXT["cancel"], "stop_no")]])
            elif cmd == "cb:stop_yes":
                if not st.d.get("paused"):
                    st.d["paused"] = True; st.save()
                tg.send(card(T("stopped"), [T("stopped_note")]))
            elif cmd == "cb:stop_no":
                tg.send(T("stop_cancelled") if not st.d.get("paused") else T("already_off"))
            elif cmd == "/resume":
                if st.d.get("paused"):
                    st.d["paused"] = False; st.save()
                    tg.send(B(T("resumed")))
                else:
                    tg.send(T("already_on"))
            elif cmd == "/mode":
                tg.send(T("paused") if st.d.get("paused") else T("already_on"))
            elif cmd in ("/status", "/start"):
                tg.send(bot.summary(ex.ticker("BTC-USDT")[0]))
            elif cmd == "/planb":
                if bot.is_b():
                    tg.send(T("b_already", time=st.d.get("b_time", ""), start=money(st.d["b_start"])))
                    continue
                p_now = ex.ticker("BTC-USDT")[0]
                base = next((lv for lv, _ in LEVELS if st.lv(lv)["btc"] != "filled"), None)
                if base is None:
                    tg.send(T("b_all_done"))
                    continue
                rest = [(lv, w) for lv, w in LEVELS if lv <= base]
                lines = [T("b_confirm_note", price=money(p_now)),
                         T("b_confirm_avg", avg_b=money(bot.avg_px([(p_now * lv / base, w) for lv, w in rest])),
                           avg_a=money(bot.avg_px(rest)))]
                if st.d.get("paused"):
                    lines.append(T("b_confirm_paused"))
                tg.send(card(T("b_confirm"), lines), inline=[[(TXT["yes_b"], "b_yes"), (TXT["cancel"], "b_no")]])
            elif cmd == "cb:b_yes":
                bot.switch_to_b(ex.ticker("BTC-USDT")[0])
            elif cmd == "/plana":
                if not bot.is_b():
                    tg.send(T("a_already"))
                    continue
                tg.send(card(T("a_confirm"), [T("a_confirm_note", arm=money(bot.arm))]),
                        inline=[[(TXT["yes_a"], "a_yes"), (TXT["cancel"], "a_no")]])
            elif cmd == "cb:a_yes":
                bot.switch_to_a()
            elif cmd in ("cb:b_no", "cb:a_no"):
                tg.send(T("plan_cancelled", pname=iso("Plan B" if bot.is_b() else "Plan A")))

    while True:
        try:
            handle_commands()
            btc = ex.ticker("BTC-USDT")[0]
            nowt = time.time()
            if nowt - st.d.get("alive", 0) >= 600:                      # heartbeat: one small write per 10 min
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
        st_ref = None                            # set -> balance goes down as the bot spends

        def usdt(self):
            return self.bal - (self.st_ref.d["spent"] if self.st_ref else 0)

        def usdt_total(self):
            return None if self.bal == float("inf") else self.usdt()

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
    assert st.lv(53400)["btc"] == "filled" and st.lv(53400)["alts"] == "done"
    assert st.lv(52600)["btc"] == "placed"
    assert "ADA" not in st.d["fills"], "ADA must be skipped"
    eth_after_53 = st.d["fills"]["ETH"]["cost"]
    assert abs(eth_after_53 - (259 * 0.20 + 259 * 0.20 * 0.40)) < 1.5, eth_after_53
    go(66000)                                    # bounce above 65k -> cancel
    assert not st.d["armed"] and st.lv(52600)["btc"] == "waiting"
    ex.chg["ADA"] = 0.0
    ex.chg["ETH"] = 0.12; ex.dead.add("NEAR")    # ETH pumping + NEAR halted -> all backup to BTC
    go(54000); assert st.d["armed"]
    go(51900)
    assert st.lv(52600)["alts"] == "done" and st.lv(51000)["btc"] == "placed"
    ex.chg = {}; ex.dead = set()
    go(48000)
    assert all(st.lv(lv)["btc"] == "filled" and st.lv(lv)["alts"] == "done" for lv, _ in LEVELS), st.d["levels"]
    assert st.d["spent"] <= 2590 * 1.001 and st.d["spent"] > 2590 * 0.97, st.d["spent"]
    ids = ex.iocs
    assert len(ids) == len(set(ids)), "duplicate client ids"
    # restart safety: interrupted alt step pauses
    st.lv(53400)["alts"] = "in progress"
    bot.alt_step(53400, 0.10)
    assert st.d["paused"]
    # status line + keyboard follow the mode
    tg.paused_fn = lambda: bool(st.d.get("paused"))
    assert "متوقف" in tg.status_line() and keyboard(True)["keyboard"][0][1]["text"] == BTN_OFF_NOW
    st.d["paused"] = False
    assert "مفعّل" in tg.status_line() and keyboard(False)["keyboard"][0][2]["text"] == BTN_ON_NOW
    assert [c for c, _ in TG.MENU] == ["status", "stop", "resume", "planb", "plana"]
    assert len(keyboard(False)["keyboard"]) == 2 and len(keyboard(False)["keyboard"][0]) == 3
    assert keyboard(False)["keyboard"][1][0]["text"] == BTN_PLAN_B and keyboard(False, "B")["keyboard"][1][0]["text"] == BTN_PLAN_A
    assert BUTTONS[BTN_ON_NOW] == "/mode" and BUTTONS["⏸ إيقاف الشراء"] == "/stop"
    st.d["paused"] = True
    # budget cap
    st2 = State(os.path.join(d, "s2.json")); st2.d["spent"] = 3800
    b2 = Bot(ex, st2, tg)
    s = st2.lv(49200); s["btc"] = "filled"
    b2.alt_step(49200, 0.30)
    assert st2.lv(49200)["alts"].startswith("skipped"), "budget cap"
    # ---- no money scenarios
    sent = []
    tg3 = TG(); tg3.tok = ""; tg3.send = lambda t: sent.append(t)
    ex3 = Fake(); ex3.bal = 0.0
    st3 = State(os.path.join(d, "s3.json")); b3 = Bot(ex3, st3, tg3)
    def go3(p):
        ex3.px["BTC"] = p; b3.tick(p)
    go3(52800)                                     # under 53k, no USDT
    assert not st3.d["armed"] and any("دون تنفيذ الشراء" in t and "53,400" in t for t in sent), sent
    go3(56500); ex3.bal = 5000.0; go3(56500)        # bounced, money added -> must NOT place/buy
    assert not st3.d["armed"] and "BTC" not in st3.d["fills"], "bought after bounce!"
    go3(54500)                                     # back under 55k -> limits at level prices only
    assert st3.d["armed"] and "BTC" not in st3.d["fills"], "54.5k must not fill a 53k limit"
    ex3.bal = 0.0; go3(52950)                      # 53k fills, but no USDT for alts
    assert st3.lv(53400)["btc"] == "filled" and st3.lv(53400)["alts"] == "waiting"
    assert any("لا يكفي لشراء العملات البديلة" in t for t in sent)
    go3(54500); ex3.bal = 5000.0; go3(54500)       # bounced above 53k+2%, money added -> no alt buy
    assert st3.lv(53400)["alts"] == "waiting" and "ADA" not in st3.d["fills"], "alts bought at higher price!"
    assert any("ينتظر البوت عودة" in t for t in sent)
    go3(53800)                                     # back within +2% -> alts bought
    assert st3.lv(53400)["alts"] == "done" and "ADA" in st3.d["fills"]
    # ---- plan size follows the balance (min 500, max 4000)
    def fresh(name, bal):
        e = Fake(); e.bal = bal
        s_ = State(os.path.join(d, name)); e.st_ref = s_
        out = []
        t_ = TG(); t_.tok = ""; t_.send = lambda t: out.append(t)
        b_ = Bot(e, s_, t_); b_.resize_every = 0
        return e, s_, b_, out
    e4, s4, b4, out4 = fresh("s4.json", 300.0)
    e4.px["BTC"] = 54000; b4.tick(54000)
    assert not s4.d["armed"] and any("الحد الأدنى" in t for t in out4), "below 500 must not arm"
    e4.bal = 1000.0; b4.tick(54000)
    assert s4.d["armed"] and abs(s4.d["plan"] - 1000) < 1 and abs(s4.lv(53400)["alloc"] - 100) < 0.5
    e4.px["BTC"] = 53300; b4.tick(53300)            # level 1 fills, alts bought from its 100
    assert s4.lv(53400)["alts"] == "done" and 95 < s4.d["spent"] < 101, s4.d["spent"]
    old_id = s4.lv(52600)["btc_id"]
    e4.bal = 3000.0 + s4.d["spent"] - 100           # he adds 2,000 (cash now ~2,900)
    b4.tick(53300)
    a2 = s4.lv(52600)["alloc"]                      # 2,900 * 15/90 = 483 (way 1: all the cash, remaining levels)
    assert abs(a2 - 2900 * 0.15 / 0.90) < 2 and s4.lv(52600)["btc_id"] != old_id, (a2, s4.lv(52600))
    assert e4.paper_limits[s4.lv(52600)["btc_id"]][1] == 52600, "resize must keep the price"
    assert e4.paper_limits[old_id][3] == "canceled"
    assert any("كبّر البوت" in t for t in out4)
    n_msgs = len(out4); b4.tick(53300); assert len(out4) == n_msgs, "no resize when nothing changed"
    e5, s5, b5, out5 = fresh("s5.json", 10000.0)
    e5.px["BTC"] = 54000; b5.tick(54000)
    assert abs(s5.d["plan"] - 4000) < 1 and abs(s5.lv(53400)["alloc"] - 400) < 0.5, "cap at 4,000"
    e5.px["BTC"] = 48000; b5.tick(48000); b5.tick(48000)
    assert 3900 < s5.d["spent"] <= 4000.5, s5.d["spent"]
    lines = {}
    for bal in (0, 1800, 2600, 3200, 10000):
        e7, s7, b7, _ = fresh(f"s7_{bal}.json", float(bal))
        lines[bal] = b7.plan_line()
    assert "أقل من الحد الأدنى" in lines[0] and "2,600" in lines[0]
    assert "أقل بـ" in lines[1800] and "30.8%" in lines[1800], lines[1800]
    assert "مطابق" in lines[2600] and "أعلى بـ" in lines[3200] and "23.1%" in lines[3200]
    assert "الحد الأقصى" in lines[10000] and "53.8%" in lines[10000], lines[10000]
    tgx = TG(); tgx.paused_fn = lambda: False; tgx.plan_fn = lambda: lines[1800]
    assert tgx.status_line().count("\n") == 1 and "30.8%" in tgx.status_line()
    # ---- Plan B
    eb, sb, bb, outb = fresh("sb.json", 2600.0)
    eb.px["BTC"] = 59000.0
    bb.tick(59000)
    assert not sb.d["armed"]
    sb.d["paused"] = True                                  # paused + switch -> switches on and buys
    bb.switch_to_b(59000)
    assert bb.is_b() and sb.d["b_start"] == 59000 and not sb.d["paused"]
    l1 = sb.lv(53400)
    assert l1["btc"] == "filled" and abs(l1["btc_cost"] - 260 * 0.30) < 1.5, l1   # instant BTC buy, its share only
    assert l1["alts"] == "done" and abs(sb.d["fills"]["ADA"]["cost"] - 52) < 1.5    # alts right after
    px2 = round(59000 * 52600 / 53400, 1)
    assert bb.px(52600) == px2 and sb.lv(52600)["btc"] == "placed" and sb.d["armed"]
    assert eb.paper_limits[sb.lv(52600)["btc_id"]][1] == px2, "Plan B limit at the scaled price"
    assert bb.px(49200) == round(59000 * 49200 / 53400, 1)
    n_lim, start = len(eb.paper_limits), sb.d["b_start"]
    eb.px["BTC"] = 61000.0; bb.switch_to_b(61000)          # pressing again: no recompute, no new orders
    assert sb.d["b_start"] == start and len(eb.paper_limits) == n_lim and any("بالفعل" in t for t in outb)
    eb.px["BTC"] = 59000 * 1.19; bb.tick(eb.px["BTC"])     # +18% above the start -> cancel
    assert not sb.d["armed"] and sb.lv(52600)["btc"] == "waiting"
    eb.px["BTC"] = 59500.0; bb.tick(59500); assert not sb.d["armed"]
    eb.px["BTC"] = 58900.0; bb.tick(58900); assert sb.d["armed"], "re-placed when back at the start price"
    eb.px["BTC"] = 58000.0; bb.tick(58000)                 # 58,116 fills -> its alts, ceiling = 58,116 + 2%
    assert sb.lv(52600)["btc"] == "filled" and sb.lv(52600)["alts"] == "done"
    assert any("Plan B" in x for x in [bb.summary(58000)]), "summary shows the active plan"
    bb.switch_to_a()                                       # back to Plan A
    assert not bb.is_b() and not sb.d["armed"] and bb.px(51000) == 51000
    assert sb.lv(53400)["btc"] == "filled" and sb.lv(51000)["btc"] == "waiting"
    # Plan A partly done, then switch: level 1 untouched, first unfinished level takes the start price
    ec, sc, bc, outc = fresh("sc.json", 2600.0)
    ec.px["BTC"] = 54000; bc.tick(54000)
    ec.px["BTC"] = 53300; bc.tick(53300)                   # Plan A level 53,400 + its alts
    ada_before = sc.d["fills"]["ADA"]["cost"]
    old_ids = [sc.lv(lv)["btc_id"] for lv in (52600, 51000)]
    ec.px["BTC"] = 58000; bc.switch_to_b(58000)
    assert all(ec.paper_limits[i][3] == "canceled" for i in old_ids), "Plan A orders cancelled first"
    assert sc.d["b_base"] == 52600 and bc.px(52600) == 58000 and bc.px(53400) == 53400
    assert sc.lv(52600)["btc"] == "filled" and bc.px(51000) == round(58000 * 51000 / 52600, 1)
    assert abs(sc.d["fills"]["ADA"]["cost"] - ada_before - sc.lv(52600)["alloc"] * 0.20) < 1.5, "no double alt buy"
    # cancel cannot be confirmed -> stop, stay on Plan A, nothing new sent
    class Stuck(Fake):
        def cancel(self, inst, cl):
            pass
        def limit_state(self, inst, cl, last_px=None):
            return ("live", 0.0, 0.0) if last_px is None else super().limit_state(inst, cl, last_px)
    ed, sd, bd, outd = fresh("sd.json", 2600.0)
    ed.__class__ = Stuck
    ed.px["BTC"] = 54000; bd.tick(54000)
    n_lim = len(ed.paper_limits)
    bd.switch_to_b(54000)
    assert not bd.is_b() and len(ed.paper_limits) == n_lim and any("تعذّر التحويل" in t for t in outd)
    e6, s6, b6, out6 = fresh("s6.json", 500.0)
    e6.px["BTC"] = 48000; b6.tick(48000); b6.tick(48000)
    assert all(s6.lv(lv)["alts"] == "done" for lv, _ in LEVELS) and 490 < s6.d["spent"] <= 500.5, s6.d["spent"]
    print()
    print(bot.summary(48000))
    print("\nSELFTEST OK: limits placed at 55k, wick fill, alts bought once, ADA pump -> 60/40 BTC/ETH backup, "
          "ETH pump -> backup all BTC, halted coin skipped, cancel at 65k and re-place at 55k, budget cap, crash pause, NO-MONEY: missed level alerted, no buy after bounce, limits only at levels, alts wait for BTC <= level+2%, "
          "PLAN SIZE: under 500 no orders, 1,000 -> 100 at level 1, +2,000 later -> open limits resized at the same "
          "prices over the remaining levels, 10,000 -> capped at 4,000, 500 -> all levels and alts filled, "
          "PLAN B: instant first level + alts, scaled limits, no recompute on 2nd press, +18% cancel / re-place, "
          "back to A, switch after a Plan A fill (no double alts), unconfirmed cancel stops the switch")


if __name__ == "__main__":
    selftest() if "--selftest" in sys.argv else main()
