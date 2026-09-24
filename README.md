<div align="center">

# Sentinel-FX

**یک ایجنت معامله‌گر خودمختار ارز، ساخته‌شده حول حفاظت از سرمایه،
صداقت آماری، و یک دفتر رویداد دستکاری‌ناپذیر.**

*An autonomous FX trading agent built around capital preservation,
statistical honesty, and a tamper-evident audit trail.*

`Python 3.11+` · `FastAPI` · `React + TypeScript` · `958 tests`

</div>

---

## پیش از هر چیز — یک هشدار صادقانه

این سامانه **سودآوری را تضمین نمی‌کند و نمی‌تواند بکند.** حدود **۷۱٪** از
حساب‌های خرد فارکس ضرر می‌کنند. آنچه اینجا ساخته شده یک دستگاه دقیق برای
*کشف زودهنگام و ارزان این حقیقت* است که آیا شما لبه‌ای دارید یا نه — نه یک
ماشین پول‌سازی.

نسخه‌ی نمایشی عمداً با یک حکم **«پذیرفته‌نشده»** عرضه می‌شود. استراتژی
`donchian_trend` از دروازه‌های پذیرش عبور نمی‌کند، و این درست‌ترین نتیجه‌ی
ممکن است. سامانه‌ای که با حکم قبولی عرضه شود یا چیز فوق‌العاده‌ای یافته، یا
به شما دروغ می‌گوید — و احتمال دوم بسیار بیشتر است.

> **ارزش واقعی این پروژه:** یک قطعه‌ی مهندسی‌شده‌ی سامانه‌های مالی که به شما
> راست می‌گوید، نه یک وعده‌ی درآمد.

---

## چرا این‌گونه ساخته شده

چهار واقعیت، تمام معماری را تعیین کرده‌اند:

**۱. هزینه عبارت غالب است، نه سیگنال.** با حد ضرر `S`، حد سود `T` و هزینه‌ی
رفت‌وبرگشت `c` پیپ، نرخ برد لازم برای سربه‌سر شدن:

```
p = (S + c) / (T + S)
```

با `T = S = ۳` پیپ و `c = ۱.۰` پیپ، این عدد **۶۶.۷٪** است. هیچ لبه‌ی معتبری در
فارکس نقدشونده از این سد عبور نمی‌کند. کل خانواده‌ی اسکالپ، پیش از نوشتن یک خط
کد استراتژی، با حساب بسته می‌شود — و موتور ریسک در زمان اجرا هر معامله‌ای را
که نرخ سربه‌سرش از ۶۰٪ بگذرد، وتو می‌کند.

**۲. بیشتر آنچه «لبه» به نظر می‌رسد، سوگیری انتخاب است.** ۲۰۰ واریانت را روی
یک تاریخچه اجرا کنید؛ بهترینشان عالی به نظر می‌رسد، چه لبه‌ای وجود داشته باشد
چه نه. پس هیچ استراتژی‌ای اجازه‌ی پول واقعی ندارد تا از یک باتری از پیش
تعیین‌شده عبور کند: PBO، Deflated Sharpe، Clark-West، Hansen SPA، CPCV.

**۳. شکست‌های خطرناک عملیاتی‌اند، نه آماری.** سفارش تکراری، حد ضرری که هرگز
روی سرور ثبت نشد، پوزیشنی که سامانه فکر می‌کند بسته است، ری‌استارتی که فراموش
می‌کند در افت سرمایه بوده. این‌ها روز اول پول واقعی می‌برند.

**۴. هیچ‌چیز اینجا اثبات سودآوری نیست.** و سامانه طوری ساخته شده که این پاسخ
*دیده شود*، نه پنهان بماند.

---

## شروع سریع — یک دستور

روی سرور اوبونتو:

```bash
tar -xzf sentinel-fx-1.0.0.tar.gz && cd sentinel-fx
sudo ./deploy/scripts/install.sh
```

نصب‌کننده همه‌چیز را انجام می‌دهد: بررسی سرور، نصب بسته‌ها، ساخت کاربر جدا،
تولید رمزها، راه‌اندازی سرویس‌ها، و چاپ رمز ورود شما. اجرای دوباره‌اش
برنامه را به‌روز می‌کند و **هیچ‌وقت** اطلاعات قبلی را پاک نمی‌کند.

📘 **راهنمای کامل فارسی برای کسی که با بازار آشنا نیست:**
[`docs/RAHNAMA-FA.md`](docs/RAHNAMA-FA.md)

<details>
<summary>نصب دستی (برای توسعه)</summary>

```bash
tar -xzf sentinel-fx-1.0.0.tar.gz && cd sentinel-fx

python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # runtime + test deps
cd dashboard && npm ci && npm run build && cd ..

.venv/bin/python -m pytest -q        # انتظار: 958 passed (+2 permission tests that need POSIX)

cp .env.example .env && chmod 600 .env
python -c "import secrets; print(secrets.token_urlsafe(48))"   # SENTINEL_JWT_SECRET
$EDITOR .env

set -a && . ./.env && set +a
.venv/bin/python scripts/serve.py
```

