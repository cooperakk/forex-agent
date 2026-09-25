"""Broker connections, quarterly licences, and the three account levels.

Everything here is a negative test in disguise. A connection manager is only
worth anything if the connection TEST cannot trade, if activating a venue while
holding positions is refused, and if the thing the operator declared is checked
against what the venue actually says. A quarterly licence is only worth
anything if the arithmetic is calendar arithmetic and if moving the clock does
not extend it. Three account levels are only worth anything if the last
administrator cannot be removed.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from sentinel.brokers.connection import (
    BrokerConnection,
    ConnectionError_,
    ConnectionStore,
    ProbeRefused,
    ReadOnlyBroker,
    activation_blockers,
    probe,
    profile_for_company,
)
from sentinel.brokers.secrets import (
    SecretCorrupt,
    SecretStore,
    SecretUnavailable,
    generate_key,
)
UTC = dt.timezone.utc


# --------------------------------------------------------------------------- #
# credential storage
# --------------------------------------------------------------------------- #


class TestSecretStore:
    def test_a_sealed_secret_round_trips(self, tmp_path):
        store = SecretStore(tmp_path / "s.json")
        store.put("broker:live", "hunter2-but-longer")
        assert store.get("broker:live") == "hunter2-but-longer"

    def test_the_plaintext_is_not_in_the_file(self, tmp_path):
        store = SecretStore(tmp_path / "s.json")
        store.put("broker:live", "a-very-distinctive-password")
        raw = (tmp_path / "s.json").read_text(encoding="utf-8")
        assert "a-very-distinctive-password" not in raw
        assert "broker:live" in raw, "names stay readable on purpose"

    def test_a_sealed_value_cannot_be_moved_between_slots(self, tmp_path):
        """The name is authenticated data.

        Without this, anyone who can write the file could swap the blob holding
        the LIVE password into the slot the system reads for the DEMO account.
        The bytes would decrypt, and the system would place live orders
        believing it was on the simulator.
        """
        store = SecretStore(tmp_path / "s.json")
        store.put("broker:demo", "demo-password-x")
        store.put("broker:live", "live-password-y")

        data = json.loads((tmp_path / "s.json").read_text(encoding="utf-8"))
        data["broker:demo"] = data["broker:live"]
        (tmp_path / "s.json").write_text(json.dumps(data), encoding="utf-8")

        with pytest.raises(SecretCorrupt):
            SecretStore(tmp_path / "s.json").get("broker:demo")

    def test_a_wrong_key_is_reported_as_unreadable_not_as_a_wrong_password(
            self, tmp_path, monkeypatch):
        store = SecretStore(tmp_path / "s.json")
        store.put("broker:live", "secret")
        monkeypatch.setenv("SENTINEL_SECRET_KEY", generate_key())
        with pytest.raises(SecretCorrupt) as exc:
            SecretStore(tmp_path / "s.json").get("broker:live")
        assert "NOT a wrong password" in str(exc.value)

    def test_no_key_at_all_refuses_rather_than_obfuscating(self, tmp_path):
        with pytest.raises(SecretUnavailable):
            SecretStore(tmp_path / "s.json", create_key=False)

    def test_the_protection_note_admits_a_colocated_key_is_weak(self, tmp_path):
        note = SecretStore(tmp_path / "s.json").protection_note()
        assert note["level"] == "weak"
        # Persian, because it is rendered verbatim in the dashboard.
        assert "دسترسی دارد" in note["note"]

    def test_an_environment_key_is_reported_as_stronger(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SENTINEL_SECRET_KEY", generate_key())
        note = SecretStore(tmp_path / "s.json").protection_note()
        assert note["level"] == "good"
        assert note["key_path"] is None


# --------------------------------------------------------------------------- #
# the read-only guard
# --------------------------------------------------------------------------- #


class _FakeBroker:
    def __init__(self) -> None:
        self.submitted = []

    def account(self):
        raise AssertionError("not used here")

    def submit(self, *a, **k):
        self.submitted.append((a, k))
        return "FILLED"

    def close_position(self, *a, **k):
        self.submitted.append((a, k))
        return "FILLED"

    def modify_position(self, *a, **k):
        return True

    def cancel(self, *a, **k):
        return True

    def instruments(self):
        return {}


class TestReadOnlyBroker:
    @pytest.mark.parametrize("method", ["submit", "close_position",
                                        "modify_position", "cancel"])
    def test_every_write_is_refused(self, method):
        inner = _FakeBroker()
        guard = ReadOnlyBroker(inner)
        with pytest.raises(ProbeRefused):
            getattr(guard, method)()
        assert inner.submitted == [], "the call reached the venue anyway"

    def test_reads_are_forwarded(self):
        guard = ReadOnlyBroker(_FakeBroker())
        assert guard.instruments() == {}

    def test_an_unknown_method_is_refused_rather_than_forwarded(self):
        """An allow-list, not a deny-list.

        A deny-list silently un-blocks whatever is added to the Broker
        interface next, and the next addition is as likely to be
        `submit_bracket` as `describe`.
        """
        guard = ReadOnlyBroker(_FakeBroker())
        with pytest.raises(ProbeRefused):
            guard.some_new_order_method()

    def test_the_adapter_cannot_be_mutated(self):
        guard = ReadOnlyBroker(_FakeBroker())
        with pytest.raises(ProbeRefused):
            guard.capabilities = None


# --------------------------------------------------------------------------- #
# connection records
# --------------------------------------------------------------------------- #


class TestConnectionRecord:
    def test_an_unknown_profile_is_refused_at_construction(self):
        with pytest.raises(ConnectionError_) as exc:
            BrokerConnection(id="x1", display_name="X", profile="not-a-broker")
        assert "generic_mt5" in str(exc.value)

    def test_a_hostile_id_is_refused(self):
        for bad in ("../etc", "a b", "A", "", "x" * 60, "-lead"):
            with pytest.raises(ConnectionError_):
                BrokerConnection(id=bad, display_name="X", profile="paper")

    def test_the_account_type_must_be_stated(self):
        with pytest.raises(ConnectionError_):
            BrokerConnection(id="x1", display_name="X", profile="paper",
                             declared_account_type="unknown")

    def test_the_redacted_view_carries_no_secret_reference(self):
        conn = BrokerConnection(id="live-1", display_name="L", profile="amarkets",
                                login="123456789", secret_ref="broker:live-1")
        view = conn.redacted()
        assert "secret_ref" not in view
        assert view["has_credential"] is True
        assert view["login"].endswith("789")
        assert "123456" not in view["login"]

    def test_profiles_are_matched_from_the_terminals_own_company_name(self):
        assert profile_for_company("AMarkets LLC") == "amarkets"
        assert profile_for_company("Alpari International") == "alpari"
        assert profile_for_company("Some Broker Nobody Shipped") == "generic_mt5"


class TestConnectionStore:
    def test_records_survive_a_reopen(self, tmp_path):
        store = ConnectionStore(tmp_path / "b.json")
        store.upsert(BrokerConnection(id="sim", display_name="Sim", profile="paper"))
        assert [c.id for c in ConnectionStore(tmp_path / "b.json").list()] == ["sim"]

    def test_a_corrupt_file_is_moved_aside_rather_than_silently_emptied(
            self, tmp_path):
        """Starting from empty would look exactly like a fresh install, and the
        operator would re-enter credentials over a file that was recoverable."""
        path = tmp_path / "b.json"
        ConnectionStore(path).upsert(
            BrokerConnection(id="sim", display_name="Sim", profile="paper"))
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ConnectionError_):
            ConnectionStore(path).list()
        assert list(tmp_path.glob("b.damaged.*")), "the original was not preserved"

    def test_one_unreadable_record_does_not_hide_the_others(self, tmp_path):
        path = tmp_path / "b.json"
        store = ConnectionStore(path)
        store.upsert(BrokerConnection(id="good", display_name="G", profile="paper"))
        data = json.loads(path.read_text(encoding="utf-8"))
        data["bad"] = {"id": "bad", "display_name": "B", "profile": "nope"}
        path.write_text(json.dumps(data), encoding="utf-8")
        reopened = ConnectionStore(path)
        assert [c.id for c in reopened.list()] == ["good"]
        assert reopened.damaged_ids() == ["bad"]


# --------------------------------------------------------------------------- #
# the probe
# --------------------------------------------------------------------------- #


class TestProbe:
    def test_the_simulator_passes_and_its_missing_feed_is_not_a_failure(self):
        conn = BrokerConnection(id="sim", display_name="Sim", profile="paper")
        report = probe(conn)
        assert report.ok, [c.id for c in report.blocking_failures]
        quote_checks = [c for c in report.checks if c.id.startswith("quote:")]
        assert quote_checks and all(c.passed is None for c in quote_checks)

    def test_a_dead_venue_fails_with_an_explanation_not_a_traceback(self):
        conn = BrokerConnection(id="dead", display_name="D", profile="paper")

        def builder(_conn, _secret):
            raise RuntimeError("MT5 initialize failed: (-10005, 'IPC timeout')")

        report = probe(conn, builder=builder)
        assert not report.ok
        connect = [c for c in report.checks if c.id == "connect"][0]
        assert connect.passed is False
        assert "ترمینال" in connect.detail

    def test_a_live_account_declared_as_demo_blocks(self):
        """The single most dangerous disagreement this module can find.

        The operator believes they are testing. The venue is charging them.
        """
        conn = BrokerConnection(id="mis", display_name="M", profile="paper",
                                declared_account_type="demo")

        class LiveLookingPaper:
            capabilities = None

            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def account(self):
                state = self._inner.account()
                object.__setattr__(state, "account_type", "live")
                return state

        from sentinel.bootstrap import DEFAULT_INSTRUMENTS
        from sentinel.brokers.paper import PaperBroker

        def builder(_conn, _secret):
            inner = PaperBroker(instruments=dict(DEFAULT_INSTRUMENTS))
            wrapped = LiveLookingPaper(inner)
            wrapped.capabilities = inner.capabilities
            return wrapped

        report = probe(conn, builder=builder)
        kinds = {c.id: c for c in report.checks}
        assert kinds["account_type"].passed is False
        assert kinds["account_type"].severity == "block"
        assert not report.ok

    def test_the_probe_never_reaches_the_order_path(self):
        """Structural, not conventional: a builder that hands back an adapter
        whose reads secretly trade still cannot trade, because the probe only
        ever touches the wrapper."""
        conn = BrokerConnection(id="sim", display_name="S", profile="paper")
        sent = []

        from sentinel.bootstrap import DEFAULT_INSTRUMENTS
        from sentinel.brokers.paper import PaperBroker

        class Nosy(PaperBroker):
            def submit(self, *a, **k):  # pragma: no cover - must never run
                sent.append(a)
                return super().submit(*a, **k)

        report = probe(conn, builder=lambda c, s: Nosy(
            instruments=dict(DEFAULT_INSTRUMENTS)))
        assert sent == []
        assert report.ok


# --------------------------------------------------------------------------- #
# the activation gate
# --------------------------------------------------------------------------- #


def _probed(conn, ok=True, finished_ns=None, account_id="PAPER-001"):
    # The SUMMARY shape the store persists: no balance, the account number
    # masked, and its fingerprint for comparison. This helper once stored the
    # number unmasked -- which the store never does -- and so hid that every
    # real account failed the "same account" check.
    from sentinel.brokers.connection import _mask_login, account_fingerprint
    conn.last_probe = {
        "ok": ok, "blocking_failures": [] if ok else ["connect"],
        "finished_ns": finished_ns if finished_ns is not None
        else int(dt.datetime.now(UTC).timestamp() * 1e9),
        "account_id": _mask_login(account_id), "account_ref": account_fingerprint(account_id),
        "checks": [],
    }
    return conn


class TestActivationGate:
    def test_an_untested_connection_cannot_be_activated(self):
        conn = BrokerConnection(id="new", display_name="N", profile="paper")
        reasons = activation_blockers(conn, open_positions=0)
        assert any("آزمایش" in r for r in reasons)

    def test_switching_venue_with_open_positions_is_refused(self):
        """They belong to the venue holding them. The new adapter reports them
        as orphans and the reconciler halts -- at best."""
        conn = _probed(BrokerConnection(id="new", display_name="N", profile="paper"))
        reasons = activation_blockers(conn, open_positions=3,
                                      current_broker_name="oanda")
        assert any("معاملهٔ باز" in r for r in reasons)

    def test_the_same_venue_with_open_positions_is_allowed(self):
        conn = _probed(BrokerConnection(id="paper", display_name="N",
                                        profile="paper"))
        reasons = activation_blockers(conn, open_positions=3,
                                      current_broker_name="paper")
        assert reasons == []

    def test_a_stale_probe_is_refused(self):
        old = int((dt.datetime.now(UTC) - dt.timedelta(days=5)).timestamp() * 1e9)
        conn = _probed(BrokerConnection(id="x1", display_name="X", profile="paper"),
                       finished_ns=old)
        reasons = activation_blockers(conn, open_positions=0)
        assert any("ساعت پیش" in r for r in reasons)

    def test_a_probe_of_a_different_account_does_not_count(self):
        conn = BrokerConnection(id="x1", display_name="X", profile="generic_mt5",
                                login="999888777")
        _probed(conn, account_id="111222333")
        reasons = activation_blockers(conn, open_positions=0)
        assert any("حساب دیگری" in r for r in reasons)

    def test_live_needs_a_licence_and_an_accepted_strategy(self):
        conn = _probed(BrokerConnection(id="live", display_name="L",
                                        profile="amarkets",
                                        declared_account_type="live",
                                        login="123"),
                       account_id="123")
        reasons = activation_blockers(conn, open_positions=0,
                                      licence_allows_live=False,
                                      licence_reason="expired",
                                      accepted_strategies=0)
        assert any("لایسنس" in r for r in reasons)
        assert any("پذیرش" in r for r in reasons)

    def test_a_clean_live_connection_passes(self):
        conn = _probed(BrokerConnection(id="live", display_name="L",
                                        profile="amarkets",
                                        declared_account_type="live",
                                        login="123"),
                       account_id="123")
        assert activation_blockers(conn, open_positions=0,
                                   licence_allows_live=True,
                                   accepted_strategies=2) == []


# --------------------------------------------------------------------------- #
# quarterly licences
# --------------------------------------------------------------------------- #


from sentinel.licensing.clock_guard import ClockGuard  # noqa: E402
from sentinel.licensing.enforcement import LicenseGate  # noqa: E402
from sentinel.licensing.license import (  # noqa: E402
    DEFAULT_TERM_MONTHS,
    LicenseInvalid,
    add_months,
    generate_keypair,
    issue,
    renew,
    verify,
)

_MACHINE = {"machine_id": "a" * 16, "mac": "b" * 16, "cpu": "c" * 16,
            "root_fs": "d" * 16, "hostname": "e" * 16}


@pytest.fixture(scope="module")
def vendor():
    return generate_keypair()


class TestCalendarTerms:
    def test_a_quarter_is_three_calendar_months_not_ninety_days(self):
        start = dt.datetime(2026, 1, 15, tzinfo=UTC)
        assert add_months(start, 3) == dt.datetime(2026, 4, 15, tzinfo=UTC)
        assert (add_months(start, 3) - start).days == 90  # coincidence, this quarter
        summer = dt.datetime(2026, 4, 15, tzinfo=UTC)
        assert (add_months(summer, 3) - summer).days == 91  # and not this one

    def test_naive_month_chaining_loses_a_day_and_the_anchor_prevents_it(self):
        """Clamping is lossy, so chaining quarters ratchets downwards for ever.

        31 Mar -> 30 Jun -> 30 Sep -> 30 Dec -> 30 Mar: one day lost per year,
        and a renewal date that drifts off the invoice. The anchor re-reaches
        for the original day whenever the month is long enough.
        """
        from sentinel.licensing.license import term_end
        start = dt.datetime(2026, 3, 31, tzinfo=UTC)

        naive = start
        for _ in range(4):
            naive = add_months(naive, 3)
        assert naive != add_months(start, 12), "the drift this guards against"

        anchored = start
        for _ in range(4):
            anchored = term_end(anchored, 3, start.day)
        assert anchored == add_months(start, 12) == dt.datetime(
            2027, 3, 31, tzinfo=UTC)

    def test_a_chain_of_quarterly_renewals_lands_on_the_anniversary(self, vendor):
        private, public = vendor
        start = dt.datetime(2026, 3, 31, tzinfo=UTC)
        doc = issue(private_key_pem=private, issued_to="X", tier="live_single",
                    term_months=3, machine=_MACHINE, now=start)
        lic = verify(doc, public, check_machine=False, now=start)
        assert lic.anchor_day == 31
        for _ in range(3):
            just_before = dt.datetime.fromisoformat(
                lic.expires_at.replace("Z", "+00:00")) - dt.timedelta(days=2)
            doc = renew(document=doc, private_key_pem=private,
                        public_key_b64=public, now=just_before)
            lic = verify(doc, public, check_machine=False, now=just_before)
        assert lic.expires_at.startswith("2027-03-31")

    def test_the_end_of_a_month_clamps_down_never_up(self):
        assert add_months(dt.datetime(2026, 1, 31, tzinfo=UTC), 1).day == 28
        assert add_months(dt.datetime(2028, 1, 31, tzinfo=UTC), 1).day == 29  # leap
        assert add_months(dt.datetime(2026, 1, 31, tzinfo=UTC), 3).day == 30
        # Never rolls into the next month: a licence must not gain a day.
        assert add_months(dt.datetime(2026, 1, 31, tzinfo=UTC), 1).month == 2

    def test_the_default_term_is_a_quarter(self, vendor):
        private, public = vendor
        now = dt.datetime(2026, 1, 15, tzinfo=UTC)
        lic = verify(issue(private_key_pem=private, issued_to="X",
                           tier="live_single", machine=_MACHINE, now=now),
                     public, check_machine=False, now=now)
        assert lic.term_months == DEFAULT_TERM_MONTHS == 3
        assert lic.expires_at.startswith("2026-04-15")
        assert lic.term_label == "سه‌ماهه"

    def test_perpetual_still_works_through_the_historical_spelling(self, vendor):
        """`valid_days=None` has meant perpetual since the first release. A
        vendor's issuing script must not start emitting three-month licences
        because of an upgrade nobody told them about."""
        private, public = vendor
        now = dt.datetime(2026, 1, 15, tzinfo=UTC)
        lic = verify(issue(private_key_pem=private, issued_to="X",
                           tier="unlimited", machine=_MACHINE,
                           valid_days=None, now=now),
                     public, check_machine=False,
                     now=now + dt.timedelta(days=4000))
        assert lic.expires_at is None
        assert lic.term_label == "دائمی"

    def test_an_absurd_term_is_refused(self, vendor):
        private, _ = vendor
        with pytest.raises(LicenseInvalid):
            issue(private_key_pem=private, issued_to="X", tier="research",
                  term_months=120, machine=_MACHINE)


class TestRenewal:
    def test_renewing_early_does_not_throw_away_the_remaining_days(self, vendor):
        private, public = vendor
        issued = dt.datetime(2026, 1, 1, tzinfo=UTC)
        first = issue(private_key_pem=private, issued_to="X", tier="live_single",
                      term_months=3, machine=_MACHINE, now=issued)
        # Renewed a week before it lapses.
        early = dt.datetime(2026, 3, 25, tzinfo=UTC)
        second = renew(document=first, private_key_pem=private,
                       public_key_b64=public, now=early)
        lic = verify(second, public, check_machine=False, now=early)
        assert lic.expires_at.startswith("2026-07-01"), (
            "the new term did not start where the old one ended")
        assert lic.term_index == 2

    def test_renewing_long_after_expiry_starts_today(self, vendor):
        """Otherwise a customer returning after six months is handed a licence
        that expired three months ago."""
        private, public = vendor
        issued = dt.datetime(2026, 1, 1, tzinfo=UTC)
        first = issue(private_key_pem=private, issued_to="X", tier="live_single",
                      term_months=3, machine=_MACHINE, now=issued)
        late = dt.datetime(2026, 10, 1, tzinfo=UTC)
        second = renew(document=first, private_key_pem=private,
                       public_key_b64=public, now=late)
        lic = verify(second, public, check_machine=False, now=late)
        assert lic.expires_at.startswith("2027-01-01")

    def test_the_subscription_thread_survives_renewal(self, vendor):
        private, public = vendor
        now = dt.datetime(2026, 1, 1, tzinfo=UTC)
        doc = issue(private_key_pem=private, issued_to="X", tier="live_single",
                    term_months=3, machine=_MACHINE, now=now)
        first = verify(doc, public, check_machine=False, now=now)
        for expected_index in (2, 3, 4):
            when = now + dt.timedelta(days=80 * (expected_index - 1))
            doc = renew(document=doc, private_key_pem=private,
                        public_key_b64=public, now=when)
            lic = verify(doc, public, check_machine=False, now=when)
            assert lic.subscription_id == first.subscription_id
            assert lic.term_index == expected_index

    def test_a_forged_licence_cannot_be_laundered_by_renewing_it(self, vendor):
        private, public = vendor
        other_private, _ = generate_keypair()
        forged = issue(private_key_pem=other_private, issued_to="X",
                       tier="unlimited", machine=_MACHINE)
        with pytest.raises(LicenseInvalid):
            renew(document=forged, private_key_pem=private, public_key_b64=public)

    def test_a_perpetual_licence_has_nothing_to_renew(self, vendor):
        private, public = vendor
        doc = issue(private_key_pem=private, issued_to="X", tier="unlimited",
                    machine=_MACHINE, perpetual=True)
        with pytest.raises(LicenseInvalid):
            renew(document=doc, private_key_pem=private, public_key_b64=public)


class TestClockRollback:
    """The cheapest bypass of a time-limited licence is `date -s`. These are
    the tests that make that cost something."""

    def _gate(self, tmp_path, public, **kw):
        return LicenseGate(licence_path=str(tmp_path / "licence.key"),
                           public_key=public, fingerprint=_MACHINE,
                           root=str(tmp_path),
                           guard_path=str(tmp_path / "timing.json"), **kw)

    def _install(self, tmp_path, vendor, **kw):
        private, public = vendor
        params = dict(private_key_pem=private, issued_to="X", tier="live_single",
                      term_months=3, machine=_MACHINE,
                      now=dt.datetime(2026, 6, 1, tzinfo=UTC))
        params.update(kw)
        (tmp_path / "licence.key").write_text(issue(**params), encoding="utf-8")
        return public

    def test_a_healthy_licence_is_valid(self, tmp_path, vendor):
        public = self._install(tmp_path, vendor)
        status = self._gate(tmp_path, public).check(
            now=dt.datetime(2026, 6, 2, tzinfo=UTC))
        assert status.valid and status.stage == "healthy"

    def test_winding_the_clock_back_refuses(self, tmp_path, vendor):
        public = self._install(tmp_path, vendor)
        self._gate(tmp_path, public).check(now=dt.datetime(2026, 8, 20, tzinfo=UTC))
        rolled = self._gate(tmp_path, public).check(
            now=dt.datetime(2026, 6, 2, tzinfo=UTC))
        assert not rolled.valid
        assert rolled.clock.rolled_back
        assert rolled.clock.rollback_seconds > 30 * 86400

    def test_a_small_correction_is_tolerated(self, tmp_path, vendor):
        """NTP steps, a resumed VM and a confused RTC are all real and small.
        Refusing them would make the guard a support burden, not a control."""
        public = self._install(tmp_path, vendor)
        self._gate(tmp_path, public).check(now=dt.datetime(2026, 6, 10, 12, tzinfo=UTC))
        back = self._gate(tmp_path, public).check(
            now=dt.datetime(2026, 6, 10, 9, tzinfo=UTC))
        assert back.valid and not back.clock.rolled_back

    def test_an_expiry_once_seen_cannot_be_undone_by_the_clock(self, tmp_path, vendor):
        public = self._install(tmp_path, vendor)
        lapsed = self._gate(tmp_path, public).check(
            now=dt.datetime(2026, 10, 1, tzinfo=UTC))
        assert not lapsed.valid

        # Now put the clock back inside the validity window. The signature will
        # verify. The licence must still be refused.
        replayed = self._gate(tmp_path, public).check(
            now=dt.datetime(2026, 7, 1, tzinfo=UTC))
        assert not replayed.valid
        assert replayed.clock.previously_expired or replayed.clock.rolled_back

    def test_a_forged_timing_record_is_detected(self, tmp_path, vendor):
        public = self._install(tmp_path, vendor)
        self._gate(tmp_path, public).check(now=dt.datetime(2026, 6, 2, tzinfo=UTC))
        (tmp_path / "timing.json").write_text(
            json.dumps({"state": {"high_water_ns": 0}, "tag": "00"}),
            encoding="utf-8")
        status = self._gate(tmp_path, public).check(
            now=dt.datetime(2026, 6, 3, tzinfo=UTC))
        assert status.clock.state_unreadable
        assert any("timing record" in w for w in status.warnings)

    def test_the_guard_never_blocks_boot_when_its_directory_is_unwritable(
            self, tmp_path):
        guard = ClockGuard(tmp_path / "nope" / "deep" / "timing.json",
                           public_key_b64="k", fingerprint=_MACHINE)
        (tmp_path / "nope").write_text("a file, not a directory", encoding="utf-8")
        report = guard.evaluate(now_ns=1_000_000_000)
        assert report.ok, "a read-only state directory must not stop trading"

    def test_the_renewal_ladder_escalates_in_words(self, tmp_path, vendor):
        public = self._install(tmp_path, vendor)
        stages = {}
        # Walk forward only, so the rollback guard is never the thing failing.
        # 1 June + 3 calendar months = 1 September, i.e. 92 days.
        # approaching <= 30 left (day 62+), due <= 14 (day 78+), critical <= 3.
        for day, label in ((2, "healthy"), (65, "approaching"), (80, "due"),
                           (90, "critical")):
            when = dt.datetime(2026, 6, 1, tzinfo=UTC) + dt.timedelta(days=day)
            status = self._gate(tmp_path, public).check(now=when)
            stages[label] = status
            assert status.stage == label, f"day {day} was {status.stage}"
            assert status.headline and status.advice
        assert stages["critical"].advice != stages["approaching"].advice


# --------------------------------------------------------------------------- #
# three account levels
# --------------------------------------------------------------------------- #


def _sm(tmp_path, name="users.db"):
    from sentinel.api.security import SecurityManager, UserStore
    from sentinel.core.audit import AuditLog
    return SecurityManager(AuditLog(tmp_path / "audit.jsonl"),
                           secret="t" * 48, store=UserStore(tmp_path / name))


GOOD_PW = "correct-horse-battery-staple"


class TestAccountLevels:
    def test_the_three_levels_have_the_authority_they_claim(self, tmp_path):
        sm = _sm(tmp_path)
        sm.add_user("boss", GOOD_PW, "owner")
        sm.add_user("hand", GOOD_PW, "operator")
        sm.add_user("eyes", GOOD_PW, "viewer")
        by_name = {u["username"]: u for u in sm.user_summaries()}
        assert (by_name["boss"]["can_write"], by_name["boss"]["can_change_risk"]) \
            == (True, True)
        assert (by_name["hand"]["can_write"], by_name["hand"]["can_change_risk"]) \
            == (True, False)
        assert (by_name["eyes"]["can_write"], by_name["eyes"]["can_change_risk"]) \
            == (False, False)

    def test_the_listing_never_carries_a_hash_or_a_totp_secret(self, tmp_path):
        sm = _sm(tmp_path)
        sm.add_user("boss", GOOD_PW, "owner")
        blob = json.dumps(sm.user_summaries())
        assert "password_hash" not in blob and "totp_secret" not in blob
        assert "$argon2" not in blob

    def test_the_last_owner_cannot_be_demoted(self, tmp_path):
        """One click would otherwise leave nobody able to change a risk limit,
        release the kill switch, or create another owner."""
        sm = _sm(tmp_path)
        sm.add_user("boss", GOOD_PW, "owner")
        sm.add_user("hand", GOOD_PW, "operator")
        with pytest.raises(ValueError) as exc:
            sm.set_role("boss", "viewer")
        assert "تنها مدیر" in str(exc.value)

    def test_the_last_owner_cannot_be_disabled_or_deleted(self, tmp_path):
        sm = _sm(tmp_path)
        sm.add_user("boss", GOOD_PW, "owner")
        with pytest.raises(ValueError):
            sm.set_disabled("boss", True)
        with pytest.raises(ValueError):
            sm.delete_user("boss")

    def test_a_second_owner_unlocks_the_first(self, tmp_path):
        sm = _sm(tmp_path)
        sm.add_user("boss", GOOD_PW, "owner")
        sm.add_user("deputy", GOOD_PW, "owner")
        assert sm.set_role("boss", "viewer") is True
        assert sm.enabled_owners() == ["deputy"]

    def test_a_disabled_owner_does_not_count_as_cover(self, tmp_path):
        sm = _sm(tmp_path)
        sm.add_user("boss", GOOD_PW, "owner")
        sm.add_user("deputy", GOOD_PW, "owner")
        sm.set_disabled("deputy", True)
        with pytest.raises(ValueError):
            sm.set_role("boss", "operator")

    def test_the_last_owner_flag_is_reported_to_the_dashboard(self, tmp_path):
        sm = _sm(tmp_path)
        sm.add_user("boss", GOOD_PW, "owner")
        sm.add_user("hand", GOOD_PW, "operator")
        rows = {u["username"]: u for u in sm.user_summaries()}
        assert rows["boss"]["is_last_owner"] is True
        assert rows["hand"]["is_last_owner"] is False


class TestPasswordPolicy:
    @pytest.mark.parametrize("password", [
        "short", "password123", "aaaaaaaaaaaaaaaa", "alice-alice-alice",
    ])
    def test_weak_passwords_are_refused_with_a_reason(self, tmp_path, password):
        sm = _sm(tmp_path)
        with pytest.raises(ValueError) as exc:
            sm.add_user("alice", password, "viewer")
        assert str(exc.value).strip(), "refused with no explanation"

    def test_a_reset_to_a_weak_password_raises_rather_than_returning_false(
            self, tmp_path):
        """False means 'no such user'. Conflating the two tells an operator
        their reset silently failed."""
        sm = _sm(tmp_path)
        sm.add_user("alice", GOOD_PW, "viewer")
        with pytest.raises(ValueError):
            sm.set_password("alice", "123456")
        assert sm.set_password("nobody-here", GOOD_PW) is False

    def test_a_hostile_username_is_refused(self, tmp_path):
        sm = _sm(tmp_path)
        for bad in ("a", "x" * 70, "alice smith", "../root", "کاربر"):
            with pytest.raises(ValueError):
                sm.add_user(bad, GOOD_PW, "viewer")


class TestTotpRotation:
    def test_rotation_invalidates_the_old_factor_and_every_session(self, tmp_path):
        import pyotp
        sm = _sm(tmp_path)
        user, _uri = sm.add_user("alice", GOOD_PW, "operator")
        old_secret = user.totp_secret
        token, _ = sm.login("alice", GOOD_PW, user_agent="ua", client_ip="127.0.0.1")
        assert sm.verify_token(token, user_agent="ua", client_ip="127.0.0.1")

        new_uri = sm.rotate_totp("alice", actor="boss")
        assert new_uri and "otpauth://" in new_uri
        assert sm.verify_token(token, user_agent="ua", client_ip="127.0.0.1") is None, \
            "sessions survived a second-factor rotation"
        ok, _ = sm.verify_totp("alice", pyotp.TOTP(old_secret).now())
        assert ok is False, "the old authenticator still worked"

    def test_rotating_an_unknown_user_returns_none(self, tmp_path):
        assert _sm(tmp_path).rotate_totp("ghost") is None


# --------------------------------------------------------------------------- #
# regressions from the adversarial audit
# --------------------------------------------------------------------------- #


from sentinel.brokers.connection import same_account  # noqa: E402


class TestAccountIdentity:
    """Substring matching was used for account identity. Brokers issue
    CONSECUTIVE account numbers, so it matched the wrong account."""

    @pytest.mark.parametrize("a,b,expected", [
        ("50123", "501234", False),     # the neighbouring account
        ("123", "51234567", False),     # a stub the operator typed
        ("7", "88887777", False),       # matches almost anything
        ("1234567", "1234-567", True),  # same account, formatted differently
        ("1234567", "0001234567", True),
        ("1234567", "1234567", True),
        ("1234567", "", None),          # the venue did not say
    ])
    def test_identity_is_equality_not_containment(self, a, b, expected):
        assert same_account(a, b) is expected

    def test_a_silent_venue_is_not_a_pass(self):
        conn = BrokerConnection(id="mt", display_name="M", profile="generic_mt5",
                                login="1234567")
        _probed(conn, account_id="")
        # An unknown account id must not silently satisfy the check, but it is
        # also not evidence of a mismatch, so it does not block activation.
        assert not any("حساب دیگری" in r for r in
                       activation_blockers(conn, open_positions=0))

    def test_a_neighbouring_account_now_blocks(self):
        conn = BrokerConnection(id="mt", display_name="M", profile="generic_mt5",
                                login="50123")
        _probed(conn, account_id="501234")
        assert any("حساب دیگری" in r for r in
                   activation_blockers(conn, open_positions=0))


class TestActivationFailsClosed:
    def test_an_unknown_licence_verdict_does_not_permit_live(self):
        """`is False` let None -- "we could not determine it" -- through, and
        the caller produces None whenever the licence gate is absent or raised."""
        conn = _probed(BrokerConnection(id="live", display_name="L",
                                        profile="amarkets",
                                        declared_account_type="live", login="123"),
                       account_id="123")
        reasons = activation_blockers(conn, open_positions=0,
                                      licence_allows_live=None,
                                      accepted_strategies=3)
        assert any("لایسنس" in r for r in reasons)

    def test_a_probe_from_the_future_is_refused(self):
        ahead = int((dt.datetime.now(UTC) + dt.timedelta(days=2)).timestamp() * 1e9)
        conn = _probed(BrokerConnection(id="x1", display_name="X", profile="paper"),
                       finished_ns=ahead)
        assert any("جلوتر از ساعت" in r for r in
                   activation_blockers(conn, open_positions=0))


class TestProbeDoesNotLeakTheAccount:
    def test_the_stored_and_redacted_probe_carry_no_balance(self, tmp_path):
        conn = BrokerConnection(id="sim", display_name="S", profile="paper")
        store = ConnectionStore(tmp_path / "b.json")
        store.upsert(conn)
        store.set_probe("sim", probe(conn))

        # Structural, not a substring sweep: "account" is also a CHECK ID.
        raw = probe(conn).to_dict()
        assert "balance" in (raw["account"] or {}), "the live report should carry it"

        on_disk = (tmp_path / "b.json").read_text(encoding="utf-8")
        view = store.get("sim").redacted()
        stored = view["last_probe"]
        for forbidden in ("balance", "equity", "margin_used", "margin_available",
                          "unrealised_pnl"):
            assert forbidden not in on_disk, f"{forbidden} was persisted"
            assert forbidden not in json.dumps(view, ensure_ascii=False)
        assert "account" not in stored, "the raw account block survived"
        assert stored["ok"] is True
        # The masked id is kept, because "which account did this test reach"
        # is the question the report exists to answer.
        assert set(stored) >= {"account_id", "account_currency", "account_type"}
        assert not any(ch.isdigit() for ch in stored["account_id"][:-3])


class TestExclusiveEnable:
    def test_enabling_one_disables_every_other_in_one_write(self, tmp_path):
        store = ConnectionStore(tmp_path / "b.json")
        for name in ("a1", "b1", "c1"):
            conn = BrokerConnection(id=name, display_name=name, profile="paper")
            conn.enabled = True
            store.upsert(conn)
        store.set_exclusive_enabled("b1")
        enabled = [c.id for c in store.list() if c.enabled]
        assert enabled == ["b1"], enabled


class TestPasswordBlocklistIsReachable:
    def test_the_blocklist_can_actually_reject(self):
        """Every entry used to be shorter than the minimum length, and the
        length check ran first -- so not one of them could ever fire."""
        from sentinel.api.security import (
            MIN_PASSWORD_LENGTH, _COMMON_PASSWORDS, check_password_policy,
        )
        assert any(len(p) >= MIN_PASSWORD_LENGTH for p in _COMMON_PASSWORDS)
        for weak in ("passwordpassword", "Password123!", "password1234",
                     "QWERTY123456"):
            assert check_password_policy(weak), f"{weak} was accepted"
        assert check_password_policy("correct-horse-battery-staple") is None


class TestVenueFailsClosed:
    """Regressions for the defects the second audit round found."""

    def test_the_leverage_check_measures_against_the_engines_assumption(self):
        """A profile declaring max_leverage=1000 made the check unfailable for
        exactly the offshore brokers where leverage matters most."""
        from sentinel.brokers.connection import ASSUMED_MAX_LEVERAGE, _probe_account
        assert ASSUMED_MAX_LEVERAGE == 30

        class Acct:
            def to_dict(self):
                return {"account_id": "1", "currency": "USD", "balance": "1",
                        "equity": "1", "leverage": 500, "account_type": "live"}

        class Fake:
            capabilities = None
            def account(self): return Acct()

        conn = BrokerConnection(id="lev", display_name="L", profile="amarkets",
                                declared_account_type="live", login="1")
        checks, report = [], type("R", (), {"account": {}})()
        _probe_account(Fake(), conn, checks, report)
        lev = [c for c in checks if c.id == "leverage"][0]
        assert lev.passed is False, "1:500 passed against a 1:1000 profile"

    def test_an_unreported_leverage_is_flagged_not_skipped(self):
        from sentinel.brokers.connection import _probe_account

        class Acct:
            def to_dict(self):
                return {"account_id": "1", "currency": "USD", "balance": "1",
                        "equity": "1", "leverage": 0, "account_type": "demo"}

        class Fake:
            capabilities = None
            def account(self): return Acct()

        conn = BrokerConnection(id="lev2", display_name="L", profile="paper")
        checks, report = [], type("R", (), {"account": {}})()
        _probe_account(Fake(), conn, checks, report)
        lev = [c for c in checks if c.id == "leverage"][0]
        assert lev.passed is None and lev.severity == "warn"

    def test_a_configured_symbol_the_venue_lacks_is_reported(self):
        conn = BrokerConnection(id="sim", display_name="S", profile="paper")
        report = probe(conn, instruments=["EUR_USD", "XAU_USD", "NOT_REAL"])
        missing = [c for c in report.checks if c.id == "instruments_missing"][0]
        assert missing.passed is False and missing.severity == "block"
        assert "NOT_REAL" in missing.detail
        assert not report.ok

    def test_the_probe_cannot_hang_the_caller(self):
        import time as _t

        def slow(_conn, _secret):
            _t.sleep(20)

        conn = BrokerConnection(id="slow", display_name="S", profile="paper")
        began = _t.monotonic()
        report = probe(conn, builder=slow, timeout_sec=1.0)
        assert _t.monotonic() - began < 8.0, "the timeout did not bound anything"
        assert not report.ok
        assert [c.id for c in report.blocking_failures] == ["timeout"]

    def test_mt5_reports_a_demo_account_as_demo(self):
        """ACCOUNT_TRADE_MODE_DEMO is 0, and `or -1` erased it -- so every
        ordinary MT5 demo account reported its type as unknown."""
        from sentinel.brokers.mt5 import _MT5_TRADE_MODE, _trade_mode

        class A:
            trade_mode = 0

        assert _trade_mode(A()) == 0
        assert _MT5_TRADE_MODE[_trade_mode(A())] == "demo"

        class B:
            pass

        assert _trade_mode(B()) == -1
        assert _MT5_TRADE_MODE.get(_trade_mode(B()), "") == ""


class TestSecretStoreFailsLoud:
    def test_a_non_object_file_refuses_rather_than_wiping(self, tmp_path):
        """Returning {} meant the next put() wrote a file holding only the new
        entry: every other credential gone, with no error."""
        store = SecretStore(tmp_path / "s.json")
        store.put("broker:a", "aaaaaaaaaaaa")
        store.put("broker:b", "bbbbbbbbbbbb")
        (tmp_path / "s.json").write_text('["not", "an", "object"]', encoding="utf-8")
        reopened = SecretStore(tmp_path / "s.json")
        with pytest.raises(SecretCorrupt):
            reopened.put("broker:c", "cccccccccccc")
        with pytest.raises(SecretCorrupt):
            reopened.names()


class TestClockGuardKeying:
    """The guard's key was derived from the LIVE fingerprint, which a licence
    deliberately tolerates changing (3 of 5 must match). So one hostname change
    rotated the key, the state failed to authenticate, the history was
    discarded, and a permanently-expired licence came back valid."""

    def _install(self, tmp_path, vendor, machine):
        private, public = vendor
        (tmp_path / "licence.key").write_text(
            issue(private_key_pem=private, issued_to="X", tier="live_single",
                  term_months=3, machine=machine,
                  now=dt.datetime(2026, 1, 1, tzinfo=UTC)), encoding="utf-8")
        return public

    def _gate(self, tmp_path, public, fingerprint):
        return LicenseGate(licence_path=str(tmp_path / "licence.key"),
                           public_key=public, fingerprint=fingerprint,
                           root=str(tmp_path),
                           guard_path=str(tmp_path / "timing.json"))

    def test_changing_the_hostname_does_not_reset_the_guard(self, tmp_path, vendor):
        bound = {"machine_id": "a" * 16, "mac": "b" * 16, "cpu": "c" * 16,
                 "root_fs": "d" * 16, "hostname": "e" * 16}
        public = self._install(tmp_path, vendor, bound)

        # Watch it expire, past the grace period.
        for when in (dt.datetime(2026, 6, 1, tzinfo=UTC),
                     dt.datetime(2026, 6, 2, tzinfo=UTC)):
            lapsed = self._gate(tmp_path, public, bound).check(now=when)
        assert not lapsed.valid

        # One fingerprint component changed -- the licence still binds (4 of 5)
        # -- and the clock wound back inside the validity window.
        changed = dict(bound, hostname="z" * 16)
        after = self._gate(tmp_path, public, changed).check(
            now=dt.datetime(2026, 3, 1, tzinfo=UTC))
        assert not after.valid, "the guard was reset by a hostname change"
        assert after.clock.rolled_back or after.clock.previously_expired
        allowed, _ = self._gate(tmp_path, public, changed).may_trade_live()
        assert allowed is False

    def test_an_unwritable_state_is_reported_and_does_not_block_trading(
            self, tmp_path, vendor):
        bound = {"machine_id": "a" * 16, "mac": "b" * 16}
        public = self._install(tmp_path, vendor, bound)
        # A FILE where the state directory must be: os.makedirs fails even for
        # root, which a permission bit does not.
        (tmp_path / "blocked").write_text("not a directory", encoding="utf-8")
        gate = LicenseGate(licence_path=str(tmp_path / "licence.key"),
                           public_key=public, fingerprint=bound,
                           root=str(tmp_path),
                           guard_path=str(tmp_path / "blocked" / "t.json"))
        status = gate.check(now=dt.datetime(2026, 2, 2, tzinfo=UTC))
        assert status.clock.state_unwritable and status.clock.degraded
        assert status.valid, "a guard that cannot write must not stop trading"
        assert any("سابقهٔ زمانی" in w for w in status.warnings)


class TestLicenceIsRecheckedWhileRunning:
    def test_the_verdict_is_not_frozen_at_boot(self, tmp_path, vendor):
        """`check()` memoised for ever, so a three-month licence on a server
        that stays up authorised live entries months past its expiry and the
        dashboard's countdown never moved."""
        private, public = vendor
        (tmp_path / "licence.key").write_text(
            issue(private_key_pem=private, issued_to="X", tier="live_single",
                  term_months=3, machine=_MACHINE,
                  now=dt.datetime(2026, 1, 1, tzinfo=UTC)), encoding="utf-8")
        gate = LicenseGate(licence_path=str(tmp_path / "licence.key"),
                           public_key=public, fingerprint=_MACHINE,
                           root=str(tmp_path), guard_path=str(tmp_path / "t.json"))
        assert gate.recheck_seconds > 0
        first = gate.check(now=dt.datetime(2026, 2, 1, tzinfo=UTC))
        assert first.valid
        # An explicit instant always re-evaluates rather than replaying a
        # cached verdict for a different moment.
        later = gate.check(now=dt.datetime(2026, 6, 1, tzinfo=UTC))
        assert not later.valid


