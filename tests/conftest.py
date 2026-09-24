import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sentinel.core.config import RiskConfig  # noqa: E402
from sentinel.core.money import Instrument  # noqa: E402
from sentinel.core.types import AccountState, Quote  # noqa: E402
from sentinel.risk.engine import RiskContext, RiskEngine  # noqa: E402

MAJORS = {
    "EUR_USD": Instrument("EUR_USD", "EUR", "USD"),
    "GBP_USD": Instrument("GBP_USD", "GBP", "USD"),
    "AUD_USD": Instrument("AUD_USD", "AUD", "USD"),
    "USD_JPY": Instrument("USD_JPY", "USD", "JPY", pip=D("0.01"), tick=D("0.001")),
    "USD_CHF": Instrument("USD_CHF", "USD", "CHF"),
}

PRICES = {
    "EUR_USD": D("1.08500"), "GBP_USD": D("1.27000"), "AUD_USD": D("0.66000"),
    "USD_JPY": D("150.000"), "USD_CHF": D("0.88000"),
}


@pytest.fixture
def instruments():
    return dict(MAJORS)


@pytest.fixture
def quotes():
    out = {}
    for sym, mid in PRICES.items():
        inst = MAJORS[sym]
        half = inst.pip * D("0.3")
        out[sym] = Quote(sym, inst.round_price(mid - half), inst.round_price(mid + half),
                         ts_ns=1_700_000_000_000_000_000, source="test")
    return out


@pytest.fixture
def account():
    return AccountState(account_id="T1", currency="USD", balance=D("10000"),
                        equity=D("10000"), margin_used=D("0"),
                        margin_available=D("10000"))


@pytest.fixture
def risk_config():
    return RiskConfig()


@pytest.fixture
def engine(risk_config):
    return RiskEngine(risk_config)


@pytest.fixture
def ctx(account, instruments, quotes):
    return RiskContext(
        now_ns=1_700_000_000_000_000_000,
        account=account, positions=[], instruments=instruments, quotes=quotes,
        conversions={"USD": D("1"), "JPY": D("1") / D("150"), "CHF": D("1") / D("0.88")},
        equity_peak=D("10000"), day_start_equity=D("10000"),
        normal_spread_pips={s: D("0.6") for s in MAJORS},
        strategy_lifecycles={"test": "accepted"},
    )
