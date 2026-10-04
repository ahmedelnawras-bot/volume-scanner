# Volume Scanner — OKX + Binance

بيلقط العملات اللي بيدخلها فوليوم غير طبيعي على فريم الساعة (USDT perpetuals على OKX و Binance)، بيصنّف الحركة (مين بيحرّكها)، وبيبعت تنبيه Telegram فيه مستويات وحجم صفقة جاهزين. مبني على الـ White Paper بتاريخ 2026‑10‑03.

**قراية بس:** مفيش API keys، ومفيش فتح أو قفل صفقات.

## بيشتغل إزاي

| الوقت | الشغل |
|---|---|
| كل ساعة عند الدقيقة 01 | Scan كامل على الـ 1h + متابعة الإشارات القديمة (4h / 24h) |
| الدقايق 16 / 31 / 46 | مسح خفيف على 15m (إنذار مبكر) |
| كل 6 ساعات | Heartbeat |
| 9 الصبح بتوقيت القاهرة | الملخص اليومي |

**المسار:** قائمة العقود → فلتر السيولة ($2M) وعمر الإدراج (7 أيام) → شموع 1h لكل العقود → فلتر سريع (RVOL ≥ 3 وحركة ≥ 2%) → للمرشحين بس: شموع 4h + OI history + funding + taker → تصنيف + score → دمج OKX/Binance بنفس العملة → تنبيه لو score ≥ 60 ومفيش cooldown.

### أنواع الإشارات

| النوع | الشرط | الاتجاه |
|---|---|---|
| `EARLY_BREAKOUT` | RVOL + خروج من قاعدة عرضية + أقل من 25% من قاع 7 أيام + OI طالع | long |
| `CONTINUATION` | ترند 4h صاعد + RVOL | long |
| `SQUEEZE_RISK` | سعر ↑ + OI ↑ + funding سالب، من غير breakout ولا ترند | ممنوع الشورت |
| `SHORT_SQUEEZE` | سعر ↑ + OI ↓ | حركة قصيرة غالبًا |
| `NEW_SHORTS` | سعر ↓ + OI ↑ | short |
| `LONG_LIQUIDATION` | سعر ↓ + OI ↓ | ارتداد محتمل |
| `LATE_PUMP` | أكتر من 100% من قاع 7 أيام، أو %B > 1 والحركة ممتدة | تحذير (−30) |
| `VOLUME_SPIKE` | فوليوم عالي من غير ما ينطبق نوع من اللي فوق | حسب اتجاه الشمعة |

**الـ score (100):** RVOL 25 · OI 20 · سياق 4h 20 · تأكيد المنصة التانية 15 · taker 10 · سيولة 10. خصم: LATE_PUMP −30، funding عكس الاتجاه −10.

## التشغيل محليًا

```bash
pip install -r requirements-dev.txt
pytest -q                                   # tests بداتا وهمية
python -m scanner --once --dry-run          # scan واحد، التنبيهات تطبع في الـ terminal
python backtest.py --symbol NIGHT --start 2026-09-26 --end 2026-10-03 --show-alerts
```

## التشغيل على Windows

1. ثبّت Python 3.12 من python.org، وعلّم على **Add python.exe to PATH**.
2. حمّل الـ repo: زرار **Code ← Download ZIP**، وفكه في فولدر.
3. دبل كليك على `start.bat`. أول مرة هيعمل `.env` ويفتحه في Notepad.
4. حط `TELEGRAM_BOT_TOKEN` و `TELEGRAM_CHAT_ID`، واحفظ، ودبل كليك على `start.bat` تاني.
5. سيب الشباك مفتوح. لو حصل crash بيعيد التشغيل لوحده بعد 30 ثانية.

خلي الجهاز ميدخلش Sleep (Settings ← System ← Power). من غير `REDIS_URL` الحالة بتتحفظ في الذاكرة بس، فالـ cooldown والإشارات بيتمسحوا مع كل restart.

## النشر على Railway

1. New Project → Deploy from GitHub repo → اختار الـ repo ده.
2. ضيف **Redis** للمشروع، وانسخ `REDIS_URL` للـ service.
3. Variables: `TELEGRAM_BOT_TOKEN` و `TELEGRAM_CHAT_ID` (واختياري `TELEGRAM_STATUS_CHAT_ID`).
4. Settings → Region: **EU أو Asia** — Binance بترجع 451 من سيرفرات أمريكا.
5. الـ start command موجود في `railway.json` (`python -m scanner`).

كل الأرقام في `scanner/config.py` وتتغير من Railway Variables بنفس الاسم من غير ما تلمس الكود (شوف `.env.example`).

## الملفات

```
scanner/
  config.py          كل الإعدادات (env overrides)
  exchanges/         OKX + Binance public REST (async + pacing لكل endpoint)
  indicators.py      RVOL, ATR, MAs, Bollinger %B, swings, S/R, OI change
  analysis.py        metrics → type → action (LONG/SHORT/…) → score → plan
  engine.py          scan كل ساعة، 15m، متابعة، ملخص، heartbeat
  state.py           Redis: cooldown, signals log, OI snapshots, contracts cache
  followup.py        تقييم 4h/24h + أرقام الملخص اليومي
  main.py            scheduler + CLI
  notify/            كل حاجة بتوصل للمستخدم (منفصلة عن منطق الـ scan)
    terms.py         الصياغة عربي/إنجليزي ← عدّل هنا لو عايز تغيّر كلام الرسائل
    fmt.py           تنسيق الأرقام والوقت
    templates.py     data → نص Telegram
    telegram_client.py  الإرسال للـ Bot API (retry, dry-run)
    notifier.py      Notifier: الواجهة الوحيدة اللي engine/main بيكلموها
backtest.py          إعادة تشغيل على داتا تاريخية لعملة واحدة
tests/               unit + end-to-end بداتا وهمية
```

### عنوان الرسالة

أول سطر في كل تنبيه بيقول تعمل إيه قبل أي تفاصيل:

| العنوان | المعنى |
|---|---|
| 🟢 LONG / 🔴 SHORT | صفقة بخطة كاملة (دخول، SL، TP، حجم) |
| 🟢 LONG (ارتداد) | تصفية لونجات: دخول ارتداد عكس الحركة |
| ⛔ ممنوع LONG | بامب متأخر |
| ⛔ ممنوع SHORT | خطر Squeeze |
| 👀 مراقبة LONG/SHORT | فوليوم من غير تأكيد OI، أو Short squeeze، أو إنذار 15m |

## ملاحظات

- لو مفيش مقاومة فوق السعر (عملة عند قمة جديدة)، R1/R2 بيتحسبوا كـ 1.5R / 3R من الـ SL وبيتكتب ده في التنبيه.
- %B فوق 1 لوحده طبيعي في أي breakout من قاعدة ضيقة، فبيتحسب LATE_PUMP بس لو السعر بعيد ≥ 25% عن قاع 7 أيام.
- Binance بيحتفظ بـ OI history آخر 30 يوم بس، فالـ backtest لازم يكون على فترات قريبة.
