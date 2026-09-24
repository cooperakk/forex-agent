"""Regressions for the defects found in the adversarial audit.

Each test reproduces the original failure. They exist so the fix cannot be
quietly undone: every one of these bugs passed the rest of the suite.
"""

import datetime as dt
import json
from decimal import Decimal as D

import numpy as np
import pandas as pd
import pytest

from sentinel.agent.memory import MemoryStore
from sentinel.agent.orchestrator import Agent
from sentinel.agent.proposals import ProposalQueue
from sentinel.brokers.paper import PaperBroker, SimProfile
from sentinel.core.audit import AuditLog
from sentinel.core.config import (
    AgentConfig, AgentMode, ExecutionConfig, OpsConfig, RiskConfig, SentinelConfig,
    StrategyAllocation,
)
from sentinel.core.ids import client_order_id
from sentinel.core.money import Instrument
from sentinel.core.types import (
    AccountState, OrderIntent, OrderState, Position, Quote, Side,
)
from sentinel.data.feed import BarStore, MarketFeed, bars_from_frame
from sentinel.data.synthetic import DEFAULT_UNIVERSE, generate_universe
from sentinel.risk.engine import RiskContext, RiskEngine, Severity

EU = Instrument("EUR_USD", "EUR", "USD")
UJ = Instrument("USD_JPY", "USD", "JPY", pip=D("0.01"), tick=D("0.001"))
INSTRUMENTS = {"EUR_USD": EU, "GBP_USD": Instrument("GBP_USD", "GBP", "USD"),
               "AUD_USD": Instrument("AUD_USD", "AUD", "USD"), "USD_JPY": UJ,
               "USD_CHF": Instrument("USD_CHF", "USD", "CHF")}


def ctx_for(account_ccy="USD", conversions=None, **kw):
    acct = AccountState("A1", account_ccy, D("10000"), D("10000"),
                        margin_available=D("10000"))
    q = {"EUR_USD": Quote("EUR_USD", D("1.08497"), D("1.08503"), ts_ns=1),
         "USD_JPY": Quote("USD_JPY", D("149.997"), D("150.003"), ts_ns=1)}
    base = dict(now_ns=1_700_000_000_000_000_000, account=acct, positions=[],
                instruments=INSTRUMENTS, quotes=q,
                conversions=conversions if conversions is not None else {"USD": D("1")},
                equity_peak=D("10000"), day_start_equity=D("10000"),
                normal_spread_pips={s: D("0.6") for s in INSTRUMENTS},
                strategy_lifecycles={"t": "accepted"})
    base.update(kw)
    return RiskContext(**base)


def intent(symbol="EUR_USD", stop=D("1.0820"), target=D("1.0910"), side=Side.BUY):
    return OrderIntent(client_order_id="C1", strategy="t", instrument=symbol,
                       side=side, lots=D("0.1"), stop_loss=stop, take_profit=target)


class TestMissingConversionFailsClosed:
    """#1 — a missing quote->account rate was silently substituted with 1.0,
    which on a JPY-denominated account sized the position 150x too large."""

    def test_a_missing_rate_blocks_the_entry(self):
        engine = RiskEngine(RiskConfig())
        # USD_JPY on a USD account with no JPY->USD rate available.
        d = engine.evaluate_entry(
            intent("USD_JPY", stop=D("149.00"), target=D("152.00")),
            ctx_for(conversions={"USD": D("1")}))
        assert not d.approved
        assert "missing_conversion" in {v.rule for v in d.vetoes}

    def test_the_rate_is_used_when_present(self):
        engine = RiskEngine(RiskConfig())
        d = engine.evaluate_entry(
            intent("USD_JPY", stop=D("149.00"), target=D("152.00")),
            ctx_for(conversions={"USD": D("1"), "JPY": D("1") / D("150")}))
        assert d.approved, [v.to_dict() for v in d.vetoes]
        # 0.5% of 10,000 over a 100-pip stop at ~$6.67/pip/lot => ~0.07 lots.
        assert D("0.04") <= d.approved_lots <= D("0.10")

    def test_the_account_currency_needs_no_rate(self):
        engine = RiskEngine(RiskConfig())
        d = engine.evaluate_entry(intent("EUR_USD"), ctx_for(conversions={}))
        assert "missing_conversion" not in {v.rule for v in d.vetoes}


class TestTotalOpenRiskIsEnforced:
    """#5 — pending and open risk were summed, formatted into the diagnostics,
    and never compared to a limit."""

    def test_in_flight_risk_counts(self):
        engine = RiskEngine(RiskConfig())
        d = engine.evaluate_entry(intent(), ctx_for(pending_risk=D("1000000")))
        assert not d.approved and "total_open_risk" in {v.rule for v in d.vetoes}

    def test_open_positions_count(self):
        # A coherent pair: two positions at 0.5% fill a 1.0% total ceiling.
        engine = RiskEngine(RiskConfig(max_total_open_risk_pct=D("1.0"),
                                       max_open_positions=2))
        positions = [Position("GBP_USD", Side.BUY, D("0.1"), D("1.27"), 0,
                              initial_risk=D("50"), broker_stop_confirmed=True),
                     Position("AUD_USD", Side.BUY, D("0.1"), D("0.66"), 0,
                              initial_risk=D("50"), broker_stop_confirmed=True)]
        d = engine.evaluate_entry(intent(), ctx_for(positions=positions))
        assert "total_open_risk" in {v.rule for v in d.vetoes}


class TestConfigCoherence:
    """#H — a total-risk ceiling below risk_per_trade x max_open_positions makes
    the position cap unreachable: four configured, three delivered."""

    def test_an_unreachable_position_cap_is_refused(self):
        with pytest.raises(ValueError, match="can never be reached"):
            RiskConfig(risk_per_trade_pct=D("0.50"), max_open_positions=4,
                       max_total_open_risk_pct=D("1.60"))

    def test_the_defaults_are_coherent(self):
        cfg = RiskConfig()
        assert (cfg.max_total_open_risk_pct
                >= cfg.risk_per_trade_pct * D(cfg.max_open_positions))


class TestBlockingAlarmsStopNewRisk:
    """#12 — an unprotected open position raised a BLOCK alarm that nothing
    acted on, so a fresh entry was approved beside a naked position."""

    def test_an_unprotected_position_blocks_new_entries(self):
        engine = RiskEngine(RiskConfig())
        naked = [Position("GBP_USD", Side.BUY, D("0.05"), D("1.27"), 0,
                          broker_stop_confirmed=False)]
        alarms = engine.portfolio_alarms(ctx_for(positions=naked))
        assert any(a.rule == "unprotected_position" and a.severity is Severity.BLOCK
                   for a in alarms)
        d = engine.evaluate_entry(intent(), ctx_for(positions=naked))
        assert not d.approved
        assert {"unprotected_book", "portfolio_alarm"} & {v.rule for v in d.vetoes}


class TestRegimeMultiplierIsApplied:
    """#20 — the stress-regime risk multiplier was computed, displayed, and
    never applied to sizing."""

    def test_stress_shrinks_the_position(self):
        engine = RiskEngine(RiskConfig())
        normal = engine.evaluate_entry(intent(), ctx_for())
        stressed = engine.evaluate_entry(
            intent(), ctx_for(regime_risk_multiplier=D("0.35")))
        assert normal.approved and stressed.approved
        assert stressed.approved_lots < normal.approved_lots
        assert stressed.risk_amount < normal.risk_amount


class TestOrderIdSurvivesRestart:
    """#10 — the key mixed in a per-process run id and a wall-clock timestamp,
    so a crash-and-restart re-derived a different key and the venue could not
    reject the duplicate."""

    def test_same_intent_same_key_across_processes(self, monkeypatch):
        import sentinel.core.ids as ids
        a = client_order_id(strategy="don", instrument="EUR_USD", side="BUY",
                            decision_ns=1_700_000_000_000_000_000, account="A1")
        monkeypatch.setattr(ids, "RUN_ID", "a-completely-different-process")
        b = client_order_id(strategy="don", instrument="EUR_USD", side="BUY",
                            decision_ns=1_700_000_000_000_000_000, account="A1")
        assert a == b

    def test_a_different_bar_is_a_different_intent(self):
        a = client_order_id(strategy="don", instrument="EUR_USD", side="BUY",
                            decision_ns=1_700_000_000_000_000_000, account="A1")
        b = client_order_id(strategy="don", instrument="EUR_USD", side="BUY",
                            decision_ns=1_700_000_014_400_000_000, account="A1")
        assert a != b


class TestPaperBarPathIsDirectionAware:
    """#11 — the intrabar path was hard-coded O->L->H->C, so on a bar that
    touched both barriers every SHORT was scored as a win and every long as a
    loss. Acceptance evidence was biased in favour of shorts."""

    def _run(self, side: Side):
        t0 = int(dt.datetime(2026, 3, 2, 10, tzinfo=dt.timezone.utc).timestamp() * 1e9)
        b = PaperBroker(instruments={"EUR_USD": EU}, starting_balance=D("10000"),
                        profile=SimProfile(last_look_reject_prob=0.0,
                                           slippage_pips_mean=D("0"),
                                           slippage_pips_sigma=D("0")),
                        seed=1, start_ns=t0)
        b.on_quote(Quote("EUR_USD", D("1.09997"), D("1.10003"), ts_ns=t0))
        sign = 1 if side is Side.BUY else -1
        b.submit(OrderIntent(client_order_id="Z1", strategy="t", instrument="EUR_USD",
                             side=side, lots=D("0.1"),
                             stop_loss=D("1.10000") - D("0.00500") * sign,
                             take_profit=D("1.10000") + D("0.00500") * sign,
                             risk_amount=D("50")))
        t1 = t0 + 3_600_000_000_000
        b.set_time(t1)
        # One bar that reaches both barriers.
        b.on_bar_prices("EUR_USD", D("1.10000"), D("1.10600"), D("1.09400"),
                        D("1.10000"), t1, t1 + 3_600_000_000_000)
        return b.closed_trades[0]

    def test_both_directions_are_scored_as_the_stop(self):
        long_trade = self._run(Side.BUY)
        short_trade = self._run(Side.SELL)
        assert long_trade.exit_reason == "stop_loss"
        assert short_trade.exit_reason == "stop_loss", (
            "a short on an ambiguous bar was scored as a win: the intrabar path "
            "is not direction-aware")
        assert float(long_trade.pnl) < 0 and float(short_trade.pnl) < 0


class TestDurableRiskState:
    """#6 — the equity peak and the period baselines lived only in memory, so a
    restart reset the drawdown ladder and the daily loss budget."""

    def _agent(self, tmp_path, balance=D("10000")):
        cfg = SentinelConfig(
            agent=AgentConfig(mode=AgentMode.OBSERVE),
            execution=ExecutionConfig(broker="paper"),
            ops=OpsConfig(state_dir=str(tmp_path), killswitch_file=str(tmp_path / "KILL"),
                          audit_log=str(tmp_path / "audit.jsonl")))
        broker = PaperBroker(instruments=INSTRUMENTS, starting_balance=balance)
        store = BarStore(tmp_path / "m.db")
        return Agent(cfg, broker, MarketFeed(broker, store),
                     AuditLog(tmp_path / "audit.jsonl", fsync_every_record=False),
                     MemoryStore(tmp_path / "mem.db"),
                     proposals=ProposalQueue(str(tmp_path / "p.json"))), broker

    def test_the_drawdown_budget_survives_a_restart(self, tmp_path):
        first, broker = self._agent(tmp_path)
        first.start()
        first.equity_peak = D("10000")
        broker._balance = D("9200")          # an 8% drawdown
        first._roll_periods(first.now(), D("9200"))
        first.day_start_equity = D("10000")
        first._save_state()
        first.stop()

        second, _ = self._agent(tmp_path, balance=D("9200"))
        second.start()
        assert second.equity_peak == D("10000"), "the peak was reset by the restart"
        assert second.day_start_equity == D("10000")

    def test_a_halt_survives_a_restart(self, tmp_path):
        first, _ = self._agent(tmp_path)
        first.start()
        first.halt("test halt")
        first.stop()
        second, _ = self._agent(tmp_path, balance=D("10000"))
        second.start()
        assert second.halted is True


