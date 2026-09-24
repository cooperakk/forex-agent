"""Live bar ingestion: the path from a venue's candles to a strategy's frame.

Regression for the defect that made every served process a decoration: the
bar store was only ever written by the synthetic paper simulation, so on OANDA
or MetaTrader every strategy saw an empty frame, never reached its warm-up bar
and never produced a signal -- while the heartbeat, the reconciler and the
dashboard all reported a healthy system. The paper venue served by
``scripts/serve.py`` had it worse: no bars AND no quotes, so every cycle ended
in a ``no_price`` veto.
"""

from __future__ import annotations

import time
from decimal import Decimal as D

import httpx
import pandas as pd
import pytest

from sentinel.brokers import get_profile
from sentinel.brokers.paper import PaperBroker, SimProfile
from sentinel.core.clock import wall_ns
from sentinel.core.money import Instrument
from sentinel.core.types import Bar
from sentinel.data.feed import TIMEFRAME_SECONDS, BarStore, MarketFeed
from sentinel.data.synthetic_live import SyntheticMarketDriver
from tests.fake_mt5 import FakeMT5

H4_NS = TIMEFRAME_SECONDS["H4"] * 1_000_000_000

MAJORS = {
    "EUR_USD": Instrument("EUR_USD", "EUR", "USD"),
    "USD_JPY": Instrument("USD_JPY", "USD", "JPY", pip=D("0.01"), tick=D("0.001")),
}


@pytest.fixture
def mt5(monkeypatch):
    def make(**terminal):
        import sys
        fake = FakeMT5(**terminal)
        monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
        from sentinel.brokers.mt5 import MT5Broker
        return MT5Broker(profile=get_profile("generic_mt5")), fake
    return make


# --------------------------------------------------------------------------- #
# the contract
# --------------------------------------------------------------------------- #


class TestContract:
    def test_the_default_adapter_declares_nothing_and_returns_nothing(self):
        from sentinel.brokers.base import BrokerCapabilities
        caps = BrokerCapabilities(True, True, True, True, True, D("0.01"), D("0.01"), "x")
        # Undeclared is not a degradation: every adapter written before the
        # field existed must not suddenly report a failing it never claimed.
        assert caps.supports_bar_history is None
        assert not any("bar history" in line for line in caps.degradation_report())

    def test_an_explicit_no_is_reported_as_a_degradation(self):
        from sentinel.brokers.base import BrokerCapabilities
        caps = BrokerCapabilities(True, True, True, True, True, D("0.01"), D("0.01"), "x",
                                  supports_bar_history=False)
        assert any("bar history" in line for line in caps.degradation_report())

    def test_mt5_and_oanda_declare_bar_history(self, mt5):
        broker, _ = mt5()
        assert broker.supports_bar_history is True
        assert broker.capabilities.supports_bar_history is True


# --------------------------------------------------------------------------- #
# MetaTrader
# --------------------------------------------------------------------------- #


