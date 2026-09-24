"""Plugin discovery, and the gate every plugin has to pass.

Dropping a file into a watched directory is the easiest way to add a strategy
and would be the easiest way to grant one real money, so the interesting tests
here are the refusals: a class cannot declare itself accepted, cannot take a
name that is already in use, and cannot take the process down by failing to
import.
"""

import pytest

from sentinel.strategy import registry as R
from sentinel.strategy.base import Strategy


@pytest.fixture
def clean_registry():
    """Snapshot and restore the global registry.

    The registry is process-global by design -- it is the one place that knows
    what exists -- so a test that registers anything has to put it back, or the
    next test sees a library that does not match the shipped one.
    """
    registry = dict(R._REGISTRY)
    sources = dict(R._SOURCES)
    errors = list(R._LOAD_ERRORS)
    yield
    R._REGISTRY.clear()
    R._REGISTRY.update(registry)
    R._SOURCES.clear()
    R._SOURCES.update(sources)
    R._LOAD_ERRORS[:] = errors


GOOD_PLUGIN = '''
from sentinel.core.types import Side, Signal
from sentinel.strategy.base import Strategy, StrategyMeta, atr


class _Helper:
    """Not a strategy. Must not end up in the registry."""


class MyBreakout(Strategy):
    meta = StrategyMeta(
        name="user_breakout", family="breakout", timeframe="H1",
        horizon_bars=12, required_history=40, lifecycle="hypothesis",
        description="user supplied",
        hypothesis="A worked example of an external strategy file.",
        failure_conditions=["it is an example"],
    )

    @staticmethod
    def default_params():
        return {"atr_window": 14, "stop_atr": 2.0, "target_atr": 4.0}

    def indicators(self, df):
        import pandas as pd
        return pd.DataFrame({"atr": atr(df, self.params["atr_window"])}, index=df.index)

    def generate(self, data, instrument, index):
        df = data[instrument]
        if index < 40:
            return None
        a = float(self.features_at(instrument, df, index)["atr"])
        close = float(df["close"].iloc[index])
        return Signal(strategy=self.meta.name, instrument=instrument, side=Side.BUY,
                      strength=0.5, stop_price=close - 2 * a, target_price=close + 4 * a,
                      horizon_bars=12)
'''

ACCEPTED_PLUGIN = '''
from sentinel.strategy.base import Strategy, StrategyMeta


class SneakyStrategy(Strategy):
    meta = StrategyMeta(name="sneaky", family="trend", lifecycle="accepted",
                        hypothesis="claims to have already passed acceptance")

    def generate(self, data, instrument, index):
        return None
'''

BROKEN_PLUGIN = "import a_module_that_does_not_exist\n"

COLLIDING_PLUGIN = '''
from sentinel.strategy.base import Strategy, StrategyMeta


class NotTheRealOne(Strategy):
    meta = StrategyMeta(name="donchian_trend", family="trend",
                        hypothesis="squats on a name that already exists")

    def generate(self, data, instrument, index):
        return None
'''


class TestBuiltinDiscovery:
    def test_discovery_is_idempotent(self):
        """Import order, a reload or a second call must not raise. The classes
        are the same objects, so re-registering them is a no-op."""
        before = R.available()
        R.discover_builtin()
        assert R.available() == before

    def test_every_family_module_is_found_without_a_central_list(self):
        import pkgutil

        from sentinel.strategy import families

        assert set(families.FAMILIES) <= set(R.families())
        # And the documented taxonomy cannot go stale: a module dropped into
        # the directory is discovered either way, but if it is not listed here
        # the docstring describing the library stops being true.
        on_disk = {m.name for m in pkgutil.iter_modules(families.__path__)
                   if not m.name.startswith("_")}
        assert on_disk == set(families.FAMILIES)

    def test_load_report_says_where_each_strategy_came_from(self):
        report = R.load_report()
        assert report["by_source"]["donchian_trend"] == "family:trend"
        assert report["by_source"]["baseline_coin_flip"] == "baseline"
        assert report["families"]["trend"] >= 6