class TestOneCycleCannotBreachThePositionCap:
    """#2 — the risk context was built once per cycle, so entries 2..N were
    judged against a snapshot that did not contain entries 1..N-1."""

    def test_caps_hold_within_a_single_cycle(self, tmp_path):
        bars, live, per_day = 420, 40, 6
        end = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
        n = bars + live
        universe = generate_universe(DEFAULT_UNIVERSE, n_bars=n, bars_per_day=per_day,
                                     seed=5, dollar_factor_strength=0.6)
        index = pd.date_range(end - dt.timedelta(hours=4 * (n - 1)), periods=n,
                              freq="4h", tz="UTC")
        for df in universe.values():
            df.index = index

        store = BarStore(tmp_path / "m.db")
        for sym, df in universe.items():
            store.upsert(bars_from_frame(df.iloc[:bars], sym, "H4", source="synthetic"))
        broker = PaperBroker(instruments=INSTRUMENTS, starting_balance=D("10000"),
                             start_ns=int(index[0].value))
        broker.set_conversion("JPY", D("1") / D("150"))
        broker.set_conversion("CHF", D("1") / D("0.88"))
        cfg = SentinelConfig(
            agent=AgentConfig(mode=AgentMode.AUTONOMOUS, session_windows_utc=[[0, 24]],
                              trade_days=[0, 1, 2, 3, 4, 5, 6]),
            execution=ExecutionConfig(broker="paper"),
            risk=RiskConfig(max_open_positions=1, min_seconds_between_entries=86400,
                            max_trades_per_day=1),
            ops=OpsConfig(state_dir=str(tmp_path), killswitch_file=str(tmp_path / "KILL"),
                          audit_log=str(tmp_path / "audit.jsonl")),
            strategies=[StrategyAllocation(name="donchian_trend", enabled=True,
                                           instruments=list(INSTRUMENTS),
                                           timeframe="H4", lifecycle="experimental")])
        agent = Agent(cfg, broker, MarketFeed(broker, store, timeframe="H4"),
                      AuditLog(tmp_path / "audit.jsonl", fsync_every_record=False),
                      MemoryStore(tmp_path / "mem.db"),
                      proposals=ProposalQueue(str(tmp_path / "p.json")),
                      clock_fn=lambda: broker.now_ns)
        agent.start()
        worst = 0
        for step in range(live):
            i = bars + step
            ns = int(index[i].value)
            broker.set_time(ns)
            for sym, df in universe.items():
                row = df.iloc[i]
                broker.on_bar_prices(sym, D(repr(float(row["open"]))),
                                     D(repr(float(row["high"]))),
                                     D(repr(float(row["low"]))),
                                     D(repr(float(row["close"]))),
                                     ns, ns + 14_400_000_000_000)
                store.upsert(bars_from_frame(df.iloc[i:i + 1], sym, "H4",
                                             source="synthetic"))
            report = agent.cycle()
            executed = sum(1 for d in report.decisions if d.action == "executed")
            assert executed <= 1, "two entries were approved inside one cycle"
            worst = max(worst, len(broker.positions()))
        agent.stop()
        assert worst <= 1, f"position cap breached: {worst} positions open at once"


class _StubResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _StubOandaClient:
    """Minimal stand-in for httpx.Client covering the endpoints we touch."""

    def __init__(self):
        self.calls = []
        self.stop_price = "1.07900"      # the strategy's own 20-pip stop

    def request(self, method, path, json=None, params=None):
        self.calls.append((method, path, json))
        if path.endswith("/instruments"):
            return _StubResponse({"instruments": [{
                "name": "EUR_USD", "displayPrecision": 5, "pipLocation": -4,
                "minimumTradeSize": "1", "tradeUnitsPrecision": 0,
                "maximumOrderUnits": "100000000", "marginRate": "0.02"}]})
        if path.endswith("/openPositions"):
            return _StubResponse({"positions": [{
                "instrument": "EUR_USD",
                "long": {"units": "10000", "averagePrice": "1.08100",
                         "realizedPL": "0", "financing": "0", "tradeIDs": ["11"]},
                "short": {"units": "0"}}]})
        if path.endswith("/openTrades"):
            return _StubResponse({"trades": [{
                "id": "11", "instrument": "EUR_USD", "currentUnits": "10000",
                "openTime": "2026-09-01T10:00:00.000000000Z",
                "stopLossOrder": {"price": self.stop_price},
                "takeProfitOrder": {"price": "1.09000"}}]})
        if path.endswith("/summary"):
            return _StubResponse({"account": {
                "id": "001", "currency": "USD", "balance": "10000", "NAV": "10000",
                "marginUsed": "200", "marginAvailable": "9800", "unrealizedPL": "0",
                "openPositionCount": 1}, "lastTransactionID": "42"})
        return _StubResponse({})

    def close(self):
        pass


@pytest.fixture
def oanda(monkeypatch):
    from sentinel.brokers.oanda import OandaBroker
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "001")
    monkeypatch.setenv("OANDA_API_TOKEN", "t" * 20)
    client = _StubOandaClient()
    broker = OandaBroker(client=client, environment="practice")
    return broker, client


class TestOandaAdapter:
    """#7, #8, #14 — the reference live adapter could not even be constructed
    against real instrument data, reported every position as unprotected, and
    had no guard against widening a stop."""

    def test_instrument_spec_builds(self, oanda):
        broker, _ = oanda
        inst = broker.instrument("EUR_USD")
        assert inst.lot_step <= inst.min_lot
        assert inst.min_lot > 0

    def test_positions_carry_their_venue_side_stop(self, oanda):
        broker, _ = oanda
        pos = broker.positions()[0]
        assert pos.stop_loss == D("1.07900")
        assert pos.broker_stop_confirmed is True
        assert pos.take_profit == D("1.09000")

    def test_widening_a_stop_is_refused(self, oanda):
        from sentinel.core.errors import PermanentError
        broker, _ = oanda
        with pytest.raises(PermanentError, match="widen"):
            broker.modify_position("EUR_USD", stop_loss=D("1.07600"))

    def test_tightening_a_stop_is_allowed(self, oanda):
        broker, client = oanda
        assert broker.modify_position("EUR_USD", stop_loss=D("1.08050")) is True
        puts = [c for c in client.calls if c[0] == "PUT"]
        assert puts and puts[-1][2]["stopLoss"]["price"] == "1.08050"

    def test_a_protected_position_does_not_trigger_the_repair_path(self, oanda, tmp_path):
        from sentinel.execution.reconcile import Reconciler
        broker, client = oanda
        rec = Reconciler(broker, AuditLog(tmp_path / "a.jsonl", fsync_every_record=False))
        report = rec.reconcile(local_positions=broker.positions())
        assert report.unprotected == []
        # The strategy's stop is untouched: no PUT was issued at all.
        assert not [c for c in client.calls if c[0] == "PUT"]

    def test_protection_can_measure_R_on_a_live_position(self, oanda):
        from sentinel.core.config import RiskConfig
        from sentinel.risk.protect import evaluate_protection
        broker, _ = oanda
        pos = broker.positions()[0]
        pos.initial_risk = D("50")      # supplied by the agent's metadata registry
        q = Quote("EUR_USD", D("1.08700"), D("1.08706"), ts_ns=1)
        acts = evaluate_protection(pos, q, broker.instrument("EUR_USD"), RiskConfig(),
                                   atr=D("0.0020"))
        assert acts, "profit protection is inert on a live position"


