"""Shipped profiles.

Every value here is a **prior**, not an authority. Each is verified against the
live terminal at startup and the terminal always wins (see
``BrokerProfile.verify_against``). Where a figure varies by account type,
promotion or region -- which is most of them -- the profile carries the
conservative end and `verify_before_live` names it as something to confirm on
the actual account.

That caveat is not boilerplate. Broker terms change without notice, differ per
account tier, and are frequently described one way in marketing and another in
the contract specification. Nobody should trade real money on the strength of
a constant in a Python file, including this one.

**On counterparty risk.** Several venues below are regulated offshore. That is
a legitimate way for much of the world to access these markets and it is not a
judgement about any particular firm -- but it does change what happens if the
firm fails or disputes a withdrawal, and no strategy compensates for a broker
that will not return your money. The acceptance protocol's gate **L0.6**
requires a real deposit-hold-withdraw cycle to have completed before scale-up,
for exactly this reason. Do that first. It is the cheapest test in the system.
"""

from __future__ import annotations

from ...core.money import D
from .base import BrokerProfile, FillingMode, SymbolMap, register_profile

# --------------------------------------------------------------------------- #
# The safe default: declare nothing, read everything.
# --------------------------------------------------------------------------- #

register_profile(BrokerProfile(
    name="generic_mt5",
    display_name="Any MetaTrader 5 broker",
    adapter="mt5",
    symbols=SymbolMap(),          # exact names; inferred at startup if needed
    min_stop_level_points=0,      # corrected upward by the live check
    default_filling=FillingMode.AUTO,
    commission_per_lot_round_turn=D("0"),
    default_spread_pips=D("1.5"),
    server_utc_offset_hours=2,
    regulator="unknown",
    notes=(
        "Declares nothing it cannot verify. Symbol convention is inferred from "
        "the terminal's own symbol list, stop levels and contract sizes are "
        "read per symbol, and the filling mode is asked for rather than "
        "assumed. Slower to start and impossible to get wrong -- which is the "
        "right trade for a venue nobody has checked."),
    verify_before_live=[
        "the symbol suffix the terminal actually uses",
        "minimum stop distance on every instrument you intend to trade",
        "commission per lot, and whether it is charged per side or per round turn",
        "server time offset, and whether it shifts with DST",
        "a completed deposit -> hold -> withdrawal cycle (gate L0.6)",
    ],
))

register_profile(BrokerProfile(
    name="generic_mt4",
    display_name="Any MetaTrader 4 broker",
    adapter="mt4",
    symbols=SymbolMap(),
    default_filling=FillingMode.FOK,
    supports_partial_close=True,
    supports_hedging=True,
    default_spread_pips=D("1.8"),
    regulator="unknown",
    notes=(
        "MT4 is a bridge, not a first-class adapter. It has no client order id, "
        "a weaker order-state model than MT5, and no Python API of its own -- "
        "it needs an Expert Advisor bridge on the terminal. The system runs "
        "against it in the same degraded mode as MT5 and refuses live "
        "promotion on capability grounds unless the operator accepts the "
        "degradation explicitly. Prefer MT5 wherever the broker offers both."),
    verify_before_live=[
        "that the EA bridge is running and reconnects on its own",
        "everything in the generic_mt5 list",
    ],
))

# --------------------------------------------------------------------------- #
# AMarkets
# --------------------------------------------------------------------------- #

