# نصب روی سرور اوبونتو — صفر تا صد، برای کسی که هیچ‌چیز بلد نیست

> نتیجهٔ آخر: ربات ۲۴ ساعته روی سرور کار می‌کند، داشبورد از لپ‌تاپ تو
> باز می‌شود، و به حساب دموی متاتریدرِ روی کامپیوتر ویندوزی‌ات وصل است.
> کل کار حدود **۲ ساعت** است و **هیچ پولی** لازم ندارد جز اجارهٔ سرور.

## تصویر کلی — سه دستگاه

```
   [ لپ‌تاپ تو ]  --SSH-->  [ سرور اوبونتو ]  <--تونل SSH--  [ ویندوز با متاتریدر ]
    داشبورد را               مغز ربات:                        ترمینال متاتریدر
    اینجا می‌بینی            تصمیم، ریسک، ثبت                 + «پل» (یک پنجرهٔ باز)
```

چرا سه‌تا؟ چون متاتریدر فقط روی ویندوز اجرا می‌شود و بروکرهای در دسترس
ایران (AMarkets، Alpari) فقط متاتریدر دارند. «ویندوز با متاتریدر» می‌تواند
**همین کامپیوتر فعلی‌ات** باشد.

---

## مرحلهٔ ۱ — یک سرور اوبونتو بگیر (۲۰ دقیقه)

از هر سرویس سرور مجازی (VPS) که در دسترس داری، این را بخواه:

| مورد | مقدار |
|---|---|
| سیستم‌عامل | **Ubuntu 22.04** یا **24.04** (Server) |
| رم | حداقل ۲ گیگ |
| دیسک | ۲۰ گیگ |
| مکان | فرقی ندارد؛ فقط باید از کامپیوتر ویندوزی‌ات به آن SSH بزنی |

بعد از خرید سه چیز به تو می‌دهند: **آی‌پی** (مثل `203.0.113.10`)، **نام
کاربری** (معمولاً `ubuntu` یا `root`)، و **رمز** (یا کلید). یادداشت کن.

---

## مرحلهٔ ۲ — وارد سرور شو (۵ دقیقه)

روی ویندوز، **PowerShell** را باز کن (Start → بنویس PowerShell → Enter) و:

```powershell
ssh ubuntu@203.0.113.10
```

(به‌جای `ubuntu` و آی‌پی، مال خودت.) بار اول می‌پرسد `Are you sure...?` →
`yes`. رمز را بزن (تایپ رمز دیده نمی‌شود؛ طبیعی است). حالا داخل سرور هستی؛
خط فرمان چیزی مثل `ubuntu@server:~$` نشان می‌دهد.

---

## مرحلهٔ ۳ — فایل را به سرور بفرست (۵ دقیقه)

یک PowerShell **دوم** روی ویندوز باز کن (نه داخل سرور) و از پوشه‌ای که
`sentinel-fx-1.3.0.tar.gz` در آن است:

```powershell
scp .\sentinel-fx-1.3.0.tar.gz ubuntu@203.0.113.10:~
```

رمز را می‌پرسد؛ بعد فایل روی سرور است.

---

## مرحلهٔ ۴ — نصب با یک دستور (۱۰ دقیقه)

برگرد به پنجرهٔ **داخل سرور** و این سه خط را بزن:

```bash
tar -xzf sentinel-fx-1.3.0.tar.gz
```

```bash
cd sentinel-fx
```

```bash
sudo ./deploy/scripts/install.sh
```

نصب‌کننده خودش همه‌چیز را انجام می‌دهد: بسته‌ها، کاربر جداگانه، رمزها،
سرویس‌ها. در پایان چیزی شبیه این چاپ می‌کند — **همین الان یادداشت کن**:

```
  2. Log in as:  owner
     Password:  Xy9...abc          <-- رمز داشبورد؛ فقط یک بار چاپ می‌شود
  3. Enrol your authenticator app from:
         sudo cat /var/lib/sentinel/enrolment-owner.txt
```

بعد این را بزن:

```bash
sudo cat /var/lib/sentinel/enrolment-owner.txt
```

یک آدرس `otpauth://...` چاپ می‌شود. در گوشی، اپ **Google Authenticator**
را باز کن → «+» → «Enter a setup key» → نام: `sentinel`، کلید: همان رشتهٔ
بعد از `secret=` در آدرس. (یا اگر ترجیح می‌دهی، آدرس را در یک سایت
QR-ساز بگذار و اسکن کن.) بعد فایل را پاک کن:

```bash
sudo rm /var/lib/sentinel/enrolment-owner.txt
```

بررسی اینکه همه‌چیز بالاست:

```bash
sudo /opt/sentinel-fx/deploy/scripts/healthcheck.sh
```

---

## مرحلهٔ ۵ — داشبورد را از لپ‌تاپ باز کن (۲ دقیقه)

داشبورد عمداً فقط از خودِ سرور در دسترس است (امنیت). یک PowerShell روی
ویندوز باز کن و **باز نگهش دار**:

```powershell
ssh -N -L 8088:127.0.0.1:8088 ubuntu@203.0.113.10
```

حالا در مرورگر: **http://127.0.0.1:8088** → کاربر `owner`، رمز چاپ‌شده،
کد ۶ رقمی گوشی.

