"""The brain (sentinel.brain): learning from every signal, shrink-only.

Each class pins one property the owner relies on. The recurring theme is that
every layer can only make the agent MORE careful, and that a broken layer
fails towards "no effect", never towards "more risk".
"""

from __future__ import annotations

import datetime as dt
import os
from decimal import Decimal as D
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pyotp
import pytest
from fastapi.testclient import TestClient

from sentinel.agent.orchestrator import Decision
from sentinel.brain import Brain
from sentinel.brain import stats
from sentinel.brain.lab import LabDeps, ResearchLab, run_safely
from sentinel.brain.shadow import resolve_path, scorecard, shadow_key
from sentinel.brain.store import BrainStore
from sentinel.core.audit import NullAudit
from sentinel.core.config import BrainConfig, SentinelConfig, StrategyAllocation
from sentinel.core.money import Instrument
from sentinel.core.types import Side, Signal

H = 3600 * 10**9
T0 = int(dt.datetime(2026, 3, 3, 10, tzinfo=dt.timezone.utc).timestamp() * 1e9)


def bars(rows, start_ns=T0 + H, step_ns=H):
    """OHLC frame from (open, high, low, close) tuples, one bar per hour."""
    idx = pd.to_datetime([start_ns + i * step_ns for i in range(len(rows))], utc=True)
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


class FakeMemory:
    def __init__(self, rows=None):
        self.rows = list(rows or [])

    def autopsies(self, strategy=None, limit=500):
        out = [r for r in self.rows if strategy is None or r["strategy"] == strategy]
        return sorted(out, key=lambda r: -r["closed_ns"])[:limit]

    def all_lessons(self):
        return []


def trades(rs, strategy="s1", regime="", start=T0):
    return [{"strategy": strategy, "r_multiple": r, "regime": regime,
             "outcome": "win" if r > 0.05 else "loss" if r < -0.05 else "scratch",
             "closed_ns": start + i * H} for i, r in enumerate(rs)]


@pytest.fixture
def make_brain(tmp_path):
    def make(cfg=None, memory=None, clock=lambda: T0):
        conf = cfg or BrainConfig()
        audit = NullAudit()
        brain = Brain(lambda: conf, BrainStore(tmp_path / "brain.db"),
                      memory if memory is not None else FakeMemory(), audit, clock=clock)
        return brain, audit
    return make


# --------------------------------------------------------------------------- #
# statistics
# --------------------------------------------------------------------------- #


class TestStatistics:
    def test_cusum_catches_a_one_sigma_drop_quickly(self):
        rng = np.random.default_rng(1)
        bad = rng.normal(-0.9, 1.0, 40)          # one sigma below a +0.1 baseline
        c = stats.cusum_down(bad, mu0=0.1, sigma=1.0)
        assert c["alarm"] and c["first_alarm_index"] is not None
        assert c["first_alarm_index"] < 20

    def test_cusum_rarely_alarms_on_a_healthy_strategy(self):
        rng = np.random.default_rng(2)
        alarms = sum(stats.cusum_down(rng.normal(0.1, 1.0, 60), mu0=0.1, sigma=1.0)["alarm"]
                     for _ in range(200))
        assert alarms / 200 < 0.4               # ARL ~170: 60 trades rarely alarm

    def test_posterior_and_allocation_only_shrink(self):
        losing = stats.posterior_positive([-1.0] * 12 + [0.5] * 3, prior_mean=0.1,
                                          prior_sd=0.25)
        assert losing["p_positive"] < 0.5
        m = stats.allocation_multiplier(losing["p_positive"], floor=0.25)
        assert 0.25 <= m < 1.0
        winning = stats.posterior_positive([1.0, 0.8, 1.2, 0.9, 1.1], prior_mean=0.1,
                                           prior_sd=0.25)
        assert stats.allocation_multiplier(winning["p_positive"], 0.25) == 1.0

    def test_small_samples_give_no_interval_not_a_flattering_one(self):
        assert stats.block_bootstrap_ci([1.0, 2.0, 3.0]) == (None, None)
        assert stats.mean_ci([0.5])[1:] == (None, None)
        assert stats.below_equity_average([1, 2], window=5) is None
        s = stats.summarise_r([])
        assert s["n"] == 0 and s["mean_r"] is None

    def test_verdicts(self):
        assert stats.verdict_of({"n": 5, "ci_high": -1}) == "insufficient"
        assert stats.verdict_of({"n": 30, "ci_low": -0.5, "ci_high": -0.1}) == "helped"
        assert stats.verdict_of({"n": 30, "ci_low": 0.1, "ci_high": 0.5}) == "hurt"
        assert stats.verdict_of({"n": 30, "ci_low": -0.1, "ci_high": 0.1}) == "unclear"

    def test_nearest_outcomes_finds_the_similar_rows(self):
        X = np.array([[0.0], [0.1], [5.0], [5.1], [5.2]])
        y = np.array([1.0, 1.0, -1.0, -1.0, -1.0])
        res = stats.nearest_outcomes(X, y, np.array([5.05]), k=3)
        assert res["n"] == 3 and res["mean_r"] == -1.0

    def test_drawdown_probability_grows_with_risk(self):
        lo = stats.probability_of_drawdown(0.1, 1.0, 0.5, 20, 200, n_paths=800)
        hi = stats.probability_of_drawdown(0.1, 1.0, 3.0, 20, 200, n_paths=800)
        assert lo is not None and hi is not None and hi > lo


