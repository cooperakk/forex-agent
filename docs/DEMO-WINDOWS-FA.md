# تست روی حساب دمو — ویندوز + متاتریدر ۵

> این راهنما برای همین ماشین ویندوزی نوشته شده. بروکر MT5 (AMarkets، Alpari
> یا هر بروکر متاتریدری) فقط از داخل ویندوز قابل اتصال است، چون بستهٔ
> `MetaTrader5` پایتون فقط برای ویندوز منتشر می‌شود. سرور اوبونتوی مستندات
> دیگر، برای OANDA است که در ایران در دسترس نیست.

## ۰) پیش‌نیاز

- پایتون ۳.۱۱ یا بالاتر نصب باشد.
- ترمینال MetaTrader 5 نصب و **با حساب دمو وارد شده** باشد (پنجرهٔ ترمینال باز بماند).
- جفت‌هایی که می‌خواهی تست کنی در «Market Watch» ترمینال دیده شوند
  (راست‌کلیک → Show All).

## ۱) نصب

از پوشهٔ پروژه، در PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pip install MetaTrader5
cd dashboard; npm ci; npm run build; cd ..
```

> اگر `npm` نداری، داشبورد از پیش ساخته‌شده در `dashboard/dist` هست و این خط را رد کن.

## ۲) تست‌ها (باید سبز باشد)

```powershell
.\.venv\Scripts\python -m pytest -q
```

انتظار: `900 passed`. دو تست دسترسی فایل (`0600`) روی ویندوز رد می‌شوند؛
آن‌ها مربوط به لینوکس‌اند و مشکلی نیستند.

## ۳) اولین اجرا — حالت تمرینی (بدون بروکر)

```powershell
.\.venv\Scripts\python scripts\manage_users.py add --username owner --role owner
.\.venv\Scripts\python scripts\serve.py
```

- اولین اجرا `var\config.json` را با **سه استراتژی روشن** (`donchian_trend`،
  `ma_cross_atr`، `inside_bar_break`) روی پنج جفت اصلی می‌سازد.
- در حالت تمرینی، یک بازار مصنوعی تکرارپذیر قیمت می‌دهد؛ برچسبش «synthetic»
  است و هرگز مدرک پذیرش حساب نمی‌شود.
- مرورگر: `http://127.0.0.1:8088` — ورود با کاربر `owner` و کد Authenticator.
- صفحهٔ «تصمیم‌های ربات» باید بعد از چند دقیقه پر شود. حالت پیش‌فرض
  «مشورتی» است: فقط پیشنهاد می‌دهد.

> ساعت ورود پیش‌فرض ۰۷ تا ۱۶ به وقت UTC (۱۰:۳۰ تا ۱۹:۳۰ تهران) و فقط
> روزهای هفتهٔ کاری فارکس است. بیرون این بازه تصمیمی نمی‌بینی — این عمدی است.

## ۴) وصل کردن حساب دمو

1. داشبورد → «بروکر و اتصال» → **جست‌وجو**: ترمینال روی این ماشین را پیدا
   می‌کند و پیکربندی پیشنهادی می‌دهد (سرور، شماره حساب، پسوند نمادها).
2. «تست اتصال» را بزن. این تست **ساختاراً نمی‌تواند سفارش بفرستد**؛ فقط
   می‌خواند. باید سبز شود و نوع حساب را **demo** گزارش کند.
3. «فعال‌سازی» با کد Authenticator. بعد سرویس را یک بار ببند و دوباره اجرا کن:

```powershell
.\.venv\Scripts\python scripts\serve.py
```

در لاگ باید ببینی:
`MetaTrader server clock measured at UTC+3:00` (یا هر مقداری که سرور بروکر
دارد) — یعنی زمان کندل‌ها به UTC تبدیل شده است.

4. استراتژی‌ها را ببین/تغییر بده:

```powershell
.\.venv\Scripts\python scripts\manage_strategies.py list
.\.venv\Scripts\python scripts\manage_strategies.py available
.\.venv\Scripts\python scripts\manage_strategies.py add --name ts_momentum --timeframe D1
.\.venv\Scripts\python scripts\manage_strategies.py disable --name inside_bar_break
```

هر استراتژی کندل تایم‌فریم **خودش** را می‌گیرد (H4 یا D1)؛ بعد از هر تغییر
سرویس را دوباره اجرا کن.

## ۵) چه چیزی را ببینی

| صفحه | چه می‌بینی |
|---|---|
| نمای کلی | موجودی، equity، افت سرمایه، رژیم بازار |
| تصمیم‌های ربات | هر سیگنال، با دلیل رد یا قبول موتور ریسک |
| معامله‌های باز | پوزیشن‌های دمو با حد ضرر سمت سرور |
| دفتر ثبت رویدادها | زنجیرهٔ هش؛ `make verify-audit` سالم بودنش را چک می‌کند |

برای اینکه ربات **خودش** روی دمو معامله کند: «تنظیمات» → حالت را به
«نیمه‌خودکار» یا «خودمختار» تغییر بده (نقش owner + کد Authenticator).
حساب دمو است؛ پول واقعی در کار نیست و کد هم اجازهٔ پول واقعی به این
استراتژی‌ها را نمی‌دهد.

## ۶) دکمهٔ توقف اضطراری (ویندوز)

```powershell
New-Item -ItemType File var\KILL
```

بلافاصله هیچ معاملهٔ جدیدی باز نمی‌شود. برداشتنش: `Remove-Item var\KILL`.

## ۷) بعد از یک هفته دمو

خروجی تاریخی H4 و D1 را از همان ترمینال بگیر (با bid/ask) و پروتکل پذیرش
را روی دادهٔ واقعی بزن:

```powershell
.\.venv\Scripts\python scripts\run_acceptance.py --strategy donchian_trend --bars data\mt5_h4 --broker amarkets --config var\config.json
```

بدون ستون‌های `bid` و `ask` برچسب داده `third-party` می‌شود و **نمی‌تواند**
قبول بدهد — این درست است، نه خرابی.
