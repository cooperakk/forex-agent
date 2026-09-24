"""Broker profiles and the MetaTrader adapter.

These tests exist because the MT5 adapter is the path EVERY MetaTrader broker
uses -- AMarkets, Alpari, and the hundreds of others -- and until now it shipped
completely untested, because the real `MetaTrader5` package is a Windows-only
binary. Every symbol translation, filling mode and stop-level rule was
discovered in production, with money on.

`tests/fake_mt5.py` is a terminal that is awkward in the ways real terminals are
awkward, so the awkwardness is discovered here instead.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from sentinel.brokers import build_broker, get_profile, list_profiles
from sentinel.brokers.profiles import BrokerProfile, FillingMode, SymbolMap
from sentinel.brokers.profiles.base import infer_symbol_map
from sentinel.core.money import D, Instrument
from tests.fake_mt5 import FakeMT5


@pytest.fixture
def mt5_factory(monkeypatch):
    """Build an MT5Broker against a fake terminal."""
    def make(profile_name="generic_mt5", **terminal):
        import sys
        fake = FakeMT5(**terminal)
        monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
        from sentinel.brokers.mt5 import MT5Broker
        broker = MT5Broker(profile=get_profile(profile_name))
        return broker, fake
    return make


class TestSymbolTranslation:
    """The system speaks EUR_USD; the venue speaks whatever it speaks."""

    @pytest.mark.parametrize("suffix,expected", [
        ("", "EURUSD"), (".m", "EURUSD.m"), ("micro", "EURUSDmicro"),
        ("-ECN", "EURUSD-ECN"), (".raw", "EURUSD.raw"),
    ])
    def test_a_suffix_round_trips(self, suffix, expected):
        mapping = SymbolMap(suffix=suffix)
        assert mapping.to_venue("EUR_USD") == expected
        assert mapping.to_canonical(expected) == "EUR_USD"

    def test_an_override_wins_over_the_rule(self):
        mapping = SymbolMap(suffix=".m", overrides={"XAU_USD": "GOLD"})
        assert mapping.to_venue("XAU_USD") == "GOLD"
        assert mapping.to_venue("EUR_USD") == "EURUSD.m"
        assert mapping.to_canonical("GOLD") == "XAU_USD"

    def test_an_unrecognised_name_is_left_alone_not_split_at_a_guess(self):
        """A wrong split is worse than no split: it would invent a currency
        pair that does not exist and net exposure against it."""
        mapping = SymbolMap()
        assert mapping.to_canonical("US30") == "US30"
        assert mapping.to_canonical("BTCUSDT") == "BTCUSDT"

    def test_the_suffix_is_inferred_from_the_terminals_own_symbol_list(self):
        inferred = infer_symbol_map(
            ["EURUSD.m", "GBPUSD.m", "USDJPY.m", "XAUUSD.m", "US30"])
        assert inferred.suffix == ".m"

    def test_inference_refuses_when_two_books_are_equally_represented(self):
        """A broker offering both a plain and a suffixed book -- Standard plus
        ECN/raw, very common -- would otherwise be decided by whichever the
        terminal happened to list first, routing orders to one book while
        conversion rates came from the other."""
        both = ["EURUSD", "GBPUSD", "EURUSD.raw", "GBPUSD.raw"]
        assert infer_symbol_map(both).suffix == ""
        assert infer_symbol_map(list(reversed(both))).suffix == "", (
            "the answer depended on listing order")

    def test_inference_needs_a_major_to_anchor_on(self):
        assert infer_symbol_map(["US30", "BTCUSD", "XAUUSD"]).suffix == ""

    def test_the_adapter_presents_canonical_names_whatever_the_venue_calls_them(
            self, mt5_factory):
        broker, fake = mt5_factory("amarkets", suffix=".m")
        instruments = broker.instruments()
        assert "EUR_USD" in instruments, sorted(instruments)
        assert "EURUSD.m" not in instruments, (
            "a venue-specific spelling leaked out of the adapter; the risk "
            "engine would treat it as a different instrument and under-count "
            "netted exposure")
        assert broker._venue("EUR_USD") == "EURUSD.m"

    def test_a_quote_is_fetched_under_the_venues_name(self, mt5_factory):
        broker, fake = mt5_factory("amarkets", suffix=".m")
        broker.instruments()
        quote = broker.quote("EUR_USD")
        assert quote.instrument == "EUR_USD"
        assert quote.bid == D("1.1")


class TestProfileVerification:
    """A profile is a prior. The terminal is the authority."""

    def test_the_terminal_overrides_a_declared_stop_level(self, mt5_factory):
        broker, fake = mt5_factory("amarkets", stops_level=25)
        broker.instruments()
        assert broker.profile.min_stop_level_points == 25, (
            "the declared 0 was kept, so the risk engine would size positions "
            "for a stop the venue rejects")
        assert broker.profile_mismatches, "the disagreement was not recorded"

    def test_stop_floors_are_per_symbol_not_one_number_for_the_whole_venue(
            self, mt5_factory):
        """Points are not comparable between instruments.

        An earlier version collapsed the venue's whole universe into one
        profile-wide maximum and multiplied it by each instrument's own tick --
        so one index with a 300-point floor became a 30-pip floor on EUR/USD
        and vetoed every ordinary FX entry, looking like a strategy problem
        rather than a unit error.
        """
        broker, fake = mt5_factory(
            "generic_mt5",
            symbols=["EURUSD", "GBPUSD", "USDJPY", "US30"],
            stops_by_symbol={"EURUSD": 0, "GBPUSD": 10,
                             "USDJPY": 10, "US30": 300})
        broker.instruments()
        # EUR/USD's own floor is zero, whatever the index demands.
        assert broker.min_stop_distance("EUR_USD") == D("0")
        # GBP/USD: 10 points on a 5-digit feed = 1 pip.
        assert broker.min_stop_distance("GBP_USD") == D("0.00010")
        # The index keeps its own, large floor.
        assert broker.min_stop_distance("US30") > D("0")

    def test_a_symbol_the_terminal_did_not_report_falls_back_to_the_profile(
            self, mt5_factory):
        broker, fake = mt5_factory("generic_mt5", stops_level=15)
        broker.instruments()
        broker._stop_points.pop("EUR_USD", None)
        assert broker.min_stop_distance("EUR_USD") == D("15") * D("0.00001")

    def test_a_stop_level_becomes_a_usable_price_distance(self, mt5_factory):
        broker, fake = mt5_factory("generic_mt5", stops_level=20)
        broker.instruments()
        # 20 points on a 5-digit feed = 0.00020 = 2 pips
        assert broker.min_stop_distance("EUR_USD") == D("0.00020")
        # and on a 3-digit JPY feed = 0.020 = 2 pips
        assert broker.min_stop_distance("USD_JPY") == D("0.020")

    def test_an_unknown_broker_falls_back_instead_of_refusing_to_start(self):
        from sentinel.brokers.profiles import resolve_profile
        assert resolve_profile("some-broker-nobody-added") is None
        assert resolve_profile(None) is None


class TestShippedProfiles:
    def test_amarkets_and_alpari_are_registered(self):
        names = {p.name for p in list_profiles()}
        assert {"amarkets", "alpari", "generic_mt5", "generic_mt4"} <= names

    @pytest.mark.parametrize("name", ["amarkets", "alpari"])
    def test_the_new_venues_route_to_the_mt5_adapter(self, name):
        assert get_profile(name).adapter == "mt5"

    @pytest.mark.parametrize("name", ["amarkets", "alpari"])
    def test_offshore_venues_carry_an_explicit_withdrawal_check(self, name):
        """Gate L0.6 exists because no strategy compensates for a broker that
        will not return your money. The profile must say so."""
        profile = get_profile(name)
        joined = " ".join(profile.verify_before_live).lower()
        assert "withdrawal" in joined
        assert profile.segregated_client_funds is None, (
            "claiming a protection nobody verified is worse than saying unknown")

    @pytest.mark.parametrize("name", [p.name for p in list_profiles()])
    def test_every_profile_declares_honestly(self, name):
        profile = get_profile(name)
        assert profile.display_name and profile.notes
        assert profile.adapter in ("paper", "oanda", "mt5", "mt4", "ccxt")
        assert profile.default_min_lot > 0 and profile.default_lot_step > 0

    def test_only_oanda_and_the_simulator_claim_a_client_order_id(self):
        """The idempotency guarantee is real on OANDA and a narrowed race
        everywhere else. A profile that claimed otherwise would let the
        acceptance protocol promote a strategy onto a venue that cannot
        actually deduplicate a resend."""
        claim = {p.name for p in list_profiles() if p.supports_client_order_id}
        assert claim == {"oanda", "paper", "ccxt"}
        for name in ("amarkets", "alpari", "generic_mt5", "generic_mt4"):
            assert not get_profile(name).supports_client_order_id


class TestFactory:
    def test_a_profile_name_builds_the_right_adapter(self, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, "MetaTrader5", FakeMT5(suffix=".m"))
        broker = build_broker("amarkets")
        assert broker.profile.name == "amarkets"
        assert "EUR_USD" in broker.instruments()

    def test_an_adapter_name_still_works(self, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, "MetaTrader5", FakeMT5())
        broker = build_broker("mt5")
        assert broker.profile.name == "generic_mt5"

    def test_mt4_is_refused_with_a_reason_rather_than_a_broken_adapter(self):
        with pytest.raises(ValueError, match="Expert Advisor bridge"):
            build_broker("generic_mt4")

    def test_an_unknown_name_lists_what_is_available(self):
        with pytest.raises(ValueError, match="Profiles:"):
            build_broker("not-a-broker")


class TestOrderPath:
    """The protocol details that reject real orders at real brokers."""

    def test_an_order_is_sent_under_the_venues_symbol(self, mt5_factory):
        from sentinel.core.types import OrderIntent, Side
        broker, fake = mt5_factory("amarkets", suffix=".m")
        broker.instruments()
        broker.submit(OrderIntent(
            client_order_id="T1", strategy="s", instrument="EUR_USD",
            side=Side.BUY, lots=D("0.10"), stop_loss=D("1.09000"),
            risk_amount=D("100"), decision_ns=1))
        assert fake.sent_requests, "nothing reached the terminal"
        assert fake.sent_requests[0]["symbol"] == "EURUSD.m"

    def test_a_position_is_reported_under_the_canonical_name(self, mt5_factory):
        from sentinel.core.types import OrderIntent, Side
        broker, fake = mt5_factory("alpari", suffix=".raw")
        broker.instruments()
        broker.submit(OrderIntent(
            client_order_id="T2", strategy="s", instrument="EUR_USD",
            side=Side.BUY, lots=D("0.10"), stop_loss=D("1.09000"),
            risk_amount=D("100"), decision_ns=1))
        positions = broker.positions()
        assert positions and positions[0].instrument == "EUR_USD"

    def test_a_venue_refused_stop_is_reported_not_assumed_accepted(self, mt5_factory):
        """MT5 applies its own stop-level rules to `sl` and can zero it while
        still reporting the deal as successful -- leaving a filled, naked
        position that reports as protected."""
        from sentinel.core.types import OrderIntent, Side
        broker, fake = mt5_factory("amarkets", stops_level=200)   # 20 pips
        broker.instruments()
        result = broker.submit(OrderIntent(
            client_order_id="T3", strategy="s", instrument="EUR_USD",
            side=Side.BUY, lots=D("0.10"),
            stop_loss=D("1.09990"),          # ~0.2 pips away: illegal here
            risk_amount=D("100"), decision_ns=1))
        assert result.state.value == "rejected" or not result.stop_confirmed, (
            "an order whose protective stop the venue refused was reported as "
            "fully protected")


class TestVenueStopFloor:
    """A stop inside the broker's minimum is a rejected order, not a tight stop."""

    def test_the_engine_refuses_a_stop_the_venue_would_reject(self, engine, ctx):
        from sentinel.core.types import OrderIntent, Side

        order = OrderIntent(client_order_id="V1", strategy="test",
                            instrument="EUR_USD", side=Side.BUY,
                            lots=Decimal("0.1"), stop_loss=Decimal("1.0820"),
                            take_profit=Decimal("1.0910"))
        # Without a venue floor this entry is fine.
        assert not any(v.rule == "venue_stop_distance"
                       for v in engine.evaluate_entry(order, ctx).vetoes)

        # The broker enforces 200 pips; the strategy's stop is far inside it.
        ctx.venue_min_stop_pips = {"EUR_USD": Decimal("200")}
        decision = engine.evaluate_entry(order, ctx)
        assert any(v.rule == "venue_stop_distance" for v in decision.vetoes), (
            "the engine sized a position from a stop the venue would refuse")

    def test_no_declared_floor_means_no_veto(self, engine, ctx):
        """Plenty of venues impose none, so an empty map must not block."""
        from sentinel.core.types import OrderIntent, Side

        order = OrderIntent(client_order_id="V2", strategy="test",
                            instrument="EUR_USD", side=Side.BUY,
                            lots=Decimal("0.1"), stop_loss=Decimal("1.0820"),
                            take_profit=Decimal("1.0910"))
        ctx.venue_min_stop_pips = {}
        assert not any(v.rule == "venue_stop_distance"
                       for v in engine.evaluate_entry(order, ctx).vetoes)