در اولین اجرا یک نشانی `otpauth://` چاپ می‌شود — **همان لحظه** با اپلیکیشن
احرازهویت خود اسکنش کنید؛ فقط یک‌بار، هنگام ساخت حساب، چاپ می‌شود. حساب‌ها در
`var/users.db` با دسترسی ۰۶۰۰ ذخیره می‌شوند و از ری‌استارت جان سالم به‌در
می‌برند. سپس <http://127.0.0.1:8088> را باز کنید.

مدیریت حساب‌ها روی سرور انجام می‌شود، نه از طریق API — چون مدیریت حساب
ارزشمندترین هدف کل سطح حمله است و هیچ بخشی از آن لازم نیست از مرورگر
در دسترس باشد:

```bash
python scripts/manage_users.py add     --username owner --role owner
python scripts/manage_users.py list
python scripts/manage_users.py passwd  --username owner
python scripts/manage_users.py disable --username bob
```

با Docker:

```bash
cp .env.example .env && chmod 600 .env && $EDITOR .env
docker compose up -d
docker compose logs -f engine
```

</details>

راهنمای کامل: [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)

### دکمه توقف اضطراری

```bash
sudo -u sentinel touch /var/lib/sentinel/var/KILL
```

بلافاصله هیچ معامله‌ی جدیدی باز نمی‌شود — بدون نیاز به رمز، اینترنت یا سالم
بودن برنامه. معامله‌های باز حد ضررشان را نگه می‌دارند.

---

## سه حالت کاری

| حالت | رفتار |
|---|---|
| **مشورتی** (پیش‌فرض) | پیشنهاد می‌دهد، دلیل می‌آورد، **هیچ سفارشی نمی‌فرستد** |
| **نیمه‌خودکار** | داخل یک پاکت باریک (نمادها، حداکثر لات، حداکثر ریسک) خودش اجرا می‌کند |
| **خودمختار** | کامل خودکار، زیر تمام محدودیت‌های موتور ریسک |

تغییر حالت فقط با نقش `owner` + کد TOTP تازه ممکن است، و ارتقا به حالت زنده
نیازمند یک **حکم پذیرش با اثر انگشت منطبق** است.

---

## آنچه این سامانه دارد

**موتور ریسک مستقل** با بیش از ۳۰ وتوی نام‌دار که استراتژی نمی‌تواند خاموشش
کند — از `cost_barrier` و `missing_conversion` تا `unprotected_book` و
`frequency_year`. نردبان افت سرمایه که اندازه‌ی پوزیشن را پله‌پله کم می‌کند، و
**اوج سرمایه را روی دیسک نگه می‌دارد** تا یک ری‌استارت آن را فراموش نکند.

**آزمایشگاه پژوهش** با برچسب‌گذاری سه‌مانعی، یکتایی میانگین، پاکسازی و
قرنطینه، CPCV، PBO، Deflated Sharpe، MinTRL، Clark-West، Hansen SPA، و انتساب
عاملی با خطای Newey-West.

**دفتر رویداد زنجیره‌ی درهم‌ساز** — هر رکورد هش رکورد قبلی را حمل می‌کند، پس
هر ویرایش یا حذفی در نقطه‌ای قابل تشخیص زنجیره را می‌شکند.

**اجرای ایمن سفارش** با کلید یکتایی قطعی که از ری‌استارت جان سالم به‌در می‌برد،
و حالت `unknown` به‌عنوان یک حالت درجه‌یک که **فقط** با پرس‌وجو از صرافی حل
می‌شود — نه با استنتاج، نه با تایم‌اوت.

**داشبورد فارسی RTL** با فونت دوران، هشت صفحه، و یک کیت نمودار SVG
دست‌نویس (بدون هیچ وابستگی نموداری). داده‌ی نمایشی‌اش عمداً غیرجذاب است:
شارپ زیر ۱، یک افت سرمایه‌ی واقعی، و یک حکم پذیرش **مردود**.

**یادگیری از اشتباه** — کالبدشکافی هر معامله‌ی بسته‌شده، با رژیم بازار
مهرخورده در لحظه‌ی **ورود** نه پردازش. پیشنهادهای پارامتری صف می‌شوند و
**هرگز خودکار روی حساب زنده اعمال نمی‌شوند**.

---

## مستندات

| سند | موضوع |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | معماری، و *چرایی* هر تصمیم طراحی |
| [`docs/SECURITY.md`](docs/SECURITY.md) | مدل تهدید، احراز هویت، و آنچه محافظت نمی‌شود |
| [`docs/ACCEPTANCE-PROTOCOL.md`](docs/ACCEPTANCE-PROTOCOL.md) | تنها مسیر مجاز به پول واقعی |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | استقرار محلی، Docker، systemd |
| [`docs/RAHNAMA-FA.md`](docs/RAHNAMA-FA.md) | **راهنمای ساده فارسی — از اینجا شروع کنید** |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | وقتی چیزی خراب شد چه کنید |
| [`docs/LICENSING.md`](docs/LICENSING.md) | سامانه لایسنس، و مرزهای واقعی محافظت |
| [`docs/BROKERS.md`](docs/BROKERS.md) | بروکرها: AMarkets، آلپاری، و هر بروکر متاتریدر |