# --------------------------------------------------------------------------- #
# the shadow book
# --------------------------------------------------------------------------- #


class TestResolvePath:
    def test_target_hit_is_a_win_after_cost(self):
        f = bars([(1.1000, 1.1030, 1.0995, 1.1025)])
        ok, r, kind, used = resolve_path(side="BUY", entry=1.1000, stop=1.0980,
                                         target=1.1020, horizon=5, bars=f, cost_r=0.1)
        assert ok and kind == "target" and used == 1
        assert r == pytest.approx(1.0 - 0.1)

    def test_a_bar_touching_both_counts_the_stop_first(self):
        f = bars([(1.1000, 1.1030, 1.0970, 1.1000)])
        ok, r, kind, _ = resolve_path(side="BUY", entry=1.1000, stop=1.0980, target=1.1020,
                                      horizon=5, bars=f)
        assert ok and kind == "stop" and r == pytest.approx(-1.0)

    def test_a_gap_through_the_stop_fills_at_the_open(self):
        f = bars([(1.0950, 1.0960, 1.0940, 1.0955)])
        ok, r, kind, _ = resolve_path(side="BUY", entry=1.1000, stop=1.0980, target=None,
                                      horizon=5, bars=f)
        assert ok and kind == "gap" and r == pytest.approx(-2.5)

    def test_sell_side_mirrors(self):
        f = bars([(1.1000, 1.1005, 1.0975, 1.0978)])
        ok, r, kind, _ = resolve_path(side="SELL", entry=1.1000, stop=1.1020, target=1.0980,
                                      horizon=5, bars=f)
        assert ok and kind == "target" and r == pytest.approx(1.0)

    def test_horizon_marks_at_the_last_close(self):
        f = bars([(1.1000, 1.1005, 1.0995, 1.1002), (1.1002, 1.1008, 1.0999, 1.1005)])
        ok, r, kind, used = resolve_path(side="BUY", entry=1.1000, stop=1.0990,
                                         target=1.1050, horizon=2, bars=f)
        assert ok and kind == "horizon" and used == 2 and r == pytest.approx(0.5)

    def test_unfinished_path_stays_open_and_a_bad_stop_is_invalid(self):
        f = bars([(1.1000, 1.1005, 1.0995, 1.1002)])
        assert resolve_path(side="BUY", entry=1.1, stop=1.09, target=None, horizon=5,
                            bars=f)[0] is False
        assert resolve_path(side="BUY", entry=1.1, stop=1.2, target=None, horizon=5,
                            bars=f)[2] == "invalid"


class TestScorecard:
    def test_a_rule_that_skipped_losers_helped(self):
        rows = [{"strategy": "s1", "action": "vetoed", "rule": "spread", "outcome_r": -0.8 + 0.01 * i}
                for i in range(20)]
        card = scorecard(rows)
        assert card["rules"][0]["rule"] == "spread" and card["rules"][0]["verdict"] == "helped"

    def test_a_shrink_before_a_loss_is_credited_as_saved_r(self):
        rows = [{"strategy": "s1", "action": "executed", "outcome_r": -1.0,
                 "layers": {"drift": 0.5}} for _ in range(20)]
        layer = scorecard(rows)["layers"][0]
        assert layer["layer"] == "drift" and layer["saved_r"] == pytest.approx(10.0)
        assert layer["n"] == 20 and layer["verdict"] == "helped"


