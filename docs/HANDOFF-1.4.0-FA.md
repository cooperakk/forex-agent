# Handoff — Sentinel-FX 1.4.0 (۱۴۰۵/۰۶/۲۸ — ۲۰۲۶‑۰۹‑۱۹)

> این سند برای شروع سشن بعدی نوشته شده. همه‌چیز از همین‌جا ادامه پیدا می‌کند.

## ۱. کجا هستیم

| مورد | مقدار |
|---|---|
| درخت کار | `C:\Users\cooper\Downloads\forex\src\sentinel-fx` (نسخهٔ ۱.۴.۰) |
| بستهٔ نهایی | `C:\Users\cooper\Downloads\forex\sentinel-fx-1.4.0.tar.gz` |
| نسخه‌های قبلی من | `sentinel-fx-1.2.0.tar.gz` (فید کندل + پژوهش صادق)، `sentinel-fx-1.3.0.tar.gz` (اوبونتو + پل MT5) |
| بستهٔ AI دیگر (بررسی‌شده) | `C:\Users\cooper\Downloads\gpt-forex\` — باز شده در `ext/` و `ext_win/` |
| venv | `src\sentinel-fx\.venv` (پایتون ۳.۱۳). برای pip/pytest باید `/d/flutter` را از PATH حذف کنی (درایو BitLocker‑قفل): `export PATH=$(echo "$PATH" \| tr ':' '\n' \| grep -v '^/d/flutter' \| paste -sd:)` |
| تست‌ها | ۹۵۸+ تست؛ ۲ تست دسترسی فایل `0600` روی ویندوز به‌عمد رد می‌شوند (POSIX‑only) |
| `CLAUDE.md` در `Downloads\` | مال پروژهٔ دیگری (Jan Plaza) است؛ فقط «فارسی ساده» از آن می‌ماند |

هدف کاربر: ربات معامله‌گر فارکس که **هم دستی، هم خودکار** کار کند و **صادقانه** بگوید سود دارد یا نه. حساب دمو MT5 (AMarkets/Alpari) و سرور اوبونتو. هنوز هیچ حساب دمو/سروری ندارد؛ راهنماهای فارسی `docs/UBUNTU-FA.md` و `docs/DEMO-WINDOWS-FA.md` مسیر صفر تا صد را می‌گویند.

## ۲. چه چیزی در ۱.۴.۰ اضافه شد (این سشن)

بستهٔ AI دیگر (`gpt-forex`) بررسی عمیق شد و هرچه از ما بهتر بود پورت شد؛ چند چیز فراتر از آن ساخته شد.

### از بستهٔ دیگر پورت شد
- **بستن هویت حساب** — `sentinel/brokers/bound.py` (`AccountBoundBroker`): هر فراخوانی هویت حساب/سرور/ارز/نوع ترمینال را دوباره می‌خواند؛ تغییر = رد همهٔ مسیریابی. `execution.expected_account_id/expected_account_server` در کانفیگ؛ live بدون آن رد می‌شود.
- **MT5 adapter** — دفتر قصد پایدار `mt5-intents.json` (حل UNKNOWN بعد از ری‌استارت)، retcode های TIMEOUT/CONNECTION → UnknownOutcome، PARTIAL، حذف نمادهای disabled، `positions_get()==None` → خطا (نه کتاب خالی)، دو تیکت روی یک نماد → خطا، **`fetch_closed_trades` از deals** (P&L واقعی با کمیسیون/fee/swap؛ حلقهٔ یادگیری روی MT5 قبلاً کاملاً خاموش بود)، `swap_pips_per_day` از ترمینال، `fetch_ticks`.
- **Orchestrator** — بازیابی کامل `position_meta` بعد از ری‌استارت (lots/entry/stop/excursion/max_hold — قبلاً reconciler را با کتاب صفر گول می‌زد)، fsync وضعیت + halt در شکست ذخیره/خواندن، CostModel از کمیسیون/اسلیپیج کانفیگ، ATR از تایم‌فریم خودِ استراتژی، close جزئی → halt، صف مشورتی محدود/بدون تکرار، `accept_advice` خارج سشن رد، معاملهٔ بدون ریسک از یادگیری حذف.
- **Performance guard** — بعد از ۵۰ معاملهٔ بسته، اگر کران بالای ۹۵٪ میانگین R منفی باشد استراتژی **معلق** می‌شود (`_guard_suspended`، `release_guard` فقط دستی).
- **Watchdog** — پیش‌فرض ۱۸۰ ثانیه (۴۵ در برابر چرخهٔ ۶۰ ثانیه هر بار می‌پرید!) + مهلت شروع؛ قاعدهٔ یکتا `deadman_timeout_ok` در `core/config.py`.
- **Verdict** — fingerprint schema 2: نمادها + پارامترها + تایم‌فریم + **هش سورس مسیر معاملاتی** + **سیاست runtime** (+ هش فایل meta-model). `INSERT` نه `REPLACE`. تعمیر authority → حالت `observe` (نه paper که حساب زنده را رها می‌کرد).
- **API/داشبورد** — توکن WebSocket در subprotocol (نه URL)؛ `/health` بدون auth؛ `/api/research/latest`؛ فیلدهای هویت حساب privileged.
- **money/config** — Decimal غیرمتناهی رد؛ `quantize` روی شبکهٔ غیر‑توان‑۱۰ (تیک ۰.۲۵)؛ `allow_inf_nan=False`.
- **Feed** — جدول `bar_features` (bid/ask/carry کنار OHLCV)، شمارش گپ آگاه از تعطیلی هفته، سن قیمت با تایم‌استمپ بروکر، `data/validation.py`.
- **paper** — rollover با ساعت نیویورک (tzrules)؛ **meta.py** — کالیبراسیون روی نیمهٔ جدا.

### فراتر از بستهٔ دیگر ساخته شد
- **`research/replay.py`** — بازپخش **خودِ Agent/OMS/Risk** روی تاریخ با ساعت اجرای جدا (کندل تصمیم H4 + کندل اجرای M1/M5)؛ `BacktestConfig(engine="agent")`. گیت **L11** فقط با این و با کادنس ≤ فاصلهٔ تصمیم پاس می‌شود.
- **`research/evidence.py`** — تأیید مانیفست دیتاست (هش فایل + bid/ask + جدول هزینه کامل)، رکورد فوروارد (تطبیق تا سنت + افت equity شناور)، پروب venue. گیت‌های **L10/L12/L13** به آن وصل‌اند. **L2** = دقت جهتی روی سیگنال‌های واقعی (نه Clark‑West روی equity).
- **`data/ticks.py` + `scripts/export_history.py`** — تیک واقعی MT5 → کندل bid/ask/mid؛ تنها راه دیتای `live-quality` از بروکر MT5.
- **`news/schedule.HistoricalCalendarSource`** — تقویم تاریخی از CSV، بازپخش در ساعت شبیه‌سازی (`--news`).
- **Meta-labeling wired** — `research/metalabel.py`: فیت روی ۶۰٪ اول، ارزیابی همه‌چیز روی ۴۰٪ بعد؛ `--meta-label --save-meta`؛ `agent.meta_model_path` در runtime (گیت act/skip قبل از موتور ریسک، `size_scale` روی احتیاط).
- **ریسک بین‌حسابی** — `risk/portfolio.py` (`GroupLedger`) + `ops.group_ledger_dir` + وتوهای `group_total_risk / group_currency_exposure / group_visibility`.
- **چند حساب** — `scripts/accounts.py` (workspace ایزوله، پورت/راز/پل جدا)، `scripts/run_account.py` (قفل OS)، `deploy/systemd/sentinel-account@.service` و watchdog، `deploy/scripts/add-account.sh`.
- **پل MT5 سخت‌تر شد** — `BridgeEnvelope`: بستن حساب، سقف لات، `--allow-live`، سقف نماد، **دفتر نوشتن پایدار** (id تکراری = replay؛ id با درخواست متفاوت = رد؛ نتیجهٔ نامعلوم = قفل تا `clear`). `BridgeRefused` ≠ خطای انتقال.
- **بهینه‌سازی** — memo روی `Strategy.prepare` و ADX رژیم (بازپخش ۲.۷× سریع‌تر، بدون تغییر رفتار).
- `scripts/make_manifest.py`، `scripts/probe_account.py`، `scripts/manage_strategies.py`.

## ۳. چه چیزی هنوز باقی است (ترتیب پیشنهادی)

1. **تست روی Windows واقعی با ترمینال MT5** — هیچ‌کدام از ۱.۲ تا ۱.۴ روی ترمینال واقعی اجرا نشده؛ فقط `tests/fake_mt5.py`. اولین کار سشن بعد: کاربر حساب دمو بسازد، `deploy/mt5-bridge/start-bridge.ps1`، بعد `connect-mt5.sh`.
2. **داشبورد** — صفحهٔ «تصمیم‌های ربات» هنوز `meta_probability`، `guard_suspended` و گروه ریسک را نشان نمی‌دهد؛ صفحهٔ Research باید `/api/research/latest` و `current_config` را بخواند. `status()` باید `guard_suspended` را برگرداند.
3. **`docs/ACCEPTANCE-PROTOCOL.md`** برای گیت‌های L10–L13 و `--engine agent/--execution-bars/--manifest/--forward/--news/--meta-label` به‌روز شود (کد جلوتر از سند است).
4. **`docs/MULTI-ACCOUNT-FA.md`** نوشته شود (systemd unit به آن ارجاع می‌دهد).
5. سرعت بازپخش: ~۳۰ms هر چرخه → M1 روی چند سال کند است؛ توصیه: M5/M15 چندساله + M1 برای ۶–۱۲ ماه آخر. جای بعدی بهینه‌سازی: `_manage_positions` و `feed.snapshot`.
6. Windows installer (`Install.ps1`/`Add-Account.ps1` مشابه بستهٔ دیگر) برای مسیر «همه‌چیز روی ویندوز».
7. مورد‌های «نسخهٔ بعد» از audit: تقویم خبری زنده (fetcher)؛ خروجی منظم فوروارد (`trades.csv/equity.csv`) از خودِ موتور — الان دستی است.

## ۴. قواعد ثابت (تغییر نده)
- هیچ override روی موتور ریسک. verdict registry منبع اختیار است، نه config.
- برچسب داده با محتوا اثبات می‌شود (مانیفست)، نه با تایپ کردن.
- Clark‑West روی equity ممنوع؛ L2 روی سیگنال‌های واقعی.
- نتیجهٔ نامعلوم سفارش = پرس‌وجو، هرگز ارسال دوباره.
- پول واقعی فقط با `expected_account_id` + verdict منطبق + fingerprint منطبق + فوروارد ≥۵۰ معامله.

## ۵. دستورهای کلیدی

```bash
# تست‌ها (با PATH اصلاح‌شده)
.venv/Scripts/python -m pytest -q
# پروتکل پذیرش کامل (موتور agent، دیتای واقعی، مانیفست، خبر، فوروارد)
python scripts/run_acceptance.py --strategy donchian_trend --bars data/amarkets/H4 \
  --execution-bars data/amarkets/M5 --manifest data/amarkets/H4-manifest.json \
  --news data/news.csv --forward evidence/forward.json --venue-probe evidence/venue.json \
  --broker amarkets --config var/config.json --meta-label --save-meta var/meta.joblib
# خروجی تاریخچهٔ bid/ask از ترمینال
python scripts/export_history.py --config var/config.json --symbols EUR_USD,GBP_USD \
  --days 400 --timeframes H4,M5 --out data/amarkets --costs-out data/amarkets/costs.json
python scripts/make_manifest.py --bars data/amarkets/H4 --broker amarkets --costs data/amarkets/costs.json --output data/amarkets/H4-manifest.json
# حساب دوم روی اوبونتو
sudo ./deploy/scripts/add-account.sh alpari-demo --broker alpari --account 123 --server "Alpari-MT5-Demo" --port 8091 --bridge 127.0.0.1:5556
```

## ۶. نکتهٔ صادقانه برای کاربر
هیچ‌کدام از ۲۹ استراتژی سودآوری اثبات‌شده ندارد و این نسخه هم چنین ادعایی نمی‌کند. آنچه اضافه شد، **راهِ اثباتِ صادقانه** است: دیتای واقعی → بازپخش همان کدی که اجرا می‌شود → فیلتر meta-label روی پنجرهٔ جدا → فوروارد روی دمو با تطبیق تا سنت. نتیجهٔ محتمل: اکثراً «NOT ACCEPTED». این خروجی درست است.
