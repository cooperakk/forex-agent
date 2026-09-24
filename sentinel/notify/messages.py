"""Journal events -> short Persian messages for Telegram / Bale.

Each message belongs to one category the owner can switch on or off:

    critical   stops, halts, the kill switch, broker mismatches, cooldowns
    trades     positions opened and closed, broker rejections
    proposals  the agent's trade proposals in advisory mode (can be many)
    learning   drift alarms, the nightly lab, the weekly report, new models
    security   failed sign-ins and refused writes
    daily      the daily summary (sent by the notifier's scheduler)

Messages carry no secret: the journal payloads they are built from hold none,
and account numbers are not included. A ``key`` lets the notifier collapse a
repeat of the same situation instead of sending it every cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

CATEGORIES = ("critical", "trades", "proposals", "learning", "security", "daily")
DEFAULT_CATEGORIES = ("critical", "trades", "learning", "security", "daily")
CATEGORY_FA = {"critical": "هشدارهای مهم", "trades": "باز و بسته شدن معامله‌ها",
               "proposals": "پیشنهادهای معامله", "learning": "یادگیری و آزمایشگاه",
               "security": "امنیت", "daily": "گزارش روزانه"}

SIDE_FA = {"BUY": "خرید", "SELL": "فروش"}
MODE_FA = {"observe": "فقط مشاهده", "advisory": "فقط پیشنهاد", "semi_auto": "نیمه‌خودکار",
           "autonomous": "خودکار"}
STATUS_FA = {"alive": "زنده", "weak": "ضعیف", "dead": "مرده", "insufficient": "دادهٔ کم",
             "no_data": "بدون داده", "error": "خطا"}


@dataclass
class Message:
    category: str
    key: str
    text: str


def _s(v: Any, limit: int = 160) -> str:
    return str(v if v is not None else "")[:limit]


def _r(v: Any) -> str:
    try:
        return f"{float(v):+.2f}R"
    except (TypeError, ValueError):
        return "—"


def render(event: str, payload: Dict[str, Any], actor: str = "") -> Optional[Message]:
    p = payload or {}
    if event == "risk.halt":
        if p.get("performance_guard"):
            return Message("critical", f"guard:{p.get('strategy')}",
                           f"⛔ استراتژی {_s(p.get('strategy'))} معلق شد، چون نتیجه‌های "
                           f"واقعی‌اش به‌طور معنادار ضرر نشان می‌دهد.\n{_s(p.get('reason'))}")
        if p.get("licence_blocks_entries"):
            return Message("critical", "licence",
                           "⚠️ لایسنس اجازهٔ معاملهٔ تازه با پول واقعی نمی‌دهد: "
                           f"{_s(p.get('licence_blocks_entries'))}\nمعامله‌های باز همچنان "
                           "مدیریت می‌شوند.")
        return Message("critical", f"halt:{_s(p.get('reason'), 60)}",
                       f"⛔ ربات متوقف شد (فقط معامله‌های تازه):\n{_s(p.get('reason'), 300)}")
    if event == "ops.kill_switch":
        if p.get("engaged") is True:
            return Message("critical", "kill:on",
                           "🛑 توقف اضطراری فعال شد. هیچ معاملهٔ تازه‌ای باز نمی‌شود؛ "
                           "معامله‌های باز حد ضررشان را نزد بروکر دارند.\n"
                           f"دلیل: {_s(p.get('reason'))}")
        if p.get("engaged") is False:
            who = _s(p.get("released_by") or actor)
            return Message("critical", "kill:off", f"✅ توقف اضطراری برداشته شد (توسط {who}).")
        return None
    if event == "ops.deadman":
        return Message("critical", "deadman",
                       "⚠️ نگهبان: ربات مدتی است ضربان نداده است. سرور را بررسی کنید "
                       f"(Diagnose).\n{_s(p.get('reason') or p.get('action'))}")
    if event == "ops.reconcile_mismatch":
        return Message("critical", "reconcile",
                       "⚠️ دفتر ربات با حساب بروکر نمی‌خواند. ربات معاملهٔ تازه باز "
                       "نمی‌کند تا اختلاف روشن شود. داشبورد را ببینید.")
    if event == "config.mode_change":
        return Message("critical", f"mode:{p.get('to')}",
                       f"🔁 حالت ربات تغییر کرد: {MODE_FA.get(p.get('from'), _s(p.get('from')))}"
                       f" ← {MODE_FA.get(p.get('to'), _s(p.get('to')))} (توسط {_s(actor)})")
    if event == "risk.ladder_step":
        tighten = p.get("direction") == "tighten"
        return Message("critical", f"ladder:{p.get('to_rung')}",
                       (f"📉 افت حساب به {_s(p.get('drawdown_pct'))}٪ رسید؛ حجم معامله‌ها "
                        f"کمتر شد (پلهٔ {_s(p.get('to_rung'))})." if tighten else
                        f"📈 حساب بهبود یافت ({_s(p.get('drawdown_pct'))}٪ افت)؛ حجم "
                        f"معامله‌ها یک پله به حالت عادی نزدیک شد."))
    if event == "data.divergence":
        if p.get("reference_block"):
            return Message("critical", f"ref:{p.get('reference_block')}",
                           f"⚠️ قیمت بروکر برای {_s(p.get('reference_block'))} با بازار "
                           f"نمی‌خواند ({_s(p.get('divergence_bp'))} واحد پایه)؛ معاملهٔ "
                           "تازه روی آن متوقف شد.")
        if p.get("reference_block_cleared"):
            return Message("critical", f"ref:{p.get('reference_block_cleared')}:ok",
                           f"✅ قیمت {_s(p.get('reference_block_cleared'))} دوباره با بازار "
                           "هم‌خوان است.")
        return None
    if event == "brain.cooldown":
        if p.get("cleared"):
            return Message("critical", f"cool:{p.get('cleared')}:off",
                           f"✅ استراحت اجباری «{_s(p.get('cleared'))}» زودتر برداشته شد "
                           f"(توسط {_s(actor)}).")
        scope = "کل حساب" if p.get("scope") == "*" else f"استراتژی {_s(p.get('scope'))}"
        return Message("critical", f"cool:{p.get('scope')}",
                       f"😮‍💨 استراحت اجباری برای {scope}: {_s(p.get('losses'))} ضرر پشت سر "
                       "هم. تا پایان استراحت معاملهٔ تازه‌ای باز نمی‌شود — این دقیقاً "
                       "جلوی «معاملهٔ انتقامی» را می‌گیرد.")
    if event == "position.open":
        tgt = f"، هدف {_s(p.get('target'))}" if p.get("target") else ""
        return Message("trades", f"open:{p.get('client_order_id')}",
                       f"📈 معامله باز شد: {SIDE_FA.get(p.get('side'), _s(p.get('side')))} "
                       f"{_s(p.get('instrument'))} — {_s(p.get('lots'))} لات، ورود "
                       f"{_s(p.get('entry'))}، حد ضرر {_s(p.get('stop'))}{tgt}، ریسک "
                       f"{_s(p.get('risk_pct'), 6)}٪ ({_s(p.get('strategy'))})")
    if event == "learn.postmortem":
        outcome = p.get("outcome")
        icon = "✅" if outcome == "win" else "❌" if outcome == "loss" else "➖"
        word = "سود" if outcome == "win" else "ضرر" if outcome == "loss" else "سر به سر"
        return Message("trades", f"close:{p.get('trade_id')}",
                       f"{icon} معامله بسته شد: {_s(p.get('instrument'))} — {word} "
                       f"{_r(p.get('r_multiple'))} ({_s(p.get('strategy'))})")
    if event == "order.rejected":
        return Message("trades", f"reject:{p.get('client_order_id')}",
                       f"⚠️ بروکر سفارش را رد کرد: {_s(p.get('reason'))}")
    if event == "decision.proposal":
        return Message("proposals", f"prop:{p.get('client_order_id')}",
                       f"💡 پیشنهاد معامله: {SIDE_FA.get(p.get('side'), _s(p.get('side')))} "
                       f"{_s(p.get('instrument'))} — {_s(p.get('lots'))} لات، حد ضرر "
                       f"{_s(p.get('stop'))} ({_s(p.get('strategy'))}). برای تأیید به "
                       "داشبورد بروید.")
    if event == "learn.param_proposal":
        return Message("learning", f"param:{p.get('id') or p.get('path')}",
                       f"💡 ربات پیشنهاد تغییر تنظیمات داد: {_s(p.get('path'))} "
                       f"{_s(p.get('current_value'))} ← {_s(p.get('proposed_value'))}. "
                       "بدون تأیید شما و آزمون، اعمال نمی‌شود.")
    if event == "brain.drift":
        if p.get("alarm"):
            return Message("learning", f"drift:{p.get('strategy')}:on",
                           f"🧠 عملکرد {_s(p.get('strategy'))} از حالت عادی‌اش افت کرده "
                           "(آزمون CUSUM)؛ حجم معامله‌هایش خودکار کم شد تا وضعیت روشن شود.")
        return Message("learning", f"drift:{p.get('strategy')}:off",
                       f"🧠 عملکرد {_s(p.get('strategy'))} به حالت عادی برگشت؛ حجم عادی شد.")
    if event == "brain.lab":
        lines = ["🧪 آزمایشگاه شبانه تمام شد:"]
        for s in (p.get("strategies") or [])[:12]:
            status = STATUS_FA.get(s.get("status"), _s(s.get("status")))
            lines.append(f"• {_s(s.get('strategy'))}: {status}"
                         f" (میانگین {_r(s.get('mean_r'))}، {_s(s.get('n'))} معامله)")
        if p.get("meta_candidate"):
            lines.append("• یک فیلتر تازهٔ «بگیرم یا نه» آماده است و منتظر تأیید شماست.")
        return Message("learning", "lab", "\n".join(lines))
    if event == "brain.report":
        t = p.get("trades") or {}
        lines = [f"📋 گزارش هفتگی ({_s(p.get('week_ending'))}):",
                 f"• معامله‌ها: {_s(t.get('n'))}، مجموع {_r(t.get('sum_r'))}، "
                 f"درصد برد {_s(round(float(t.get('win_rate') or 0) * 100))}٪"]
        for rule in ((p.get("shadow") or {}).get("rules") or [])[:4]:
            verdict = {"helped": "مفید بود", "hurt": "هزینه داشت", "unclear": "نامشخص",
                       "insufficient": "دادهٔ کم"}.get(rule.get("verdict"), "")
            lines.append(f"• قاعدهٔ {_s(rule.get('rule'))}: {_s(rule.get('n'))} سیگنال رد شد، "
                         f"میانگین {_r(rule.get('mean_r'))} — {verdict}")
        return Message("learning", f"weekly:{p.get('week_ending')}", "\n".join(lines))
    if event == "brain.model":
        if p.get("approved"):
            return Message("learning", "model",
                           "🧠 فیلتر «بگیرم یا نه» فعال شد (با تأیید مالک).")
        if p.get("retired"):
            return Message("learning", "model:off", "🧠 فیلتر «بگیرم یا نه» غیرفعال شد.")
        return None
    if event == "sec.write_denied":
        return Message("security", f"deny:{p.get('username')}:{p.get('reason')}",
                       f"🔐 یک تغییر رد شد: «{_s(p.get('action'))}» توسط "
                       f"{_s(p.get('username'))} ({_s(p.get('reason'))}).")
    return None