class TestStore:
    def test_one_row_per_signal_and_an_execution_upgrades_it(self, tmp_path):
        store = BrainStore(tmp_path / "b.db")
        row = {"key": "k1", "ts_ns": T0, "strategy": "s1", "instrument": "EUR_USD",
               "side": "BUY", "entry": 1.1, "stop": 1.09, "horizon": 10, "action": "vetoed",
               "rule": "spread"}
        assert store.record_signal(row) is True
        assert store.record_signal(row) is False
        store.record_signal({**row, "action": "executed", "layers": {"drift": 0.5}})
        got = store.open_signals()[0]
        assert got["action"] == "executed" and got["rule"] is None
        assert got["layers"] == {"drift": 0.5}
        assert oct(os.stat(tmp_path / "b.db").st_mode & 0o777) == "0o600"


# --------------------------------------------------------------------------- #
# the Brain service
# --------------------------------------------------------------------------- #


def _signal(ts=T0, side=Side.BUY, stop=D("1.0980"), target=D("1.1020"), strategy="s1"):
    return Signal(strategy=strategy, instrument="EUR_USD", side=side, strength=0.7,
                  stop_price=stop, target_price=target, horizon_bars=5, timeframe="H1",
                  decision_ns=ts, features={"mom": 0.3})


def _decision(action="vetoed", rule="spread", entry="1.1000"):
    return Decision(ts_ns=T0, strategy="s1", instrument="EUR_USD", action=action, side="BUY",
                    entry=entry, vetoes=[{"rule": rule}] if action == "vetoed" else [],
                    diagnostics={"stop_pips": "20", "round_trip_cost_pips": "1",
                                 "meta_features": {"x": 1.0}})


class TestShadowRecording:
    def test_a_vetoed_signal_is_scored_against_the_bars_that_followed(self, make_brain):
        brain, _ = make_brain()
        brain.record(_signal(), _decision())
        frame = bars([(1.1000, 1.1030, 1.0995, 1.1025)])
        snap = SimpleNamespace(frames_for=lambda tf: {"EUR_USD": frame}, frames={})
        assert brain.resolve(snap, T0 + 2 * H) == 1
        row = brain.store.resolved_signals()[0]
        assert row["exit"] == "target" and row["rule"] == "spread"
        assert row["outcome_r"] == pytest.approx(1.0 - 1 / 20)

    def test_bars_before_the_signal_are_never_used(self, make_brain):
        brain, _ = make_brain()
        brain.record(_signal(ts=T0 + 5 * H), _decision())
        frame = bars([(1.1000, 1.1030, 1.0995, 1.1025)])          # at T0 + 1h only
        snap = SimpleNamespace(frames_for=lambda tf: {"EUR_USD": frame}, frames={})
        assert brain.resolve(snap, T0 + 6 * H) == 0
        assert brain.store.open_signals()

    def test_recording_never_raises(self, make_brain):
        brain, _ = make_brain()
        brain.record(_signal(), Decision(ts_ns=T0, strategy="s1", instrument="EUR_USD",
                                         action="vetoed", entry="not-a-number"))
        assert "record" in brain.last_error

    def test_disabled_records_nothing(self, make_brain):
        brain, _ = make_brain(BrainConfig(shadow_book=False))
        brain.record(_signal(), _decision())
        assert brain.store.open_signals() == []