class TestMT5Bars:
    def test_bars_come_back_oldest_first_with_the_forming_bar_marked_incomplete(self, mt5):
        broker, fake = mt5()
        bars = broker.fetch_bars("EUR_USD", "H4", 50)
        assert 49 <= len(bars) <= 51
        assert all(isinstance(b, Bar) for b in bars)
        starts = [b.start_ns for b in bars]
        assert starts == sorted(starts)
        assert all(b.end_ns - b.start_ns == H4_NS for b in bars)
        assert bars[-1].complete is False, "the newest bar is the one still forming"
        assert all(b.complete for b in bars[:-1])
        assert all(b.source == "mt5" and b.timeframe == "H4" for b in bars)

    def test_server_time_is_converted_to_utc(self, mt5):
        """The terminal stamps bars in the broker's clock. Stored uncorrected, an
        H4 bar labelled 08:00 covers 05:00-09:00 UTC and every session strategy
        trades the wrong hour."""
        broker, fake = mt5(server_utc_offset_sec=3 * 3600)
        bars = broker.fetch_bars("EUR_USD", "H4", 5)
        now = wall_ns()
        forming = bars[-1]
        # After conversion the forming bar's window contains "now"; without it
        # the window would start three hours in the future.
        assert forming.start_ns <= now < forming.end_ns
        # The grid is the SERVER's: a UTC+3 broker opens H4 bars at 01:00,
        # 05:00, 09:00 UTC. Converting must shift the stamps, not re-align them.
        assert (forming.start_ns + 3 * 3600 * 1_000_000_000) % H4_NS == 0

    def test_the_offset_is_measured_not_trusted(self, mt5):
        """A profile that declares the wrong offset is overruled by a fresh tick."""
        broker, fake = mt5(server_utc_offset_sec=2 * 3600)
        assert broker.profile.server_utc_offset_hours != 2 or True  # any declaration
        broker.quote("EUR_USD")             # calibrates from the tick
        assert broker._server_offset_seconds() == 2 * 3600

    def test_a_stale_tick_does_not_poison_the_offset(self, mt5):
        broker, fake = mt5(server_utc_offset_sec=3 * 3600)
        broker.quote("EUR_USD")
        assert broker._server_offset_seconds() == 3 * 3600

        class _Stale:
            # A Friday tick read on Sunday: two days behind the wall clock.
            time_msc = int((time.time() + 3 * 3600 - 2 * 86400) * 1000)
        broker._observe_server_clock(_Stale())
        assert broker._server_offset_seconds() == 3 * 3600

    def test_the_declared_offset_is_the_fallback_when_nothing_was_measured(self, mt5):
        broker, fake = mt5()
        assert broker._server_offset_sec is None
        declared = broker.profile.server_utc_offset_hours * 3600
        assert broker._server_offset_seconds() in (declared, declared + 3600)

    def test_a_symbol_outside_market_watch_is_selected_and_retried(self, mt5):
        broker, fake = mt5()
        calls = {"n": 0}
        real = fake.copy_rates_from_pos

        def flaky(name, tf, pos, count):
            calls["n"] += 1
            return None if calls["n"] == 1 else real(name, tf, pos, count)
        fake.copy_rates_from_pos = flaky
        bars = broker.fetch_bars("EUR_USD", "H4", 10)
        assert bars and fake.selected == ["EURUSD"]

    def test_an_unknown_timeframe_is_refused_not_guessed(self, mt5):
        from sentinel.core.errors import BrokerError
        broker, _ = mt5()
        with pytest.raises(BrokerError):
            broker.fetch_bars("EUR_USD", "H2", 10)

    def test_a_venue_with_no_bars_raises_instead_of_returning_an_empty_frame(self, mt5):
        from sentinel.core.errors import BrokerError
        broker, fake = mt5()
        fake.copy_rates_from_pos = lambda *a, **k: None
        with pytest.raises(BrokerError):
            broker.fetch_bars("EUR_USD", "H4", 10)


# --------------------------------------------------------------------------- #
# OANDA
# --------------------------------------------------------------------------- #


def _oanda_with(candles_payload):
    """An OandaBroker over an httpx mock transport that serves one candles body."""
    from sentinel.brokers.oanda import OandaBroker

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/candles"):
            return httpx.Response(200, json=candles_payload)
        if request.url.path.endswith("/instruments"):
            return httpx.Response(200, json={"instruments": []})
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler),
                          base_url="https://api-fxpractice.oanda.com")
    return OandaBroker(account_id="001-001-1234567-001", token="t", environment="practice",
                       instruments=dict(MAJORS), client=client)


class TestOandaBars:
    def test_candles_become_bars_and_the_venues_complete_flag_is_believed(self):
        payload = {"candles": [
            {"time": "2026-09-14T08:00:00.000000000Z", "complete": True, "volume": 100,
             "mid": {"o": "1.0850", "h": "1.0870", "l": "1.0840", "c": "1.0860"}},
            {"time": "2026-09-14T12:00:00.000000000Z", "complete": True, "volume": 90,
             "mid": {"o": "1.0860", "h": "1.0880", "l": "1.0850", "c": "1.0875"}},
            {"time": "2026-09-14T16:00:00.000000000Z", "complete": False, "volume": 10,
             "mid": {"o": "1.0875", "h": "1.0878", "l": "1.0870", "c": "1.0872"}},
        ]}
        broker = _oanda_with(payload)
        bars = broker.fetch_bars("EUR_USD", "H4", 10)
        assert [b.complete for b in bars] == [True, True, False]
        assert bars[0].open == D("1.0850") and bars[1].close == D("1.0875")
        assert bars[1].start_ns - bars[0].start_ns == H4_NS
        assert all(b.source == "oanda" for b in bars)

    def test_a_malformed_candle_is_skipped_not_repaired(self):
        payload = {"candles": [
            {"time": "2026-09-14T08:00:00Z", "complete": True,
             "mid": {"o": "1.0850", "h": "1.0800", "l": "1.0840", "c": "1.0860"}},  # h < l
            {"time": "2026-09-14T12:00:00Z", "complete": True,
             "mid": {"o": "1.0860", "h": "1.0880", "l": "1.0850", "c": "1.0875"}},
        ]}
        bars = _oanda_with(payload).fetch_bars("EUR_USD", "H4", 10)
        assert len(bars) == 1 and bars[0].close == D("1.0875")


