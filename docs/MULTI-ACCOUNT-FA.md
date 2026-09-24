# چند حساب — یک موتور برای هر حساب

هر حساب بروکر یک **موتور جداگانه** است: کانفیگ، پوشهٔ وضعیت، پورت داشبورد،
رمز JWT، پل متاتریدر و سرویس systemd خودش. «سوییچ» بین حساب‌ها یعنی باز
کردن داشبورد حساب دیگر؛ هر دو موتور ادامه می‌دهند و هیچ‌چیز منتقل نمی‌شود.

چرا؟ حافظهٔ موتور همان حساب است: اوج سرمایه، پلهٔ افت، مبناهای روز/هفته،
متادیتای پوزیشن، دفتر سفارش، زنجیرهٔ رویداد، درس‌ها. یک موتور که بین دو
حساب جابه‌جا شود، بودجهٔ افت یک حساب را به کتاب حساب دیگر می‌برد.

## روی اوبونتو

```bash
sudo ./deploy/scripts/add-account.sh alpari-demo --broker alpari --account 12345678 \
     --server "Alpari-MT5-Demo" --port 8091 --bridge 127.0.0.1:5556
```

- توکن پل را می‌پرسد (هیچ‌وقت آرگومان خط فرمان نیست).
- پوشهٔ `/var/lib/sentinel/accounts/alpari-demo/` را می‌سازد و سرویس‌های
  `sentinel-account@alpari-demo` و `sentinel-account-watchdog@alpari-demo` را روشن می‌کند.
- روی **ویندوز**: برای این حساب یک ترمینال MT5 جدا نصب کن، وارد حساب دوم شو،
  `start-bridge.ps1` را با `--port 5556` اجرا کن و تونل دوم:
  `.\tunnel.ps1 -Server ... -Port 5556`.
- داشبورد: `ssh -N -L 8091:127.0.0.1:8091 ...` → `http://127.0.0.1:8091`.
- فهرست همه: `/var/lib/sentinel/accounts/accounts.html`.

## ریسک بین حساب‌ها

دو حساب که هر کدام ۲٪ ریسک باز دارند، ۴٪ سرمایهٔ تو هستند. `add-account.sh`
همهٔ حساب‌ها را به یک دفتر مشترک (`/var/lib/sentinel/group`) وصل می‌کند؛ هر
موتور هر چرخه ردیف خودش (equity، ریسک باز، مواجههٔ ارزی) را می‌نویسد و
ردیف بقیه را می‌خواند. سقف‌ها در `risk.max_group_open_risk_pct` (پیش‌فرض ۳٪)
و `risk.max_group_currency_exposure_pct` (۲٪) هستند. ردیفی که کهنه یا ناخوانا
باشد = ریسک نامعلوم = ورود جدید رد می‌شود (`group_visibility`).

## دستورها

```bash
python scripts/accounts.py list
python scripts/accounts.py show alpari-demo
python scripts/accounts.py freeze alpari-demo      # فایل KILL همان حساب
python scripts/accounts.py fingerprint alpari-demo # اثرانگشت پذیرش
sudo systemctl status sentinel-account@alpari-demo
sudo journalctl -u sentinel-account@alpari-demo -f
```