class TestPromotionRequiresAStoredVerdict:
    """#9 — the lifecycle field only validated that the run id was a non-empty
    string, so one authenticated config write could mark any strategy accepted
    and switch the venue to live with no acceptance run having been executed."""

    @pytest.fixture
    def runtime(self, tmp_path):
        from sentinel.api.state import Runtime
        cfg = SentinelConfig(
            execution=ExecutionConfig(broker="paper"),
            ops=OpsConfig(state_dir=str(tmp_path), killswitch_file=str(tmp_path / "KILL"),
                          audit_log=str(tmp_path / "audit.jsonl")),
            strategies=[StrategyAllocation(name="donchian_trend", enabled=True,
                                           lifecycle="experimental")])
        broker = PaperBroker(instruments=INSTRUMENTS, starting_balance=D("10000"))
        store = BarStore(tmp_path / "m.db")
        agent = Agent(cfg, broker, MarketFeed(broker, store),
                      AuditLog(tmp_path / "audit.jsonl", fsync_every_record=False),
                      MemoryStore(tmp_path / "mem.db"),
                      proposals=ProposalQueue(str(tmp_path / "p.json")))
        return Runtime(agent, tmp_path / "config.json")

    def test_an_invented_run_id_is_refused(self, runtime):
        with pytest.raises(ValueError, match="not in the verdict store"):
            runtime.update_config({"strategies": [
                {"name": "donchian_trend", "enabled": True, "lifecycle": "accepted",
                 "acceptance_run_id": "whatever"}]}, "attacker")
        assert runtime.agent.config.strategies[0].lifecycle == "experimental"

    def test_a_failed_verdict_is_refused(self, runtime):
        from sentinel.research.acceptance import Gate, Verdict
        runtime.verdicts.record(Verdict(
            run_id="RUN-1", strategy="donchian_trend", created_at_ns=1, accepted=False,
            gates=[Gate("L2", "random walk", False, "p=0.4", "<0.01")],
            data_label="live-quality", summary="failed L2"))
        with pytest.raises(ValueError, match="did NOT pass"):
            runtime.update_config({"strategies": [
                {"name": "donchian_trend", "enabled": True, "lifecycle": "accepted",
                 "acceptance_run_id": "RUN-1"}]}, "owner")

    def test_a_synthetic_data_verdict_is_refused(self, runtime):
        from sentinel.research.acceptance import Verdict
        runtime.verdicts.record(Verdict(
            run_id="RUN-2", strategy="donchian_trend", created_at_ns=1, accepted=True,
            data_label="synthetic", summary="passed on synthetic data"))
        with pytest.raises(ValueError, match="synthetic"):
            runtime.update_config({"strategies": [
                {"name": "donchian_trend", "enabled": True, "lifecycle": "accepted",
                 "acceptance_run_id": "RUN-2"}]}, "owner")

    def test_a_verdict_for_another_strategy_is_refused(self, runtime):
        from sentinel.research.acceptance import Verdict
        runtime.verdicts.record(Verdict(
            run_id="RUN-3", strategy="vol_reversion", created_at_ns=1, accepted=True,
            data_label="live-quality", summary="ok"))
        with pytest.raises(ValueError, match="belongs to"):
            runtime.update_config({"strategies": [
                {"name": "donchian_trend", "enabled": True, "lifecycle": "accepted",
                 "acceptance_run_id": "RUN-3"}]}, "owner")

    def test_a_genuine_verdict_promotes(self, runtime):
        from sentinel.research.acceptance import Verdict
        from sentinel.research.verdicts import config_fingerprint
        alloc = {"name": "donchian_trend", "enabled": True, "lifecycle": "accepted",
                 "acceptance_run_id": "RUN-4", "instruments": ["EUR_USD"],
                 "timeframe": "H4", "params": {"channel": 55}}
        runtime.verdicts.record(
            Verdict(run_id="RUN-4", strategy="donchian_trend", created_at_ns=1,
                    accepted=True, data_label="live-quality", summary="every gate passed"),
            config_hash=config_fingerprint(alloc["instruments"], alloc["params"],
                                           alloc["timeframe"]))
        runtime.update_config({"strategies": [alloc]}, "owner")
        assert runtime.agent.config.strategies[0].lifecycle == "accepted"

    def test_a_verdict_cannot_be_reused_for_a_different_configuration(self, runtime):
        """#9 residual — once accepted, the instruments and parameters could be
        swapped wholesale under the same run id, so the badge was worn by a
        strategy the verdict never evaluated."""
        from sentinel.research.acceptance import Verdict
        from sentinel.research.verdicts import config_fingerprint
        alloc = {"name": "donchian_trend", "enabled": True, "lifecycle": "accepted",
                 "acceptance_run_id": "RUN-5", "instruments": ["EUR_USD"],
                 "timeframe": "H4", "params": {"channel": 55}}
        runtime.verdicts.record(
            Verdict(run_id="RUN-5", strategy="donchian_trend", created_at_ns=1,
                    accepted=True, data_label="live-quality", summary="ok"),
            config_hash=config_fingerprint(["EUR_USD"], {"channel": 55}, "H4",
                                           runtime_config=runtime.agent.config))
        runtime.update_config({"strategies": [alloc]}, "owner")
        swapped = {**alloc, "instruments": ["USD_JPY", "GBP_USD", "AUD_USD"],
                   "params": {"channel": 5}}
        with pytest.raises(ValueError, match="different configuration"):
            runtime.update_config({"strategies": [swapped]}, "owner")
        assert runtime.agent.config.strategies[0].instruments == ["EUR_USD"]

    def test_an_unchanged_accepted_allocation_still_passes(self, runtime):
        from sentinel.research.acceptance import Verdict
        from sentinel.research.verdicts import config_fingerprint
        alloc = {"name": "donchian_trend", "enabled": True, "lifecycle": "accepted",
                 "acceptance_run_id": "RUN-6", "instruments": ["EUR_USD"],
                 "timeframe": "H4", "params": {"channel": 55}}
        runtime.verdicts.record(
            Verdict(run_id="RUN-6", strategy="donchian_trend", created_at_ns=1,
                    accepted=True, data_label="live-quality", summary="ok"),
            config_hash=config_fingerprint(["EUR_USD"], {"channel": 55}, "H4",
                                           runtime_config=runtime.agent.config))
        runtime.update_config({"strategies": [alloc]}, "owner")
        # An edit OUTSIDE the runtime policy (a dashboard session setting) does
        # not touch the fingerprint and needs no fresh run.
        runtime.update_config({"ops": {"log_level": "DEBUG"}}, "owner")
        assert runtime.agent.config.strategies[0].lifecycle == "accepted"
        # An edit INSIDE it -- a stop floor changes which trades happen -- is
        # refused with a message that names the remedy, rather than letting an
        # accepted badge describe a system that was never validated.
        with pytest.raises(ValueError, match="runtime policy"):
            runtime.update_config({"risk": {"min_stop_pips": "12"}}, "owner")
        assert runtime.agent.config.risk.min_stop_pips == D("10")

    @pytest.mark.parametrize("patch,needle", [
        ({"agent": {"mode": "autonomous"}}, "agent.mode"),
        ({"execution": {"venue_mode": "live"}}, "execution.venue_mode"),
        ({"execution": {"broker": "oanda"}}, "execution.broker"),
    ])
    def test_privileged_fields_cannot_be_changed_by_a_config_write(self, runtime,
                                                                   patch, needle):
        with pytest.raises(ValueError, match=needle.replace(".", r"\.")):
            runtime.update_config(patch, "owner")

    def test_an_ordinary_field_still_writes(self, runtime):
        res = runtime.update_config({"risk": {"risk_per_trade_pct": "0.25"}}, "owner")
        assert runtime.agent.config.risk.risk_per_trade_pct == D("0.25")
        assert res["version"] == 2


class TestCloseSemantics:
    """#20 — `lots="0"` was falsy and closed the ENTIRE position, and a flatten
    where every close failed returned the same shape as one that worked."""

    @pytest.fixture
    def runtime(self, tmp_path):
        from sentinel.api.state import Runtime
        cfg = SentinelConfig(
            execution=ExecutionConfig(broker="paper"),
            ops=OpsConfig(state_dir=str(tmp_path), killswitch_file=str(tmp_path / "KILL"),
                          audit_log=str(tmp_path / "audit.jsonl")))
        t0 = int(dt.datetime(2026, 3, 2, 10, tzinfo=dt.timezone.utc).timestamp() * 1e9)
        broker = PaperBroker(instruments=INSTRUMENTS, starting_balance=D("10000"),
                             start_ns=t0)
        broker.on_quote(Quote("EUR_USD", D("1.08497"), D("1.08503"), ts_ns=t0))
        broker.submit(OrderIntent(client_order_id="K1", strategy="t",
                                  instrument="EUR_USD", side=Side.BUY, lots=D("1.00"),
                                  stop_loss=D("1.0800"), risk_amount=D("50")))
        store = BarStore(tmp_path / "m.db")
        agent = Agent(cfg, broker, MarketFeed(broker, store),
                      AuditLog(tmp_path / "audit.jsonl", fsync_every_record=False),
                      MemoryStore(tmp_path / "mem.db"),
                      proposals=ProposalQueue(str(tmp_path / "p.json")))
        return Runtime(agent, tmp_path / "config.json"), broker

    def test_zero_lots_closes_nothing(self, runtime):
        rt, broker = runtime
        res = rt.close_position("EUR_USD", "owner", lots="0")
        assert res["state"] == "rejected"
        assert broker.positions()[0].lots == D("1.00")

    def test_omitting_lots_closes_everything(self, runtime):
        rt, broker = runtime
        rt.close_position("EUR_USD", "owner", lots=None)
        assert broker.positions() == []

    def test_a_partial_close_closes_only_that_much(self, runtime):
        rt, broker = runtime
        rt.close_position("EUR_USD", "owner", lots="0.25")
        assert broker.positions()[0].lots == D("0.75")

    def test_a_failed_flatten_is_not_reported_as_success(self, runtime, monkeypatch):
        rt, broker = runtime
        from sentinel.brokers.base import SubmitResult
        monkeypatch.setattr(broker, "close_position",
                            lambda *a, **k: SubmitResult(state=OrderState.REJECTED,
                                                         reject_reason="NO_PRICE"))
        res = rt.flatten_all("owner")
        assert res["ok"] is False
        assert res["failed"] and res["still_open"] == ["EUR_USD"]
        assert rt.agent.halted is True


class TestHeartbeatMeasuresTheLoop:
    """#15 — the background thread beat unconditionally, so the dead-man switch
    measured process liveness and would never trip on a stalled decision loop."""

    def test_a_stalled_loop_ages_the_heartbeat(self, tmp_path):
        import time
        from sentinel.ops.killswitch import Heartbeat
        hb = Heartbeat(tmp_path / "hb.json", interval_sec=1)
        hb.update(cycle=1)
        hb.start()
        try:
            time.sleep(0.2)
            assert hb.age_sec() < 0.6
            time.sleep(1.4)          # the thread keeps writing; the loop does not run
            assert hb.age_sec() > 1.0, "a stalled loop still looked alive"
        finally:
            hb.stop()

    def test_the_file_is_still_refreshed(self, tmp_path):
        import time
        from sentinel.ops.killswitch import Heartbeat
        hb = Heartbeat(tmp_path / "hb.json", interval_sec=1)
        hb.update(cycle=1)
        hb.beat()
        first = json.loads((tmp_path / "hb.json").read_text())["written_ns"]
        time.sleep(0.01)
        hb.beat()
        second = json.loads((tmp_path / "hb.json").read_text())["written_ns"]
        assert second > first


class TestRetriesAreGatedOnVenueCapability:
    """#19 — the OMS retried on a transient error without consulting whether the
    venue could reject a duplicate, so one intent could become three orders."""

    def test_a_venue_without_client_ids_gets_no_retries(self, tmp_path):
        from sentinel.execution.oms import OrderManager
        from tests.test_execution import FakeBroker
        audit = AuditLog(tmp_path / "a.jsonl", fsync_every_record=False)
        oms = OrderManager(FakeBroker(supports_client_id=False), audit, max_retries=3)
        assert oms.max_retries == 0

    def test_a_venue_with_client_ids_keeps_them(self, tmp_path):
        from sentinel.execution.oms import OrderManager
        from tests.test_execution import FakeBroker
        audit = AuditLog(tmp_path / "a.jsonl", fsync_every_record=False)
        oms = OrderManager(FakeBroker(supports_client_id=True), audit, max_retries=3)
        assert oms.max_retries == 3


class TestTimeExitsDoNotDependOnAnExchangeRate:
    """N2 — suspending R-denominated protection on a missing rate also suspended
    the weekend flatten, which reads only the clock. A missing rate is itself a
    connectivity symptom, so it is most likely to coincide with a Friday close."""

    def _agent_with_position(self, tmp_path, *, jpy_rate: bool):
        friday = dt.datetime(2026, 9, 11, 20, 0, tzinfo=dt.timezone.utc)   # Friday 20:00
        ns = int(friday.timestamp() * 1e9)
        broker = PaperBroker(instruments=INSTRUMENTS, starting_balance=D("10000"),
                             start_ns=ns)
        if jpy_rate:
            broker.set_conversion("JPY", D("1") / D("150"))
        broker.on_quote(Quote("USD_JPY", D("149.997"), D("150.003"), ts_ns=ns))
        broker.submit(OrderIntent(client_order_id="W1", strategy="t",
                                  instrument="USD_JPY", side=Side.BUY, lots=D("0.10"),
                                  stop_loss=D("149.500"), risk_amount=D("33.33")))
        cfg = SentinelConfig(
            agent=AgentConfig(mode=AgentMode.OBSERVE, session_windows_utc=[[0, 24]],
                              trade_days=[0, 1, 2, 3, 4, 5, 6]),
            execution=ExecutionConfig(broker="paper"),
            ops=OpsConfig(state_dir=str(tmp_path), killswitch_file=str(tmp_path / "KILL"),
                          audit_log=str(tmp_path / "audit.jsonl")))
        store = BarStore(tmp_path / "m.db")
        agent = Agent(cfg, broker, MarketFeed(broker, store),
                      AuditLog(tmp_path / "audit.jsonl", fsync_every_record=False),
                      MemoryStore(tmp_path / "mem.db"),
                      proposals=ProposalQueue(str(tmp_path / "p.json")),
                      clock_fn=lambda: broker.now_ns)
        return agent, broker

    def test_the_weekend_flatten_is_attempted_without_a_rate(self, tmp_path):
        """The flatten must be ATTEMPTED. If the venue then refuses it, the
        agent halts loudly -- what must never happen is the position quietly
        riding the gap because a missing rate skipped the check entirely."""
        agent, broker = self._agent_with_position(tmp_path, jpy_rate=False)
        agent.start()
        report = agent.cycle()
        actions = [p.get("action") for p in report.protections]
        assert "protection_suspended" not in actions, (
            "a missing rate suspended a check that reads only the clock")
        assert "close" in actions
        flat = broker.positions() == []
        assert flat or agent.halted, (
            "the position is still open and nothing halted: it would ride the gap")

    def test_it_also_runs_with_a_rate(self, tmp_path):
        agent, broker = self._agent_with_position(tmp_path, jpy_rate=True)
        agent.start()
        agent.cycle()
        assert broker.positions() == []