class TestFillingMode:
    """The most common first-run failure at a new broker."""

    def test_a_venue_that_only_accepts_fok_gets_fok(self, mt5_factory):
        from tests.fake_mt5 import ORDER_FILLING_FOK
        from sentinel.core.types import OrderIntent, Side

        broker, fake = mt5_factory("generic_mt5",
                                   supported_filling=ORDER_FILLING_FOK)
        broker.instruments()
        result = broker.submit(OrderIntent(
            client_order_id="F1", strategy="s", instrument="EUR_USD",
            side=Side.BUY, lots=D("0.10"), stop_loss=D("1.09000"),
            risk_amount=D("100"), decision_ns=1))
        assert fake.sent_requests[0]["type_filling"] == ORDER_FILLING_FOK
        assert result.state.value != "rejected", (
            "a hard-coded IOC would have been rejected with 'Unsupported "
            "filling mode' on every single order at this broker")

    def test_a_venue_that_only_accepts_ioc_gets_ioc(self, mt5_factory):
        from tests.fake_mt5 import ORDER_FILLING_IOC
        from sentinel.core.types import OrderIntent, Side

        broker, fake = mt5_factory("generic_mt5",
                                   supported_filling=ORDER_FILLING_IOC)
        broker.instruments()
        broker.submit(OrderIntent(
            client_order_id="F2", strategy="s", instrument="EUR_USD",
            side=Side.BUY, lots=D("0.10"), stop_loss=D("1.09000"),
            risk_amount=D("100"), decision_ns=1))
        assert fake.sent_requests[0]["type_filling"] == ORDER_FILLING_IOC


