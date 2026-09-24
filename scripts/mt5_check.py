#!/usr/bin/env python3
"""Why won't my MetaTrader 5 account connect? A read-only answer, in Persian.

    python scripts/mt5_check.py                          # attach to the running terminal
    python scripts/mt5_check.py --login 12345678 --server "Alpari-MT5-Demo"
    python scripts/mt5_check.py --path "C:\\Program Files\\Alpari MT5\\terminal64.exe"

On Windows, double-click deploy\\windows\\Check-MT5.cmd instead: it asks for
the password without echoing it and opens the report in the browser.

The password is read from the SENTINEL_MT5_PASSWORD environment variable or
typed at a hidden prompt; it is never printed, logged or written anywhere.
Nothing here places, modifies or closes an order. The report goes to
``mt5-check.html`` (right-to-left Persian) and a short English summary to the
console.
"""

from __future__ import annotations

import argparse
import html
import os
import platform
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: MetaTrader5.last_error() codes (MetaTrader5 Python documentation) -> what
#: they usually mean for someone connecting for the first time.
ERRORS_FA: Dict[int, str] = {
    -1: "خطای کلی ترمینال. متاتریدر را ببندید، دوباره باز کنید و دوباره امتحان کنید.",
    -2: "پارامترهای ورود نامعتبرند: شمارهٔ حساب باید فقط عدد باشد و نام سرور دقیقاً همان "
        "باشد که بروکر داده است.",
    -4: "ترمینال یا نماد پیدا نشد. مسیر terminal64.exe را با --path بدهید.",
    -5: "نسخهٔ ترمینال با کتابخانهٔ پایتون سازگار نیست. متاتریدر ۵ را به‌روز کنید "
        "(Help > Check for updates).",
    -6: "ورود ناموفق بود (Authorization failed). رایج‌ترین دلیل‌ها: رمز اشتباه؛ استفاده از "
        "«رمز سرمایه‌گذار» یا رمز ناحیهٔ شخصی بروکر به‌جای «رمز معاملاتی»؛ نام سرور اشتباه "
        "(دمو و واقعی سرورهای جدا دارند)؛ یا حسابی که برای متاتریدر ۴ ساخته شده، نه ۵؛ یا "
        "حساب دمو که منقضی شده است.",
    -7: "این قابلیت در این ترمینال پشتیبانی نمی‌شود.",
    -8: "معامله الگوریتمی خاموش است. در متاتریدر دکمهٔ Algo Trading را روشن کنید.",
    -10003: "اتصال به ترمینال راه‌اندازی نشد: ترمینال باز نیست، یا پایتون و ترمینال با "
            "کاربرهای مختلف ویندوز اجرا شده‌اند، یا پایتون ۳۲ بیتی است.",
    -10004: "اتصال به ترمینال برقرار نشد. ترمینال را با همان کاربر ویندوز باز کنید.",
    -10005: "مهلت اتصال تمام شد (IPC timeout): ترمینال هنوز در حال بالا آمدن یا به‌روزرسانی "
            "است. یک دقیقه صبر کنید و دوباره امتحان کنید.",
}

COMMON_PATHS = [
    r"C:\Program Files\Alpari MT5\terminal64.exe",
    r"C:\Program Files\MetaTrader 5\terminal64.exe",
    r"C:\Program Files\MetaTrader 5 Alpari\terminal64.exe",
]

TRADE_MODE_FA = {0: "دمو", 1: "مسابقه", 2: "واقعی"}


@dataclass
class Check:
    status: str            # ok | warn | fail | info
    title_fa: str
    detail_fa: str = ""
    detail_en: str = ""


@dataclass
class Report:
    checks: List[Check] = field(default_factory=list)
    facts: Dict[str, Any] = field(default_factory=dict)

    def add(self, status: str, title_fa: str, detail_fa: str = "", detail_en: str = "") -> None:
        self.checks.append(Check(status, title_fa, detail_fa, detail_en))

    @property
    def ok(self) -> bool:
        return not any(c.status == "fail" for c in self.checks)