class TestCooldowns:
    def test_three_losses_rest_the_account(self, make_brain):
        brain, audit = make_brain()
        brain.on_trades(trades([-1.0, -1.0, -1.0]), now_ns=T0)
        cd = brain.cooldowns(T0 + H)
        assert "*" in cd and "3 losing trades" in cd["*"]
        assert any(r.event == "brain.cooldown" for r in audit.records)
        assert brain.cooldowns(T0 + 5 * H) == {}            # 4h rest is over

    def test_a_win_resets_the_streak(self, make_brain):
        brain, _ = make_brain()
        brain.on_trades(trades([-1.0, -1.0, 1.5, -1.0]), now_ns=T0)
        assert brain.cooldowns(T0 + H) == {}

    def test_a_strategy_rests_on_its_own_streak(self, make_brain):
        brain, _ = make_brain(BrainConfig(loss_streak_limit=0))
        brain.on_trades(trades([-1.0] * 4, strategy="s2"), now_ns=T0)
        assert set(brain.cooldowns(T0 + H)) == {"s2"}

    def test_the_owner_can_lift_a_rest_and_it_is_journalled(self, make_brain):
        brain, audit = make_brain()
        brain.on_trades(trades([-1.0] * 3), now_ns=T0)
        brain.clear_cooldown("*", "owner1")
        assert brain.cooldowns(T0 + H) == {}
        assert audit.records[-1].actor == "owner1"
        with pytest.raises(ValueError):
            brain.clear_cooldown("*", "owner1")


class TestStrategyLayers:
    def test_drift_shrinks_a_strategy_that_stopped_working(self, make_brain):
        brain, audit = make_brain(memory=FakeMemory(trades([0.2] * 5 + [-1.0] * 12)))
        m, reasons, layers = brain.strategy_layers("s1")
        assert layers.get("drift") == 0.5 and m <= 0.5
        assert any("drift" in r for r in reasons)
        assert any(r.event == "brain.drift" and r.payload["alarm"] for r in audit.records)

    def test_drift_has_hysteresis(self, make_brain):
        mem = FakeMemory(trades([-1.0] * 10))
        brain, _ = make_brain(memory=mem, cfg=BrainConfig(equity_filter_enabled=False))
        assert "drift" in brain.strategy_layers("s1")[2]
        # Each -1R adds (0.1 + 1) - 0.5 = 0.6 (ten: 6.0 > h = 4); each +0.6R
        # removes 1.0. Three wins leave 3.0: below h, above h/2 -- the alarm
        # holds rather than flapping. Five wins leave 1.0: it clears.
        mem.rows = trades([-1.0] * 10 + [0.6] * 3)
        brain._version += 1
        assert stats.cusum_down([r["r_multiple"] for r in mem.rows], mu0=0.1,
                                sigma=1.0)["stat"] == pytest.approx(3.0)
        assert "drift" in brain.strategy_layers("s1")[2]
        mem.rows = trades([-1.0] * 10 + [0.6] * 5)
        brain._version += 1
        assert "drift" not in brain.strategy_layers("s1")[2]
        # A fresh brain that never alarmed does not alarm at 3.0 either.
        brain._drift_alarmed = {}
        mem.rows = trades([-1.0] * 10 + [0.6] * 3)
        brain._version += 1
        assert "drift" not in brain.strategy_layers("s1")[2]

    def test_a_healthy_strategy_is_left_alone(self, make_brain):
        rng = np.random.default_rng(5)
        brain, _ = make_brain(memory=FakeMemory(trades(list(rng.normal(0.5, 0.3, 30)))))
        m, reasons, layers = brain.strategy_layers("s1", "trend")
        assert m == 1.0 and not layers

    def test_allocation_shrinks_a_losing_regime_cell_only(self, make_brain):
        rows = trades([-0.8] * 10, regime="range") + trades([0.9] * 10, regime="trend",
                                                             start=T0 + 100 * H)
        cfg = BrainConfig(drift_enabled=False, equity_filter_enabled=False)
        brain, _ = make_brain(cfg=cfg, memory=FakeMemory(rows))
        assert "allocation" in brain.strategy_layers("s1", "range")[2]
        assert "allocation" not in brain.strategy_layers("s1", "trend")[2]

    def test_a_broken_memory_means_no_effect(self, make_brain):
        class Broken(FakeMemory):
            def autopsies(self, *a, **k):
                raise RuntimeError("disk")
        brain, _ = make_brain(memory=Broken())
        assert brain.strategy_layers("s1") == (1.0, [], {})

    def test_every_multiplier_is_at_most_one(self, make_brain):
        brain, _ = make_brain(memory=FakeMemory(trades([-2.0] * 30, regime="r")))
        m, _, layers = brain.strategy_layers("s1", "r")
        assert 0.0 <= m <= 1.0 and all(0.0 <= v <= 1.0 for v in layers.values())