class TestSilentSuccess:
    """Reporting success when nothing matched was the single most dangerous
    defect in this adapter: the dead-man watchdog journals a completed flatten
    on the strength of these return values."""

    def test_closing_a_position_that_does_not_exist_is_refused(self, mt5_factory):
        broker, fake = mt5_factory("amarkets", suffix=".m")
        result = broker.close_position("GBP_USD", reason="deadman_flatten")
        assert result.state.value == "rejected"
        assert result.reject_reason == "NO_POSITION"
        assert not fake.sent_requests, "an order was sent for a phantom position"

    def test_closing_an_unknown_symbol_is_refused(self, mt5_factory):
        broker, fake = mt5_factory("generic_mt5")
        assert broker.close_position("NOT_AREALSYMBOL").state.value == "rejected"

    def test_modifying_a_position_that_does_not_exist_returns_false(self, mt5_factory):
        """reconcile.py marks a position protected and rewrites its risk when
        this returns True."""
        broker, fake = mt5_factory("amarkets", suffix=".m")
        assert broker.modify_position("GBP_USD", stop_loss=D("1.20000")) is False

    def test_the_dead_man_flatten_actually_closes_the_book(self, mt5_factory):
        """The end-to-end version of the above: a fresh broker, positions open
        at a suffixed venue, flatten everything."""
        from sentinel.core.types import OrderIntent, Side

        broker, fake = mt5_factory("amarkets", suffix=".m")
        broker.submit(OrderIntent(
            client_order_id="D1", strategy="s", instrument="EUR_USD",
            side=Side.BUY, lots=D("0.20"), stop_loss=D("1.09000"),
            risk_amount=D("100"), decision_ns=1))
        assert len(broker.positions()) == 1

        for position in broker.positions():
            result = broker.close_position(position.instrument, reason="deadman")
            assert result.state.value == "filled", result.reject_reason
        assert broker.positions() == [], "the book was still on after a flatten"