class TestLicenceGeneratorCLI:
    """The generator is the vendor's only interface to the licence format, and
    a defect there is invisible until a customer's licence behaves wrongly."""

    def _run(self, tmp_path, *args):
        import subprocess
        import sys
        script = Path(__file__).resolve().parents[1] / "scripts" / "licensegen.py"
        return subprocess.run([sys.executable, str(script), *args],
                              capture_output=True, text=True, cwd=str(tmp_path))

    def test_months_actually_produces_a_term(self, tmp_path):
        """`--months 3` used to emit a PERPETUAL licence: the command passed
        `valid_days=None` whenever --days was absent, and None is the
        historical spelling of "never expires"."""
        keys = tmp_path / "keys"
        assert self._run(tmp_path, "keygen", "--out", str(keys)).returncode == 0
        (tmp_path / "fp.json").write_text(json.dumps(
            {"fingerprint": {"machine_id": "a", "cpu_model": "b", "hostname": "c"}}),
            encoding="utf-8")

        out = tmp_path / "q1.key"
        r = self._run(tmp_path, "issue", "--key", str(keys / "private.pem"),
                      "--to", "Acme", "--tier", "live_single", "--months", "3",
                      "--machine-file", str(tmp_path / "fp.json"),
                      "--out", str(out))
        assert r.returncode == 0, r.stderr
        licence = verify(out.read_text(encoding="utf-8"),
                         (keys / "public.txt").read_text(encoding="utf-8").strip(),
                         check_machine=False)
        assert licence.expires_at is not None, "a --months licence never expires"
        assert licence.term_months == 3
        assert licence.term_index == 1

    def test_the_renew_command_extends_the_same_subscription(self, tmp_path):
        keys = tmp_path / "keys"
        self._run(tmp_path, "keygen", "--out", str(keys))
        (tmp_path / "fp.json").write_text(json.dumps(
            {"fingerprint": {"machine_id": "a", "cpu_model": "b", "hostname": "c"}}),
            encoding="utf-8")
        pub = (keys / "public.txt").read_text(encoding="utf-8").strip()
        q1, q2 = tmp_path / "q1.key", tmp_path / "q2.key"
        self._run(tmp_path, "issue", "--key", str(keys / "private.pem"),
                  "--to", "Acme", "--tier", "live_single", "--months", "3",
                  "--machine-file", str(tmp_path / "fp.json"), "--out", str(q1))
        r = self._run(tmp_path, "renew", "--key", str(keys / "private.pem"),
                      "--pubkey", str(keys / "public.txt"),
                      "--licence", str(q1), "--out", str(q2))
        assert r.returncode == 0, r.stderr

        first = verify(q1.read_text(encoding="utf-8"), pub, check_machine=False)
        second = verify(q2.read_text(encoding="utf-8"), pub, check_machine=False)
        assert second.subscription_id == first.subscription_id
        assert second.term_index == first.term_index + 1
        assert second.expires_at > first.expires_at
        assert second.machine == first.machine, "the machine binding was dropped"

    def test_perpetual_still_needs_to_be_asked_for(self, tmp_path):
        keys = tmp_path / "keys"
        self._run(tmp_path, "keygen", "--out", str(keys))
        out = tmp_path / "site.key"
        r = self._run(tmp_path, "issue", "--key", str(keys / "private.pem"),
                      "--to", "Acme", "--tier", "unlimited", "--perpetual",
                      "--unbound", "--out", str(out))
        assert r.returncode == 0, r.stderr
        assert "NEVER EXPIRES" in r.stderr, "a perpetual licence issued quietly"
        licence = verify(out.read_text(encoding="utf-8"),
                         (keys / "public.txt").read_text(encoding="utf-8").strip(),
                         check_machine=False)
        assert licence.expires_at is None
