"""Tests for the news subsystem and the learn-from-mistakes loop.

Each one pins a specific way the previous version was wrong, in the style of
test_audit_fixes.py: the docstring names the failure, not the feature.
"""

import datetime as dt
import tempfile
from decimal import Decimal as D
from pathlib import Path

import numpy as np
import pytest

from sentinel.agent.memory import Lesson, MemoryStore
from sentinel.agent.postmortem import (
    MODES,
    Counterfactual,
    PathPoint,
    PatternFinding,
    TradeAutopsy,
    aggregate,
    autopsy,
    counterfactuals,
    diagnose,
    exit_kind,
    path_in_r,
)
from sentinel.agent.proposals import ProposalError, derive_proposals, propose, regime_lessons
from sentinel.core.types import ClosedTrade, Side
from sentinel.core.tzrules import (
    local_to_utc,
    utc_offset_hours,
    utc_to_local,
    verify_against_zoneinfo,
)
from sentinel.news.calendar import CalendarEvent, EconomicCalendar
from sentinel.news.classify import curated_tier, observe_release_volatility, tier_from_observations
from sentinel.news.lap import LAPScore, analyse, date_accuracy, redact_dates
from sentinel.news.policy import NewsPolicy
from sentinel.news.schedule import RecurringScheduleSource


def ns(y, m, d, hh=0, mm=0):
    return int(dt.datetime(y, m, d, hh, mm, tzinfo=dt.UTC).timestamp() * 1e9)


@pytest.fixture
def cal():
    with tempfile.TemporaryDirectory() as tmp:
        c = EconomicCalendar(Path(tmp) / "cal.db")
        yield c
        c.close()


# --------------------------------------------------------------------------- #
# DST
# --------------------------------------------------------------------------- #


class TestDST:
    def test_a_new_york_release_moves_an_hour_in_utc_across_the_switch(self):
        """A calendar that stores a fixed UTC hour is right for seven months and
        silently an hour out for the rest -- so the blackout either sits out an
        empty hour and trades through the release, or the reverse."""
        winter = local_to_utc(dt.datetime(2026, 2, 6, 8, 30), "America/New_York")
        summer = local_to_utc(dt.datetime(2026, 6, 5, 8, 30), "America/New_York")
        assert winter.hour == 13 and winter.minute == 30
        assert summer.hour == 12 and summer.minute == 30

    def test_frankfurt_and_new_york_switch_on_different_dates(self):
        """The US switches on the second Sunday in March, Europe on the last.
        For three weeks a year the offset between them is five hours, not six --
        and those weeks contain an ECB meeting and a US CPI print. Deriving one
        zone's offset from the other's is wrong exactly when it matters."""
        # 19 March 2026: US already on EDT, Europe still on CET.
        assert utc_offset_hours("America/New_York", dt.datetime(2026, 3, 19, 12)) == -4
        assert utc_offset_hours("Europe/Frankfurt", dt.datetime(2026, 3, 19, 12)) == 1
        # 2 April 2026: both have switched.
        assert utc_offset_hours("America/New_York", dt.datetime(2026, 4, 2, 12)) == -4
        assert utc_offset_hours("Europe/Frankfurt", dt.datetime(2026, 4, 2, 12)) == 2

    def test_tokyo_never_switches(self):
        """Japan has observed no summer time since 1951. A generic 'apply DST to
        every zone' rule puts every BoJ decision an hour out for half the year."""
        for month in range(1, 13):
            assert utc_offset_hours("Asia/Tokyo", dt.datetime(2026, month, 15, 12)) == 9

    def test_an_unknown_zone_is_refused_rather_than_defaulted_to_utc(self):
        """Defaulting an unknown zone to UTC is the quiet version of the bug:
        the event lands at the right clock time in the wrong timezone."""
        with pytest.raises(ValueError, match="unknown release zone"):
            utc_offset_hours("Mars/Olympus", dt.datetime(2026, 1, 1))

    def test_the_arithmetic_rules_agree_with_the_tz_database(self):
        """The rules are hand-written because the deployment image may have no
        tzdata. Where tzdata IS present they must agree, or the offline path is
        quietly wrong in a way nothing else would catch."""
        moments = []
        d = dt.datetime(2020, 1, 1, 8, 30)
        while d.year < 2031:
            moments.append(d)
            d += dt.timedelta(days=1)
        for zone in ("America/New_York", "Europe/Frankfurt", "Europe/London", "Asia/Tokyo"):
            bad = verify_against_zoneinfo(zone, moments)
            assert bad == [], f"{zone} disagrees with tzdata at {bad[:3]}"

    def test_round_tripping_local_to_utc_and_back(self):
        for zone in ("America/New_York", "Europe/Frankfurt", "Europe/London", "Asia/Tokyo"):
            for month in (1, 4, 7, 10):
                local = dt.datetime(2026, month, 15, 14, 15)
                back = utc_to_local(local_to_utc(local, zone), zone)
                assert back == local, f"{zone} {month}"

    def test_the_bundled_schedule_tracks_dst_not_a_fixed_utc_hour(self):
        """Non-farm payrolls is 08:30 New York, always. In UTC that is 13:30 in
        March and 12:30 in April, and a schedule that emits one number for both
        misses one of the two releases entirely."""
        src = RecurringScheduleSource()
        events = src.fetch(ns(2026, 3, 1), ns(2026, 5, 1))
        nfp = [e for e in events if e["series_id"] == "US.NFP"]
        hours = [dt.datetime.fromtimestamp(e["event_ns"] / 1e9,
                                           tz=dt.UTC).hour for e in nfp]
        assert 13 in hours and 12 in hours, f"payrolls did not move with DST: {hours}"


# --------------------------------------------------------------------------- #
# Calendar: dedup and revisions
# --------------------------------------------------------------------------- #