class TestDegradedReadsDoNotKillTheCycle:
    """N3 — `account()` was guarded and `positions()` was not, so an unreadable
    stop list escaped the cycle entirely and starved the heartbeat."""

    def test_a_failing_position_read_degrades_cleanly(self, tmp_path, monkeypatch):
        from sentinel.core.errors import TransientError
        cfg = SentinelConfig(
            agent=AgentConfig(mode=AgentMode.OBSERVE),
            execution=ExecutionConfig(broker="paper"),
            ops=OpsConfig(state_dir=str(tmp_path), killswitch_file=str(tmp_path / "KILL"),
                          audit_log=str(tmp_path / "audit.jsonl")))
        broker = PaperBroker(instruments=INSTRUMENTS, starting_balance=D("10000"))
        store = BarStore(tmp_path / "m.db")
        agent = Agent(cfg, broker, MarketFeed(broker, store),
                      AuditLog(tmp_path / "audit.jsonl", fsync_every_record=False),
                      MemoryStore(tmp_path / "mem.db"),
                      proposals=ProposalQueue(str(tmp_path / "p.json")))
        agent.start()
        monkeypatch.setattr(broker, "positions",
                            lambda: (_ for _ in ()).throw(TransientError("stops unreadable")))
        before = agent.heartbeat.age_sec()
        report = agent.cycle()          # must not raise
        assert any("position read failed" in e for e in report.errors)
        assert agent.heartbeat.age_sec() <= max(0.5, before)


class TestConfigFileIsNotAnEscalationSurface:
    """R1 — the promotion guard lived only in the API, so a hand-edited or
    restored config.json granted live mode and an accepted badge on restart."""

    def _raw_config(self, tmp_path, **overrides):
        raw = json.loads(SentinelConfig().to_json())
        raw["execution"]["venue_mode"] = "live"
        raw["execution"]["broker"] = "oanda"
        raw["execution"]["expected_account_id"] = "001-001-1234567-001"
        raw["agent"]["mode"] = "autonomous"
        raw["strategies"] = [{
            "name": "donchian_trend", "enabled": True, "weight": "1.0",
            "instruments": ["EUR_USD"], "timeframe": "H4", "params": {},
            "lifecycle": "accepted", "accepted_at_ns": 1,
            "acceptance_run_id": overrides.get("run_id", "never-ran-anything")}]
        (tmp_path / "config.json").write_text(json.dumps(raw))
        return SentinelConfig.load(tmp_path / "config.json")

    def test_an_unbacked_badge_is_demoted_at_startup(self, tmp_path):
        """A readable registry that simply does not contain the claimed run."""
        from sentinel.research.acceptance import Verdict
        from sentinel.research.verdicts import VerdictStore, enforce_config_authority
        store = VerdictStore(tmp_path / "v.db")
        store.record(Verdict(run_id="R-SOMETHING-ELSE", strategy="other",
                             created_at_ns=1, accepted=True, data_label="live-quality",
                             summary="unrelated"), config_hash="zz")
        cfg = self._raw_config(tmp_path)
        assert cfg.strategies[0].lifecycle == "accepted"      # the file says so
        checked, violations, repaired = enforce_config_authority(cfg, store)
        assert checked.strategies[0].lifecycle == "suspended"
        assert checked.strategies[0].enabled is False
        # The venue stays LIVE and the mode drops to observe: forcing paper
        # would disconnect from an account that may be holding positions.
        assert checked.execution.venue_mode.value == "live"
        assert checked.agent.mode.value == "observe"
        assert len(violations) == 2
        # A repair is a mutation and takes a new version, or one version number
        # would denote two different configurations.
        assert repaired is True
        assert checked.version == cfg.version + 1
        assert checked.updated_by == "startup-authority"

    def test_a_missing_registry_refuses_to_start_rather_than_repair(self, tmp_path):
        """#4 — a lost verdicts.db used to demote every accepted strategy AND
        persist that, destroying acceptance state irreversibly."""
        from sentinel.research.verdicts import (
            RegistryUnreadable, VerdictStore, enforce_config_authority,
        )
        cfg = self._raw_config(tmp_path)
        with pytest.raises(RegistryUnreadable, match="missing or empty"):
            enforce_config_authority(cfg, VerdictStore(tmp_path / "gone.db"))

    def test_a_corrupt_registry_refuses_to_open(self, tmp_path):
        from sentinel.research.verdicts import RegistryUnreadable, VerdictStore
        (tmp_path / "bad.db").write_bytes(b"not a database, not even close")
        with pytest.raises(RegistryUnreadable, match="not a readable database"):
            VerdictStore(tmp_path / "bad.db")

    def test_a_backed_badge_survives_startup(self, tmp_path):
        from sentinel.research.acceptance import Verdict
        from sentinel.research.verdicts import (
            VerdictStore, config_fingerprint, enforce_config_authority,
        )
        store = VerdictStore(tmp_path / "v.db")
        cfg = self._raw_config(tmp_path, run_id="R-OK")
        store.record(Verdict(run_id="R-OK", strategy="donchian_trend", created_at_ns=1,
                             accepted=True, data_label="live-quality", summary="ok"),
                     config_hash=config_fingerprint(["EUR_USD"], {}, "H4", runtime_config=cfg))
        checked, violations, repaired = enforce_config_authority(cfg, store)
        assert violations == [] and repaired is False
        assert checked.strategies[0].lifecycle == "accepted"
        assert checked.execution.venue_mode.value == "live"
        assert checked.version == cfg.version      # nothing changed, nothing bumped


class TestUnreadableStopsAreNotAbsentStops:
    """R2/I — on both live adapters, a failed read of the protective orders must
    raise rather than report every position as naked."""

    def test_ccxt_open_stops_raises(self, monkeypatch):
        from sentinel.brokers import ccxt_adapter
        from sentinel.core.errors import TransientError

        class _Ex:
            has = {"fetchOpenOrders": True}

            def fetch_open_orders(self):
                raise RuntimeError("502")

        broker = ccxt_adapter.CCXTBroker.__new__(ccxt_adapter.CCXTBroker)
        broker._ex = _Ex()
        with pytest.raises(TransientError, match="not evidence"):
            broker._open_stops()

    def test_oanda_open_trades_raises(self, oanda, monkeypatch):
        from sentinel.core.errors import BrokerError, TransientError
        broker, client = oanda
        monkeypatch.setattr(broker, "_request",
                            lambda *a, **k: (_ for _ in ()).throw(BrokerError("503")))
        with pytest.raises(TransientError, match="not evidence"):
            broker._open_trades_by_instrument()


class TestConfigBindingIsNotOptIn:
    """R3 — both binding checks were behind `if config_hash is not None`, so the
    default argument silently disabled the protection."""

    def test_the_fingerprint_is_required(self, tmp_path):
        from sentinel.research.acceptance import Verdict
        from sentinel.research.verdicts import VerdictStore
        store = VerdictStore(tmp_path / "v.db")
        store.record(Verdict(run_id="R1", strategy="s", created_at_ns=1, accepted=True,
                             data_label="live-quality", summary="ok"), config_hash="abc")
        ok, why = store.authorises("s", "R1", "")
        assert not ok and "fingerprint" in why
        with pytest.raises(TypeError):
            store.authorises("s", "R1")      # no default to fall through


# --------------------------------------------------------------------------- #
# Accounts must survive a restart.
#
# Holding users only in memory meant every restart regenerated the owner's TOTP
# secret and printed a fresh otpauth:// URI to the log. The second factor
# protecting every write would change without anyone deciding it should, and
# the recovery ritual -- re-enrol from whatever the log says -- is
# indistinguishable from an attack.
# --------------------------------------------------------------------------- #


def _sm(tmp_path, name="users.db"):
    from sentinel.api.security import SecurityManager, UserStore
    from sentinel.core.audit import AuditLog
    return SecurityManager(AuditLog(tmp_path / "audit.jsonl"),
                           secret="t" * 48,
                           store=UserStore(tmp_path / name))


def test_accounts_and_totp_secrets_survive_a_restart(tmp_path):
    first = _sm(tmp_path)
    user, _ = first.add_user("owner", "correct-horse-battery", "owner")
    original_secret = user.totp_secret
    original_hash = user.password_hash

    second = _sm(tmp_path)                      # "restart"
    restored = second.get_user("owner")

    assert restored is not None, "the account did not survive the restart"
    assert restored.totp_secret == original_secret, (
        "the TOTP secret was regenerated -- the operator's authenticator would "
        "have silently stopped working")
    assert restored.password_hash == original_hash
    assert restored.role == "owner"

    token, msg = second.login("owner", "correct-horse-battery",
                              user_agent="ua", client_ip="127.0.0.1")
    assert token is not None, msg


def test_a_second_boot_does_not_rotate_credentials_from_the_environment(tmp_path, monkeypatch):
    """bootstrap must not re-create an existing owner from the environment.

    Otherwise anyone who can set an environment variable replaces the owner's
    password and second factor on the next restart.
    """
    sm = _sm(tmp_path)
    user, _ = sm.add_user("owner", "correct-horse-battery", "owner")
    secret_before = user.totp_secret

    # Simulate the bootstrap branch: the user exists, so nothing is written.
    assert sm.get_user("owner") is not None
    with pytest.raises(ValueError, match="already exists"):
        sm.add_user("owner", "a-completely-different-pw", "owner")

    reopened = _sm(tmp_path)
    assert reopened.get_user("owner").totp_secret == secret_before


def test_disabling_an_account_drops_its_live_sessions(tmp_path):
    sm = _sm(tmp_path)
    sm.add_user("alice", "correct-horse-battery", "operator")
    token, _ = sm.login("alice", "correct-horse-battery",
                        user_agent="ua", client_ip="127.0.0.1")
    assert token is not None

    sm.set_disabled("alice", True)

    session = sm.verify_token(token, user_agent="ua", client_ip="127.0.0.1")
    assert session is None, "a disabled account kept a usable session"

    again, msg = sm.login("alice", "correct-horse-battery",
                          user_agent="ua", client_ip="127.0.0.1")
    assert again is None, "a disabled account could still log in"


def test_a_password_change_does_not_rotate_the_totp_secret(tmp_path):
    """A password reset is routine; re-enrolling an authenticator is not.
    Conflating them trains the operator to accept a new second factor on
    request."""
    sm = _sm(tmp_path)
    user, _ = sm.add_user("owner", "correct-horse-battery", "owner")
    before = user.totp_secret

    assert sm.set_password("owner", "an-entirely-new-passphrase")
    assert sm.get_user("owner").totp_secret == before
    assert sm.login("owner", "correct-horse-battery",
                    user_agent="ua", client_ip="127.0.0.1")[0] is None
    assert sm.login("owner", "an-entirely-new-passphrase",
                    user_agent="ua", client_ip="127.0.0.1")[0] is not None


def test_the_user_store_file_is_not_world_readable(tmp_path):
    """It holds TOTP secrets, which cannot be hashed -- the server must
    recompute the code. That puts the file in the same category as a private
    key."""
    import stat
    _sm(tmp_path)
    mode = stat.S_IMODE((tmp_path / "users.db").stat().st_mode)
    assert mode & 0o077 == 0, f"users.db is readable by others: {oct(mode)}"


