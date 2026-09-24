# Brokers

## How "works with any broker" actually works

Nearly every retail FX broker in the world runs MetaTrader. They all speak the
same protocol and **none of them behaves the same way**:

| What differs | Range seen in practice | What breaks if you get it wrong |
|---|---|---|
| Symbol name | `EURUSD`, `EURUSD.m`, `EURUSDmicro`, `EURUSD-ECN` | Nothing trades; or worse, two spellings of one pair are netted as two instruments |
| Minimum stop distance | 0 to 50+ points, varying per symbol | Every order rejected with "Invalid stops" **after** the position was sized |
| Filling mode | FOK / IOC / RETURN, varying per symbol | Every order rejected with "Unsupported filling mode" |
| Contract size | 100,000 / 10,000 / 1 | Position sized 10× or 100× wrong |
| Server clock | UTC+2, UTC+3, with or without DST | Swap charged on the wrong day; triple-swap missed |
| Commission | 0, or $3.5–$7 per lot, per side or per round turn | The break-even calculation is wrong in the direction that matters |

A **broker profile** writes those differences down once, and the adapter
reconciles the profile against the live terminal at startup.

> **The profile is a prior. The terminal is the authority.** Every disagreement
> is recorded and the terminal's value wins. A profile that silently overrode
> the venue would be a confident lie — and the specific lie ("the minimum stop
> is 0 points") produces an order the venue rejects after the risk engine has
> already sized a position for a stop that cannot exist.

## Using a broker

```json
{ "execution": { "broker": "amarkets", "venue_mode": "paper" } }
```

That is the whole configuration. `broker` accepts either an adapter name
(`paper`, `oanda`, `mt5`, `ccxt`) or a profile name.

```bash
python -c "from sentinel.brokers import list_profiles; \
           [print(f'{p.name:14s} {p.display_name}') for p in list_profiles()]"
```

| Profile | Adapter | Notes |
|---|---|---|
| `paper` | internal | Adversarial simulator. Start here, always. |
| `oanda` | REST v20 | The reference venue. **The only one with a real client order id.** |
| `amarkets` | MT5 | Standard / Fixed / ECN accounts differ in cost and symbols |
| `alpari` | MT5 | Several tiers; very high headline leverage |
| `generic_mt5` | MT5 | **Any unlisted MetaTrader broker.** Declares nothing; reads everything. |
| `generic_mt4` | — | Needs an EA bridge. Prefer the broker's MT5 server. |
| `ccxt` | CCXT | Crypto venues |

### An unlisted broker

Use `generic_mt5`. It declares nothing it cannot verify: it infers the symbol
convention from the terminal's own symbol list, reads stop levels and contract
sizes per symbol, and asks for the filling mode rather than assuming it. Slower
to start and impossible to get wrong.

### Adding a profile

```python
from sentinel.brokers import register_profile
from sentinel.brokers.profiles import BrokerProfile, SymbolMap
from sentinel.core.money import D

register_profile(BrokerProfile(
    name="my_broker",
    display_name="My Broker Ltd",
    adapter="mt5",
    symbols=SymbolMap(suffix=".ecn"),
    min_stop_level_points=20,
    commission_per_lot_round_turn=D("7"),
    server_utc_offset_hours=3,
    regulator="...",
    notes="...",
    verify_before_live=["..."],
))
```

Declare only what you have checked. A blank field costs one startup query; a
wrong field costs an afternoon.

---

## The idempotency problem — read this before going live on MetaTrader

MT5 has **no caller-supplied order identifier that the server deduplicates
on**. `magic` is a strategy tag, not a unique key. `comment` is advisory and
frequently truncated or rewritten by the broker.

That means the guarantee the order manager relies on — *submit twice, fill
once* — **is unavailable on every MetaTrader broker**, including AMarkets and
Alpari. The system does not pretend otherwise. It runs a degraded protocol:

1. A process-level lock serialises submissions per instrument.
2. Before any resend, the venue is queried for a matching deal signature.
3. A blackout window after a timeout: no new order on that instrument until
   the state is proven.

This **narrows** the race. It does not close it. Gate `L0.3` of the acceptance
protocol records it as a standing degradation, the dashboard shows it, and the
comparison is honest:

- **OANDA**: a lost response is resolved by re-sending the same client order
  id. The venue deduplicates. This is a guarantee.
- **MetaTrader**: a lost response is resolved by querying and inferring. This
  is a good inference.

If you are choosing a broker and have the option, this is a real reason to
prefer one with a REST API and a client order id.

---

## Counterparty risk

Several supported venues are regulated offshore. That is a legitimate and often
the only way to access these markets, and it is **not a judgement about any
particular firm** — but it changes what happens if the firm fails or disputes a
withdrawal, and no strategy compensates for a broker that will not return your
money.

This is why the acceptance protocol has gate **L0.6**: a completed
deposit → hold → **withdrawal** cycle, with real money, before any scale-up. It
is the cheapest test in the entire system and the one people skip.

Each profile carries a `verify_before_live` list. Work through it on your
actual account:

```python
from sentinel.brokers import get_profile
for item in get_profile("amarkets").verify_before_live:
    print("[ ]", item)
```

Every figure in a shipped profile — spread, commission, leverage — is a
**prior**, taken from the conservative end of publicly described terms. Broker
terms change without notice, differ per account tier, and are frequently
described one way in marketing and another in the contract specification.
Nobody should trade real money on the strength of a constant in a Python file,
including these.

---

## Leverage is not a feature

A profile records a venue's maximum leverage because it affects whether an
order is accepted. It is not a recommendation.

At 200:1 (AMarkets) a 0.5% adverse move is 100% of margin. At 1000:1 (some
Alpari tiers) it is 0.1%. The broker's limit is what it will **allow**, not
what is survivable, and the system's own `max_gross_leverage` should be set far
below it. The default is 3×.

---

## Verifying a connection

```bash
python -c "
from sentinel.brokers import build_broker
b = build_broker('amarkets')
inst = b.instruments()
print(f'{len(inst)} instruments')
print('profile mismatches (terminal disagreed with the profile):')
for m in b.profile_mismatches: print('  ', m)
print('min stop EUR_USD:', b.min_stop_distance('EUR_USD'))
print('capabilities:'); [print('  -', d) for d in b.capabilities().degradation_report()]
"
```

Mismatches are normal and are not errors — they are the profile being
corrected by reality, which is the design. What matters is that they are
**visible**, and that the corrected values are what the risk engine uses.

The MT5 adapter is exercised in `tests/test_brokers.py` against a fake terminal
(`tests/fake_mt5.py`) that is deliberately awkward in the ways real terminals
are awkward — suffixed symbols, enforced stop levels, a single supported
filling mode. Before that existed, this entire code path shipped untested,
because the real `MetaTrader5` package is a Windows-only binary.


---

## Configuring a venue from the dashboard

`sentinel/brokers/connection.py` holds the connection records, the read-only
probe and the activation gate. The dashboard page is "بروکر و اتصال".

### Discovery

`discover()` reads what is present on this machine and **proposes** a
configuration; it never applies one. For MetaTrader it attaches to a running
terminal and reads the company name, account number, currency, leverage and
— critically — `ACCOUNT_TRADE_MODE`, which is how the system learns whether the
account is real money without being told. The company name selects a profile
(`profile_for_company`), falling back to `generic_mt5`, which declares nothing
it cannot read from the terminal.

Discovery is owner-only. It attaches to a terminal and reads the signed-in
account number out of it; that is not a viewer's business.

### The probe cannot trade

`probe()` runs against a `ReadOnlyBroker` wrapper whose `submit`,
`close_position`, `modify_position` and `cancel` raise, implemented as an
explicit **allow-list** of forwarded names rather than a deny-list — a deny-list
silently un-blocks whatever is added to the `Broker` interface next.

It is bounded by a real timeout (a worker thread joined with a deadline), and
the HTTP handler is a plain `def` so FastAPI runs it in the threadpool. Both
matter: a venue that accepts the connection and never answers used to hold the
event loop, and with it every other request in the process — including the kill
switch.

**One venue cannot be probed while another is live on the same adapter.** The
MetaTrader5 Python package is one module object per process: `initialize()` with
credentials makes the terminal switch accounts and `shutdown()` ends the session
for everyone. Constructing a second MT5 adapter to "test" a connection therefore
re-points the running engine at the probed account and then disconnects it. The
runtime refuses, with an explanation, rather than reaching the order path one
layer below where `ReadOnlyBroker` sits.

### What the probe checks

| Check | Why it blocks |
|---|---|
| `connect` | nothing else means anything |
| `account` | the venue answered with an account |
| `account_match` | the account reached is the one configured — compared by **normalised equality**, not substring. Brokers issue consecutive numbers, so `50123` "in" `501234` matched the neighbouring account |
| `account_type` | declared demo, venue says **live**: blocked. The reverse only warns |
| `instruments_missing` | a configured symbol this venue does not offer |
| `quote:*` | a live price, and a spread measured against the profile's own typical spread rather than a flat pip count |
| `conversion` | a rate for **every configured** instrument's quote currency. A missing rate is the 166× sizing error |
| `server_stop` | a stop that lives only in our process is not a stop when the link drops |
| `leverage` | measured against the leverage the **risk engine** assumed (1:30), not against the venue's own advertised ceiling |
| `history` | without closed trades the learning loop has nothing to analyse |

### The activation gate

`activation_blockers()` refuses, in plain Persian, when:

* positions are open on a different connection — they belong to the venue
  holding them, and the new adapter reports them as orphans;
* the connection has never been tested, failed its last test, or the test is
  older than 24 hours (venue properties change between sessions) — or is dated
  in the future, which means the clock moved or the file was edited;
* the stored test was run against a different account;
* the account is live and the licence does not permit live trading — this fails
  **closed**: an undetermined licence verdict is refused, not waved through;
* the account is live and no strategy holds an ACCEPTED verdict.

Activation is recorded and **applied at the next start**, not hot-swapped.
Replacing a live broker under a running decision loop changes the reconciler,
the open positions, the idempotency blackout and the symbol table between one
cycle and the next, and there is no ordering of those four that is safe.

Editing a saved connection's identity (login, server, profile, terminal path,
account type) clears both the stored test **and** the activation: losing the
evidence must lose the authority with it.