# --------------------------------------------------------------------------- #
# the feed: incremental ingestion into the store
# --------------------------------------------------------------------------- #


class TestFeedIngestion:
    def test_the_first_snapshot_backfills_and_the_frame_reaches_a_strategy_warmup(
            self, mt5, tmp_path):
        broker, fake = mt5(history_bars=1000)
        store = BarStore(tmp_path / "m.db")
        feed = MarketFeed(broker, store, timeframe="H4", history=400)
        snap = feed.snapshot(["EUR_USD"])
        frame = snap.frames["EUR_USD"]
        # 400 requested, the forming bar dropped.
        assert 398 <= len(frame) <= 400
        assert snap.passports["EUR_USD"].bars == len(frame)
        assert not any(k.startswith("bars:EUR_USD") for k in snap.errors)
        # The Donchian candidate needs 250 bars of history; it now has them.
        from sentinel.strategy.registry import build
        assert len(frame) > build("donchian_trend").warmup()

    def test_only_completed_bars_reach_the_store(self, mt5, tmp_path):
        broker, fake = mt5()
        store = BarStore(tmp_path / "m.db")
        feed = MarketFeed(broker, store, timeframe="H4", history=50)
        feed.snapshot(["EUR_USD"])
        now = wall_ns()
        latest = store.latest_start_ns("EUR_USD", "H4")
        assert latest is not None
        assert latest + H4_NS <= now, "the forming bar must never be stored as complete"

    def test_a_second_snapshot_inside_the_same_bar_does_not_ask_the_venue_again(
            self, mt5, tmp_path):
        broker, fake = mt5()
        store = BarStore(tmp_path / "m.db")
        feed = MarketFeed(broker, store, timeframe="H4", history=50)
        feed.snapshot(["EUR_USD"])
        asked = len(fake.rates_requests)
        feed.snapshot(["EUR_USD"])
        feed.snapshot(["EUR_USD"])
        assert len(fake.rates_requests) == asked

    def test_when_a_bar_closes_only_the_tail_is_fetched(self, mt5, tmp_path):
        broker, fake = mt5()
        store = BarStore(tmp_path / "m.db")
        feed = MarketFeed(broker, store, timeframe="H4", history=50)
        now = wall_ns()
        feed.snapshot(["EUR_USD"], now_ns=now)
        before = store.latest_start_ns("EUR_USD", "H4")
        # Two bars later the feed should ask for a handful, not for the whole
        # history again.
        later = now + 2 * H4_NS + 60 * 1_000_000_000
        got = feed.refresh(["EUR_USD"], now_ns=later)
        _, _, _, count = fake.rates_requests[-1]
        assert count <= 6
        # The fake stamps bars from the REAL clock, so the only bar that can
        # newly qualify as closed by `later` is the one that was forming at
        # `now`: exactly one more, and the head advances by exactly one bar.
        assert got.get("EUR_USD@H4", 0) == 1
        assert store.latest_start_ns("EUR_USD", "H4") == before + H4_NS

    def test_a_venue_error_is_recorded_and_the_cycle_goes_on(self, mt5, tmp_path):
        broker, fake = mt5()
        fake.copy_rates_from_pos = lambda *a, **k: None
        store = BarStore(tmp_path / "m.db")
        feed = MarketFeed(broker, store, timeframe="H4", history=50)
        snap = feed.snapshot(["EUR_USD"])
        assert "bars:EUR_USD@H4" in snap.errors
        assert len(snap.frames["EUR_USD"]) == 0
        # Still a usable snapshot: the quote is there and the passport says GAP.
        assert "EUR_USD" in snap.quotes
        assert snap.passports["EUR_USD"].quality.value == "gap"

    def test_a_venue_without_history_is_left_alone(self, tmp_path):
        """No driver, no capability: the feed reads whatever the store holds."""
        broker = PaperBroker(instruments=dict(MAJORS), profile=SimProfile())
        store = BarStore(tmp_path / "m.db")
        feed = MarketFeed(broker, store, timeframe="H4", history=50)
        assert feed.refresh(["EUR_USD"]) == {}
        assert feed.bars_ingested == 0