def test_a_role_downgrade_takes_effect_immediately_not_at_session_expiry(tmp_path):
    """The role is re-resolved per request. Trusting the role baked into the
    token at login leaves a demoted user with their old authority for the rest
    of the session -- the exact window in which you demoted them."""
    sm = _sm(tmp_path)
    sm.add_user("alice", "correct-horse-battery", "owner")
    # A second owner, because demoting the LAST one is refused outright: the
    # system would be left with nobody who can change a risk limit or release
    # the kill switch.
    sm.add_user("root-owner", "correct-horse-battery-2", "owner")
    token, _ = sm.login("alice", "correct-horse-battery",
                        user_agent="ua", client_ip="127.0.0.1")

    session = sm.verify_token(token, user_agent="ua", client_ip="127.0.0.1")
    assert session.role == "owner"

    sm.set_role("alice", "viewer")

    session = sm.verify_token(token, user_agent="ua", client_ip="127.0.0.1")
    assert session is not None
    assert session.role == "viewer", "the demoted user kept owner authority"


def test_a_removed_account_cannot_use_an_outstanding_token(tmp_path):
    from sentinel.api.security import UserStore
    sm = _sm(tmp_path)
    sm.add_user("mallory", "correct-horse-battery", "operator")
    token, _ = sm.login("mallory", "correct-horse-battery",
                        user_agent="ua", client_ip="127.0.0.1")
    assert sm.verify_token(token, user_agent="ua", client_ip="127.0.0.1") is not None

    # Removed out of band -- the store is the source of truth, and a stale
    # in-memory session must not outlive the account.
    UserStore(tmp_path / "users.db").delete("mallory")
    sm._users.pop("mallory")

    assert sm.verify_token(token, user_agent="ua", client_ip="127.0.0.1") is None


# --------------------------------------------------------------------------- #
# Second adversarial audit round: revocation, brute force, journal integrity.
# --------------------------------------------------------------------------- #


def test_revoking_an_account_reaches_a_running_server(tmp_path):
    """Accounts are changed by a CLI in a SEPARATE process.

    A server that consults only its in-memory cache could never see that, so
    `manage_users.py disable` printed success while the account kept full write
    authority until the next restart.
    """
    from sentinel.api.security import SecurityManager, UserStore
    from sentinel.core.audit import AuditLog

    server = _sm(tmp_path)
    server.add_user("oper", "correct-horse-battery", "operator")
    token, _ = server.login("oper", "correct-horse-battery",
                            user_agent="ua", client_ip="127.0.0.1")
    assert server.verify_token(token, user_agent="ua", client_ip="127.0.0.1") is not None

    # A different process, its own SecurityManager, the same store.
    cli = SecurityManager(AuditLog(tmp_path / "audit.jsonl"), secret="t" * 48,
                          store=UserStore(tmp_path / "users.db"))
    assert cli.set_disabled("oper", True)

    assert server.verify_token(token, user_agent="ua", client_ip="127.0.0.1") is None, (
        "a disabled account kept a usable session on the running server")
    assert server.login("oper", "correct-horse-battery",
                        user_agent="ua", client_ip="127.0.0.1")[0] is None


def test_a_role_change_from_another_process_reaches_a_running_server(tmp_path):
    from sentinel.api.security import SecurityManager, UserStore
    from sentinel.core.audit import AuditLog

    server = _sm(tmp_path)
    server.add_user("alice", "correct-horse-battery", "owner")
    server.add_user("root-owner", "correct-horse-battery-2", "owner")
    token, _ = server.login("alice", "correct-horse-battery",
                            user_agent="ua", client_ip="127.0.0.1")
    assert server.verify_token(token, user_agent="ua", client_ip="127.0.0.1").role == "owner"

    cli = SecurityManager(AuditLog(tmp_path / "audit.jsonl"), secret="t" * 48,
                          store=UserStore(tmp_path / "users.db"))
    cli.set_role("alice", "viewer")

    session = server.verify_token(token, user_agent="ua", client_ip="127.0.0.1")
    assert session is not None and session.role == "viewer"


def test_a_wrong_totp_consumes_the_write_budget_and_locks_out(tmp_path):
    """Unlimited second-factor guessing was possible because the rate limit ran
    AFTER the code check and a wrong code tripped no lockout at all."""
    sm = _sm(tmp_path)
    sm.write_rate_per_minute = 5
    sm.max_login_attempts = 5
    sm.add_user("dan", "correct-horse-battery", "operator")
    token, _ = sm.login("dan", "correct-horse-battery",
                        user_agent="ua", client_ip="127.0.0.1")
    session = sm.verify_token(token, user_agent="ua", client_ip="127.0.0.1")

    refusals = [sm.authorise_write(session, "000000", "halt")[1] for _ in range(40)]
    assert any("slow down" in m for m in refusals), (
        "wrong TOTP codes never consumed the write rate limit")
    # And the account is now locked, so guessing cannot simply continue.
    assert sm.login("dan", "correct-horse-battery",
                    user_agent="ua", client_ip="127.0.0.1")[0] is None


def test_a_rate_limited_write_does_not_burn_a_valid_totp_code(tmp_path):
    """Burning the operator's code on a refused write locked them out of their
    own emergency controls with 'this code has already been used'."""
    import pyotp
    sm = _sm(tmp_path)
    sm.write_rate_per_minute = 1
    user, _ = sm.add_user("dan", "correct-horse-battery", "owner")
    token, _ = sm.login("dan", "correct-horse-battery",
                        user_agent="ua", client_ip="127.0.0.1")
    session = sm.verify_token(token, user_agent="ua", client_ip="127.0.0.1")

    code = pyotp.TOTP(user.totp_secret).now()
    assert sm.authorise_write(session, code, "halt")[0]         # spends the budget
    ok, msg = sm.authorise_write(session, code, "flatten")
    assert not ok and "slow down" in msg
    # The refusal was the limiter, not a replay claim about an unrelated code.
    assert "already been used" not in msg


def test_the_lockout_actually_backs_off_exponentially(tmp_path):
    sm = _sm(tmp_path)
    sm.max_login_attempts = 2
    sm.lockout_sec = 60
    sm.add_user("eve", "correct-horse-battery", "viewer")
    durations = []
    for _ in range(3):
        for _ in range(2):
            sm.login("eve", "wrong-password-here", user_agent="ua", client_ip="1.2.3.4")
        durations.append(sm._locked_out("user:eve"))
        sm._lockouts.clear()
    assert durations[1] > durations[0] and durations[2] > durations[1], (
        f"lockout did not back off: {durations}")


def test_a_torn_final_line_does_not_erase_the_journal(tmp_path):
    """One stray byte used to move the WHOLE journal aside and restart at
    GENESIS -- and verify() then reported 'chain intact (0 records)'."""
    from sentinel.core.audit import AuditLog, EventType

    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for i in range(20):
        log.append(EventType.SIGNAL, {"i": i})
    log.close()

    with open(path, "a", encoding="utf-8") as fh:
        fh.write("{\n")

    reopened = AuditLog(path)
    ok, _, msg = reopened.verify()
    records = [r for r in reopened.iter_records() if not r.get("__unparseable__")]
    reopened.close()
    assert ok, msg
    assert len(records) >= 20, "the journal was erased by a torn final line"
    assert any(r["payload"].get("chain_repaired") for r in records), (
        "the repair was not itself recorded in the chain")


def test_a_corrupt_interior_line_is_reported_not_raised(tmp_path):
    from sentinel.core.audit import AuditLog, EventType

    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for i in range(10):
        log.append(EventType.SIGNAL, {"i": i})
    log.close()

    lines = path.read_text(encoding="utf-8").splitlines()
    lines[4] = "{not json"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    reopened = AuditLog(path)
    ok, _, msg = reopened.verify()       # must not raise
    reopened.close()
    assert not ok
    assert "not valid JSON" in msg


# --------------------------------------------------------------------------- #
# Financial / mathematical audit round.
# --------------------------------------------------------------------------- #


def test_deflated_sharpe_rejects_the_best_of_many_noise_trials():
    """The DSR variance was de-annualised twice, shrinking it ~200x, so the one
    gate whose job is to price in multiple testing certified pure noise."""
    import numpy as np
    from sentinel.research.stats import deflated_sharpe_ratio

    rng = np.random.default_rng(11)
    best, best_sr = None, -9.0
    for _ in range(300):
        r = rng.normal(0.0, 0.004, 3000)
        sr = float(r.mean() / r.std(ddof=1) * np.sqrt(1512))
        if sr > best_sr:
            best_sr, best = sr, r

    out = deflated_sharpe_ratio(best, n_trials=300, periods_per_year=1512)
    assert out["sr"] > 1.0, "the search should have found a flattering noise series"
    assert out["sr_star"] > out["sr"], (
        f"the bar for best-of-300 ({out['sr_star']:.2f}) must exceed the observed "
        f"Sharpe ({out['sr']:.2f})")
    assert out["dsr"] < 0.95, f"pure noise passed the DSR gate at {out['dsr']:.3f}"


def test_a_short_is_labelled_by_the_correct_barrier():
    """Both of a short's barriers were placed BELOW the entry, so every short
    was stopped on its first bar and reported a positive return alongside a
    'stop' label -- a row contradicting itself."""
    import numpy as np
    import pandas as pd
    from sentinel.research.labeling import BarrierConfig, triple_barrier_labels

    n = 12
    sigma = pd.Series([0.02] * n)
    side = pd.Series([-1.0] * n)
    cfg = BarrierConfig(profit_mult=2.0, stop_mult=1.0, max_hold_bars=10)

    falling = pd.Series(np.linspace(100, 90, n))
    row = triple_barrier_labels(falling, [0], sigma, cfg, side=side,
                                high=falling * 1.001, low=falling * 0.999).iloc[0]
    assert row["touched"] == "target" and row["ret"] > 0, (
        "a short into a falling market was not labelled a win")

    rising = pd.Series(np.linspace(100, 110, n))
    row = triple_barrier_labels(rising, [0], sigma, cfg, side=side,
                                high=rising * 1.001, low=rising * 0.999).iloc[0]
    assert row["touched"] == "stop" and row["ret"] < 0


def test_sortino_uses_downside_deviation_not_the_std_of_losses():
    import numpy as np
    from sentinel.research.metrics import compute_performance
    import pandas as pd

    # Ninety gains and ten IDENTICAL losses: std(negatives) is ~1e-18 here, so
    # the degenerate guard used to report Sortino = 0 for a strategy whose true
    # Sortino is ~17.6.
    rets = np.array([0.01] * 90 + [-0.02] * 10)
    dsd = float(np.sqrt(np.mean(np.minimum(rets, 0.0) ** 2)))
    expected = float(rets.mean() / dsd * np.sqrt(252))
    assert 17.0 < expected < 18.5

    values = 10000.0 * np.cumprod(1.0 + rets)
    equity = pd.Series(values, index=pd.date_range("2026-01-01", periods=len(values),
                                                   freq="1D"))
    perf = compute_performance([], equity, 252, 10000.0)
    assert perf.sortino > 5.0, (
        f"tightly clustered losses collapsed Sortino to {perf.sortino}")


