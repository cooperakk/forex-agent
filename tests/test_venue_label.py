"""After a demo is connected, tested and activated, the console must say so --
and nothing on that path may ever move the engine to real money.

Before 1.8.2:
* activation changed only ``execution.broker``, so ``venue_mode`` stayed
  ``paper`` and every label read «تمرینی — شبیه‌ساز» on an Alpari demo;
* the engine bound itself to no account, and a real-money account under a
  non-live configuration traded with every live-only gate switched off;
* and no MetaTrader connection with an account number could be activated at
  all: the stored probe keeps the number masked, and the gate compared the
  mask ("•••••678") with the full number.
"""

from __future__ import annotations

import datetime as dt
import sys
from decimal import Decimal as D

import pytest

from sentinel.brokers import get_profile
from sentinel.brokers.bound import AccountBoundBroker
from sentinel.brokers.connection import (
    BrokerConnection, _probe_summary, account_fingerprint, activation_blockers,
)
from sentinel.brokers.mt5 import MT5Broker
from sentinel.core.config import ExecutionVenueMode, SentinelConfig
from sentinel.core.errors import BrokerError
from tests.fake_mt5 import FakeMT5
from tests.test_api import auth, system  # noqa: F401  (the API fixture)

UTC = dt.timezone.utc


def _now_ns() -> int:
    return int(dt.datetime.now(UTC).timestamp() * 1e9)


def _summary(account_id: str, currency: str = "USD", account_type: str = "demo",
             ok: bool = True) -> dict:
    """What the store persists after a probe, built by the store's own code."""
    return _probe_summary({
        "ok": ok, "finished_ns": _now_ns(), "blocking_failures": [],
        "account": {"account_id": account_id, "currency": currency,
                    "account_type": account_type},
        "checks": [],
    })


@pytest.fixture
def mt5(monkeypatch):
    def make(**terminal):
        fake = FakeMT5(**terminal)
        monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
        return MT5Broker(profile=get_profile("generic_mt5"), mt5_module=fake), fake
    return make


# --------------------------------------------------------------------------- #
# the probe of a real account now lets the account be activated
# --------------------------------------------------------------------------- #

class TestTheStoredProbeMatchesItsAccount:
    def test_a_probe_of_the_same_account_does_not_block(self):
        conn = BrokerConnection(id="alp-1", display_name="A", profile="alpari",
                                login="12345678", server="Alpari-MT5-Demo")
        conn.last_probe = _summary("12345678")
        assert "•" in conn.last_probe["account_id"], "the summary still masks the number"
        assert not any("حساب دیگری" in r for r in activation_blockers(conn, open_positions=0))

    def test_the_number_may_be_typed_with_spaces(self):
        conn = BrokerConnection(id="alp-1", display_name="A", profile="alpari",
                                login="12 345 678")
        conn.last_probe = _summary("12345678")
        assert activation_blockers(conn, open_positions=0) == []

    def test_a_probe_of_another_account_still_blocks(self):
        conn = BrokerConnection(id="alp-1", display_name="A", profile="alpari",
                                login="12345678")
        conn.last_probe = _summary("12345679")
        assert any("حساب دیگری" in r for r in activation_blockers(conn, open_positions=0))

    def test_a_summary_written_before_the_fingerprint_is_compared_masked(self):
        conn = BrokerConnection(id="alp-1", display_name="A", profile="alpari",
                                login="12345678")
        old = _summary("12345678")
        old.pop("account_ref")
        conn.last_probe = old
        assert activation_blockers(conn, open_positions=0) == []
        conn.login = "99999999"
        assert any("حساب دیگری" in r for r in activation_blockers(conn, open_positions=0))

    def test_the_fingerprint_is_not_the_number(self):
        ref = account_fingerprint("12345678")
        assert ref and "12345678" not in ref
        assert account_fingerprint("0012345678") == ref
        assert account_fingerprint("") == ""


# --------------------------------------------------------------------------- #
# the binding: a non-live configuration never trades real money
# --------------------------------------------------------------------------- #

class TestModeBinding:
    def test_a_paper_binding_refuses_a_live_terminal(self, mt5):
        broker, fake = mt5()
        fake.account.trade_mode = 2          # MT5: real account
        with pytest.raises(BrokerError, match="live"):
            AccountBoundBroker(broker, "", "paper", "", strict_start=False)

    def test_a_mode_only_binding_does_not_check_identity_or_currency(self, mt5):
        broker, fake = mt5()
        bound = AccountBoundBroker(broker, "", "demo", "")
        fake.account.login = 2000002
        assert bound.account().account_id == "2000002"

    def test_an_unreadable_account_does_not_fail_the_boot_but_every_call_checks(self):
        class Offline:
            capabilities = None

            def account(self):
                raise BrokerError("account_info unavailable")

            def quote(self, *_):
                return "q"

        bound = AccountBoundBroker(Offline(), "", "paper", "", strict_start=False)
        with pytest.raises(BrokerError, match="unavailable"):
            bound.quote("EUR_USD")
        with pytest.raises(BrokerError):
            AccountBoundBroker(Offline(), "1", "demo", "USD")   # strict: a declared account

    def test_bootstrap_guards_an_unbound_external_venue(self, mt5, tmp_path, monkeypatch):
        import sentinel.bootstrap as bs
        from sentinel.bootstrap import build_runtime
        broker, fake = mt5()
        monkeypatch.setattr(bs, "build_broker",
                            lambda *a, **k: MT5Broker(profile=get_profile("generic_mt5"),
                                                      mt5_module=fake))
        cfg = SentinelConfig()
        cfg.execution.broker = "generic_mt5"
        cfg.ops.state_dir = str(tmp_path)
        cfg.ops.audit_log = str(tmp_path / "a.jsonl")
        cfg.ops.killswitch_file = str(tmp_path / "KILL")
        cfg.data.store_path = str(tmp_path / "m.db")
        cfg.save(tmp_path / "config.json")
        runtime, _ = build_runtime(tmp_path / "config.json")
        assert isinstance(runtime.agent.broker, AccountBoundBroker)
        assert runtime.agent.broker.bound_to["account_type"] == "paper"
        assert runtime.agent.broker.bound_to["account_id"] == ""

        fake.account.trade_mode = 2
        with pytest.raises(BrokerError, match="live"):
            build_runtime(tmp_path / "config.json")