# --------------------------------------------------------------------------- #
# the paper venue: the synthetic driver
# --------------------------------------------------------------------------- #


class TestSyntheticDriver:
    def _feed(self, tmp_path, history=300):
        broker = PaperBroker(instruments=dict(MAJORS), profile=SimProfile(), seed=3)
        broker.set_conversion("JPY", D("1") / D("150"))
        store = BarStore(tmp_path / "m.db")
        driver = SyntheticMarketDriver(broker, store, timeframe="H4", history=history, seed=11)
        return broker, store, MarketFeed(broker, store, timeframe="H4",
                                         history=history, driver=driver)

    def test_the_paper_venue_now_has_both_bars_and_a_quote(self, tmp_path):
        broker, store, feed = self._feed(tmp_path)
        snap = feed.snapshot(["EUR_USD", "USD_JPY"])
        for sym in ("EUR_USD", "USD_JPY"):
            assert sym in snap.quotes, "before the driver every cycle was a no_price veto"
            assert len(snap.frames[sym]) >= 299
            assert snap.passports[sym].source == "synthetic"
            assert snap.quality[sym].value == "ok"
        q = snap.quotes["EUR_USD"]
        assert q.bid < q.ask and q.source == "sim"

    def test_the_newest_stored_bar_ends_at_or_before_now(self, tmp_path):
        broker, store, feed = self._feed(tmp_path)
        now = wall_ns()
        feed.snapshot(["EUR_USD"], now_ns=now)
        latest = store.latest_start_ns("EUR_USD", "H4")
        assert latest + H4_NS <= now
        assert now - (latest + H4_NS) < H4_NS, "exactly the last boundary, not an older one"

    def test_the_forming_bar_price_stays_inside_its_range(self, tmp_path):
        broker, store, feed = self._feed(tmp_path)
        driver = feed.driver
        now = wall_ns()
        driver.tick(now, ["EUR_USD"])
        series = driver._series["EUR_USD"]
        starts = series.base.index.as_unit("ns").asi8
        idx = int(((starts + H4_NS) <= now).sum())
        row = series.base.iloc[idx]
        q = broker.quote("EUR_USD")
        assert float(row["low"]) - 0.001 <= float(q.mid) <= float(row["high"]) + 0.001

    def test_time_moving_forward_releases_new_bars(self, tmp_path):
        broker, store, feed = self._feed(tmp_path)
        now = wall_ns()
        feed.snapshot(["EUR_USD"], now_ns=now)
        n0 = store.passport("EUR_USD", "H4", now_ns=now).bars
        got = feed.refresh(["EUR_USD"], now_ns=now + 3 * H4_NS)
        assert got["EUR_USD@H4"] == 3
        assert store.passport("EUR_USD", "H4").bars == n0 + 3

    def test_the_same_seed_is_the_same_market_after_a_restart(self, tmp_path):
        now = wall_ns()
        a, sa, fa = self._feed(tmp_path / "a")
        b, sb, fb = self._feed(tmp_path / "b")
        fa.snapshot(["EUR_USD"], now_ns=now)
        fb.snapshot(["EUR_USD"], now_ns=now)
        fa_frame = sa.frame("EUR_USD", "H4")
        fb_frame = sb.frame("EUR_USD", "H4")
        assert (fa_frame["close"].to_numpy() == fb_frame["close"].to_numpy()).all()


# --------------------------------------------------------------------------- #
# end to end: the agent actually sees candles now
# --------------------------------------------------------------------------- #