def run_checks(mt5=None, *, login: Optional[int] = None, password: str = "",
               server: str = "", path: str = "", timeout_ms: int = 60_000,
               is_windows: Optional[bool] = None) -> Report:
    rep = Report()
    win = platform.system() == "Windows" if is_windows is None else is_windows
    if not win:
        rep.add("fail", "این بررسی باید روی ویندوز اجرا شود",
                "کتابخانهٔ MetaTrader5 فقط روی ویندوز و کنار خود ترمینال کار می‌کند. روی "
                "لینوکس از «پل» (docs/BROKERS.md) استفاده کنید.",
                "MetaTrader5 is Windows-only; use the bridge on Linux.")
        return rep
    if struct.calcsize("P") * 8 != 64:
        rep.add("fail", "پایتون ۳۲ بیتی است",
                "کتابخانهٔ MetaTrader5 به پایتون ۶۴ بیتی نیاز دارد. نصب‌کنندهٔ Sentinel "
                "خودش نسخهٔ درست را نصب می‌کند.", "64-bit Python is required.")
        return rep
    if mt5 is None:
        try:
            import MetaTrader5 as mt5  # type: ignore[no-redef]
        except ImportError:
            rep.add("fail", "کتابخانهٔ MetaTrader5 نصب نیست",
                    "دستور: .venv\\Scripts\\python.exe -m pip install MetaTrader5",
                    "pip install MetaTrader5")
            return rep
    rep.add("ok", "کتابخانهٔ MetaTrader5 نصب است")

    found = [p for p in ([path] if path else []) + COMMON_PATHS if p and Path(p).is_file()]
    rep.facts["terminals_found"] = found
    if path and not Path(path).is_file():
        rep.add("fail", "فایل ترمینال پیدا نشد", f"مسیر داده‌شده وجود ندارد: {path}")
        return rep

    kwargs: Dict[str, Any] = {"timeout": int(timeout_ms)}
    if login:
        kwargs.update(login=int(login), password=password, server=server)
    init_path = path or ""
    ok = mt5.initialize(init_path, **kwargs) if init_path else mt5.initialize(**kwargs)
    if not ok:
        code, text = _last_error(mt5)
        rep.facts["last_error"] = [code, text]
        rep.add("fail", "اتصال به متاتریدر ۵ ناموفق بود",
                ERRORS_FA.get(code, f"خطای {code}: {text}"), f"initialize failed: {code} {text}")
        _next_steps(rep, code)
        return rep
    rep.add("ok", "به ترمینال متاتریدر ۵ وصل شدیم")

    try:
        term = mt5.terminal_info()
        acct = mt5.account_info()
        if term is not None:
            rep.facts["terminal"] = {"name": getattr(term, "name", ""),
                                     "company": getattr(term, "company", ""),
                                     "path": getattr(term, "path", ""),
                                     "build": getattr(term, "build", None)}
            if not getattr(term, "connected", True):
                rep.add("fail", "ترمینال به سرور بروکر وصل نیست",
                        "پایین‌راست متاتریدر «No connection» یا «Invalid account» نوشته است؟ "
                        "نام سرور و رمز را بررسی کنید، یا اینترنت/فایروال سرور را.")
            if getattr(term, "trade_allowed", True) is False:
                rep.add("warn", "دکمهٔ Algo Trading خاموش است",
                        "در نوار بالای متاتریدر دکمهٔ «Algo Trading» را روشن کنید؛ و در "
                        "Tools > Options > Expert Advisors گزینهٔ «Allow algorithmic "
                        "trading» را تیک بزنید.")
        if acct is None:
            code, text = _last_error(mt5)
            rep.add("fail", "حسابی وارد نشده است",
                    "ترمینال باز است ولی با هیچ حسابی وارد نشده. در متاتریدر: File > Login to "
                    "Trade Account، یا این ابزار را با --login و --server اجرا کنید. "
                    + ERRORS_FA.get(code, ""))
            return rep
        mode = int(getattr(acct, "trade_mode", -1))
        currency = str(getattr(acct, "currency", ""))
        rep.facts["account"] = {
            "login": getattr(acct, "login", None), "server": getattr(acct, "server", ""),
            "company": getattr(acct, "company", ""), "currency": currency,
            "type": TRADE_MODE_FA.get(mode, "نامشخص"), "leverage": getattr(acct, "leverage", 0),
            "balance": getattr(acct, "balance", None)}
        rep.add("ok", f"وارد حساب {TRADE_MODE_FA.get(mode, 'نامشخص')} شدیم",
                f"شرکت: {getattr(acct, 'company', '')} — سرور: {getattr(acct, 'server', '')} — "
                f"موجودی: {getattr(acct, 'balance', '')} {currency} — اهرم 1:"
                f"{getattr(acct, 'leverage', '')}")
        if currency.upper() in ("USC", "EUC", "USCENT", "CENT"):
            rep.add("info", "این یک حساب سنتی است",
                    f"موجودی به «سنت» ({currency}) نمایش داده می‌شود؛ ۱۰۰۰۰ {currency} یعنی "
                    "۱۰۰ دلار. Sentinel نرخ تبدیل را از خود ترمینال می‌خواند.")
        if getattr(acct, "trade_allowed", True) is False:
            rep.add("fail", "این رمز اجازهٔ معامله ندارد",
                    "احتمالاً با «رمز سرمایه‌گذار» (Investor / فقط‌خواندنی) وارد شده‌اید. با "
                    "«رمز معاملاتی» (Master / Trading password) وارد شوید.")
        if getattr(acct, "trade_expert", True) is False:
            rep.add("fail", "معامله با ربات برای این حساب بسته است",
                    "بروکر معاملهٔ خودکار را روی این حساب غیرفعال کرده، یا Algo Trading "
                    "خاموش است.")
        if mode == 2:
            rep.add("warn", "این حساب واقعی است",
                    "اول با حساب دمو کار کنید. Sentinel معامله با پول واقعی را فقط برای "
                    "استراتژی‌های پذیرفته‌شده اجازه می‌دهد.")

        symbols = [s.name for s in (mt5.symbols_get() or [])]
        rep.facts["symbols"] = len(symbols)
        eur = [s for s in symbols if s.upper().startswith("EURUSD")]
        if not symbols:
            rep.add("fail", "هیچ نمادی در ترمینال نیست", "در Market Watch راست‌کلیک > Show All.")
        elif not eur:
            rep.add("warn", "EURUSD پیدا نشد",
                    f"نمونهٔ نمادها: {', '.join(symbols[:8])}")
        else:
            from sentinel.brokers.profiles.base import infer_symbol_map
            inferred = infer_symbol_map(symbols)
            rep.facts["suffix"] = inferred.suffix
            rep.add("ok", f"نمادها پیدا شدند ({len(symbols)} نماد)",
                    f"نام EURUSD در این سرور: {eur[0]}"
                    + (f" — پسوند نمادها: «{inferred.suffix}» (Sentinel خودکار تشخیص می‌دهد)"
                       if inferred.suffix else ""))
            tick = mt5.symbol_info_tick(eur[0])
            if tick is None or not getattr(tick, "bid", 0):
                rep.add("warn", "قیمت زنده برای EURUSD نیامد",
                        "بازار ممکن است بسته باشد (آخر هفته)، یا نماد در Market Watch نیست.")
            else:
                rep.add("ok", "قیمت زنده دریافت شد",
                        f"EURUSD خرید {tick.ask} / فروش {tick.bid}")
    finally:
        try:
            mt5.shutdown()
        except Exception:  # noqa: BLE001
            pass
    if rep.ok:
        rep.add("info", "قدم بعد",
                "در داشبورد Sentinel بروید به «بروکر و اتصال»، پروفایل Alpari (یا MetaTrader 5) "
                "را انتخاب کنید، همین شمارهٔ حساب و سرور را وارد کنید، «آزمایش اتصال» و بعد "
                "«فعال‌سازی» را بزنید. اول در حالت «فقط پیشنهاد» کار کنید.")
    return rep


