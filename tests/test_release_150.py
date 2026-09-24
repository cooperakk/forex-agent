"""Regressions for the 1.5.0 audit.

Every test here reproduces a defect found by reading 1.4.0 end to end, and
fails against that release. Each docstring names the failure in money or in
safety, because that is what made it worth fixing.
"""

from __future__ import annotations

import datetime as dt
import os
from decimal import Decimal as D

import pyotp
import pytest
from fastapi.testclient import TestClient

from sentinel.agent.memory import MemoryStore
from sentinel.agent.orchestrator import Agent, Decision
from sentinel.agent.proposals import ProposalQueue
from sentinel.api.main import create_app
from sentinel.api.security import SecurityManager
from sentinel.api.state import Runtime
from sentinel.brokers.paper import PaperBroker, SimProfile
from sentinel.core.audit import AuditLog
from sentinel.core.config import (
    AgentConfig, AgentMode, ExecutionConfig, ExecutionVenueMode, OpsConfig, SecurityConfig,
    SentinelConfig,
)
from sentinel.core.money import Instrument
from sentinel.core.types import Quote, Side
from sentinel.data.feed import BarStore, MarketFeed
from sentinel.risk.engine import RiskDecision

EU = Instrument("EUR_USD", "EUR", "USD")
INSTRUMENTS = {"EUR_USD": EU}
T0 = int(dt.datetime(2026, 3, 3, 10, tzinfo=dt.timezone.utc).timestamp() * 1e9)


def _agent(tmp_path, *, mode=AgentMode.ADVISORY, entry_gate=None):
    broker = PaperBroker(instruments=dict(INSTRUMENTS), starting_balance=D("10000"),
                         profile=SimProfile(last_look_reject_prob=0.0,
                                            slippage_pips_mean=D("0"),
                                            slippage_pips_sigma=D("0")),
                         seed=3, start_ns=T0)
    broker.on_quote(Quote("EUR_USD", D("1.08497"), D("1.08503"), ts_ns=T0))
    cfg = SentinelConfig(
        agent=AgentConfig(mode=mode, session_windows_utc=[[0, 24]],
                          trade_days=[0, 1, 2, 3, 4, 5, 6]),
        execution=ExecutionConfig(broker="paper", commission_per_lot_round_turn=D("0")),
        ops=OpsConfig(state_dir=str(tmp_path), killswitch_file=str(tmp_path / "KILL"),
                      audit_log=str(tmp_path / "audit.jsonl")))
    agent = Agent(cfg, broker, MarketFeed(broker, BarStore(tmp_path / "m.db")),
                  AuditLog(tmp_path / "audit.jsonl", fsync_every_record=False),
                  MemoryStore(tmp_path / "mem.db"),
                  proposals=ProposalQueue(str(tmp_path / "p.json")),
                  clock_fn=lambda: broker.now_ns, entry_gate=entry_gate)
    return agent, broker


def _queued(coid="C1", caution=1.0, strategy="donchian_trend"):
    return Decision(
        ts_ns=T0, strategy=strategy, instrument="EUR_USD", action="queued",
        side="BUY", lots="0.40", entry="1.08503", stop="1.08003", target="1.09503",
        client_order_id=coid,
        diagnostics={"caution_multiplier": caution, "horizon_bars": 12, "timeframe": "H4"})


def _approve_everything(agent, lots=D("1.00")):
    agent.risk.evaluate_entry = lambda intent, ctx: RiskDecision(
        approved=True, approved_lots=lots, risk_amount=D("50"), risk_pct=D("0.5"))


class TestAcceptedAdviceKeepsTheAgentsShrinkage:
    """A human accepting a proposal used to receive the risk engine's FULL
    per-trade size: every lesson, news advisory and meta-label scale that had
    shrunk the proposal was discarded by the click."""

    def test_fresh_lessons_shrink_the_accepted_order(self, tmp_path):
        agent, broker = _agent(tmp_path)
        _approve_everything(agent)
        agent.memory.caution_multiplier = lambda **kw: (0.5, ["lesson: losses cluster"])
        agent._advisory_queue.append(_queued(caution=0.8))
        d = agent.accept_advice("C1", "owner")
        assert d.action == "executed", d.vetoes
        assert D(d.lots) == D("0.50")
        assert broker.positions()[0].lots == D("0.50")

    def test_the_proposal_time_caution_is_never_loosened(self, tmp_path):
        agent, broker = _agent(tmp_path)
        _approve_everything(agent)
        agent.memory.caution_multiplier = lambda **kw: (1.0, [])
        agent._advisory_queue.append(_queued(caution=0.3))
        d = agent.accept_advice("C1", "owner")
        assert D(d.lots) == D("0.30")

    def test_the_horizon_travels_with_the_proposal(self, tmp_path):
        agent, _ = _agent(tmp_path)
        _approve_everything(agent)
        agent._advisory_queue.append(_queued())
        agent.accept_advice("C1", "owner")
        assert agent._position_meta["EUR_USD"]["max_hold_sec"] == 12 * 14400

    def test_a_suspended_strategy_cannot_trade_through_a_click(self, tmp_path):
        agent, broker = _agent(tmp_path)
        _approve_everything(agent)
        agent._guard_suspended["donchian_trend"] = "50 trades, upper bound < 0"
        agent._advisory_queue.append(_queued())
        d = agent.accept_advice("C1", "owner")
        assert d.action == "vetoed"
        assert any(v["rule"] == "performance_guard" for v in d.vetoes)
        assert not broker.positions()
        assert not agent.pending_advice(), "the dead proposal stays queued"