---
---

## English

### What this is

An end-to-end autonomous FX trading system: an independent risk engine, a
research laboratory that applies a pre-declared statistical battery, a
safe order-execution layer, a tamper-evident audit journal, a learning loop
that cannot promote itself, and a Persian-RTL management dashboard.

It ships **refusing to promote its own demo strategy**, which is the honest
result and is left in place deliberately.

### Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt   # runtime + test deps
cd dashboard && npm ci && npm run build && cd ..
.venv/bin/python -m pytest -q          # 958 passed (+2 POSIX-only permission tests)
cp .env.example .env && chmod 600 .env && $EDITOR .env
set -a && . ./.env && set +a && .venv/bin/python scripts/serve.py
```

Or `docker compose up -d`. Full instructions in `docs/DEPLOYMENT.md`.

### Layout

```
sentinel/
  core/        Decimal money · deterministic ids · clock · config · AUDIT
  risk/        the independent veto engine, sizing, exposure, protection
  execution/   order state machine, OMS, venue reconciliation
  brokers/     abstract adapter · paper (adversarial sim) · OANDA · MT5 · CCXT
    profiles/  per-venue quirks: AMarkets, Alpari, any MetaTrader broker
    connection discovery · a probe that STRUCTURALLY cannot trade · the
               activation gate · AES-GCM sealed credentials
  strategy/    families/ (trend · mean-reversion · breakout · momentum · carry ·
               volatility · session · pattern — 29 candidates, none accepted),
               baselines, plugin registry, ensemble, meta-labelling
  research/    labelling · CPCV · PBO · DSR · MinTRL · Clark-West · SPA ·
               verdicts · trial ledger (every backtest is a counted trial)
  agent/       decision loop, memory, postmortem, proposals, regime
  news/        economic calendar, LLM extraction, lookahead-propensity test
  ops/         kill switch, heartbeat, dead-man watchdog, health
  licensing/   Ed25519 signed grants · machine binding · integrity manifest ·
               quarterly calendar terms with a billing anchor · renewal chain ·
               anti-rollback clock guard
  api/         FastAPI — read-only by default, TOTP per write, three roles
dashboard/     Vite + React + TS, hand-written SVG charts, Persian RTL
scripts/       serve · run_acceptance · run_paper_sim · manage_users ·
               licensegen · package
deploy/        systemd units · nginx · backup ·
               scripts/ (install, update, restore, healthcheck, uninstall)
docs/          architecture · security · acceptance protocol · deployment
tests/         958 tests, including a regression for every audit finding
```

### Commands

```bash
sudo ./deploy/scripts/install.sh     # one-command server install
sudo ./deploy/scripts/activate-licensing.sh  # switch licence enforcement ON
sudo ./deploy/scripts/healthcheck.sh # is everything actually working?
sudo ./deploy/scripts/diagnose.sh    # what is wrong, and how to fix it
sudo ./deploy/scripts/diagnose.sh --fix --bundle
sudo ./deploy/scripts/backup-now.sh  # backup, verified
sudo ./deploy/scripts/restore.sh     # restore, verified before touching anything
sudo ./deploy/scripts/update.sh pkg.tar.gz   # upgrade, auto-rollback on test failure

make test           # full suite
make acceptance STRATEGY=donchian_trend
make paper          # end-to-end paper simulation
make serve          # engine + dashboard on loopback
make verify-audit   # check the hash chain
make docker         # build the image
```

### Three design decisions worth knowing about

**The idempotency key contains no run-id and no wall clock.** Every input is a
property of the *decision* — including the timestamp of the bar rather than
`now()`. The failure this defends against is: submit → crash before reading
the response → restart → re-derive the key. If the key changed across that
boundary, the venue would accept a duplicate order.

**A missing FX conversion rate is a veto, not a 1.0.** When the account
currency differs from the instrument's quote currency, substituting 1.0 for an
unknown rate is a 166× sizing error on a JPY account. `RiskContext.conversion()`
returns `Optional[Decimal]`, and returning `None` is the entire point.

**The verdict registry, not the config file, is the root of trading
authority.** A config can *claim* a strategy is accepted; a verdict bound to a
`config_fingerprint` is what makes it true. Unbacked badges are repaired
downward at every startup. An unreadable registry refuses to start rather than
being treated as empty — because that repair is irreversible.

### Licence and honest framing

Provided as-is, for research and personal use. Trading leveraged FX carries a
substantial risk of loss. Nothing in this repository is financial advice, and
passing every gate in the acceptance protocol means only that a strategy is
*not obviously the product of selection bias on the data available* — a much
weaker statement than "profitable", and the strongest one any apparatus can
honestly make about the future.