def _last_error(mt5) -> tuple:
    try:
        err = mt5.last_error()
        return int(err[0]), str(err[1])
    except Exception:  # noqa: BLE001
        return 0, "unknown"


def _next_steps(rep: Report, code: int) -> None:
    if code == -6:
        rep.add("info", "برای حساب آلپاری این‌ها را یکی‌یکی بررسی کنید",
                "۱) حساب را برای «MetaTrader 5» ساخته‌اید، نه MetaTrader 4؟ (در ناحیهٔ شخصی "
                "آلپاری نوع پلتفرم کنار هر حساب نوشته شده.)\n"
                "۲) نام سرور را دقیقاً از ایمیل یا ناحیهٔ شخصی بردارید؛ سرور دمو و واقعی "
                "متفاوت‌اند.\n"
                "۳) «رمز معاملاتی» را وارد کنید، نه رمز ورود به سایت و نه رمز سرمایه‌گذار. "
                "اگر ندارید، از ناحیهٔ شخصی رمز معاملاتی تازه بسازید.\n"
                "۴) حساب دمو اگر مدتی استفاده نشود منقضی می‌شود؛ یک دموی تازه بسازید.\n"
                "۵) در متاتریدر: File > Open an Account، «Alpari» را جست‌وجو کنید تا سرورهای "
                "بروکر به فهرست اضافه شوند، بعد File > Login to Trade Account.")
    elif code in (-10003, -10004, -10005):
        rep.add("info", "برای اتصال به ترمینال",
                "متاتریدر ۵ را باز کنید و وارد حساب شوید، بعد همین ابزار را با همان کاربر "
                "ویندوز اجرا کنید. اگر چند متاتریدر نصب است، مسیر درست را با --path بدهید.")