class TestSimilarity:
    def test_situations_that_lost_before_shrink_the_next_one(self, make_brain):
        brain, _ = make_brain(BrainConfig(similarity_min_samples=40, similarity_k=15))
        rng = np.random.default_rng(3)
        for i in range(80):
            x = float(rng.uniform(-1, 1))
            key = shadow_key("s1", "EUR_USD", "BUY", T0 + i)
            brain.store.record_signal({"key": key, "ts_ns": T0 + i, "strategy": "s1",
                                       "instrument": "EUR_USD", "side": "BUY", "entry": 1.1,
                                       "stop": 1.09, "horizon": 5, "action": "executed",
                                       "features": {"x": x}})
            brain.store.resolve_signal(key, outcome_r=-1.0 if x > 0 else 1.0,
                                       exit_kind="stop", bars_seen=1, resolved=True)
        m, why, info = brain.similarity("s1", {"x": 0.8})
        assert m == 0.5 and "similar" in why and info["n"] == 15
        assert brain.similarity("s1", {"x": -0.8})[0] == 1.0

    def test_too_little_history_means_no_opinion(self, make_brain):
        brain, _ = make_brain()
        assert brain.similarity("s1", {"x": 1.0})[0] == 1.0


class TestMetaModel:
    def test_only_an_eligible_model_can_be_approved(self, make_brain, tmp_path):
        brain, _ = make_brain()
        brain.store.add_model("m1", str(tmp_path / "m1.joblib"), "0" * 64,
                              {"eligible": False})
        with pytest.raises(ValueError):
            brain.approve_model("m1", "owner1")

    def test_a_tampered_model_file_is_not_loaded(self, make_brain, tmp_path):
        brain, _ = make_brain()
        path = tmp_path / "m2.joblib"
        path.write_bytes(b"not the model that was approved")
        brain.store.add_model("m2", str(path), "f" * 64, {"eligible": True})
        brain.approve_model("m2", "owner1")
        assert brain.meta_gate() is None and "hash" in brain.last_error
        brain.retire_model("owner1")
        assert brain.store.active_model() is None


class TestWeeklyReport:
    def test_the_report_is_stored_and_announced_once_a_week(self, make_brain):
        sunday = int(dt.datetime(2026, 3, 8, 9, tzinfo=dt.timezone.utc).timestamp() * 1e9)
        brain, audit = make_brain(cfg=BrainConfig(lab_enabled=False),
                                  memory=FakeMemory(trades([1.0, -1.0], start=sunday - 2 * H)))
        brain.tick(sunday)
        brain.tick(sunday + H)
        reports = [r for r in audit.records if r.event == "brain.report"]
        assert len(reports) == 1 and reports[0].payload["trades"]["n"] == 2
        assert brain.store.reports("weekly")


# --------------------------------------------------------------------------- #
# the research lab
# --------------------------------------------------------------------------- #


def _trending_frame(n=1500, seed=11):
    rng = np.random.default_rng(seed)
    close = 1.10 * np.exp(np.cumsum(rng.normal(0.00004, 0.0012, n)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.0004, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.0004, n)))
    idx = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                         "volume": 100.0}, index=idx)