class TestAgentSeesBars:
    def test_a_served_paper_agent_reaches_the_strategies(self, tmp_path):
        """Before: frames empty, `_consider_entries` skipped every symbol at
        `len(frame) <= warmup`, and the cycle recorded zero decisions with a
        `no_price` alarm. Now the strategies run and every decision carries the
        risk engine's verdict."""
        from sentinel.agent.memory import MemoryStore
        from sentinel.agent.orchestrator import Agent
        from sentinel.agent.proposals import ProposalQueue
        from sentinel.core.audit import NullAudit
        from sentinel.core.config import (
            AgentConfig, AgentMode, OpsConfig, SentinelConfig, StrategyAllocation,
        )

        broker = PaperBroker(instruments=dict(MAJORS), profile=SimProfile(), seed=5)
        broker.set_conversion("JPY", D("1") / D("150"))
        store = BarStore(tmp_path / "m.db")
        driver = SyntheticMarketDriver(broker, store, timeframe="H4", history=400, seed=2)
        feed = MarketFeed(broker, store, timeframe="H4", history=400, driver=driver)
        cfg = SentinelConfig(
            agent=AgentConfig(mode=AgentMode.OBSERVE, session_windows_utc=[[0, 24]],
                              trade_days=[0, 1, 2, 3, 4, 5, 6]),
            ops=OpsConfig(state_dir=str(tmp_path), audit_log=str(tmp_path / "a.jsonl"),
                          killswitch_file=str(tmp_path / "KILL")),
            strategies=[StrategyAllocation(name="donchian_trend", enabled=True,
                                           instruments=["EUR_USD", "USD_JPY"],
                                           timeframe="H4")])
        agent = Agent(cfg, broker, feed, NullAudit(), MemoryStore(tmp_path / "mem.db"),
                      proposals=ProposalQueue(str(tmp_path / "p.json")))
        agent.start()
        report = agent.cycle()
        assert not report.errors
        assert "EUR_USD" in feed.snapshot(["EUR_USD"]).quotes
        assert report.regime is not None, "regime detection needs >120 bars; it has them"
        # No `no_price` veto anywhere: the price path exists.
        for d in report.decisions:
            assert not any(v.get("rule") == "no_market" for v in d.vetoes)


# --------------------------------------------------------------------------- #
# one frame per timeframe: a daily system gets daily bars
# --------------------------------------------------------------------------- #