class TestUserDirectory:
    def test_a_missing_directory_is_not_an_error(self, tmp_path):
        """Not having configured a plugin directory is the normal case."""
        assert R.load_user_strategies(tmp_path / "nope") == []

    def test_a_user_strategy_is_registered_and_buildable(self, tmp_path, clean_registry):
        (tmp_path / "mine.py").write_text(GOOD_PLUGIN)
        loaded = R.load_user_strategies(tmp_path)
        assert loaded == ["user_breakout"]
        assert R.family_of("user_breakout") == "breakout"
        assert isinstance(R.build("user_breakout"), Strategy)
        assert R.load_report()["by_source"]["user_breakout"] == "user:mine.py"

    def test_helpers_in_a_plugin_file_are_not_registered(self, tmp_path, clean_registry):
        (tmp_path / "mine.py").write_text(GOOD_PLUGIN)
        R.load_user_strategies(tmp_path)
        assert "_Helper" not in R.available()

    def test_a_strategy_may_not_declare_itself_accepted(self, tmp_path, clean_registry):
        """THE test in this file.

        Without this refusal, one file in a watched directory is a complete
        bypass of the acceptance protocol: the class asserts the lifecycle that
        authorises real money and no verdict was ever produced. Acceptance is a
        property of a configuration, held in the verdict store; a class cannot
        claim it.
        """
        (tmp_path / "sneaky.py").write_text(ACCEPTED_PLUGIN)
        loaded = R.load_user_strategies(tmp_path)
        assert loaded == []
        assert "sneaky" not in R.available()
        assert any("sneaky" in e and "accepted" in e for e in R.load_report()["errors"])

    def test_registering_an_accepted_class_directly_also_raises(self, clean_registry):
        from sentinel.strategy.base import StrategyMeta

        class Claimed(Strategy):
            meta = StrategyMeta(name="claimed", lifecycle="accepted")

            def generate(self, data, instrument, index):
                return None

        with pytest.raises(ValueError, match="accepted"):
            R.register(Claimed)

    def test_a_broken_file_is_skipped_and_reported(self, tmp_path, clean_registry):
        """A typo in an experimental strategy must not stop a process that may
        be holding positions. It is skipped, and the reason is recorded."""
        (tmp_path / "broken.py").write_text(BROKEN_PLUGIN)
        (tmp_path / "ok.py").write_text(GOOD_PLUGIN)
        loaded = R.load_user_strategies(tmp_path)
        assert loaded == ["user_breakout"]
        assert any("broken.py" in e for e in R.load_report()["errors"])

    def test_a_name_collision_is_refused(self, tmp_path, clean_registry):
        """Two strategies sharing a name makes every verdict, allocation and
        ledger entry ambiguous, so the newcomer loses."""
        (tmp_path / "squat.py").write_text(COLLIDING_PLUGIN)
        R.load_user_strategies(tmp_path)
        from sentinel.strategy.families.trend import DonchianTrend

        assert R.get("donchian_trend") is DonchianTrend
        assert any("collision" in e for e in R.load_report()["errors"])

    def test_files_starting_with_underscore_are_ignored(self, tmp_path, clean_registry):
        (tmp_path / "_private.py").write_text(GOOD_PLUGIN)
        assert R.load_user_strategies(tmp_path) == []


class TestDecorator:
    def test_the_decorator_registers(self, clean_registry):
        from sentinel.strategy.base import StrategyMeta

        @R.strategy
        class Decorated(Strategy):
            meta = StrategyMeta(name="decorated_example", family="trend")

            def generate(self, data, instrument, index):
                return None

        assert "decorated_example" in R.available()
        assert R.load_report()["by_source"]["decorated_example"] == "decorator"

    def test_an_abstract_class_cannot_be_registered(self, clean_registry):
        with pytest.raises(TypeError):
            R.register(Strategy)

    def test_a_non_strategy_cannot_be_registered(self, clean_registry):
        with pytest.raises(TypeError):
            R.register(dict)


class TestQuery:
    def test_families_partition_the_registry(self):
        total = sum(len(R.available(family=f)) for f in R.families())
        assert total == len(R.available())

    def test_describe_all_carries_the_family_and_the_source(self):
        rows = {r["name"]: r for r in R.describe_all()}
        assert rows["supertrend_flip"]["family"] == "trend"
        assert rows["supertrend_flip"]["source"] == "family:trend"
        assert rows["supertrend_flip"]["lifecycle"] == "hypothesis"

    def test_an_unknown_name_names_what_is_available(self):
        with pytest.raises(KeyError, match="unknown strategy"):
            R.get("no_such_strategy")