register_profile(BrokerProfile(
    name="amarkets",
    display_name="AMarkets",
    adapter="mt5",
    # AMarkets runs several account types. Standard and Fixed use plain
    # symbols; the ECN book carries a suffix on many servers. The startup
    # check resolves which one this account sees, so an empty suffix here is a
    # starting guess and not a claim.
    symbols=SymbolMap(suffix=""),
    min_stop_level_points=0,
    default_filling=FillingMode.AUTO,
    deviation_points=20,
    default_contract_size=D("100000"),
    default_min_lot=D("0.01"),
    default_lot_step=D("0.01"),
    default_max_lot=D("100"),
    # ECN accounts charge commission and quote a rawer spread; Standard
    # accounts charge none and widen the spread instead. Neither is free.
    commission_per_lot_round_turn=D("0"),
    default_spread_pips=D("1.3"),
    typical_spread_pips={
        "EUR_USD": D("1.1"), "GBP_USD": D("1.4"), "USD_JPY": D("1.2"),
        "AUD_USD": D("1.4"), "USD_CHF": D("1.6"), "USD_CAD": D("1.6"),
        "EUR_JPY": D("1.8"), "GBP_JPY": D("2.4"),
    },
    server_utc_offset_hours=3,
    server_observes_dst=True,
    max_leverage=200,
    supports_client_order_id=False,
    supports_server_side_stop=True,
    supports_partial_close=True,
    supports_hedging=True,
    regulator="offshore (verify the entity your account is actually with)",
    segregated_client_funds=None,
    negative_balance_protection=None,
    notes=(
        "MT5 broker with Standard, Fixed and ECN account types whose costs and "
        "symbol names differ. High headline leverage: 200:1 lets a 0.5% "
        "per-trade risk budget coexist with a position that a 2% adverse move "
        "liquidates, so the leverage ceiling in RiskConfig matters more here "
        "than the broker's own limit -- the broker's limit is what it will "
        "ALLOW, not what is survivable."),
    verify_before_live=[
        "which account type this login is (Standard / Fixed / ECN) -- it "
        "changes both the spread and whether commission is charged",
        "the exact commission per lot, per side or per round turn",
        "the symbol suffix on THIS server (ECN books often differ)",
        "minimum stop distance, especially on JPY crosses and metals",
        "the regulated entity your account is with, and what protection it carries",
        "a completed deposit -> hold -> withdrawal cycle (gate L0.6)",
    ],
))

# --------------------------------------------------------------------------- #
# Alpari
# --------------------------------------------------------------------------- #

register_profile(BrokerProfile(
    name="alpari",
    display_name="Alpari",
    adapter="mt5",
    # Alpari's micro and ECN books have historically carried suffixes on some
    # servers. Resolved at startup rather than guessed here.
    symbols=SymbolMap(suffix=""),
    min_stop_level_points=0,
    default_filling=FillingMode.AUTO,
    deviation_points=20,
    default_contract_size=D("100000"),
    default_min_lot=D("0.01"),
    default_lot_step=D("0.01"),
    default_max_lot=D("100"),
    commission_per_lot_round_turn=D("0"),
    default_spread_pips=D("1.4"),
    typical_spread_pips={
        "EUR_USD": D("1.2"), "GBP_USD": D("1.5"), "USD_JPY": D("1.3"),
        "AUD_USD": D("1.5"), "USD_CHF": D("1.7"), "USD_CAD": D("1.7"),
        "EUR_JPY": D("1.9"), "GBP_JPY": D("2.6"),
    },
    server_utc_offset_hours=2,
    server_observes_dst=True,
    max_leverage=1000,
    supports_client_order_id=False,
    supports_server_side_stop=True,
    supports_partial_close=True,
    supports_hedging=True,
    regulator="offshore (verify the entity your account is actually with)",
    segregated_client_funds=None,
    negative_balance_protection=None,
    notes=(
        "MT4 and MT5 broker with several account tiers. Headline leverage up "
        "to 1000:1 on some accounts: that is a margin allowance, not a "
        "suggestion, and at 1000:1 a 0.1% adverse move is a full margin call. "
        "The risk engine's own gross-leverage ceiling is what should bind, and "
        "it should be set far below what the account permits.\n\n"
        "Alpari has been through corporate changes historically. That is a "
        "reason to complete gate L0.6 -- a real withdrawal -- before scaling, "
        "not a prediction about the present firm."),
    verify_before_live=[
        "which account tier this login is, and its cost structure",
        "whether the platform is MT4 or MT5 (this profile assumes MT5)",
        "the symbol suffix on THIS server",
        "minimum stop distance on every instrument you will trade",
        "the regulated entity your account is with, and what protection it carries",
        "that your configured max_gross_leverage is far below the account's",
        "a completed deposit -> hold -> withdrawal cycle (gate L0.6)",
    ],
))