def test_a_carry_credit_is_not_counted_as_a_cost():
    """total_cost used abs(financing), so a positive-carry trade had its credit
    charged as an expense and gross_profit came out wrong by 2x financing."""
    import pandas as pd
    from decimal import Decimal
    from sentinel.core.types import ClosedTrade, Side
    from sentinel.research.metrics import compute_performance

    trade = ClosedTrade(
        trade_id="T1", strategy="s", instrument="EUR_USD", side=Side.BUY,
        lots=Decimal("1"), entry_price=Decimal("1.1"), exit_price=Decimal("1.11"),
        opened_ns=0, closed_ns=10**9, pnl=Decimal("105"), pnl_pips=Decimal("100"),
        commission=Decimal("7"), financing=Decimal("12"),     # a CREDIT
        initial_risk=Decimal("100"), r_multiple=Decimal("1.05"), exit_reason="tp")
    equity = pd.Series([10000.0, 10105.0],
                       index=pd.date_range("2026-01-01", periods=2, freq="1D"))
    perf = compute_performance([trade], equity, 252, 10000.0)
    assert perf.total_cost == pytest.approx(-5.0), (
        f"a 12 credit against 7 commission is a net -5 cost, got {perf.total_cost}")
    assert perf.gross_profit == pytest.approx(100.0)


def test_rsi_is_100_in_a_one_sided_rally_and_nan_during_warmup():
    import numpy as np
    import pandas as pd
    from sentinel.strategy.base import rsi

    up = pd.Series(np.arange(60, dtype=float) + 100)
    out = rsi(up, 14)
    assert out.iloc[-1] == 100.0, (
        "a rally with no down-closes reported RSI 50, so a strategy gating "
        "shorts on rsi >= 68 could never fire in the rally it exists to fade")
    assert pd.isna(out.iloc[5]), "the warmup was filled with a fake neutral 50"

    down = pd.Series(200 - np.arange(60, dtype=float))
    assert rsi(down, 14).iloc[-1] == 0.0


# --------------------------------------------------------------------------- #
# Risk & profit-preservation audit round.
# --------------------------------------------------------------------------- #


def _sim_broker(balance="100000"):
    from sentinel.brokers.paper import PaperBroker, SimProfile
    from sentinel.core.money import D, Instrument
    profile = SimProfile(latency_ms_median=1, latency_ms_tail=2, latency_tail_prob=0.0,
                         last_look_reject_prob=0.0, slippage_pips_mean=D("0"),
                         slippage_pips_sigma=D("0"), default_spread_pips=D("0"),
                         asia_spread_multiple=D("1"), london_spread_multiple=D("1"),
                         rollover_spread_multiple=D("1"))
    return PaperBroker(instruments={"EUR_USD": Instrument("EUR_USD", "EUR", "USD")},
                       starting_balance=D(balance), account_currency="USD",
                       profile=profile, start_ns=1_772_409_600 * 10**9)


def _quote(broker, price, hours=0):
    from sentinel.core.money import D
    from sentinel.core.types import Quote
    px = D(str(price))
    broker.on_quote(Quote(instrument="EUR_USD", bid=px, ask=px,
                          ts_ns=(1_772_409_600 + hours * 3600) * 10**9))


def _buy(broker, lots, price, risk="500", oid="T1", hours=0):
    from sentinel.core.money import D
    from sentinel.core.types import OrderIntent, Side
    _quote(broker, price, hours)
    return broker.submit(OrderIntent(
        client_order_id=oid, strategy="s", instrument="EUR_USD", side=Side.BUY,
        lots=D(str(lots)), risk_amount=D(risk), decision_ns=10**9))


def test_a_scale_out_does_not_halve_the_runners_r_multiple(tmp_path):
    """initial_risk stayed at the FULL position's risk while lots shrank, so
    after a 50% scale-out the runner -- the leg carrying the profit -- reported
    half its true excursion and became the LEAST protected leg."""
    from sentinel.core.money import D

    broker = _sim_broker()
    _buy(broker, 1.0, 1.10000, risk="500")
    _quote(broker, 1.10500, hours=1)
    inst = broker.instruments()["EUR_USD"]
    before = broker.positions()[0].r_multiple(broker._quotes["EUR_USD"], inst, D("1"))

    broker.close_position("EUR_USD", D("0.5"), reason="scale_out")
    after = broker.positions()[0].r_multiple(broker._quotes["EUR_USD"], inst, D("1"))

    assert abs(before - after) < D("0.01"), (
        f"R collapsed from {before} to {after} at an unchanged price")
    assert broker.positions()[0].initial_risk == D("250.00")


def test_a_partial_fill_scales_the_recorded_risk_down():
    from sentinel.core.money import D

    broker = _sim_broker()
    # 3.0 lots trips the simulator's partial-fill threshold.
    _buy(broker, 3.0, 1.10000, risk="1500")
    pos = broker.positions()[0]
    expected = D("1500") * pos.lots / D("3.0")
    assert abs(pos.initial_risk - expected) < D("0.01"), (
        f"a {pos.lots}-lot fill of a 3.0-lot intent still claims {pos.initial_risk} "
        f"of risk instead of {expected}")


def test_a_reversal_does_not_orphan_its_commission():
    """The closing lots' commission was taken from cash but recorded in no
    ClosedTrade, so the ledger stopped matching the balance on every reversal."""
    from sentinel.core.money import D
    from sentinel.core.types import OrderIntent, Side

    broker = _sim_broker()
    _buy(broker, 1.0, 1.10000, oid="A1")
    _quote(broker, 1.10500, hours=1)
    broker.submit(OrderIntent(client_order_id="A2", strategy="s", instrument="EUR_USD",
                              side=Side.SELL, lots=D("2.0"), risk_amount=D("500"),
                              decision_ns=2 * 10**9))
    _quote(broker, 1.10000, hours=2)
    broker.submit(OrderIntent(client_order_id="A3", strategy="s", instrument="EUR_USD",
                              side=Side.BUY, lots=D("1.0"), risk_amount=D("500"),
                              decision_ns=3 * 10**9))

    ledger = sum((t.pnl for t in broker.closed_trades), D("0"))
    moved = broker.account().balance - D("100000")
    assert moved == ledger, f"ledger {ledger} != balance change {moved}"


def test_financing_accrues_on_daily_bars():
    """Rollover only fired on a quote whose UTC hour was >= 21. A D1 bar emits
    quotes at 00:00/08:00/16:00/24:00, so a whole daily backtest accrued ZERO
    carry -- and a carry strategy with no carry is not testing its hypothesis."""
    broker = _sim_broker()
    _buy(broker, 1.0, 1.10000)
    for day in range(1, 11):
        _quote(broker, 1.10000, hours=day * 24)
    assert broker.positions()[0].financing_paid != 0, "no carry was ever charged"


def test_the_simulator_refuses_an_order_it_cannot_margin():
    from sentinel.core.types import OrderState

    broker = _sim_broker(balance="500")
    result = _buy(broker, 50, 1.10000)
    assert result.state is OrderState.REJECTED
    assert result.reject_reason == "INSUFFICIENT_MARGIN", (
        "a 5.5m notional position was opened on a $500 account and then "
        "margin-stopped, manufacturing trades that could never have existed")


def test_closing_zero_lots_closes_nothing():
    from sentinel.core.money import D

    broker = _sim_broker()
    _buy(broker, 1.0, 1.10000)
    broker.close_position("EUR_USD", D("0"))
    assert len(broker.positions()) == 1, (
        "Decimal('0') is falsy, so asking to close none of the position closed "
        "all of it")


def test_the_breakeven_stop_actually_breaks_even():
    """A half-pip buffer did not cover a 1.5-pip round trip, so every trade that
    came back to its 'break-even' stop realised a small, guaranteed loss."""
    from decimal import Decimal
    from sentinel.core.config import RiskConfig
    from sentinel.core.money import CostModel, D, Instrument
    from sentinel.core.types import Position, Quote, Side
    from sentinel.risk.protect import ProtectAction, evaluate_protection

    inst = Instrument("EUR_USD", "EUR", "USD")
    pos = Position(instrument="EUR_USD", side=Side.BUY, lots=D("1"),
                   entry_price=D("1.10000"), opened_ns=0, strategy="s",
                   stop_loss=D("1.09800"), initial_risk=D("200"))
    quote = Quote(instrument="EUR_USD", bid=D("1.10250"), ask=D("1.10250"), ts_ns=10**9)
    cost = CostModel(spread_pips=D("0.6"))

    acts = evaluate_protection(pos, quote, inst, RiskConfig(), atr=None,
                               quote_to_account=D("1"), cost_model=cost)
    moves = [a for a in acts if a.action is ProtectAction.MOVE_STOP]
    assert moves, "no break-even move was produced"
    stop = moves[0].new_stop
    cost_pips = cost.round_trip_pips(inst, D("1"), inst.pip_value_quote(D("1")))
    assert stop >= pos.entry_price + cost_pips * inst.pip, (
        f"stop {stop} does not cover the {cost_pips}p round trip from "
        f"{pos.entry_price}")


def test_the_giveback_ratchet_protects_a_peak_without_an_atr():
    """The ATR trail was the ONLY mechanism converting open profit into a
    tighter stop, and it silently did nothing whenever the ATR was unavailable
    -- so a position could run to +8R protected by the break-even stop alone."""
    from sentinel.core.config import RiskConfig
    from sentinel.core.money import D, Instrument
    from sentinel.core.types import Position, Quote, Side
    from sentinel.risk.protect import ProtectAction, evaluate_protection

    inst = Instrument("EUR_USD", "EUR", "USD")
    pos = Position(instrument="EUR_USD", side=Side.BUY, lots=D("1"),
                   entry_price=D("1.10000"), opened_ns=0, strategy="s",
                   stop_loss=D("1.09800"), initial_risk=D("200"))
    pos.max_favourable = D("4.0")          # peaked at +4R
    quote = Quote(instrument="EUR_USD", bid=D("1.10600"), ask=D("1.10600"), ts_ns=10**9)

    acts = evaluate_protection(pos, quote, inst, RiskConfig(), atr=None,
                               quote_to_account=D("1"))
    ratchet = [a for a in acts if a.rule == "giveback"]
    assert ratchet, "no give-back instruction with the ATR unavailable"
    act = ratchet[0]
    if act.action is ProtectAction.MOVE_STOP:
        # keep 0.5 of a 4R peak = a stop at +2R = entry + 2 * 20 pips
        assert act.new_stop == inst.round_price(D("1.10400")), act.new_stop
    else:
        assert act.action is ProtectAction.CLOSE


def test_the_ladder_does_not_flip_flop_at_a_boundary():
    from sentinel.core.config import RiskConfig
    from sentinel.core.money import D
    from sentinel.risk.sizing import ladder_rung

    ladder = RiskConfig().ladder
    rung = 0
    seen = []
    for dd in ("2.99", "3.00", "2.99", "3.01", "2.95"):
        rung = ladder_rung(D(dd), ladder, current_rung=rung, hysteresis_pct=D("0.5"))
        seen.append(rung)
    assert seen[0] == 0 and seen[1] == 1, "the ladder did not tighten at the threshold"
    assert all(r == 1 for r in seen[2:]), (
        f"the ladder released inside the hysteresis band: {seen}")


def test_the_learning_loop_cannot_propose_more_risk():
    """All nine risk-increasing directions used to be accepted, and each
    approval re-bases the current value -- so the news blackout could be walked
    to zero one legal 35% step at a time."""
    from sentinel.agent.proposals import PatternFinding, ProposalError, propose

    finding = PatternFinding(pattern="p", n=100, share=0.4, mean_r=-0.2,
                             mean_delta_r=0.3, t_stat=4.0, p_value=0.001,
                             recommendation="r")
    riskier = [
        ("risk.block_minutes_before_high_impact", 30, 20),
        ("risk.trail_atr_multiple", 2.5, 3.2),
        ("risk.min_stop_pips", 12.0, 8.0),
        ("risk.min_reward_risk", 1.8, 1.4),
        ("risk.partial_take_fraction", 0.5, 0.3),
        ("risk.giveback_keep_fraction", 0.5, 0.3),
    ]
    for path, current, proposed in riskier:
        with pytest.raises(ProposalError, match="MORE risk"):
            propose(path=path, current_value=current, proposed_value=proposed,
                    rationale="test", finding=finding)

    # The safer direction still works.
    safer = propose(path="risk.min_stop_pips", current_value=12.0, proposed_value=14.0,
                    rationale="test", finding=finding)
    assert float(safer.proposed_value) > 12.0