class TestCalendarDedup:
    def _event(self, when_ns, *, event_id="X", certainty="confirmed", source="feed"):
        return CalendarEvent(
            event_id=event_id, event_ns=when_ns, country="US", currency="USD",
            name="Non-Farm Payrolls", impact="high", curated_impact="high",
            period="2026-02", series_id="US.NFP", zone="America/New_York",
            local_time="08:30", certainty=certainty, source=source)

    def test_a_rescheduled_event_does_not_open_a_second_blackout_window(self, cal):
        """A provider that republishes a moved release under a new id used to
        create a second row. Two rows is two overlapping windows for one
        release: the agent sits out twice as long and the calendar it shows the
        operator disagrees with the provider's."""
        first = ns(2026, 3, 6, 13, 30)
        moved = ns(2026, 3, 6, 14, 30)
        assert cal.add_event(self._event(first, event_id="A")).action == "inserted"
        result = cal.add_event(self._event(moved, event_id="B-new-id"))
        assert result.action == "rescheduled"
        assert result.event_id == "A", "a new id created a second event"

        rows = cal._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert rows == 1, f"{rows} rows for one release"
        out = cal.blackout(moved, ["USD"], before_min=30, after_min=30)
        assert list(out) == ["USD"] and "1" not in out.get("USD", "").split("(")[0]
        revisions = cal.event_revisions("A")
        assert any(r["field"] == "event_ns" for r in revisions), \
            "the time change was not recorded as a revision"

    def test_a_pattern_guess_cannot_overwrite_a_confirmed_time(self, cal):
        """The bundled pattern source runs on every boot. Letting it restate a
        feed's confirmed 13:30 as its own guessed 12:30 would undo the ingestion
        silently, and the calendar would drift back to the guess over time."""
        confirmed = ns(2026, 3, 6, 13, 30)
        cal.add_event(self._event(confirmed, event_id="A", certainty="confirmed"))
        cal.add_event(self._event(ns(2026, 3, 6, 12, 30), event_id="A",
                                  certainty="approximate", source="bundled_pattern"))
        stored = cal.get("A")
        assert stored.event_ns == confirmed
        assert stored.certainty == "confirmed"

    def test_a_confirmed_time_upgrades_a_pattern_guess(self, cal):
        cal.add_event(self._event(ns(2026, 3, 6, 12, 30), event_id="A",
                                  certainty="approximate", source="bundled_pattern"))
        cal.add_event(self._event(ns(2026, 3, 6, 13, 30), event_id="A",
                                  certainty="confirmed", source="feed"))
        stored = cal.get("A")
        assert stored.event_ns == ns(2026, 3, 6, 13, 30)
        assert stored.certainty == "confirmed"

    def test_dst_recompute_moves_the_stored_instant(self, cal):
        """A calendar seeded in February holds every New York release at its
        winter UTC instant. After the March switch each of them is an hour
        early, which is worse than having no window at all."""
        wrong = ns(2026, 4, 3, 13, 30)          # winter offset applied in April
        cal.add_event(CalendarEvent(
            event_id="N", event_ns=wrong, country="US", currency="USD",
            name="Non-Farm Payrolls", impact="high", curated_impact="high",
            series_id="US.NFP", period="2026-03", zone="America/New_York",
            local_time="08:30", certainty="scheduled_pattern"))
        moved = cal.reschedule_for_dst(ns(2026, 1, 1), ns(2026, 12, 31))
        assert moved == 1
        assert cal.get("N").event_ns == ns(2026, 4, 3, 12, 30)

    def test_an_approximate_date_advises_but_never_blocks(self, cal):
        """Blocking on a guessed date costs opportunity for nothing AND clears
        the real release day, so the agent trades into the print believing it is
        protected. That is strictly worse than having no calendar."""
        when = ns(2026, 4, 10, 12, 30)
        cal.add_event(CalendarEvent(
            event_id="C", event_ns=when, country="US", currency="USD", name="CPI",
            impact="high", curated_impact="high", series_id="US.CPI", period="2026-03",
            zone="America/New_York", local_time="08:30", certainty="approximate"))
        assert cal.blackout(when, ["USD"], before_min=30, after_min=30) == {}
        assert cal.blackout(when, ["USD"], before_min=30, after_min=30,
                            require_certain=False) != {}
        adv = cal.advisories(when - 3600 * 10 ** 9, ["USD"], horizon_sec=86400)
        assert len(adv) == 1 and adv[0]["name"] == "CPI"

        policy = NewsPolicy(cal, before_min=120, after_min=30)
        assessed = policy.assess(when - 600 * 10 ** 9, ["EUR_USD"])["EUR_USD"]
        assert assessed.blocked is False
        assert assessed.size_multiplier < D("1"), "an unconfirmed date did not shrink size"
        assert assessed.advisories

    def test_the_news_filter_can_never_enlarge_a_position(self, cal):
        """The one invariant the whole news role hierarchy rests on."""
        policy = NewsPolicy(cal, before_min=120, after_min=30)
        for a in policy.assess(ns(2026, 5, 1), ["EUR_USD", "USD_JPY"]).values():
            assert a.size_multiplier <= D("1")


# --------------------------------------------------------------------------- #
# Surprise
# --------------------------------------------------------------------------- #