class TestResearchLab:
    def test_a_night_run_reports_every_enabled_strategy(self, tmp_path):
        cfg = SentinelConfig(strategies=[StrategyAllocation(
            name="donchian_trend", enabled=True, instruments=["EUR_USD"], timeframe="H1")])
        cfg.agent.session_windows_utc = [[0, 24]]
        cfg.agent.trade_days = [0, 1, 2, 3, 4, 5, 6]
        store = SimpleNamespace(frame=lambda sym, tf, limit=5000: _trending_frame())
        lab = ResearchLab(LabDeps(config=lambda: cfg, bar_store=store,
                                  instruments=lambda: {"EUR_USD": Instrument("EUR_USD", "EUR",
                                                                             "USD")},
                                  conversions=lambda: {"USD": D("1")},
                                  model_dir=tmp_path / "models"))
        report = run_safely(lab, max_seconds=120, max_bars=1500)
        assert not report["errors"], report["errors"]
        row = report["strategies"][0]
        assert row["strategy"] == "donchian_trend"
        assert row["status"] in ("alive", "weak", "dead", "insufficient")
        assert row["bars"] == {"EUR_USD": 1500}

    def test_no_broker_bars_is_reported_not_invented(self, tmp_path):
        cfg = SentinelConfig(strategies=[StrategyAllocation(
            name="donchian_trend", enabled=True, instruments=["EUR_USD"], timeframe="H1")])
        store = SimpleNamespace(frame=lambda sym, tf, limit=5000: None)
        lab = ResearchLab(LabDeps(config=lambda: cfg, bar_store=store,
                                  instruments=lambda: {"EUR_USD": Instrument("EUR_USD", "EUR",
                                                                             "USD")},
                                  conversions=lambda: {"USD": D("1")}))
        report = lab.run(max_seconds=30, max_bars=1000)
        assert report["strategies"][0]["status"] == "no_data"

    def test_meta_training_purges_labels_that_saw_the_holdout(self, tmp_path):
        rng = np.random.default_rng(4)
        logs = []
        for i in range(700):
            x = float(rng.normal())
            up = (x + rng.normal(0, 0.6)) > 0
            ts = T0 + i * H
            logs.append({"ts_ns": ts, "label_end_ns": ts + 8 * H, "side_sign": 1,
                         "fwd_ret_h": 0.002 if up else -0.002,
                         "features": {"x": x, "noise": float(rng.normal())}})
        lab = ResearchLab(LabDeps(config=lambda: None, bar_store=None,
                                  instruments=dict, conversions=dict,
                                  model_dir=tmp_path / "models"))
        out = lab._train_meta(logs, min_auc=0.55)
        assert out["n_purged"] >= 7
        assert out["trained"] and out["holdout"]["auc"] > 0.7
        assert out["eligible"] and (tmp_path / "models").exists()
        assert len(out["sha256"]) == 64

    def test_absorbing_a_run_sets_baselines_and_offers_the_model(self, make_brain):
        brain, audit = make_brain()
        brain._absorb_lab({"baselines": {"s1": {"mean_r": 0.3, "sd_r": 1.2, "n": 40}},
                           "strategies": [{"strategy": "s1", "status": "alive"}],
                           "meta": {"model_id": "meta-1", "eligible": True, "path": "/x",
                                    "sha256": "a" * 64}})
        assert brain.baseline("s1")["source"] == "lab"
        assert brain.store.model("meta-1")["status"] == "candidate"
        assert any(r.event == "brain.lab" for r in audit.records)


# --------------------------------------------------------------------------- #
# inside the agent
# --------------------------------------------------------------------------- #


def _agent_with_brain(tmp_path, cfg=None):
    from tests.test_ai_news_manual import TICKET, _agent
    agent, broker = _agent(tmp_path)
    if cfg is not None:
        agent.config.brain = cfg
    brain = Brain(lambda: agent.config.brain, BrainStore(tmp_path / "brain.db"),
                  agent.memory, agent.audit, clock=lambda: broker.now_ns)
    agent.brain = brain
    return agent, broker, brain, TICKET


class TestInsideTheAgent:
    def test_a_rest_blocks_a_manual_ticket_too(self, tmp_path):
        agent, broker, brain, ticket = _agent_with_brain(tmp_path)
        assert agent.manual_order(**ticket, preview=True).action == "preview"
        brain.on_trades(trades([-1.0] * 3, start=broker.now_ns - 3 * H), broker.now_ns)
        d = agent.manual_order(**ticket, preview=True)
        assert d.action == "vetoed"
        assert any(v["rule"] == "loss_streak_cooldown" for v in d.vetoes)

    def test_gap_stress_shrinks_then_refuses(self, tmp_path):
        agent, broker, brain, ticket = _agent_with_brain(tmp_path)
        full = D(agent.manual_order(**ticket, preview=True).lots)
        # 1% of equity for a 2% EUR/USD gap: about 0.05 lots on 10 000 USD.
        agent.config.brain.stress_loss_limit_pct = 1.0
        shrunk = agent.manual_order(**ticket, preview=True)
        assert shrunk.action == "preview"
        assert D(shrunk.lots) < full
        assert any(w["rule"] == "stress_shrunk" for w in shrunk.warnings)
        agent.config.brain.stress_loss_limit_pct = 0.01
        refused = agent.manual_order(**ticket, preview=True)
        assert refused.action == "vetoed"
        assert any(v["rule"] == "stress_gap" for v in refused.vetoes)

    def test_brain_layers_join_the_caution_product(self, tmp_path):
        agent, broker, brain, _ = _agent_with_brain(tmp_path)
        brain.strategy_layers = lambda strategy, regime="": (0.5, ["drift: test"],
                                                             {"drift": 0.5})
        caution, reasons = agent._caution_for("s1", "EUR_USD", "")
        assert caution <= 0.5 and "drift: test" in reasons
        assert agent._last_layers == {"drift": 0.5}

    def test_a_crashing_brain_cannot_stop_the_cycle(self, tmp_path):
        agent, broker, brain, _ = _agent_with_brain(tmp_path)

        def boom(*a, **k):
            raise RuntimeError("brain down")
        brain.cooldowns = boom
        brain.strategy_layers = boom
        report = agent.cycle()
        assert report is not None
        # The failure is journalled, and the manual path still works.
        assert "brain_context_failed" in (tmp_path / "audit.jsonl").read_text()
        from tests.test_ai_news_manual import TICKET
        assert agent.manual_order(**TICKET, preview=True).action == "preview"