# --------------------------------------------------------------------------- #
# Already-supported venues, given profiles so the layer is uniform.
# --------------------------------------------------------------------------- #

register_profile(BrokerProfile(
    name="oanda",
    display_name="OANDA (v20 REST)",
    adapter="oanda",
    kind="fx-cfd",
    symbols=SymbolMap(strip_separator=False),   # OANDA already uses EUR_USD
    min_stop_level_points=0,
    default_contract_size=D("1"),               # OANDA sizes in UNITS, not lots
    default_min_lot=D("1"),
    default_lot_step=D("1"),
    default_max_lot=D("10000000"),
    commission_per_lot_round_turn=D("0"),
    default_spread_pips=D("1.0"),
    server_utc_offset_hours=0,                  # OANDA speaks UTC
    server_observes_dst=False,
    max_leverage=30,
    supports_client_order_id=True,              # the important one
    supports_server_side_stop=True,
    supports_partial_close=True,
    supports_hedging=False,
    regulator="varies by entity (FCA / ASIC / CFTC / MAS ...)",
    segregated_client_funds=True,
    negative_balance_protection=True,
    notes=(
        "The reference venue for this system, and the only supported one with "
        "a venue-deduplicated client order id -- which is why it is the only "
        "one where the idempotency guarantee is a guarantee rather than a "
        "narrowed race. Sizes in units, not lots, so the lot arithmetic "
        "elsewhere maps to a contract size of 1."),
    verify_before_live=["which OANDA entity your account is with",
                        "practice vs live host in OANDA_API_HOST",
                        "a completed deposit -> hold -> withdrawal cycle (gate L0.6)"],
))

register_profile(BrokerProfile(
    name="paper",
    display_name="Internal simulator",
    adapter="paper",
    kind="simulator",
    symbols=SymbolMap(strip_separator=False),
    default_contract_size=D("100000"),
    commission_per_lot_round_turn=D("7"),
    default_spread_pips=D("0.6"),
    server_utc_offset_hours=0,
    server_observes_dst=False,
    supports_client_order_id=True,
    supports_server_side_stop=True,
    supports_partial_close=True,
    regulator="n/a",
    segregated_client_funds=True,
    negative_balance_protection=True,
    notes=(
        "Deliberately adversarial: direction-aware intrabar paths, session "
        "spread widening, partial fills, last-look rejections biased against "
        "the client, and carry. It is pessimistic on purpose, because a "
        "simulator that flatters you is worse than no simulator."),
))

register_profile(BrokerProfile(
    name="ccxt",
    display_name="Crypto venue via CCXT",
    adapter="ccxt",
    kind="crypto",
    symbols=SymbolMap(strip_separator=False),
    default_contract_size=D("1"),
    default_min_lot=D("0.0001"),
    default_lot_step=D("0.0001"),
    commission_per_lot_round_turn=D("0"),
    default_spread_pips=D("2.0"),
    server_utc_offset_hours=0,
    server_observes_dst=False,
    supports_client_order_id=True,
    supports_server_side_stop=False,      # varies wildly; assume the worst
    supports_partial_close=True,
    regulator="varies; frequently none",
    notes=(
        "Crypto venues differ more from each other than FX brokers do. Stop "
        "support in particular is inconsistent and several silently drop an "
        "unsupported parameter -- which is why the adapter checks that a stop "
        "was echoed back rather than assuming it was accepted. Weekend "
        "trading also means the weekend-flatten rule is inappropriate here."),
    verify_before_live=["that the venue accepts and honours a server-side stop",
                        "the fee tier this account is actually on",
                        "withdrawal limits and delays"],
))