def render_html(rep: Report) -> str:
    colors = {"ok": "#1a7f37", "warn": "#9a6700", "fail": "#cf222e", "info": "#0969da"}
    icons = {"ok": "✔", "warn": "!", "fail": "✘", "info": "ℹ"}
    rows = "".join(
        f"<li><b style='color:{colors[c.status]}'>{icons[c.status]} {html.escape(c.title_fa)}"
        f"</b><div style='white-space:pre-line;margin:4px 0 0'>{html.escape(c.detail_fa)}</div>"
        f"</li>" for c in rep.checks)
    verdict = ("<p style='color:#1a7f37'><b>همه چیز برای وصل شدن آماده است.</b></p>" if rep.ok
               else "<p style='color:#cf222e'><b>یک یا چند مشکل باید حل شود (موارد قرمز).</b></p>")
    return ("<!doctype html><html lang='fa' dir='rtl'><head><meta charset='utf-8'>"
            "<title>بررسی اتصال متاتریدر ۵</title><style>body{font:16px/1.9 Tahoma,sans-serif;"
            "max-width:820px;margin:24px auto;padding:0 16px}li{margin:12px 0}</style></head>"
            f"<body><h1>بررسی اتصال متاتریدر ۵</h1>{verdict}<ul>{rows}</ul></body></html>")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--login", type=int, default=None)
    p.add_argument("--server", default="")
    p.add_argument("--path", default="")
    p.add_argument("--timeout", type=int, default=60_000, help="milliseconds")
    p.add_argument("--report", default="mt5-check.html")
    args = p.parse_args(argv)
    password = os.environ.get("SENTINEL_MT5_PASSWORD", "")
    if args.login and not password:
        import getpass
        password = getpass.getpass("MT5 trading password (not shown): ")
    rep = run_checks(login=args.login, password=password, server=args.server,
                     path=args.path, timeout_ms=args.timeout)
    password = ""
    for c in rep.checks:
        print(f"[{c.status.upper():4}] {c.detail_en or c.title_fa}")
    try:
        Path(args.report).write_text(render_html(rep), encoding="utf-8")
        print(f"report: {Path(args.report).resolve()}")
    except OSError as exc:
        print(f"could not write the report: {exc}")
    return 0 if rep.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