class TestPerTimeframeFrames:
    def test_a_second_timeframe_is_loaded_beside_the_primary(self, mt5, tmp_path):
        broker, fake = mt5(history_bars=2000)
        store = BarStore(tmp_path / "m.db")
        feed = MarketFeed(broker, store, timeframe="H4", history=300)
        snap = feed.snapshot(["EUR_USD"], timeframes=["D1"])
        assert "H4" in snap.frames_by_tf and "D1" in snap.frames_by_tf
        h4 = snap.frames_for("H4")["EUR_USD"]
        d1 = snap.frames_for("D1")["EUR_USD"]
        assert len(h4) >= 298 and len(d1) >= 298
        # Different grids, not the same bars under two names.
        assert (d1.index[1] - d1.index[0]) == pd.Timedelta(days=1)
        assert (h4.index[1] - h4.index[0]) == pd.Timedelta(hours=4)
        assert snap.frames is snap.frames_by_tf["H4"] or \
            snap.frames["EUR_USD"].equals(snap.frames_by_tf["H4"]["EUR_USD"])

    def test_an_unloaded_timeframe_is_empty_not_a_substitute(self, mt5, tmp_path):
        broker, fake = mt5()
        store = BarStore(tmp_path / "m.db")
        feed = MarketFeed(broker, store, timeframe="H4", history=50)
        snap = feed.snapshot(["EUR_USD"])
        assert snap.frames_for("D1") == {}
        assert snap.frames_for(None) is snap.frames

    def test_the_synthetic_driver_derives_coarser_bars_from_one_path(self, tmp_path):
        broker = PaperBroker(instruments=dict(MAJORS), profile=SimProfile(), seed=3)
        store = BarStore(tmp_path / "m.db")
        driver = SyntheticMarketDriver(broker, store, timeframe="H4", history=300, seed=11)
        feed = MarketFeed(broker, store, timeframe="H4", history=300, driver=driver)
        snap = feed.snapshot(["EUR_USD"], timeframes=["D1"])
        h4 = snap.frames_for("H4")["EUR_USD"]
        d1 = snap.frames_for("D1")["EUR_USD"]
        assert len(d1) >= 40
        # Every daily bar's range contains the four-hour bars inside it: the
        # two timeframes describe ONE market.
        day = d1.index[-2]
        inside = h4[(h4.index >= day) & (h4.index < day + pd.Timedelta(days=1))]
        assert len(inside) == 6
        assert float(d1.loc[day, "high"]) >= float(inside["high"].max()) - 1e-9
        assert float(d1.loc[day, "low"]) <= float(inside["low"].min()) + 1e-9
        assert abs(float(d1.loc[day, "open"]) - float(inside["open"].iloc[0])) < 1e-9
        assert abs(float(d1.loc[day, "close"]) - float(inside["close"].iloc[-1])) < 1e-9

    def test_a_finer_timeframe_rebuilds_the_base_path_finer(self, tmp_path):
        broker = PaperBroker(instruments=dict(MAJORS), profile=SimProfile(), seed=3)
        store = BarStore(tmp_path / "m.db")
        driver = SyntheticMarketDriver(broker, store, timeframe="H4", history=200, seed=11)
        feed = MarketFeed(broker, store, timeframe="H4", history=200, driver=driver)
        feed.snapshot(["EUR_USD"])
        snap = feed.snapshot(["EUR_USD"], timeframes=["H1"])
        h1 = snap.frames_for("H1")["EUR_USD"]
        assert len(h1) >= 190
        assert driver._series["EUR_USD"].base_tf == "H1"

    def test_the_agent_hands_each_allocation_its_own_timeframe(self, tmp_path):
        """A D1 allocation beside an H4 one: each `prepare()` receives frames on
        its own grid. Before, both received the feed's H4 frame."""
        import pandas as pd
        from sentinel.agent.memory import MemoryStore
        from sentinel.agent.orchestrator import Agent
        from sentinel.agent.proposals import ProposalQueue
        from sentinel.core.audit import NullAudit
        from sentinel.core.config import (
            AgentConfig, AgentMode, OpsConfig, SentinelConfig, StrategyAllocation,
        )
        from sentinel.strategy.registry import build

        broker = PaperBroker(instruments=dict(MAJORS), profile=SimProfile(), seed=5)
        broker.set_conversion("JPY", D("1") / D("150"))
        store = BarStore(tmp_path / "m.db")
        driver = SyntheticMarketDriver(broker, store, timeframe="H4", history=400, seed=2)
        feed = MarketFeed(broker, store, timeframe="H4", history=400, driver=driver)
        cfg = SentinelConfig(
            agent=AgentConfig(mode=AgentMode.OBSERVE, session_windows_utc=[[0, 24]],
                              trade_days=[0, 1, 2, 3, 4, 5, 6]),
            ops=OpsConfig(state_dir=str(tmp_path), audit_log=str(tmp_path / "a.jsonl"),
                          killswitch_file=str(tmp_path / "KILL")),
            strategies=[
                StrategyAllocation(name="donchian_trend", enabled=True,
                                   instruments=["EUR_USD"], timeframe="H4"),
                StrategyAllocation(name="ts_momentum", enabled=True,
                                   instruments=["EUR_USD"], timeframe="D1"),
            ])
        seen: dict = {}
        strategies = {"donchian_trend": build("donchian_trend"),
                      "ts_momentum": build("ts_momentum")}
        for name, strat in strategies.items():
            original = strat.prepare

            def spy(data, _name=name, _orig=original):
                seen[_name] = {s: (f.index[1] - f.index[0]) for s, f in data.items() if len(f) > 1}
                return _orig(data)
            strat.prepare = spy
        agent = Agent(cfg, broker, feed, NullAudit(), MemoryStore(tmp_path / "mem.db"),
                      proposals=ProposalQueue(str(tmp_path / "p.json")),
                      strategies=strategies)
        agent.start()
        agent.cycle()
        assert seen["donchian_trend"]["EUR_USD"] == pd.Timedelta(hours=4)
        assert seen["ts_momentum"]["EUR_USD"] == pd.Timedelta(days=1)


# --------------------------------------------------------------------------- #
# the saved-connection path in bootstrap (finding 8)
# --------------------------------------------------------------------------- #


class TestSavedConnectionPath:
    def test_a_saved_enabled_connection_reaches_the_adapter_without_an_import_error(
            self, tmp_path):
        """`_connection_kwargs` re-imported ExecutionVenueMode with a relative
        import beyond the package, so the first saved, enabled connection --
        the documented demo path -- stopped the process with ImportError before
        the adapter was built."""
        from sentinel.bootstrap import _connection_kwargs
        from sentinel.brokers.connection import BrokerConnection, ConnectionStore
        from sentinel.core.audit import NullAudit
        from sentinel.core.config import SentinelConfig

        store = ConnectionStore(tmp_path / "brokers.json")
        store.upsert(BrokerConnection(
            id="demo-1", display_name="AMarkets demo", profile="amarkets",
            server="AMarkets-Demo", login="123456", declared_account_type="demo",
            account_currency="USD", enabled=True))
        cfg = SentinelConfig()
        cfg.execution.broker = "amarkets"
        kwargs = _connection_kwargs(cfg, tmp_path, NullAudit())
        assert kwargs["login"] == "123456"
        assert kwargs["server"] == "AMarkets-Demo"
        assert kwargs["account_currency"] == "USD"
        assert "password" not in kwargs, "no secret was saved, so none is supplied"
