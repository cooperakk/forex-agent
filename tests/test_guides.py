"""The Persian guides are part of the product: a newcomer follows them click by
click. These tests keep them true to the console they describe, and keep the
rendered pages openable offline."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
PANEL_GUIDE = DOCS / "PANEL-GUIDE-FA.md"
DASHBOARD_SRC = ROOT / "dashboard" / "src"


def _guide() -> str:
    """The panel guide with line wrapping undone, so a label split across two
    source lines still matches."""
    return re.sub(r"\s+", " ", PANEL_GUIDE.read_text(encoding="utf-8"))


def _dashboard_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8")
                     for p in DASHBOARD_SRC.rglob("*.tsx"))


def test_render_guide_embeds_local_images(tmp_path):
    pytest.importorskip("markdown")
    from scripts.render_guide import render

    (tmp_path / "img").mkdir()
    (tmp_path / "img" / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\nnot-really")
    md = tmp_path / "g.md"
    md.write_text("# راهنما\n\nمتن\n\n![عکس صفحه](img/shot.png)\n", encoding="utf-8")
    page = render(md)
    assert "data:image/png;base64," in page
    assert 'src="img/' not in page
    assert "<figcaption>عکس صفحه</figcaption>" in page


def test_render_guide_refuses_a_missing_image(tmp_path):
    pytest.importorskip("markdown")
    from scripts.render_guide import render

    md = tmp_path / "g.md"
    md.write_text("# x\n\n![gone](img/missing.jpg)\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="image not found"):
        render(md)


def test_every_image_a_guide_shows_exists():
    missing = []
    for md in DOCS.glob("*.md"):
        for ref in re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", md.read_text(encoding="utf-8")):
            if not re.match(r"(?i)https?:", ref) and not (md.parent / ref).is_file():
                missing.append(f"{md.name}: {ref}")
    assert not missing, "guides point at images that do not exist: " + ", ".join(missing)


def test_panel_guide_names_every_page_of_the_console():
    """A page renamed in the menu must be renamed in the guide too."""
    app = (DASHBOARD_SRC / "App.tsx").read_text(encoding="utf-8")
    labels = re.findall(r'\{ id: "[a-z]+", label: "([^"]+)"', app)
    assert len(labels) >= 18
    guide = _guide()
    absent = [label for label in labels if label not in guide]
    assert not absent, "pages the panel guide never mentions: " + ", ".join(absent)


# Buttons the guide tells a newcomer to press, word for word.
_BUTTONS = [
    "برداشتن توقف اضطراری", "توقف فوری همه‌چیز", "تأیید و اجرا", "تازه‌سازی",
    "بستن همه معامله‌ها", "پیش‌نمایش", "ثبت معامله", "ذخیره تغییرات",
    "افزودن دستی", "آزمایش اتصال", "فعال کن", "جست‌وجو کن",
    "پیدا کردن شناسه گفت‌وگو", "پیام آزمایشی", "ساخت گزارش تازه",
    "برداشتن زودتر", "بفرست برای آزمایش", "آزاد کردن", "افزودن کاربر",
    "کد دومرحله‌ای", "دریافت تازه",
]


def test_panel_guide_buttons_exist_in_the_dashboard():
    guide = _guide()
    ui = _dashboard_text()
    for label in _BUTTONS:
        assert label in guide, f"the guide no longer mentions «{label}»; update _BUTTONS"
        assert label in ui, f"the guide tells people to press «{label}», which is not in the dashboard"


def test_panel_guide_trading_hours_match_the_default_session():
    """The guide gives the hours in Tehran time (UTC+03:30, no daylight saving
    since 2022); they must follow the configured default window."""
    from sentinel.core.config import AgentConfig

    windows = AgentConfig().session_windows_utc
    assert len(windows) == 1
    start, end = windows[0]
    fa = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

    def tehran(hour_utc: int) -> str:
        minutes = (hour_utc * 60 + 210) % (24 * 60)
        return f"{minutes // 60:02d}:{minutes % 60:02d}".lstrip("0").translate(fa)

    phrase = f"{tehran(start)} تا {tehran(end)}"
    assert phrase in _guide(), phrase


def test_panel_guide_risk_numbers_match_the_defaults():
    from sentinel.core.config import AgentConfig, RiskConfig

    risk = RiskConfig()
    guide = _guide()
    fa = str.maketrans("0123456789.", "۰۱۲۳۴۵۶۷۸۹٫")

    def pct(value) -> str:
        text = format(value.normalize(), "f")
        return f"{text.translate(fa)}٪"

    for value in (risk.risk_per_trade_pct, risk.daily_loss_limit_pct,
                  risk.weekly_loss_limit_pct, risk.monthly_loss_limit_pct,
                  risk.max_drawdown_halt_pct, risk.daily_profit_lock_pct):
        assert pct(value) in guide, f"the guide does not state the default {pct(value)}"
    envelope = AgentConfig().semi_auto_envelope
    assert all(f"`{i}`" in guide for i in envelope["instruments"])