class TestProfileIsolation:
    """A profile fetched from the registry must not be mutated through."""

    def test_an_inferred_suffix_does_not_leak_into_the_registry(self, monkeypatch):
        import sys
        from sentinel.brokers.profiles import get_profile

        before = get_profile("amarkets").symbols.suffix
        monkeypatch.setitem(sys.modules, "MetaTrader5", FakeMT5(suffix=".m"))
        from sentinel.brokers.mt5 import MT5Broker
        broker = MT5Broker(profile=get_profile("amarkets"))
        broker.instruments()

        assert broker.profile.symbols.suffix == ".m"
        assert get_profile("amarkets").symbols.suffix == before, (
            "one broker's inferred suffix became every later broker's declared "
            "suffix, process-wide")

    def test_a_second_account_on_plain_symbols_is_unaffected(self, monkeypatch):
        import sys
        from sentinel.brokers.profiles import get_profile
        from sentinel.brokers.mt5 import MT5Broker

        monkeypatch.setitem(sys.modules, "MetaTrader5", FakeMT5(suffix=".m"))
        MT5Broker(profile=get_profile("amarkets")).instruments()

        monkeypatch.setitem(sys.modules, "MetaTrader5", FakeMT5(suffix=""))
        second = MT5Broker(profile=get_profile("amarkets"))
        assert second._venue("EUR_USD") == "EURUSD"
        assert second.quote("EUR_USD").bid == D("1.1")