def test_the_profit_lock_latches_for_the_rest_of_the_day(engine, ctx):
    """As a bare level test it released itself the moment the day's gain fell
    back below the threshold -- so the agent resumed trading into exactly the
    give-back it was meant to prevent."""
    from decimal import Decimal
    from sentinel.core.types import OrderIntent, Side

    order = OrderIntent(client_order_id="PL1", strategy="test", instrument="EUR_USD",
                        side=Side.BUY, lots=Decimal("0.1"), stop_loss=Decimal("1.0820"),
                        take_profit=Decimal("1.0910"))

    # The day's gain has faded back below the threshold, but the lock tripped
    # earlier in the day and must stay latched.
    ctx.day_pnl = Decimal("50")
    ctx.day_profit_locked = True
    decision = engine.evaluate_entry(order, ctx)
    assert any(v.rule == "profit_lock" for v in decision.vetoes), (
        "the profit lock released itself once the day's gain faded")

    # Without the latch and below the threshold, entries are allowed again.
    ctx.day_profit_locked = False
    decision = engine.evaluate_entry(order, ctx)
    assert not any(v.rule == "profit_lock" for v in decision.vetoes)


def test_a_position_with_unknown_risk_is_charged_the_full_budget(engine, ctx):
    """Live adapters cannot report initial_risk. With the agent's metadata
    lost, a 2.5%-committed book read as 0.49% and a sixth entry was approved on
    the strength of it."""
    from decimal import Decimal
    from sentinel.core.types import OrderIntent, Position, Side

    ctx.positions = [
        Position(instrument="GBP_USD", side=Side.BUY, lots=Decimal("0.1"),
                 entry_price=Decimal("1.27"), opened_ns=0, strategy="s",
                 stop_loss=Decimal("1.26"), initial_risk=Decimal("0")),
        Position(instrument="AUD_USD", side=Side.BUY, lots=Decimal("0.1"),
                 entry_price=Decimal("0.65"), opened_ns=0, strategy="s",
                 stop_loss=Decimal("0.64"), initial_risk=Decimal("0")),
    ]
    order = OrderIntent(client_order_id="UR1", strategy="test", instrument="EUR_USD",
                        side=Side.BUY, lots=Decimal("0.1"), stop_loss=Decimal("1.0820"),
                        take_profit=Decimal("1.0910"))
    decision = engine.evaluate_entry(order, ctx)
    assert "positions_with_unknown_risk" in decision.diagnostics
    assert Decimal(decision.diagnostics["assumed_risk_for_unknown"]) > 0, (
        "positions with no recorded risk were counted as risk-free")


# --------------------------------------------------------------------------- #
# Final verification round: defects introduced BY the earlier fixes.
# --------------------------------------------------------------------------- #


def _long(stop, risk="500", peak="8.0"):
    from decimal import Decimal
    from sentinel.core.types import Position, Side
    pos = Position(instrument="EUR_USD", side=Side.BUY, lots=Decimal("1"),
                   entry_price=Decimal("1.10000"), opened_ns=0, strategy="s",
                   stop_loss=stop, initial_risk=Decimal(risk))
    pos.max_favourable = Decimal(peak)
    return pos


def test_the_giveback_floor_is_unaffected_by_earlier_stop_moves():
    """1R must come from `initial_risk`, never from the LIVE stop.

    Reading it from position.stop_loss was subtly catastrophic, because the
    stop moves. After break-even tightened it to entry+1.8p, "1R" read as 1.8
    pips and the ratchet went quiet on the +8R position it exists for; after an
    ATR trail it read as 100 pips and the ratchet CLOSED a winner at +6R.
    """
    from decimal import Decimal
    from sentinel.core.config import RiskConfig
    from sentinel.core.money import D, Instrument
    from sentinel.core.types import Quote
    from sentinel.risk.protect import ProtectAction, evaluate_protection

    inst = Instrument("EUR_USD", "EUR", "USD")          # 1R = 500/10 = 50 pips
    quote = Quote(instrument="EUR_USD", bid=D("1.13000"), ask=D("1.13000"),
                  ts_ns=10**9)                          # +6R
    expected = inst.round_price(D("1.12000"))           # keep 0.5 of an 8R peak

    for label, stop in (("original 1R stop", D("1.09500")),
                        ("after break-even", D("1.10018")),
                        ("after an ATR trail", D("1.11000"))):
        acts = [a for a in evaluate_protection(_long(stop), quote, inst, RiskConfig(),
                                               atr=None, quote_to_account=D("1"))
                if a.rule == "giveback"]
        assert acts, f"{label}: the ratchet produced nothing"
        act = acts[0]
        assert act.action is ProtectAction.MOVE_STOP, (
            f"{label}: the ratchet wanted to {act.action.value} a +6R winner")
        assert act.new_stop == expected, (
            f"{label}: floor at {act.new_stop}, expected {expected}")


def test_the_peak_excursion_is_tracked_for_venues_that_cannot_report_it():
    """Only PaperBroker maintained max_favourable. Live adapters rebuild
    Position objects each poll, so the ratchet could never arm on a real
    account -- and the backtest, which uses the paper broker, hid that."""
    import inspect
    from sentinel.agent import orchestrator

    source = inspect.getsource(orchestrator.Agent)
    assert "_track_excursion" in source
    assert "self._track_excursion(pos, quote, inst, conv)" in source, (
        "the excursion is not tracked inside _manage_positions")
    hydrate = inspect.getsource(orchestrator.Agent._hydrate)
    assert "max_favourable" in hydrate, (
        "the peak is not restored on restart, so a ratchet that had already "
        "tightened would re-arm from zero")


def test_the_signals_horizon_reaches_the_order_intent():
    """_horizon_sec read intent.metadata["horizon_bars"], but the intent was
    built without metadata -- so the time stop was called every cycle with a
    limit of zero. A fix that runs and does nothing is worse than a missing
    one."""
    import inspect
    from sentinel.agent import orchestrator

    source = inspect.getsource(orchestrator.Agent._act_on_signal)
    assert "metadata=" in source and "horizon_bars" in source


def test_a_risk_increasing_proposal_is_refused_even_when_the_clamp_flips_it():
    """The direction guard ran BEFORE the clamp, and the clamp bounds are
    narrower than the config's -- so a 'safer' 0.2 clamped up to 0.5, an
    increase the guard had already waved through."""
    from sentinel.agent.proposals import PatternFinding, ProposalError, propose

    finding = PatternFinding(pattern="p", n=100, share=0.4, mean_r=-0.2,
                             mean_delta_r=0.3, t_stat=4.0, p_value=0.001,
                             recommendation="r")
    with pytest.raises(ProposalError, match="MORE risk"):
        propose(path="risk.giveback_arm_r", current_value=0.3, proposed_value=0.2,
                rationale="looks safer, clamps to an increase", finding=finding)
    with pytest.raises(ProposalError, match="MORE risk"):
        propose(path="risk.giveback_keep_fraction", current_value=0.95,
                proposed_value=0.99, rationale="clamps to a decrease", finding=finding)


def test_an_armed_lockout_reaches_a_session_that_is_already_open(tmp_path):
    """Checking the lockout only at login stopped the door and left the
    window: a holder of a live token kept guessing TOTP indefinitely."""
    sm = _sm(tmp_path)
    sm.max_login_attempts = 3
    sm.write_rate_per_minute = 100
    sm.add_user("dana", "correct-horse-battery", "operator")
    token, _ = sm.login("dana", "correct-horse-battery",
                        user_agent="ua", client_ip="127.0.0.1")
    session = sm.verify_token(token, user_agent="ua", client_ip="127.0.0.1")

    messages = [sm.authorise_write(session, "000000", "halt")[1] for _ in range(10)]
    assert any("locked" in m for m in messages), (
        f"the lockout never reached the open session: {set(messages)}")
    assert sm.verify_token(token, user_agent="ua", client_ip="127.0.0.1") is None, (
        "the locked-out user kept a usable session")


def test_a_bind_to_a_specific_public_address_counts_as_public():
    """Matching only 0.0.0.0 and :: meant binding to a routable address --
    203.0.113.5, say -- skipped both the startup refusal and the dashboard's
    permanent exposure banner."""
    from sentinel.api.main import _is_loopback

    assert _is_loopback("127.0.0.1")
    assert _is_loopback("localhost")
    assert _is_loopback("::1")
    assert not _is_loopback("0.0.0.0")
    assert not _is_loopback("203.0.113.5")
    assert not _is_loopback("10.0.0.7")
    assert not _is_loopback("some-host.internal")     # unresolvable: warn, don't hide


def test_rsi_on_a_dead_feed_is_neutral_not_maximum_overbought():
    """gain == 0 AND loss == 0 hit the 'no losses -> 100' rule first, so a
    stale or forward-filled price read as maximum overbought."""
    import pandas as pd
    from sentinel.strategy.base import rsi

    assert rsi(pd.Series([1.10] * 40), 14).iloc[-1] == 50.0


def test_the_multiple_testing_gate_cannot_be_switched_off_by_declaring_one_trial():
    """With one declared trial the bar is zero, and L5.1 degenerates into 'is
    the Sharpe positive' -- the multiple-testing gate switched off inside the
    run that exists to price in multiple testing."""
    import numpy as np
    from sentinel.research.stats import deflated_sharpe_ratio

    rng = np.random.default_rng(5)
    returns = rng.normal(0.0006, 0.004, 3000)

    honest = deflated_sharpe_ratio(returns, 2, periods_per_year=1512)
    assert honest["sr_star"] > 0, (
        "the bar is still zero at the floor of 2 trials")

    many = deflated_sharpe_ratio(returns, 200, periods_per_year=1512)
    assert many["sr_star"] > honest["sr_star"], (
        "more declared trials must raise the bar")


# --------------------------------------------------------------------------- #
# Audit round 5: the broker profile layer, the strategy library, the learning
# loop's statistics. Every one of these was invisible to 677 passing tests.
# --------------------------------------------------------------------------- #