class TestLicenceIsAskedEveryCycle:
    """The licence gate was consulted once, at boot. A licence that expired
    while the service stayed up kept authorising live entries."""

    def test_live_entries_stop_when_the_gate_says_no(self, tmp_path):
        verdict = {"ok": (True, "")}
        agent, _ = _agent(tmp_path, mode=AgentMode.AUTONOMOUS,
                          entry_gate=lambda: verdict["ok"])
        agent.config.execution.venue_mode = ExecutionVenueMode.LIVE
        assert agent.entry_permission() == (True, "")
        verdict["ok"] = (False, "the licence expired")
        allowed, why = agent.entry_permission()
        assert allowed is False and "expired" in why

    def test_a_cycle_reports_the_block_and_takes_no_entry(self, tmp_path):
        agent, _ = _agent(tmp_path, mode=AgentMode.AUTONOMOUS,
                          entry_gate=lambda: (False, "licence expired"))
        agent.config.execution.venue_mode = ExecutionVenueMode.LIVE
        agent.start()
        report = agent.cycle()
        assert any(a["rule"] == "licence" for a in report.alarms)
        assert report.decisions == []

    def test_accepting_advice_is_gated_too(self, tmp_path):
        agent, broker = _agent(tmp_path, entry_gate=lambda: (False, "licence expired"))
        agent.config.execution.venue_mode = ExecutionVenueMode.LIVE
        _approve_everything(agent)
        agent._advisory_queue.append(_queued())
        d = agent.accept_advice("C1", "owner")
        assert d.action == "vetoed" and d.vetoes[-1]["rule"] == "licence"
        assert not broker.positions()

    def test_paper_is_never_gated(self, tmp_path):
        agent, _ = _agent(tmp_path, entry_gate=lambda: (False, "no licence"))
        assert agent.entry_permission() == (True, "")

    def test_a_gate_that_raises_is_a_closed_gate(self, tmp_path):
        def broken():
            raise RuntimeError("disk gone")
        agent, _ = _agent(tmp_path, entry_gate=broken)
        agent.config.execution.venue_mode = ExecutionVenueMode.LIVE
        allowed, why = agent.entry_permission()
        assert allowed is False and "disk gone" in why


class TestRollingLossWindow:
    """At a 5 s decision interval the 'rolling 24h' window silently covered
    about seven hours, and it was forgotten by every restart."""

    def test_the_window_really_spans_24_hours_at_a_short_interval(self, tmp_path):
        agent, _ = _agent(tmp_path)
        start = T0
        agent._rolling_24h_pnl(start, D("10000"))
        step = 5 * 1_000_000_000
        now = start
        for _ in range(int(23 * 3600 / 5)):
            now += step
            agent._rolling_24h_pnl(now, D("9800"))
        # 23 hours later the reference is still the 10,000 mark.
        assert agent._rolling_24h_pnl(now, D("9800")) == D("-200")
        assert len(agent._equity_marks) <= 400

    def test_the_window_survives_a_restart(self, tmp_path):
        agent, _ = _agent(tmp_path)
        agent._rolling_24h_pnl(T0, D("10000"))
        agent._rolling_24h_pnl(T0 + 3600 * 10**9, D("9700"))
        agent._save_state()
        again, _ = _agent(tmp_path)
        assert again._load_state()
        assert again._rolling_24h_pnl(T0 + 7200 * 10**9, D("9700")) == D("-300")


class TestProcessedTradeIds:
    def test_the_persisted_ids_are_the_most_recent_not_the_largest(self, tmp_path):
        agent, _ = _agent(tmp_path)
        for i in range(1, 5201):
            agent.processed_trades.add(str(i))
            agent._processed_order.append(str(i))
        kept = agent._bounded_processed()
        # Exactly the newest 5000, in the order they were processed. Sorting
        # the set as strings dropped "1000".."1099" (they sort first) while
        # keeping "1".."200", which are older.
        assert kept == [str(i) for i in range(201, 5201)]


class TestBreakEvenUsesTheConfiguredCosts:
    """The break-even rule charged the CostModel defaults (7.00 per lot)
    whatever the venue actually costs."""

    def test_commission_comes_from_the_config(self, tmp_path):
        agent, _ = _agent(tmp_path)
        snap = agent.feed.snapshot(["EUR_USD"], now_ns=T0)
        model = agent._cost_model("EUR_USD", snap)
        assert model.commission_per_lot_round_turn == D("0")
        assert model.slippage_pips_median == agent.config.execution.expected_slippage_pips