# --------------------------------------------------------------------------- #
# activation
# --------------------------------------------------------------------------- #

def _conn(runtime, **kw) -> BrokerConnection:
    base = dict(id="alp-1", display_name="Alpari demo", profile="alpari",
                login="12345678", server="Alpari-MT5-Demo",
                declared_account_type="demo")
    base.update(kw)
    conn = BrokerConnection(**base)
    conn.last_probe = _summary(base["login"] or "", account_type=base["declared_account_type"])
    runtime.connections.upsert(conn)
    return conn


class TestActivation:
    def test_a_demo_is_bound_and_labelled_demo_after_the_restart(self, system):
        runtime = system["runtime"]
        _conn(runtime)
        out = runtime.activate_connection("alp-1", by="owner1")
        assert out["restart_required"] is True
        ex = runtime.agent.config.execution
        assert ex.broker == "alpari"
        assert ex.venue_mode is ExecutionVenueMode.DEMO
        assert ex.expected_account_id == "12345678"
        assert ex.expected_account_server == "Alpari-MT5-Demo"
        assert ex.account_currency == "USD"
        # Until the restart the simulator is still what runs, and the label
        # says so -- not the configuration's new claim.
        assert runtime.effective_venue() == {"venue": "paper", "source": "simulator",
                                             "configured": "demo"}

    def test_a_cent_account_is_bound_in_its_own_currency(self, system):
        runtime = system["runtime"]
        conn = BrokerConnection(id="alp-c", display_name="Cent", profile="alpari",
                                login="7654321", server="Alpari-MT5-Demo",
                                declared_account_type="demo")
        conn.last_probe = _summary("7654321", currency="USC")
        runtime.connections.upsert(conn)
        runtime.activate_connection("alp-c", by="owner1")
        assert runtime.agent.config.execution.account_currency == "USC"

    def test_a_live_declared_connection_never_moves_the_mode_to_live(self, system):
        runtime = system["runtime"]
        conn = BrokerConnection(id="alp-l", display_name="Live", profile="alpari",
                                login="5550001", server="Alpari-MT5-Live",
                                declared_account_type="live")
        conn.last_probe = _summary("5550001", account_type="live")
        patch, allow = runtime._activation_patch(conn)
        assert "venue_mode" not in patch["execution"]
        assert ("execution", "venue_mode") not in allow
        assert patch["execution"]["expected_account_id"] == "5550001"

    def test_activating_the_simulator_clears_the_binding(self, system):
        runtime = system["runtime"]
        _conn(runtime)
        runtime.activate_connection("alp-1", by="owner1")
        sim = BrokerConnection(id="sim-1", display_name="S", profile="paper",
                               declared_account_type="demo")
        sim.last_probe = _summary("PAPER-001")
        runtime.connections.upsert(sim)
        patch, _ = runtime._activation_patch(sim)
        assert patch["execution"]["venue_mode"] == "paper"
        assert patch["execution"]["expected_account_id"] == ""


# --------------------------------------------------------------------------- #
# the label
# --------------------------------------------------------------------------- #

class TestEffectiveVenue:
    def test_the_status_reports_what_is_running(self, system):
        headers = auth(system["client"], "owner1", "a-sufficiently-long-password")
        status = system["client"].get("/api/status", headers=headers).json()
        assert status["venue_effective"] == "paper"
        assert status["venue_source"] == "simulator"
        brokers = system["client"].get("/api/brokers", headers=headers).json()
        assert brokers["venue_effective"] == "paper"

    @pytest.mark.parametrize("trade_mode,expected", [(0, "demo"), (1, "demo"), (2, "live")])
    def test_an_external_venue_is_labelled_by_the_account_itself(self, system, mt5,
                                                                trade_mode, expected):
        runtime = system["runtime"]
        broker, fake = mt5()
        fake.account.trade_mode = trade_mode
        runtime.agent.broker = broker
        assert runtime.agent.config.execution.venue_mode is ExecutionVenueMode.PAPER
        view = runtime.effective_venue(broker.account())
        assert view == {"venue": expected, "source": "account", "configured": "paper"}

    def test_a_venue_that_does_not_say_falls_back_to_the_configuration(self, system):
        runtime = system["runtime"]

        class Quiet:
            capabilities = None

        runtime.agent.broker = Quiet()
        assert runtime.effective_venue() == {"venue": "paper", "source": "config",
                                             "configured": "paper"}