داشبورد را می‌بینی. الان روی بازار مصنوعی (حالت تمرینی داخلی) کار می‌کند.
تا اینجا ربات کار می‌کند ولی هنوز به بروکر وصل نیست.

---

## مرحلهٔ ۶ — پل متاتریدر روی ویندوز (۱۵ دقیقه)

روی همین کامپیوتر ویندوزی:

1. **متاتریدر ۵** را باز کن و با حساب **دمو** وارد شو (راهنمای ساخت
   حساب دمو: `docs/DEMO-WINDOWS-FA.md` مرحلهٔ ۲). پایین پنجره باید
   **Connected** باشد.
2. همان فایل `sentinel-fx-1.3.0.tar.gz` را روی ویندوز هم باز کن (مثلاً در
   `C:\sentinel`). به پوشهٔ `deploy\mt5-bridge` برو.
3. روی `start-bridge.ps1` راست‌کلیک → **Run with PowerShell**. بار اول
   پایتون‌ لازم را نصب می‌کند. در آخر چاپ می‌کند:
   ```
   [bridge] SENTINEL_MT5_BRIDGE_TOKEN=Abc123...      <-- این توکن را کپی کن
   [bridge] attached to AMarkets account 123456 (DEMO)
   [bridge] serving. Leave this window open.
   ```
   > اگر نوشت «Python was not found»: پایتون را از python.org نصب کن و
   > تیک **Add python.exe to PATH** را بزن؛ دوباره اجرا کن.
4. یک PowerShell دیگر باز کن:
   ```powershell
   cd C:\sentinel\sentinel-fx\deploy\mt5-bridge
   .\tunnel.ps1 -Server 203.0.113.10 -User ubuntu
   ```
   رمز سرور را می‌پرسد. **این پنجره را هم باز بگذار.**

---

## مرحلهٔ ۷ — سرور را به پل وصل کن (۲ دقیقه)

در پنجرهٔ **داخل سرور**:

```bash
sudo /opt/sentinel-fx/deploy/scripts/connect-mt5.sh
```

توکن را که کپی کردی می‌پرسد → Paste → Enter. باید ببینی:

```
[connect-mt5] checking the account through the bridge (read-only)...
  account   123456  AMarkets  AMarkets-Demo
  type      DEMO
  currency  USD   balance 10000.0   leverage 1:100
  symbols   84 visible in the terminal
  tick      EURUSD bid 1.0851 ask 1.0852
  OK: the engine can see this account. The probe placed no order.
[connect-mt5] engine restarted.
```

بعد در داشبورد: **«بروکر و اتصال»** → **جست‌وجو** (حساب دمو را پیدا می‌کند)
→ **تست اتصال** (باید سبز شود و بنویسد demo) → **فعال‌سازی** با کد گوشی →
و در سرور:

```bash
sudo systemctl restart sentinel-engine
```

**تمام.** از این لحظه ربات روی سرور، با قیمت‌های واقعی بروکر، در حالت
«مشورتی» کار می‌کند: پیشنهاد می‌دهد، هیچ سفارشی بدون تأیید تو نمی‌فرستد.

---

## هر روز چه کنی

- تونل داشبورد (مرحلهٔ ۵) را باز کن و `http://127.0.0.1:8088` را ببین:
  صفحهٔ «تصمیم‌های ربات».
- دو پنجرهٔ پل روی ویندوز باید باز باشند. اگر بسته شدند، دوباره اجرا کن؛
  سرور خودش دوباره وصل می‌شود.

## اگر خواستی ربات خودش معامله کند (روی دمو)

داشبورد → «تنظیمات» → حالت: **نیمه‌خودکار** (با کد گوشی). پاکت پیش‌فرض
کوچک است (۰.۲ لات، ۰.۵٪ ریسک). پول خیالی است.

## توقف فوری

```bash
sudo -u sentinel touch /var/lib/sentinel/var/KILL
```

هیچ معاملهٔ جدیدی باز نمی‌شود. برداشتن: `sudo rm /var/lib/sentinel/var/KILL`.

## اگر چیزی خراب شد

```bash
sudo /opt/sentinel-fx/deploy/scripts/diagnose.sh
```

می‌گوید چه چیزی خراب است و چطور درست می‌شود. خروجی‌اش را برای من بفرست.

## دستورات مفید

| کار | دستور |
|---|---|
| وضعیت | `sudo systemctl status sentinel-engine` |
| لاگ زنده | `sudo journalctl -u sentinel-engine -f` |
| تست پل | `sudo /opt/sentinel-fx/deploy/scripts/connect-mt5.sh --test` |
| قطع پل (برگشت به تمرینی) | `sudo /opt/sentinel-fx/deploy/scripts/connect-mt5.sh --off` |
| استراتژی‌ها | `sudo -u sentinel /opt/sentinel-fx/.venv/bin/python /opt/sentinel-fx/scripts/manage_strategies.py --config /var/lib/sentinel/config.json list` |
| بکاپ همین الان | `sudo /opt/sentinel-fx/deploy/scripts/backup-now.sh` |
| به‌روزرسانی به نسخهٔ بعد | `sudo /opt/sentinel-fx/deploy/scripts/update.sh sentinel-fx-1.4.0.tar.gz` |