def test_outcome_derived_tags_do_not_manufacture_findings_from_noise():
    """Tags assigned FROM a trade's own outcome cannot be evidence about a
    parameter: testing `consider_wider_stop` against everything else is testing
    whether losers lose. On 400 pure random walks that produced five
    "actionable" findings at p < 1e-10 every single pass -- and, worse, raised
    the Benjamini-Hochberg threshold for every genuine hypothesis beside them.
    """
    import numpy as np
    from sentinel.agent.postmortem import OUTCOME_DERIVED_TAGS, TradeAutopsy, aggregate

    def noise(seed, n=400):
        rng = np.random.default_rng(seed)
        out = []
        for i in range(n):
            r = float(rng.normal(0, 1))
            mfe = max(0.0, r) + abs(rng.normal(0, 0.4))
            mae = min(0.0, r) - abs(rng.normal(0, 0.4))
            tags = []
            if r <= -0.95:
                tags += ["stop_placement", "consider_wider_stop"]
            if mfe > 1.0 and r < mfe * 0.5:
                tags += ["exit_discipline", "consider_breakeven_move"]
            if mae < -1.0 and r > 0:
                tags.append("survived_deep_drawdown")
            out.append(TradeAutopsy(
                trade_id=f"T{i}", strategy="s", instrument="EUR_USD",
                outcome="win" if r > 0 else "loss",
                mode="clean_win" if r > 0 else "clean_loss",
                r_multiple=r, mae_r=mae, mfe_r=mfe,
                capture_ratio=(r / mfe if mfe else 0.0), tags=tags,
                regime="quiet_range", closed_ns=i * 10**9, had_path=True))
        return out

    significant = []
    for seed in range(8):
        significant += [f.pattern for f in aggregate(noise(seed), min_sample=25)
                        if getattr(f, "significant", False)]
    assert not significant, (
        f"pure noise produced {len(significant)} significant findings: "
        f"{sorted(set(significant))}")

    # And the tautological tags are still REPORTED, just not tested.
    described = {f.pattern for f in aggregate(noise(0), min_sample=25)
                 if f.diagnosis == "descriptive"}
    assert any(f"tag:{t}" in described for t in OUTCOME_DERIVED_TAGS)


def test_a_retired_lesson_cannot_resurrect_itself_with_a_clean_record(tmp_path):
    """Retired for a sign reversal, or retired deliberately by an operator, and
    then re-minted by the next postmortem producing the same string -- with its
    contradiction count back at zero and its caution multiplier back in force.
    An operator's decision has to survive the next scheduled run."""
    from sentinel.agent.memory import Lesson, MemoryStore

    store = MemoryStore(tmp_path / "memory.db")
    statement = "stress trades lose 0.4R"

    def make():
        return Lesson(scope="regime", regime="stress", statement=statement,
                      evidence={}, sample_size=44, effect_r=-0.4,
                      p_value=0.001, caution=0.7)

    first = store.add_lesson(make())

    # Two contradictions retire it.
    for _ in range(2):
        store.review_lesson(first, supported=False, sample_size=30,
                            effect_r=0.3, p_value=0.01)
    live = [lsn for lsn in store.all_lessons() if lsn.statement == statement]
    assert not live, "it was not retired"

    # The next postmortem produces the identical recommendation.
    store.add_lesson(make())
    revived = [lsn for lsn in store.all_lessons() if lsn.statement == statement]
    assert not revived, (
        "a lesson retired by contradiction came back active with a clean record")


def test_the_live_path_and_the_backtest_compute_the_same_indicators():
    """The backtest calls prepare() over the whole frame; the live cycle used
    to fall through to a trailing-window recompute. Across a volatility regime
    shift the same gate read 3.4x apart, so a strategy was validated on one set
    of numbers and traded on another."""
    import numpy as np
    import pandas as pd
    from sentinel.strategy.registry import build, discover_builtin

    discover_builtin()
    rng = np.random.default_rng(11)
    index = pd.date_range("2026-01-01", periods=1400, freq="4h")
    index = index[index.weekday < 5]
    n = len(index)
    vol = np.where(np.arange(n) < n // 2, 0.0008, 0.0035)
    price = 1.10 + np.cumsum(rng.normal(0, 1, n) * vol)
    frame = pd.DataFrame({"open": price, "high": price + vol * 2,
                          "low": price - vol * 2, "close": price,
                          "volume": 1000.0}, index=index)
    frames = {"EUR_USD": frame}

    for name in ("donchian_trend", "carry_tilt", "carry_momentum"):
        prepared_strategy = build(name)
        prepared_strategy.prepare(frames)
        prepared = prepared_strategy.features_at("EUR_USD", frame, n - 1)
        live = build(name).features_at("EUR_USD", frame, n - 1)
        for key in prepared.index:
            a, b = prepared.get(key), live.get(key)
            if a is None or b is None:
                continue
            try:
                a_f, b_f = float(a), float(b)
            except (TypeError, ValueError):
                continue
            if np.isnan(a_f) and np.isnan(b_f):
                continue
            assert abs(a_f - b_f) < 1e-9, (
                f"{name}.{key}: prepared {a_f} vs live {b_f}")


def test_strategy_metadata_cannot_be_relabelled_accepted_after_registration():
    """The registry refuses a class that DECLARES lifecycle='accepted', because
    a plugin file would otherwise bypass the acceptance protocol entirely. With
    a mutable dataclass that refusal was one line from useless."""
    import dataclasses

    from sentinel.strategy.base import StrategyMeta
    from sentinel.strategy.families import trend

    assert dataclasses.fields(StrategyMeta)
    with pytest.raises(dataclasses.FrozenInstanceError):
        trend.DonchianTrend.meta.lifecycle = "accepted"


@pytest.mark.parametrize("raw", ["paper", " Paper ", "PAPER", "  paper"])
def test_the_paper_live_guard_is_not_defeated_by_whitespace(raw):
    """resolve_profile() lowercases and strips before matching, but the
    validator returned the value unchanged -- and every downstream safety
    comparison is an exact `== "paper"`."""
    from sentinel.core.config import ExecutionConfig, SentinelConfig

    assert ExecutionConfig(broker=raw).broker == "paper"
    with pytest.raises(Exception):
        SentinelConfig(execution=ExecutionConfig(broker=raw, venue_mode="live"))


def test_the_trial_family_resolves_from_the_registry(tmp_path):
    """summary()'s docstring promised the registry and the code never consulted
    it, so a renamed strategy was charged ZERO trials -- switching the
    multiple-testing bar off for exactly the case (a new variant of an existing
    idea) where it matters most."""
    from sentinel.research.trials import _family_from_registry
    from sentinel.strategy.registry import discover_builtin

    discover_builtin()
    assert _family_from_registry("donchian_trend") == "trend"
    assert _family_from_registry("supertrend_flip") == "trend"
    assert _family_from_registry("carry_tilt") == "carry"
    assert _family_from_registry("no_such_strategy") is None


def test_the_timezone_check_cannot_pass_vacuously():
    """'Europe/Frankfurt' is not an IANA key. ZoneInfo raised, a bare except
    turned that into 'nothing to verify', and an empty list read as
    agreement -- so the ECB's zone was never actually checked."""
    import datetime as dt

    from sentinel.core.tzrules import verify_against_zoneinfo

    try:
        import zoneinfo
        zoneinfo.ZoneInfo("UTC")
    except Exception:
        pytest.skip("no tzdata on this machine; nothing to verify against")

    moments = [dt.datetime(2026, month, 15, 12) for month in range(1, 13)]
    assert verify_against_zoneinfo("Europe/Frankfurt", moments) == []
    with pytest.raises(ValueError, match="not a zone"):
        verify_against_zoneinfo("Mars/Olympus", moments)


# --------------------------------------------------------------------------- #
# Final acceptance round: the defects a full end-to-end test found.
# --------------------------------------------------------------------------- #


def test_the_status_endpoint_survives_an_instrument_with_no_quote():
    """THE P0. `inf` means "this instrument has no quote at all", which a
    fresh install produces on its very first cycle -- the market store is
    empty. FastAPI serialises with allow_nan=False, so /api/status returned
    HTTP 500 from the first cycle onward, with nothing in the server log.
    """
    import json

    from sentinel.ops.health import HealthSnapshot

    snapshot = HealthSnapshot(
        connected=True, uptime_pct=100.0, offline_seconds=0.0,
        median_latency_ms=None, p95_latency_ms=float("nan"),
        clock_skew_ms=float("inf"), clock_regressions=0, outages_24h=0,
        longest_outage_sec=0.0,
        data_age_sec={"EUR_USD": float("inf"), "GBP_USD": 12.34,
                      "USD_JPY": float("nan")},
        warnings=[])
    payload = snapshot.to_dict()

    # This is the exact call FastAPI makes, and the exact one that raised.
    json.dumps(payload, allow_nan=False)

    assert payload["data_age_sec"]["EUR_USD"] is None, (
        "an unknown age must serialise as null, not as a number a caller "
        "could compare")
    assert payload["data_age_sec"]["GBP_USD"] == 12.3
    assert payload["data_age_sec"]["USD_JPY"] is None
    assert payload["clock_skew_ms"] is None


def test_an_adapter_refuses_to_run_without_a_symbol_table(monkeypatch):
    """A venue spelling reaching the reconciler turned a known position into a
    critical orphan and wrote a string matching nothing at the venue into the
    tamper-evident journal. MT5's symbols_get can transiently fail or return
    nothing right after initialize(), and carrying on was not survivable."""
    import sys

    from sentinel.core.errors import BrokerError
    from tests.fake_mt5 import FakeMT5

    class NoSymbols(FakeMT5):
        def symbols_get(self, *a, **k):
            raise RuntimeError("terminal busy at startup")

    class SilentlyEmpty(FakeMT5):
        def symbols_get(self, *a, **k):
            return []

    from sentinel.brokers.profiles import get_profile

    for terminal in (NoSymbols(suffix=".m"), SilentlyEmpty(suffix=".m")):
        monkeypatch.setitem(sys.modules, "MetaTrader5", terminal)
        # Re-import so the module-level lazy import picks up the fake.
        from sentinel.brokers.mt5 import MT5Broker
        monkeypatch.setattr("sentinel.brokers.mt5._SYMBOL_TABLE_RETRIES", 0)
        with pytest.raises(BrokerError, match="no symbols"):
            MT5Broker(profile=get_profile("amarkets"))


def test_the_watchdog_records_whether_the_flatten_actually_worked():
    """Journalling a flatten without reading the return value recorded "the
    book was closed" whenever the close was refused -- at the one moment
    nobody is watching."""
    import inspect

    from sentinel.ops import watchdog

    source = inspect.getsource(watchdog.flatten_positions)
    assert "flatten_summary" in source
    assert "still_open" in source
    assert "result" in source and "reject_reason" in source, (
        "the return value of close_position() is still not inspected")


def test_a_refused_privileged_config_write_is_journalled():
    """The TOTP gate logs the authorisation and the guard then raises, so an
    attempt to switch to LIVE TRADING or disable the second factor looked
    identical in the record to an ordinary risk-limit tweak."""
    import inspect

    from sentinel.api import main

    source = inspect.getsource(main.create_app)
    marker = source.index('@app.post("/api/config")')
    block = source[marker:marker + 2500]
    assert "WRITE_DENIED" in block, (
        "a refused config write leaves no trace in the audit journal")
    assert "attempted_paths" in block


def test_an_untracked_data_age_is_not_treated_as_perfectly_fresh(engine, ctx):
    """An instrument MISSING from a populated map has an unknown age, and was
    being approved with no veto at all by the control whose only job is to
    refuse stale prices."""
    from decimal import Decimal
    from sentinel.core.types import OrderIntent, Side

    order = OrderIntent(client_order_id="ST1", strategy="test",
                        instrument="EUR_USD", side=Side.BUY,
                        lots=Decimal("0.1"), stop_loss=Decimal("1.0820"),
                        take_profit=Decimal("1.0910"))

    # Ages ARE tracked, but this instrument is missing from the map.
    ctx.data_age_sec = {"GBP_USD": 0.0}
    assert any(v.rule == "stale_data"
               for v in engine.evaluate_entry(order, ctx).vetoes), (
        "an instrument with an unknown age was approved silently")

    # An EMPTY map is a different, legitimate mode: the caller tracks no ages
    # at all, which is how the backtest runs. It must not veto everything.
    ctx.data_age_sec = {}
    assert not any(v.rule == "stale_data"
                   for v in engine.evaluate_entry(order, ctx).vetoes)