# --------------------------------------------------------------------------- #
# the API
# --------------------------------------------------------------------------- #


@pytest.fixture
def api(tmp_path):
    from sentinel.api.main import create_app
    from sentinel.api.security import SecurityManager
    from sentinel.api.state import Runtime
    os.environ["SENTINEL_JWT_SECRET"] = "t" * 48
    agent, broker, brain, _ = _agent_with_brain(tmp_path)
    runtime = Runtime(agent, tmp_path / "config.json")
    runtime.brain = brain
    security = SecurityManager(agent.audit, secret="t" * 48)
    owner, _ = security.add_user("owner1", "a-sufficiently-long-password", "owner")
    security.add_user("viewer1", "another-long-password-x", "viewer")
    client = TestClient(create_app(runtime, security))

    def login(name, pw):
        r = client.post("/api/auth/login", json={"username": name, "password": pw})
        return {"Authorization": f"Bearer {r.json()['token']}"}
    oh = login("owner1", "a-sufficiently-long-password")

    def totp():
        security._used_totp.clear()
        return dict(oh, **{"X-TOTP": pyotp.TOTP(owner.totp_secret).now()})
    return {"client": client, "runtime": runtime, "brain": brain, "broker": broker,
            "oh": oh, "totp": totp, "vh": login("viewer1", "another-long-password-x")}


class TestBrainApi:
    def test_everyone_signed_in_can_read(self, api):
        r = api["client"].get("/api/brain", headers=api["vh"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["available"] and body["enabled"]
        assert "scorecard" in body and "cooldowns" in body

    def test_settings_need_the_owner_and_are_validated(self, api):
        c = api["client"]
        body = {"patch": {"loss_streak_limit": 5}}
        assert c.post("/api/brain/settings", json=body, headers=api["vh"]).status_code == 403
        r = c.post("/api/brain/settings", json=body, headers=api["totp"]())
        assert r.status_code == 200, r.text
        assert api["runtime"].agent.config.brain.loss_streak_limit == 5
        bad = c.post("/api/brain/settings", json={"patch": {"drift_multiplier": 3.0}},
                     headers=api["totp"]())
        assert bad.status_code == 400

    def test_the_scenario_table_is_replaced_not_merged(self, api):
        r = api["client"].post("/api/brain/settings",
                               json={"patch": {"stress_scenarios": {"CHF": 0.3}}},
                               headers=api["totp"]())
        assert r.status_code == 200, r.text
        assert api["runtime"].agent.config.brain.stress_scenarios == {"CHF": 0.3, "*": 0.03}

    def test_lifting_a_rest(self, api):
        brain, now = api["brain"], api["broker"].now_ns
        brain.on_trades(trades([-1.0] * 3, start=now - 3 * H), now)
        assert "*" in api["client"].get("/api/brain", headers=api["vh"]).json()["cooldowns"]
        r = api["client"].post("/api/brain/cooldown/clear", json={"scope": "*"},
                               headers=api["totp"]())
        assert r.status_code == 200, r.text
        r = api["client"].post("/api/brain/cooldown/clear", json={"scope": "*"},
                               headers=api["totp"]())
        assert r.status_code == 404

    def test_approving_an_unknown_model_is_refused(self, api):
        r = api["client"].post("/api/brain/model/approve", json={"model_id": "meta-nope"},
                               headers=api["totp"]())
        assert r.status_code == 409