### Credentials

Sealed with AES-256-GCM in a file separate from the connection records. The
secret's **name** is authenticated as associated data, so a sealed value cannot
be moved from the demo slot to the live one — the bytes would otherwise decrypt
fine and the system would place live orders believing it was on the simulator.

The key comes from `SENTINEL_SECRET_KEY` (preferred: not on disk) or a 0600 key
file. There is no third option; the store refuses to operate without a key
rather than falling back to an obfuscation that looks like encryption.

`protection_note()` states, in the dashboard, what the current arrangement
actually protects against. A key file beside the sealed data protects a stolen
backup and a misdirected copy; it does **not** protect against anyone who can
already read files on the machine.


---

## Bar history: how candles reach the strategies

The strategies read bars from `var/market.db`; the venue adapter fills it.
`Broker.fetch_bars(symbol, timeframe, count)` is the contract, and
`data.feed.MarketFeed` polls it: a full backfill (`data.history_bars`) on the
first cycle, then only the bars that could have closed since. Only *completed*
bars are stored — the bar still forming is never a bar.

| Venue | Source | Notes |
|---|---|---|
| MetaTrader 5 (AMarkets, Alpari, generic) | `copy_rates_from_pos` | The terminal stamps bars in the **broker's server clock** and the Python package presents it as UTC. The adapter measures the offset from a fresh tick (rounded to the half hour) and converts; the profile's `server_utc_offset_hours` is only the fallback for a weekend start. H4 bars therefore open at 01:00/05:00/09:00 UTC on a UTC+3 server — the server's grid, not UTC's. |
| OANDA v20 | `/v3/instruments/{i}/candles` | Mid candles, UTC, with the venue's own `complete` flag. |
| Paper | `data.synthetic_live.SyntheticMarketDriver` | A reproducible synthetic path in wall-clock time, labelled `synthetic`; coarser timeframes are resampled from one base path so every strategy sees one market. |
| CCXT | — | Not implemented; the capability flag says so and the dashboard shows the degradation. |

Each strategy allocation is handed the frame of **its own declared
timeframe**. The feed's primary timeframe (H4) drives regime detection and the
ATR trail; a D1 allocation gets D1 bars beside it, and an allocation whose
timeframe could not be loaded is skipped with a reason rather than handed a
substitute.