class TestNonPairInstruments:
    def test_an_index_does_not_get_a_currency_code_sliced_out_of_its_name(
            self, mt5_factory):
        """"US30.m" became base "US3" / quote "0.M", and that quote currency
        flowed into the conversion table and into sizing."""
        broker, fake = mt5_factory("generic_mt5", suffix=".m",
                                   symbols=["EURUSD", "US30", "USOIL"])
        instruments = broker.instruments()
        assert instruments["EUR_USD"].base == "EUR"
        assert instruments["EUR_USD"].quote == "USD"
        assert instruments["US30"].quote == "USD", instruments["US30"].quote
        assert instruments["USOIL"].quote == "USD"
        for inst in instruments.values():
            assert "." not in inst.quote and "." not in inst.base


class TestStartupOrdering:
    """Orchestrator.start() reconciles BEFORE anything calls instruments()."""

    def test_positions_are_canonical_before_instruments_is_ever_called(
            self, monkeypatch):
        import sys
        from sentinel.brokers.profiles import get_profile
        from sentinel.core.types import OrderIntent, Side

        fake = FakeMT5(suffix=".m")
        monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
        from sentinel.brokers.mt5 import MT5Broker

        seeding = MT5Broker(profile=get_profile("amarkets"))
        seeding.submit(OrderIntent(
            client_order_id="S1", strategy="s", instrument="EUR_USD",
            side=Side.BUY, lots=D("0.10"), stop_loss=D("1.09000"),
            risk_amount=D("100"), decision_ns=1))

        # A brand-new adapter, exactly as bootstrap builds it, reading
        # positions as the very first thing it does.
        fresh = MT5Broker(profile=get_profile("amarkets"))
        names = [p.instrument for p in fresh.positions()]
        assert names == ["EUR_USD"], (
            f"a venue spelling reached the caller: {names}. The reconciler "
            "would class this as a critical orphan and halt the agent.")