class TestSurprise:
    def _series(self, cal, values, *, consensus=100.0, start=(2024, 1)):
        """Build N released events of one series with a causal consensus."""
        y, m = start
        ids = []
        for i, actual in enumerate(values):
            when = ns(y + (m + i - 1) // 12, (m + i - 1) % 12 + 1, 5, 13, 30)
            eid = f"E{i}"
            cal.add_event(CalendarEvent(
                event_id=eid, event_ns=when, country="US", currency="USD", name="NFP",
                impact="high", curated_impact="high", series_id="US.NFP",
                period=f"{y}-{i:02d}", zone="America/New_York", local_time="08:30"))
            cal.record_consensus(eid, consensus, None, "poll", recorded_ns=when - 86400 * 10 ** 9)
            cal.record_actual(eid, actual, None, published_ns=when, source="feed",
                              received_ns=when)
            ids.append((eid, when))
        return ids

    def test_a_consensus_recorded_after_the_release_is_refused(self, cal):
        """The purest form of look-ahead, and it looks completely normal in the
        code: providers serve only the latest revision, so the 'consensus' you
        read tomorrow was written knowing the print."""
        when = ns(2026, 3, 6, 13, 30)
        cal.add_event(CalendarEvent(
            event_id="E", event_ns=when, country="US", currency="USD", name="NFP",
            impact="high", curated_impact="high", series_id="US.NFP", period="2026-02"))
        cal.record_actual("E", 250.0, None, published_ns=when, source="f", received_ns=when)
        cal.record_consensus("E", 180.0, None, "revised", recorded_ns=when + 60 * 10 ** 9)
        s = cal.surprise("E")
        assert s.valid is False and "look-ahead" in s.reason

    def test_the_dispersion_scale_uses_only_releases_published_before_the_cutoff(self, cal):
        """A z-score normalised by a scale that includes the future is not a
        z-score. It is also the kind of error that IMPROVES a backtest, which is
        why it survives review."""
        ids = self._series(cal, [100, 105, 95, 110, 90, 103, 97, 108, 92, 200])
        early_cutoff = ids[5][1]
        d_early = cal.surprise_dispersion("US.NFP", early_cutoff, min_obs=3)
        d_late = cal.surprise_dispersion("US.NFP", ids[-1][1] + 10 ** 12, min_obs=3)
        assert d_early["n"] == 5, d_early
        assert d_late["n"] == 10
        assert d_early["scale"] != d_late["scale"]

    def test_an_event_is_excluded_from_its_own_dispersion(self, cal):
        """Including it shrinks its own z-score toward zero. Every surprise then
        looks ordinary, and the feature the strategy was built on is dead while
        still returning numbers."""
        ids = self._series(cal, [100, 105, 95, 110, 90, 103, 97, 108, 92, 500])
        last_id, last_when = ids[-1]
        with_self = cal.surprise_dispersion("US.NFP", last_when + 10 ** 12, min_obs=3)
        without = cal.surprise_dispersion("US.NFP", last_when + 10 ** 12,
                                          exclude_event_id=last_id, min_obs=3)
        assert without["n"] == with_self["n"] - 1
        s = cal.surprise(last_id, decision_ns=last_when + 10 ** 12, min_history=3)
        assert s.standardised is not None and abs(s.standardised) > 10, (
            "the outlier's own value was allowed into the scale that judges it")

    def test_a_robust_scale_is_not_destroyed_by_one_crisis_print(self, cal):
        """SD is the tempting choice and it is the wrong one: one 10-sigma print
        inflates it enough that every later surprise reads as small -- exactly
        backwards, since the fat tail is the part that moves the market."""
        self._series(cal, [100, 101, 99, 102, 98, 101, 99, 100, 102, 1000])
        d = cal.surprise_dispersion("US.NFP", ns(2030, 1, 1), min_obs=3)
        raw = np.array([v for _, v in cal.historical_surprises("US.NFP", ns(2030, 1, 1))])
        assert d["method"] == "mad"
        assert d["scale"] < raw.std(ddof=1) / 10, (
            f"the robust scale {d['scale']} tracked the SD {raw.std(ddof=1)}")

    def test_too_little_history_returns_no_z_score_and_says_why(self, cal):
        """A z-score from four observations is not a z-score. Handing one back
        anyway makes it indistinguishable from one built on four hundred."""
        ids = self._series(cal, [100, 105, 95, 110])
        s = cal.surprise(ids[-1][0], min_history=8)
        assert s.valid is True and s.standardised is None
        assert "prior surprises" in s.reason and s.dispersion_method == "insufficient"

    def test_a_standardised_surprise_is_produced_once_history_exists(self, cal):
        ids = self._series(cal, [100, 106, 94, 108, 92, 104, 96, 110, 90, 130])
        s = cal.surprise(ids[-1][0], min_history=5)
        assert s.standardised is not None and s.dispersion_n == 9
        assert s.surprise == pytest.approx(30.0)


# --------------------------------------------------------------------------- #
# Event classification
# --------------------------------------------------------------------------- #


class TestClassification:
    def test_measurement_promotes_a_series_the_curation_underrates(self):
        v = tier_from_observations("US.RETAIL_SALES", [2.4] * 20)
        assert v.tier == "high" and v.source == "measured"

    def test_demoting_a_curated_high_needs_more_evidence_than_promoting(self):
        """Over-blacking-out costs opportunity. Under-blacking-out costs money
        during the one hour a month when the spread is eight times normal, so
        the evidence bar is deliberately asymmetric."""
        thin = tier_from_observations("US.NFP", [1.0] * 13, min_observations=12)
        assert thin.tier == "high", "a curated high was demoted on thin evidence"
        assert any("demotion" in w for w in thin.warnings)
        plenty = tier_from_observations("US.NFP", [1.0] * 30, min_observations=12)
        assert plenty.tier == "low"

    def test_a_bimodal_series_is_flagged_rather_than_averaged(self):
        v = tier_from_observations("US.CPI", [0.9] * 15 + [6.0] * 15, min_observations=12)
        assert any("two different events" in w for w in v.warnings)

    def test_the_curated_tier_is_labelled_as_a_judgement(self):
        v = curated_tier("US.NFP")
        assert v.source == "curated" and "judgement" in v.rationale

    def test_an_unknown_release_defaults_below_the_blackout_gate(self):
        v = curated_tier("XX.WHATEVER", "Some Survey")
        assert v.tier == "medium" and v.source == "default"

    def test_the_volatility_baseline_excludes_the_release_itself(self):
        """A baseline measured AROUND the event is inflated by the event, and
        every ratio then collapses toward one -- so nothing ever tiers as high."""
        import pandas as pd

        idx = pd.date_range("2026-01-01", periods=200, freq="h", tz="UTC")
        rng = np.random.default_rng(0)
        close = 1.10 + np.cumsum(rng.normal(0, 0.0002, 200))
        df = pd.DataFrame({"open": close, "high": close + 0.0003,
                           "low": close - 0.0003, "close": close}, index=idx)
        spike = 150
        df.iloc[spike, df.columns.get_loc("high")] += 0.0060
        df.iloc[spike, df.columns.get_loc("low")] -= 0.0060
        obs = observe_release_volatility(
            df, int(idx[spike].value), pip=0.0001, window_bars=2, baseline_bars=100)
        assert obs is not None and obs["ratio"] > 3.0, obs


# --------------------------------------------------------------------------- #
# LAP
# --------------------------------------------------------------------------- #


class TestLAP:
    def _scores(self, n, *, seed=0, effect=0.0, noise=1.0, **kw):
        rng = np.random.default_rng(seed)
        fam = rng.uniform(0, 1, n)
        perf = effect * fam + rng.normal(0, noise, n)
        return [LAPScore(f"a{i}", float(fam[i]), True, False, **kw)
                for i in range(n)], perf.tolist()

    def test_it_fails_closed_when_too_many_articles_could_not_be_scored(self):
        """A failed model call leaves recalls_outcome=False, so averaging over
        the unmasked list let errors DILUTE the recall share: the more calls
        failed, the cleaner the corpus looked."""
        scores, perf = self._scores(40)
        scores += [LAPScore(f"e{i}", float("nan"), False, False, error="boom")
                   for i in range(40)]
        perf += [0.0] * 40
        r = analyse(scores, perf)
        assert r.contaminated is True and r.conclusive is False
        assert "could not be scored" in " ".join(r.notes)

    def test_the_inconclusive_verdict_does_not_fabricate_a_slope_finding(self):
        """Forcing contamination by setting slope_fired=True made the verdict
        read 'performance rises 0.0000 per unit of familiarity (p=1.0000)' --
        a statement that is not true, and one an operator would reasonably
        override because it looks like a broken calculation."""
        scores, perf = self._scores(40)
        scores += [LAPScore(f"e{i}", float("nan"), False, False, error="boom")
                   for i in range(40)]
        perf += [0.0] * 40
        r = analyse(scores, perf)
        assert "performance rises 0.0000" not in r.verdict
        assert r.verdict.startswith("inconclusive")

    def test_a_clean_result_from_an_underpowered_test_is_not_a_clean_result(self):
        """At realistic R-multiple noise the slope test detects a contamination
        of 0.5R per unit of familiarity about 10% of the time at n=20. A test
        that passes because it could not see anything is an audit that reads as
        a clearance."""
        scores, perf = self._scores(40, seed=11, noise=1.0)
        r = analyse(scores, perf)
        assert r.powered is False
        assert r.mde_slope > 0.02
        assert r.contaminated is True and r.verdict.startswith("inconclusive")

    def test_a_powered_clean_result_is_reported_as_evidence_not_proof(self):
        scores, perf = self._scores(60, seed=5, noise=0.005)
        r = analyse(scores, perf)
        assert r.powered is True and r.contaminated is False
        assert "evidence, not proof" in r.verdict

    def test_a_post_cutoff_corpus_does_not_need_the_power_check(self):
        """Post-cutoff articles settle the question by design; the test's power
        is beside the point there."""
        scores, perf = self._scores(40, seed=11, noise=1.0)
        r = analyse(scores, perf, post_cutoff_only=True)
        assert r.contaminated is False and r.conclusive is True

    def test_date_accuracy_is_an_objective_detector_the_model_cannot_talk_down(self):
        """share_recalling_outcome is a self-report, and a model optimised to be
        helpful will understate it. Dating a DATE-REDACTED article correctly is
        recall whatever the model says about itself."""
        scores, perf = self._scores(40, seed=3, noise=0.005,
                                    estimated_date="2024-05", true_date="2024-05")
        r = analyse(scores, perf)
        assert r.date_accuracy == 1.0
        assert r.contaminated is True and "date-redacted" in r.verdict

    def test_declining_to_guess_counts_as_a_miss(self):
        """Otherwise a model cleans its own record by declining on exactly the
        articles it recognised."""
        scores = [LAPScore(f"a{i}", 0.5, True, False, estimated_date=None,
                           true_date="2024-05") for i in range(20)]
        acc, n = date_accuracy(scores)
        assert acc == 0.0 and n == 20

    def test_redaction_removes_the_dateline(self):
        """Without it, date accuracy measures reading comprehension and reads as
        maximum contamination on a perfectly clean corpus."""
        text = "On March 12, 2023 the Fed held. See also 2023-03-12, Q1 2023 and 12/03/2023."
        out = redact_dates(text)
        assert "2023" not in out and "March 12" not in out
        assert "the Fed held" in out


# --------------------------------------------------------------------------- #
# Post-mortem modes
# --------------------------------------------------------------------------- #


def trade(**kw):
    base = dict(trade_id="T1", strategy="s", instrument="EUR_USD", side=Side.BUY,
                lots=D("0.1"), entry_price=D("1.10000"), exit_price=D("1.10500"),
                opened_ns=10 ** 15, closed_ns=10 ** 15 + 10 ** 12, pnl=D("10"),
                pnl_pips=D("50"), commission=D("1"), initial_risk=D("100"),
                r_multiple=D("1"), exit_reason="take_profit",
                max_favourable_r=D("1.2"), max_adverse_r=D("-0.2"))
    base.update(kw)
    return ClosedTrade(**base)


class TestModes:
    def test_the_time_stop_reason_the_venue_records_is_recognised(self):
        """_close passes the reason to the broker, which truncates it to 32
        characters. 'time stop: held 5.0h beyond the ' never equalled
        'time_stop', so slow_bleed -- the mode that tells you the holding
        horizon is wrong -- was structurally unreachable on every venue."""
        for reason in ("time_stop", "time stop: held 5.0h beyond the ",
                       "TIME STOP: held 12.0h beyond the 4.0h horizon", "horizon_stop"):
            assert exit_kind(reason) == "time_stop", reason

    def test_slow_bleed_now_fires_on_a_real_venue_string(self):
        a = autopsy(trade(exit_reason="time stop: held 5.0h beyond the ",
                          r_multiple=D("0.05"), max_favourable_r=D("0.3"),
                          max_adverse_r=D("-0.2")))
        assert a.mode == "slow_bleed"

    def test_the_weekend_flatten_is_its_own_mode(self):
        a = autopsy(trade(exit_reason="weekend flat: a stop does not sur",
                          r_multiple=D("-0.5"), max_favourable_r=D("0.1"),
                          max_adverse_r=D("-0.6")))
        assert a.mode == "weekend_flat_exit"

    def test_every_declared_mode_is_reachable(self):
        """A mode that cannot fire is a category the learning loop can never
        see. The old set had two of them and they looked like coverage."""
        cases = {
            "gap_loss": trade(exit_reason="stop_loss", r_multiple=D("-1.5"),
                              max_adverse_r=D("-1.5"), max_favourable_r=D("0.1")),
            "stopped_at_breakeven": trade(exit_reason="stop_loss", r_multiple=D("0.02"),
                                          max_favourable_r=D("1.6"),
                                          max_adverse_r=D("-0.1")),
            "gave_back_open_profit": trade(exit_reason="stop_loss", r_multiple=D("-0.9"),
                                           max_favourable_r=D("2.0"),
                                           max_adverse_r=D("-0.9")),
            "stopped_then_reversed": trade(exit_reason="stop_loss", r_multiple=D("-1.0"),
                                           max_favourable_r=D("0.6"),
                                           max_adverse_r=D("-1.0")),
            "news_shock_loss": trade(exit_reason="stop_loss", r_multiple=D("-1.0"),
                                     max_favourable_r=D("0.2"), max_adverse_r=D("-1.0"),
                                     news_context={"event": "NFP"}),
            "horizon_cut_a_winner": trade(exit_reason="time_stop", r_multiple=D("0.6"),
                                          max_favourable_r=D("1.8"),
                                          max_adverse_r=D("-0.2")),
            "slow_bleed": trade(exit_reason="time_stop", r_multiple=D("0.05"),
                                max_favourable_r=D("0.4"), max_adverse_r=D("-0.3")),
            "weekend_flat_exit": trade(exit_reason="weekend_flat", r_multiple=D("-0.4"),
                                       max_favourable_r=D("0.2"),
                                       max_adverse_r=D("-0.5")),
            "cost_dominated": trade(exit_reason="closed", r_multiple=D("-0.05"),
                                    commission=D("8"), max_favourable_r=D("0.2"),
                                    max_adverse_r=D("-0.1")),
            "target_too_far": trade(exit_reason="closed", r_multiple=D("-0.4"),
                                    max_favourable_r=D("0.9"), max_adverse_r=D("-0.5")),
            "clean_win": trade(exit_reason="take_profit", r_multiple=D("1.5"),
                               max_favourable_r=D("1.6"), max_adverse_r=D("-0.2")),
            "clean_loss": trade(exit_reason="stop_loss", r_multiple=D("-1.0"),
                                max_favourable_r=D("0.1"), max_adverse_r=D("-1.0")),
            "scratch": trade(exit_reason="closed", r_multiple=D("-0.1"),
                             max_favourable_r=D("0.2"), max_adverse_r=D("-0.2")),
        }
        assert set(cases) == set(MODES), (
            f"declared but untested: {set(MODES) - set(cases)}")
        for want, t in cases.items():
            got = autopsy(t).mode
            assert got == want, f"expected {want}, got {got}"

    def test_every_mode_declares_whether_it_can_change_anything(self):
        """A category that changes nothing is a diagnostic for a human, and
        pretending otherwise is how a learning loop generates busywork."""
        from sentinel.agent.postmortem import MODE_ACTIONS

        assert set(MODE_ACTIONS) == set(MODES)
        assert sum("none" in v for v in MODE_ACTIONS.values()) >= 3


# --------------------------------------------------------------------------- #
# Counterfactual honesty
# --------------------------------------------------------------------------- #


class TestCounterfactualHonesty:
    def test_a_wider_stop_is_never_scored_because_it_cannot_be(self):
        """The trade ended AT the stop. What the price did afterwards was never
        recorded anywhere, so 'would a wider stop have worked' is unanswerable.
        The old formula answered it favourably every time -- min(mfe,2)*0.667+1,
        which is at least +1R on every stopped-out trade."""
        t = trade(exit_reason="stop_loss", r_multiple=D("-1.0"),
                  max_favourable_r=D("0.8"), max_adverse_r=D("-1.05"))
        for path in (None, [PathPoint(i, 0.5, -0.5, 0.0, 0.0) for i in range(10)]):
            cfs = {c.name: c for c in counterfactuals(t, path=path)}
            w = cfs["wider_stop_1.5x"]
            assert w.computable is False and w.delta_r == 0.0
            assert "after" in w.reason.lower()

    def test_a_partial_is_scored_on_winners_too_where_it_costs_money(self):
        """Marking it applicable only when r < 1 made the delta positive by
        CONSTRUCTION, and the one-sided t-test on that population cannot fail."""
        winner = trade(r_multiple=D("3.0"), max_favourable_r=D("3.1"),
                       exit_reason="take_profit")
        cf = {c.name: c for c in counterfactuals(winner)}["partial_at_1R"]
        assert cf.applicable and cf.computable
        assert cf.delta_r < -0.9, f"banking half of a +3R trade at 1R should cost ~1R, got {cf.delta_r}"

    def test_a_partial_that_never_triggered_is_not_counted(self):
        t = trade(r_multiple=D("-1.0"), max_favourable_r=D("0.3"), exit_reason="stop_loss")
        cf = {c.name: c for c in counterfactuals(t)}["partial_at_1R"]
        assert cf.applicable is False and cf.computable is True

    def test_costs_and_slippage_are_charged_to_the_alternative(self):
        """A scale-out adds an exit leg. Scoring it at the trigger price with no
        cost is the assumption that makes every such rule look free."""
        t = trade(r_multiple=D("0.0"), max_favourable_r=D("1.5"), commission=D("4"),
                  exit_reason="closed")
        cf = {c.name: c for c in counterfactuals(t)}["partial_at_1R"]
        assert cf.delta_r < 0.5, (
            "the alternative was scored at a free fill: half of +1R with no cost")
        assert any("exit leg" in a for a in cf.assumptions)

    def test_path_dependent_counterfactuals_refuse_to_guess_without_bars(self):
        """MAE and MFE record how far the position went, not WHEN. A stop rule
        is entirely a question of order."""
        t = trade(r_multiple=D("-0.8"), max_favourable_r=D("1.4"), exit_reason="stop_loss")
        cfs = {c.name: c for c in counterfactuals(t, path=None)}
        for name in ("breakeven_at_1R", "trail_tighter", "horizon_exit"):
            assert cfs[name].computable is False, name

    def test_a_gap_through_the_level_fills_at_the_open_not_the_level(self):
        """Assuming a touched level always fills at that level is the classic
        way a counterfactual backtest manufactures money."""
        path = [PathPoint(1, 1.2, 0.9, 1.1, 0.0), PathPoint(2, -0.4, -0.9, -0.8, -0.5)]
        t = trade(r_multiple=D("-1.0"), max_favourable_r=D("1.2"),
                  max_adverse_r=D("-1.0"), exit_reason="stop_loss", commission=D("0"))
        cf = {c.name: c for c in counterfactuals(t, path=path)}["breakeven_at_1R"]
        # The second bar opened at -0.5R, having gapped through break-even.
        assert cf.computable and cf.delta_r == pytest.approx(0.48, abs=0.02), cf.delta_r

    def test_no_counterfactual_survives_a_driftless_random_walk(self):
        """The decisive test. On a pure random walk with no exploitable
        structure the OLD engine produced p between 1e-26 and 1e-152 for all
        four rules and would have generated four parameter proposals. Nothing
        here may be diagnosed as a parameter problem."""
        rng = np.random.default_rng(42)
        autopsies = []
        for i in range(400):
            px = [1.10000]
            risk = 0.00300
            for _ in range(400):
                px.append(px[-1] + rng.normal(0, 0.00030))
                rr = (px[-1] - px[0]) / risk
                if rr <= -1.0 or rr >= 2.0:
                    break
            arr = np.array(px)
            rs = (arr - arr[0]) / risk
            r = float(rs[-1]) - 0.03
            reason = ("stop_loss" if rs[-1] <= -1.0 else
                      "take_profit" if rs[-1] >= 2.0 else "time stop: horizon")
            bars = [(j * 10 ** 12, float(arr[j]), float(arr[j]), float(arr[j]),
                     float(arr[j])) for j in range(len(arr))]
            t = trade(trade_id=f"T{i}", exit_price=D(str(round(float(arr[-1]), 5))),
                      opened_ns=i * 10 ** 14, closed_ns=i * 10 ** 14 + 10 ** 12,
                      pnl_pips=D(str(round((float(arr[-1]) - arr[0]) * 10000, 2))),
                      commission=D("3"), r_multiple=D(str(round(r, 4))),
                      exit_reason=reason,
                      max_favourable_r=D(str(round(float(rs.max()), 4))),
                      max_adverse_r=D(str(round(float(rs.min()), 4))))
            p = path_in_r(bars, entry_price=1.10000, risk_price_distance=risk,
                          side=Side.BUY)
            autopsies.append(autopsy(t, path=p))
        findings = aggregate(autopsies, min_sample=25, alpha=0.05)
        cfs = [f for f in findings if f.pattern.startswith("counterfactual:")]
        assert cfs, "no counterfactual was tested at all"
        bad = [f.pattern for f in cfs if f.diagnosis == "parameter"]
        assert not bad, f"a parameter change was derived from a random walk: {bad}"
        assert not derive_proposals(findings, {}, min_sample=25, alpha=0.05)


# --------------------------------------------------------------------------- #
# Multiple comparisons and diagnosis
# --------------------------------------------------------------------------- #


class TestStatisticalHonesty:
    def test_the_family_is_corrected_not_each_test_alone(self):
        """Twenty patterns tested at p<0.05 finds one by chance every time, and
        the proposal engine cannot tell it from a real one."""
        rng = np.random.default_rng(1)
        autopsies = []
        for i in range(160):
            r = float(rng.normal(0, 1))
            a = TradeAutopsy(
                trade_id=f"T{i}", strategy="s", instrument="EUR_USD",
                outcome="win" if r > 0 else "loss", mode="clean_win",
                r_multiple=r, mae_r=min(0.0, r), mfe_r=max(0.0, r) + 0.5,
                capture_ratio=0.5, closed_ns=i * 10 ** 12,
                tags=[f"tag{i % 8}", f"regime:{'stress' if i % 3 == 0 else 'trending'}"],
                counterfactuals=[])
            autopsies.append(a)
        findings = [f for f in aggregate(autopsies, min_sample=15, alpha=0.05)
                    if f.n_tests_in_family]
        assert findings, "nothing was tested"
        for f in findings:
            assert f.p_value_adjusted >= f.p_value - 1e-12, (
                "an adjusted p-value below the raw one is not a correction")
            assert f.n_tests_in_family >= 5

    def test_correction_collapses_the_family_wise_false_discovery_rate(self):
        """Measured, not asserted. Screening ~16 null hypotheses against pure
        noise turns up at least one raw p<0.05 about 58% of the time. After
        Benjamini-Hochberg it is about 4%. Without the correction the loop finds
        something to 'learn' on more than half of all passes, forever."""
        rng = np.random.default_rng(0)
        raw_hits = adj_hits = 0
        trials = 40
        for _ in range(trials):
            autopsies = []
            for i in range(120):
                r = float(rng.normal(0, 1))
                autopsies.append(TradeAutopsy(
                    trade_id=f"T{i}", strategy="s", instrument="EUR_USD",
                    outcome="win" if r > 0 else "loss", mode="clean_win",
                    r_multiple=r, mae_r=min(0.0, r), mfe_r=max(0.0, r) + 0.5,
                    capture_ratio=0.5, closed_ns=i * 10 ** 12,
                    tags=[f"t{j}" for j in range(12) if rng.random() < 0.4],
                    counterfactuals=[Counterfactual(f"c{j}", "d",
                                                    float(rng.normal(0, 1)))
                                     for j in range(4)]))
            f = [x for x in aggregate(autopsies, min_sample=20, alpha=0.05)
                 if x.n_tests_in_family]
            raw_hits += any(x.p_value < 0.05 for x in f)
            adj_hits += any(x.significant for x in f)
        assert raw_hits / trials > 0.3, "the screen was not wide enough to prove anything"
        assert adj_hits / trials < 0.20, (
            f"BH let {adj_hits}/{trials} null screens through")
        assert adj_hits < raw_hits

    def test_a_finding_significant_raw_but_not_adjusted_is_refused(self):
        f = PatternFinding(pattern="counterfactual:x", n=100, share=0.5, mean_r=0.0,
                           mean_delta_r=0.4, t_stat=2.2, p_value=0.02,
                           recommendation="", p_value_adjusted=0.40,
                           significant=False, n_tests_in_family=20,
                           diagnosis="luck", diagnosis_note="noise")
        with pytest.raises(ProposalError, match="corrected|false-discovery"):
            propose(path="risk.partial_take_r", current_value=1.5, proposed_value=1.2,
                    rationale="t", finding=f, alpha=0.05)

    def test_a_regime_confined_effect_becomes_a_lesson_not_a_parameter_change(self):
        """A parameter applies in every market state; the evidence came from
        one. The honest output is a scoped, reversible lesson."""
        f = PatternFinding(pattern="counterfactual:partial_at_1R", n=80, share=0.5,
                           mean_r=0.0, mean_delta_r=-0.4, t_stat=4.0, p_value=0.0001,
                           recommendation="", p_value_adjusted=0.001, significant=True,
                           n_tests_in_family=12, stability=0.8,
                           regime_concentration=0.85, dominant_regime="stress")
        f.diagnosis, f.diagnosis_note = diagnose(f, min_sample=40)
        assert f.diagnosis == "regime"
        with pytest.raises(ProposalError, match="diagnosed as 'regime'"):
            propose(path="risk.partial_take_r", current_value=1.5, proposed_value=1.2,
                    rationale="t", finding=f, min_sample=40, alpha=0.05)
        lessons = regime_lessons([f], min_sample=40, alpha=0.05)
        assert len(lessons) == 1
        assert lessons[0]["regime"] == "stress" and lessons[0]["caution"] <= 1.0

    def test_an_effect_confined_to_one_half_of_the_sample_is_a_changed_market(self):
        """Present in the first half and absent in the second is the signature
        of a market that changed. Tuning to it fits the half that is over."""
        f = PatternFinding(pattern="counterfactual:x", n=80, share=0.5, mean_r=0.0,
                           mean_delta_r=0.4, t_stat=4.0, p_value=0.0001,
                           recommendation="", p_value_adjusted=0.001, significant=True,
                           n_tests_in_family=12, stability=0.02,
                           regime_concentration=0.3, dominant_regime="trending")
        assert diagnose(f, min_sample=40)[0] == "regime"

    def test_luck_is_the_default_explanation(self):
        f = PatternFinding(pattern="counterfactual:x", n=80, share=0.5, mean_r=0.0,
                           mean_delta_r=0.4, t_stat=1.2, p_value=0.2,
                           recommendation="", p_value_adjusted=0.9, significant=False,
                           n_tests_in_family=12)
        kind, note = diagnose(f, min_sample=40)
        assert kind == "luck" and "chance" in note

    def test_a_degenerate_sample_gets_p_one_not_p_half(self):
        """`1 - t.cdf(0, n-1)` is 0.5, not 1.0. A zero-variance delta sample was
        being reported as halfway to significant."""
        autopsies = [TradeAutopsy(
            trade_id=f"T{i}", strategy="s", instrument="EUR_USD", outcome="win",
            mode="clean_win", r_multiple=1.0, mae_r=0.0, mfe_r=1.5,
            capture_ratio=1.0, closed_ns=i * 10 ** 12,
            counterfactuals=[Counterfactual("k", "d", delta_r=0.0)]) for i in range(40)]
        f = [x for x in aggregate(autopsies, min_sample=10, alpha=0.05)
             if x.pattern == "counterfactual:k"]
        assert f and f[0].p_value == 1.0


# --------------------------------------------------------------------------- #
# Proposals: the direction guard
# --------------------------------------------------------------------------- #


GOOD = PatternFinding(pattern="counterfactual:partial_at_1R", n=100, share=0.5,
                      mean_r=0.0, mean_delta_r=0.4, t_stat=5.0, p_value=0.0001,
                      recommendation="", p_value_adjusted=0.0005, significant=True,
                      n_tests_in_family=12, stability=0.8, regime_concentration=0.3,
                      dominant_regime="trending", diagnosis="parameter")


class TestDirectionGuard:
    @pytest.mark.parametrize("path,current,proposed", [
        ("risk.block_minutes_before_high_impact", 30, 20),
        ("risk.block_minutes_after_high_impact", 30, 15),
        ("risk.trail_atr_multiple", 2.5, 3.2),
        ("risk.min_stop_pips", 12.0, 8.0),
        ("risk.min_reward_risk", 1.8, 1.4),
        ("risk.partial_take_fraction", 0.5, 0.3),
        ("risk.giveback_keep_fraction", 0.5, 0.3),
        ("risk.partial_take_r", 1.5, 2.5),
        ("risk.breakeven_trigger_r", 1.0, 2.0),
        ("risk.max_spread_pips_multiple", 2.0, 3.0),
        ("risk.min_seconds_between_entries", 600, 300),
        ("risk.trail_activate_r", 1.0, 2.0),
        ("risk.giveback_arm_r", 2.0, 3.0),
    ])
    def test_no_proposable_parameter_may_move_toward_more_risk(self, path, current,
                                                               proposed):
        with pytest.raises(ProposalError, match="MORE risk"):
            propose(path=path, current_value=current, proposed_value=proposed,
                    rationale="t", finding=GOOD, alpha=0.05)

    def test_a_frozen_risk_limit_is_outside_the_agents_reach(self):
        with pytest.raises(ProposalError, match="hard risk control"):
            propose(path="risk.daily_loss_limit_pct", current_value=2.0,
                    proposed_value=1.0, rationale="t", finding=GOOD, alpha=0.05)

    def test_the_step_limiter_cannot_land_on_a_riskier_value(self):
        """The limiter runs AFTER the direction guard and re-clamps its own
        output, so the invariant has to hold for the number that lands in the
        config, not the one the guard happened to see."""
        for path, current, proposed in (
                ("risk.min_stop_pips", 12.0, 90.0),
                ("risk.block_minutes_before_high_impact", 30, 240),
                ("risk.partial_take_r", 4.0, 0.5),
                ("risk.giveback_arm_r", 4.0, 0.5)):
            p = propose(path=path, current_value=current, proposed_value=proposed,
                        rationale="t", finding=GOOD, alpha=0.05)
            spec_dir = {"risk.min_stop_pips": 1, "risk.block_minutes_before_high_impact": 1,
                        "risk.partial_take_r": -1, "risk.giveback_arm_r": -1}[path]
            delta = float(p.proposed_value) - float(current)
            assert delta * spec_dir >= 0, f"{path}: {current} -> {p.proposed_value}"
            assert abs(delta) <= abs(current) * 0.35 + 1e-6

    def test_no_proposal_is_ever_derived_from_the_uncomputable_counterfactual(self):
        """The old loop proposed a 30% wider minimum stop from a formula that
        assumed the stopped-out trade recovered. It passed the direction guard
        -- a wider stop at constant risk is a smaller position -- and was pure
        invention underneath."""
        f = PatternFinding(pattern="counterfactual:wider_stop_1.5x", n=200, share=0.9,
                           mean_r=-0.3, mean_delta_r=1.3, t_stat=20.0, p_value=1e-40,
                           recommendation="", p_value_adjusted=1e-39, significant=True,
                           n_tests_in_family=8, stability=0.9, regime_concentration=0.3,
                           dominant_regime="trending", diagnosis="parameter")
        assert derive_proposals([f], {}, min_sample=40, alpha=0.05) == []

    def test_the_slippage_tag_branch_is_reachable(self):
        """Every finding had to clear `mean_delta_r > 0.10` before the loop
        reached the tag branches, but a tag finding's delta is tagged-minus-
        untagged: an UNDERPERFORMING tag is negative. The one tag-derived
        proposal could never fire and the silence looked like 'no pattern'."""
        f = PatternFinding(pattern="tag:entry_slippage", n=60, share=0.4, mean_r=-0.3,
                           mean_delta_r=-0.35, t_stat=-4.0, p_value=0.0002,
                           recommendation="", p_value_adjusted=0.002, significant=True,
                           n_tests_in_family=10, stability=0.7, regime_concentration=0.35,
                           dominant_regime="trending", diagnosis="parameter")
        out = derive_proposals([f], {"risk": {"max_spread_pips_multiple": 2.5}},
                               min_sample=40, alpha=0.05)
        assert len(out) == 1
        assert out[0].path == "risk.max_spread_pips_multiple"
        assert float(out[0].proposed_value) < 2.5

    def test_a_news_underperformance_widens_the_blackout(self):
        f = PatternFinding(pattern="tag:news_adjacent", n=60, share=0.4, mean_r=-0.3,
                           mean_delta_r=-0.4, t_stat=-4.0, p_value=0.0002,
                           recommendation="", p_value_adjusted=0.002, significant=True,
                           n_tests_in_family=10, stability=0.7, regime_concentration=0.35,
                           dominant_regime="trending", diagnosis="parameter")
        out = derive_proposals(
            [f], {"risk": {"block_minutes_before_high_impact": 30}},
            min_sample=40, alpha=0.05)
        assert len(out) == 1 and int(out[0].proposed_value) > 30


# --------------------------------------------------------------------------- #
# Lessons expire
# --------------------------------------------------------------------------- #


@pytest.fixture
def mem():
    with tempfile.TemporaryDirectory() as tmp:
        m = MemoryStore(Path(tmp) / "m.db")
        yield m
        m.close()


class TestLessonExpiry:
    def _lesson(self, **kw):
        base = dict(scope="global", statement="stress hurts",
                    evidence={"pattern": "tag:regime:stress"}, sample_size=80,
                    effect_r=-0.4, p_value=0.001, caution=0.7)
        base.update(kw)
        return Lesson(**base)

    def test_influence_decays_when_nothing_confirms_the_lesson(self):
        """A lesson with no expiry is a permanent bias: learned in one market,
        recalled in every market afterwards, with no mechanism that could ever
        notice it stopped being true."""
        with tempfile.TemporaryDirectory() as tmp:
            m = MemoryStore(Path(tmp) / "m.db")
            m.add_lesson(self._lesson())
            now = m.all_lessons()[0].created_ns
            fresh, _ = m.caution_multiplier(now_ns=now)
            stale, _ = m.caution_multiplier(now_ns=now + 400 * 86400 * 10 ** 9)
            assert fresh < stale < 1.0000001
            assert stale > 0.98, "a year-old unconfirmed lesson still taxed every trade"
            m.close()

    def test_decay_can_never_take_the_agent_above_baseline(self):
        """Decay relaxes a restriction. The limit is 1.0, and there is nothing
        above 1.0 to decay to -- so this cannot become a risk increase."""
        lesson = self._lesson(caution=0.3)
        for days in (0, 10, 100, 1000, 100000):
            assert lesson.effective_caution(
                lesson.created_ns + int(days * 86400e9)) <= 1.0

    def test_fresh_supporting_evidence_refreshes_a_lesson(self, mem):
        lid = mem.add_lesson(self._lesson())
        assert mem.review_lesson(lid, supported=True, sample_size=40,
                                 effect_r=-0.35, p_value=0.002) == "confirmed"
        after = [x for x in mem.all_lessons() if x.id == lid][0]
        assert after.review_count == 1 and after.sample_size == 120

    def test_a_reversed_effect_retires_the_lesson_immediately(self, mem):
        """A lesson saying stress trades lose 0.4R, re-tested on fresh stress
        trades that GAIN 0.3R, is not a smaller effect. It is wrong, and
        keeping it at reduced confidence is a slower way of being wrong."""
        lid = mem.add_lesson(self._lesson())
        assert mem.review_lesson(lid, supported=True, sample_size=40,
                                 effect_r=+0.3, p_value=0.002) == "retired"
        assert mem.all_lessons() == []

    def test_two_contradictions_retire_a_lesson(self, mem):
        lid = mem.add_lesson(self._lesson())
        assert mem.review_lesson(lid, supported=False, sample_size=40,
                                 effect_r=-0.05, p_value=0.6) == "contradicted"
        assert mem.all_lessons(), "one contradiction should not be enough"
        assert mem.review_lesson(lid, supported=False, sample_size=40,
                                 effect_r=-0.05, p_value=0.6) == "retired"
        assert mem.all_lessons() == []

    def test_absence_of_evidence_is_not_contradiction(self, mem):
        """A quiet month with too few trades to test anything must not retire
        every lesson the agent holds."""
        mem.add_lesson(self._lesson())
        counts = mem.review_against([])
        assert counts["untested"] == 1 and counts["retired"] == 0
        assert len(mem.all_lessons()) == 1

    def test_review_against_findings_confirms_and_retires(self, mem):
        mem.add_lesson(self._lesson())
        still_true = PatternFinding(
            pattern="tag:regime:stress", n=50, share=0.4, mean_r=-0.4,
            mean_delta_r=-0.45, t_stat=-4.0, p_value=0.0005,
            recommendation="", p_value_adjusted=0.004, significant=True,
            n_tests_in_family=10)
        assert mem.review_against([still_true])["confirmed"] == 1
        gone = PatternFinding(
            pattern="tag:regime:stress", n=50, share=0.4, mean_r=0.0,
            mean_delta_r=-0.4, t_stat=-0.4, p_value=0.7,
            recommendation="", p_value_adjusted=0.99, significant=False,
            n_tests_in_family=10)
        mem.review_against([gone])
        mem.review_against([gone])
        assert mem.all_lessons() == [], "a lesson no longer supported stayed active"

    def test_an_unconfirmed_lesson_is_eventually_retired_outright(self, mem):
        mem.add_lesson(self._lesson())
        assert mem.expire_unconfirmed(max_unconfirmed_days=3650) == 0
        assert mem.expire_unconfirmed(max_unconfirmed_days=0.0) == 1
        assert mem.all_lessons() == []

    def test_a_regime_scoped_lesson_is_recalled_only_in_that_regime(self, mem):
        """'Losses are bigger in high volatility' held globally is a permanent
        tax on every trade. Scoped, it applies where the evidence came from."""
        mem.add_lesson(self._lesson(scope="regime", regime="stress", caution=0.6))
        in_regime, reasons = mem.caution_multiplier(regime="stress")
        out_regime, _ = mem.caution_multiplier(regime="trending")
        assert in_regime < 1.0 and reasons
        assert out_regime == 1.0, "a stress lesson shrank a trending position"

    def test_a_lesson_can_never_enlarge_a_position(self, mem):
        for caution in (0.1, 0.5, 1.0, 2.0, 99.0):
            mem.add_lesson(self._lesson(statement=f"s{caution}", caution=caution))
        mult, _ = mem.caution_multiplier()
        assert 0.0 < mult <= 1.0


class TestRegimeLessonsDoNotDoubleCount:
    def _f(self, pattern, delta, regime="stress"):
        f = PatternFinding(pattern=pattern, n=60, share=0.4, mean_r=delta,
                           mean_delta_r=delta, t_stat=-5.0, p_value=1e-6,
                           recommendation="", p_value_adjusted=1e-5, significant=True,
                           n_tests_in_family=8, stability=0.8,
                           regime_concentration=1.0, dominant_regime=regime)
        f.diagnosis, f.diagnosis_note = diagnose(f, min_sample=40)
        return f

    def test_findings_describing_the_same_trades_produce_one_lesson(self):
        """tag:regime:stress and tag:entry_slippage routinely cover the SAME
        fifty-four trades. Two lessons multiply their cautions -- 0.7 x 0.7 =
        0.49 -- so the agent halves its size on the strength of counting one
        body of evidence twice."""
        out = regime_lessons([self._f("tag:regime:stress", -0.6),
                              self._f("tag:entry_slippage", -0.85)],
                             min_sample=40, alpha=0.05)
        assert len(out) == 1
        assert out[0]["effect_r"] == -0.85, "the smaller effect won"
        assert "tag:regime:stress" in out[0]["evidence"]["also_seen"]

    def test_a_favourable_regime_finding_produces_no_lesson(self):
        """A lesson's only power is a caution capped at 1.0, so a favourable
        finding has nothing to express. Emitting one anyway fills the store with
        entries that do nothing, which is how a lesson list stops being read."""
        assert regime_lessons([self._f("tag:regime:trending", +0.9, "trending")],
                              min_sample=40, alpha=0.05) == []

    def test_separate_regimes_still_produce_separate_lessons(self):
        out = regime_lessons([self._f("tag:a", -0.5, "stress"),
                              self._f("tag:b", -0.4, "volatile_range")],
                             min_sample=40, alpha=0.05)
        assert {x["regime"] for x in out} == {"stress", "volatile_range"}


@pytest.mark.filterwarnings("ignore:Precision loss occurred")
def test_a_degenerate_comparison_does_not_produce_a_nan_p_value():
    """`nan >= alpha` is False, so an unguarded NaN sails through every
    significance gate downstream and arrives as a finding with no p-value."""
    autopsies = []
    for i in range(60):
        tagged = i % 2 == 0
        autopsies.append(TradeAutopsy(
            trade_id=f"T{i}", strategy="s", instrument="EUR_USD",
            outcome="win", mode="clean_win", r_multiple=1.0, mae_r=0.0,
            mfe_r=1.5, capture_ratio=1.0, closed_ns=i * 10 ** 12,
            tags=["flat"] if tagged else []))
    for f in aggregate(autopsies, min_sample=10, alpha=0.05):
        assert np.isfinite(f.p_value), f.pattern
        assert np.isfinite(f.p_value_adjusted), f.pattern


def test_tiering_with_no_evidence_never_demotes_an_operators_high_impact_event(cal):
    """set_measured_impact(None) resets the effective impact to the curated one.
    Calling it because there was no evidence either way would silently demote an
    event an operator had marked high, removing its blackout window."""
    from sentinel.news.classify import classify_calendar

    when = ns(2026, 6, 5, 12, 30)
    cal.add_event(CalendarEvent(
        event_id="X", event_ns=when, country="XX", currency="USD",
        name="Operator override", impact="high", curated_impact="low",
        series_id="XX.CUSTOM", period="2026-05", zone="UTC", local_time="12:30"))
    classify_calendar(cal, min_observations=12)
    assert cal.get("X").impact == "high"
    assert cal.blackout(when, ["USD"], before_min=30, after_min=30) != {}