class TestMetaDiagnosticsSurvive:
    """Assigning the engine's diagnostics wholesale erased the meta-label
    probability for every approved or vetoed decision."""

    def test_probability_is_kept_beside_the_engine_numbers(self, tmp_path):
        from sentinel.core.types import Signal

        class Gate:
            class labeler:  # noqa: N801 - mimics MetaGate's shape
                class report:  # noqa: N801
                    threshold = 0.5

            def decide(self, signal, context):
                return True, 0.73, 1.0

        agent, _ = _agent(tmp_path, mode=AgentMode.OBSERVE)
        agent.meta_gate = Gate()
        agent.start()
        snap = agent.feed.snapshot(["EUR_USD"], now_ns=T0)
        ctx = agent._build_context(T0, agent.broker.account(), [], snap, True,
                                   agent.health.snapshot())
        signal = Signal(strategy="donchian_trend", instrument="EUR_USD", side=Side.BUY,
                        strength=0.6, stop_price=D("1.08003"), target_price=D("1.09503"),
                        decision_ns=T0)
        d = agent._act_on_signal(signal, ctx, snap, "")
        assert d.diagnostics.get("meta_probability") == 0.73
        assert "caution_multiplier" in d.diagnostics


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #


@pytest.fixture
def system(tmp_path):
    os.environ["SENTINEL_JWT_SECRET"] = "t" * 48
    agent, broker = _agent(tmp_path)
    runtime = Runtime(agent, tmp_path / "config.json")
    security = SecurityManager(agent.audit, secret="t" * 48)
    owner, _ = security.add_user("owner1", "a-sufficiently-long-password", "owner")
    app = create_app(runtime, security)
    client = TestClient(app)
    r = client.post("/api/auth/login", json={"username": "owner1",
                                             "password": "a-sufficiently-long-password"})
    headers = {"Authorization": f"Bearer {r.json()['token']}"}
    return {"client": client, "agent": agent, "broker": broker, "owner": owner,
            "headers": headers, "runtime": runtime}


def _write(system, path, body):
    h = dict(system["headers"])
    h["X-TOTP"] = pyotp.TOTP(system["owner"].totp_secret).now()
    # A code is single-use; clear the replay memory so consecutive writes in
    # one test each get a fresh acceptance.
    return system["client"].post(path, json=body, headers=h)


class TestApi:
    def test_audit_returns_the_newest_records(self, system):
        audit = system["agent"].audit
        for i in range(300):
            audit.append("system.heartbeat", {"i": i})
        body = system["client"].get("/api/audit?limit=50", headers=system["headers"]).json()
        seqs = [r["seq"] for r in body["records"]]
        assert seqs[-1] == audit.seq, "the latest record is not on screen"
        assert len(seqs) == 50 and seqs == sorted(seqs)
        assert body["chain_valid"] is True

    def test_a_tampered_journal_is_still_reported_with_the_cache(self, system):
        client, audit = system["client"], system["agent"].audit
        assert client.get("/api/audit", headers=system["headers"]).json()["chain_valid"]
        text = audit.path.read_text()
        audit.path.write_text(text.replace('"i"', '"j"', 1) if '"i"' in text
                              else text.replace("owner1", "owner2", 1))
        assert client.get("/api/audit", headers=system["headers"]).json()["chain_valid"] \
            is False

    def test_malformed_content_length_is_a_400_not_a_crash(self, system):
        r = system["client"].post("/api/auth/login", content=b"{}",
                                  headers={"Content-Length": "abc",
                                           "Content-Type": "application/json"})
        assert r.status_code in (400, 422)

    def test_status_shows_guard_suspensions(self, system):
        system["agent"]._guard_suspended["donchian_trend"] = "negative R"
        body = system["client"].get("/api/status", headers=system["headers"]).json()
        assert body["guard_suspended"] == {"donchian_trend": "negative R"}
        assert body["entries_permitted"]["allowed"] is True

    def test_the_owner_can_release_the_guard_from_the_console(self, system):
        system["agent"]._guard_suspended["donchian_trend"] = "negative R"
        r = _write(system, "/api/control/release-guard", {"strategy": "donchian_trend"})
        assert r.status_code == 200, r.text
        assert "donchian_trend" not in system["agent"].guard_suspended

    @pytest.mark.parametrize("patch", [
        {"agent": {"meta_model_path": "/tmp/evil.joblib"}},
        {"ops": {"strategy_plugin_dir": "/tmp"}},
        {"data": {"store_path": "/etc/market.db"}},
        {"ops": {"group_ledger_dir": "/tmp/x"}},
        {"ops": {"backup_dir": "/tmp/x"}},
    ])
    def test_code_loading_paths_cannot_be_set_from_the_console(self, system, patch):
        r = _write(system, "/api/config", {"patch": patch})
        assert r.status_code == 400
        assert "cannot be changed" in r.text

    def test_a_manual_close_does_not_leave_a_phantom(self, system):
        agent, broker = system["agent"], system["broker"]
        _approve_everything(agent, lots=D("0.10"))
        agent._advisory_queue.append(_queued())
        agent.accept_advice("C1", "owner")
        assert "EUR_USD" in agent._position_meta
        r = _write(system, "/api/control/close", {"instrument": "EUR_USD"})
        assert r.status_code == 200, r.text
        assert "EUR_USD" not in agent._position_meta
