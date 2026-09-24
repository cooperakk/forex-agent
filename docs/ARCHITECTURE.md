# Sentinel-FX — Architecture

> This document explains *why* the system is shaped the way it is. The
> shape is a direct consequence of four facts established in the research
> brief, and almost every design decision below traces back to one of them.

## The four facts the design answers to

**1. Cost is the dominant term, not the signal.** With a round-trip cost of
`c` pips, a take-profit of `T` and a stop of `S`, the win rate needed just to
break even is

```
p = (S + c) / (T + S)
```

At `T = S = 3` pips and `c = 1.0`, that is **66.7 %**. No credible edge in
liquid FX survives that. The entire scalping family is closed by arithmetic
before a single line of strategy code is written, which is why this system
trades H4/D1 structure and refuses, in code, to enter a trade whose break-even
win rate exceeds 60 % (`L0.1`, and the `cost_barrier` veto at runtime).

**2. Most of what looks like edge is selection.** Run 200 variants over the
same history and the best one will look excellent whether or not any of them
work. So no strategy may trade real money until it has survived a fixed,
pre-declared battery — PBO, Deflated Sharpe, Clark-West, Hansen SPA, CPCV —
and the badge that says it survived is issued by a *separate registry*, not by
the configuration file. See `ACCEPTANCE-PROTOCOL.md`.

**3. The dangerous failures are operational, not statistical.** A duplicated
order, a stop that was never placed at the venue, a position the system
believes it closed, a restart that forgets it was in drawdown. These lose real
money on day one, whereas a mediocre edge merely loses slowly. So the
execution and risk layers get the same paranoia budget as the research layer.

**4. Nothing here is proof of profitability.** The honest expected outcome is
a rigorous, well-instrumented piece of financial-systems engineering that
tells you the truth about whether it has an edge — and the truthful answer
will usually be "not yet". The system is built so that this answer is
*visible* rather than hidden. It ships refusing to promote its own demo
strategy.

---

## Layer map

```
                       ┌──────────────────────────────┐
                       │  Dashboard (React, RTL)      │
                       │  read-only by default        │
                       └──────────────┬───────────────┘
                                      │ HTTPS + JWT + TOTP-per-write
                       ┌──────────────▼───────────────┐
                       │  API  (FastAPI)              │
                       │  viewer / operator / owner   │
                       └──────────────┬───────────────┘
                                      │
  ┌───────────────────────────────────▼──────────────────────────────────┐
  │                          Agent  (decision loop)                      │
  │  kill → health → reconcile → snapshot → regime → news → risk ctx →   │
  │  manage positions → signals → filter → RISK → act → learn            │
  └───┬────────────┬──────────────┬─────────────┬──────────────┬─────────┘
      │            │              │             │              │
 ┌────▼─────┐ ┌────▼──────┐ ┌─────▼──────┐ ┌────▼─────┐ ┌──────▼───────┐
 │ Strategy │ │   Risk    │ │ Execution  │ │  News    │ │   Learning   │
 │ library  │ │  engine   │ │  OMS +     │ │  policy  │ │  postmortem  │
 │ + meta   │ │ (vetoes)  │ │  reconcile │ │  + LAP   │ │  + proposals │
 └────┬─────┘ └────┬──────┘ └─────┬──────┘ └────┬─────┘ └──────┬───────┘
      │            │              │             │              │
  ┌───▼────────────▼──────────────▼─────────────▼──────────────▼───────┐
  │  Core: Decimal money · deterministic ids · clock · config · AUDIT  │
  └───────────────────────────┬────────────────────────────────────────┘
                              │
              ┌───────────────▼────────────────┐
              │  Broker adapter (abstract)     │
              │  paper · OANDA · MT5 · CCXT    │
              └────────────────────────────────┘
```

---

## `sentinel/core` — the parts everything else stands on

### `money.py` — Decimal or nothing

Every price, pip, lot and currency amount is a `Decimal`. Floats are not
merely discouraged; `D()` converts through `repr()` so that a value that
entered as a float cannot silently acquire binary-rounding noise, and
non-finite values raise rather than propagate.

> A subtle trap worth naming: `numpy.float64` **is** a subclass of `float`,
> and in NumPy 2.x its `repr()` is `"np.float64(1.085)"`. Passing one straight
> to `Decimal` raises. `D()` normalises through `float()` first. This is the
> kind of bug that shows up once, in production, on a JPY pair.

The module also owns the cost arithmetic that gates the whole system:
`break_even_win_rate`, `annual_cost_pct_of_equity`, `min_equity_for_granularity`.
The tests assert these reproduce the brief's tables exactly.

### `ids.py` — an idempotency key that survives a crash

```python
client_order_id(strategy, instrument, side, decision_ns, seq, account, attempt)
```

Every input is a property of **the decision**, never of the process that made
it. `decision_ns` is the timestamp of the *bar*, not `now()`. There is
deliberately no run-id and no wall clock in the digest, because the failure
this defends against is precisely: submit → crash before the response is
read → restart → re-derive the key. If the key changed across that boundary,
the venue would accept a second, duplicate order. It does not change.

### `audit.py` — a tamper-evident journal

Append-only JSONL. Each record carries `prev_hash` and its own SHA-256 over a
canonical serialisation, so the file is a hash chain. `verify()` returns
`(ok, first_bad_seq, message)` — it tells you *where* the chain broke, which is
the only useful form of that answer. The file is created `0o600`. A torn final
line (power loss mid-write) is moved aside rather than parsed optimistically.

The audit log is the system's memory of *why*, and it is deliberately not a
database: an append-only text file is the format most likely to still be
readable, and verifiable, years later.

### `config.py` — a configuration that argues back

A strict pydantic surface where the cross-validators refuse whole classes of
incoherent state rather than trusting the operator:

- a strategy marked `accepted` while `agent.mode` is live, without a matching
  verdict → refused
- a non-monotone drawdown ladder → refused
- loss budgets ordered so the weekly limit is tighter than the daily → refused
- `max_total_open_risk_pct` below `risk_per_trade × max_open_positions` → refused
  (a ceiling you can breach by following your own rules is not a ceiling)

Defaults are conservative on purpose: 0.50 % risk per trade, 2.00 % total open
risk, 4 concurrent positions, advisory mode, paper venue, loopback binding.

---

## `sentinel/risk` — the engine that says no

The risk engine is **independent of the strategy layer by construction**: it
receives a proposed order and a `RiskContext` and returns a list of named
`Veto`s. It never asks the strategy what it thinks. A strategy cannot
suppress a veto, and there is no override flag.

Thirty-plus named vetoes, grouped:

| Group | Vetoes |
|---|---|
| State | `halted`, `kill_switch`, `ladder_halt`, `lifecycle`, `unresolved_orders` |
| Data | `no_price`, `stale_data`, `data_quality`, `clock_skew`, `unknown_instrument` |
| Cost | `cost_barrier`, `spread` |
| Geometry | `no_stop`, `stop_side`, `stop_too_tight`, `stop_too_wide`, `reward_risk`, `sizing` |
| Exposure | `per_instrument`, `max_positions`, `total_open_risk`, `leverage`, `margin_level`, `exposure_unconvertible` |
| Loss budget | `daily_loss`, `weekly_loss`, `monthly_loss`, `max_drawdown`, `profit_lock` |
| Frequency | `frequency_day`, `frequency_week`, `frequency_year`, `entry_spacing` |
| External | `news_blackout`, `connectivity`, `portfolio_alarm`, `unprotected_book` |
| Conversion | `missing_conversion` |

Four of these deserve explanation.

**`missing_conversion`.** When the account is denominated in a currency that
differs from the instrument's quote currency, position size depends on an FX
rate. If that rate is unknown, the arithmetically "convenient" thing is to
substitute 1.0. On a JPY account that is a **166×** sizing error. So
`RiskContext.conversion()` returns `Optional[Decimal]` and `None` is a veto.
Returning `None` rather than `1.0` is the entire point of the method.

**`frequency_*`.** Trade frequency is treated as a first-class risk limit, not
a by-product. Cost scales linearly with turnover (fact 1), so an unbounded
frequency is an unbounded cost, and a strategy that "found more setups" after
a parameter change has usually found more noise.

**`total_open_risk`.** Per-trade risk and position count are not enough: four
positions at 0.5 % each is 2 % only if they are independent. The engine nets
exposure by **currency leg** (long EUR/USD + long EUR/JPY is one EUR bet, not
two) and clusters by realised correlation using single-linkage over the
cycle's own returns, then applies `max_correlated_risk_pct` to the cluster.

**`unprotected_book`.** If any open position is found without a venue-side
stop, no new entry is permitted until that is resolved. The system will not
add risk to a book it cannot prove is protected.

### The drawdown ladder

Risk per trade is multiplied by `ladder × regime`. The ladder steps size down
as drawdown from the equity peak deepens and must be monotone (validated).
Crucially **the equity peak and the period baselines are persisted** to
`var/agent_state.json`: a restart that forgot it was 8 % down would resume at
full size, which is exactly when full size is least appropriate.

---

## `sentinel/execution` — the order lifecycle

The state machine has five terminal-ish states and one that is not:
`acked`, `filled`, `partial`, `rejected`, `cancelled`, and **`unknown`**.

`unknown` is first-class. It means "we sent it and do not know what happened",
and it is resolved **only** by querying the venue — never by inference, never
by a timeout deciding it probably failed. While any order is `unknown`, the
`unresolved_orders` veto blocks new entries (configurable via
`quarantine_on_unknown_state`, which defaults to on).

The reconciler runs every 30 s and on startup, and compares the venue's truth
to local belief across positions, stops and orders. On a mismatch it logs
`RECONCILE_MISMATCH` and adopts the venue's view — the venue is the
authority; the local view is a cache.

On startup, the agent replays unterminated orders found in the audit journal
and queries each one, before doing anything else.

---

## `sentinel/agent` — the decision loop

One cycle, in order:

1. **kill switch** — an out-of-band file. Present ⇒ no new risk, full stop.
2. **health** — clock drift, feed freshness, broker reachability.
3. **reconcile** — venue truth wins.
4. **snapshot** — account, positions, bars.
5. **regime** — volatility bucket (expanding quantiles, so no lookahead) ×
   cross-pair correlation state, producing a risk multiplier.
6. **news** — blackout windows around high-impact events.
7. **risk context** — built once *and rebuilt after every order that reaches
   the venue*. Building it once per cycle made the position cap, the entry
   spacing and the daily trade cap all bypassable inside a single millisecond;
   the refresh is keyed on the venue order state, not the display label.
8. **manage positions** — trailing, scale-outs, time-based exits. Weekend-flat
   is evaluated *before* the conversion guard, because "I cannot price this"
   must not prevent an exit.
9. **signals → filter → RISK → act**.
10. **learn** — postmortems on closed trades.

Position metadata is keyed on `(instrument, side)` and re-hydrated on restart
with a **halt on mismatch**: if the venue's side disagrees with what the agent
remembers, that is not something to reconcile silently.

### Learning

`memory.py` stores closed trades; `postmortem.py` labels each against what the
risk context was at entry; `proposals.py` queues parameter changes. The
learning loop **cannot apply a change to live trading by itself**
(`proposal_requires_human` defaults true, and any change to an accepted
strategy's parameters invalidates its verdict fingerprint — see below). A
system that retunes itself into a drawdown at 3 a.m. is not intelligent.

`ClosedTrade.regime` is stamped at **entry** time, not at processing time.
Attributing a loss to the regime that happened to be current when the trade
closed would teach the system the wrong lesson.

Four rules make the loop's arithmetic honest rather than merely careful:

1. **Counterfactuals use only the price path that occurred.** "What if the stop
   had been 50% wider" needs prices from *after* the position was closed, which
   were never recorded, so it is marked not-computable and excluded — not
   filled in. Everything that depends on when a level was touched needs the
   bars, and says so when it does not have them.
2. **A counterfactual is scored on every trade the rule would have fired on**,
   including the ones where it loses money. Scoring only the trades it helps
   makes the effect positive by construction, and the significance test on that
   population cannot fail. On pure random-walk data the previous version
   returned p between 1e-26 and 1e-152 for all four alternative rules.
3. **One false-discovery correction across the whole family** tested in a pass.
   Screening sixteen patterns against noise turns up a raw p < 0.05 about 58%
   of the time; after Benjamini–Hochberg, about 4%.
4. **Three explanations, not one.** A surviving pattern is diagnosed as a wrong
   `parameter`, a changed `regime`, or `luck` — in ascending order of how often
   it is actually true. Only the first may become a parameter proposal; a
   regime-confined effect becomes a lesson scoped to that regime.

**Lessons expire.** Each carries the time fresh evidence last confirmed it, its
influence decays toward 1.0 on a half-life between confirmations, and two
contradictions — or one sign reversal — retire it. A lesson with no expiry is a
permanent bias: learned in one market, applied in every market afterwards, with
nothing in the system able to notice it stopped being true. Decay can only ever
*relax* a lesson, because a caution multiplier is capped at 1.0 and there is
nothing above it to decay to.

### News

`calendar.py` stores events, `schedule.py` ingests them, `classify.py` tiers
them, `policy.py` decides what they are allowed to do.

- Release times are stored as **local wall time plus a zone**, never a fixed UTC
  hour. Payrolls is 08:30 New York and the ECB decision is 14:15 Frankfurt; both
  move in UTC twice a year, on dates three weeks apart. `core/tzrules.py`
  implements the US and EU rules arithmetically so no `tzdata` package is
  needed, and is verified against `zoneinfo` in the test suite where it exists.
- Events are keyed on `(series_id, period)` — the reference period, not the
  release date — so a **rescheduled release updates one row** instead of opening
  a second blackout window beside the first.
- Every event carries a `certainty`. Only `confirmed` and `scheduled_pattern`
  dates create blackouts. An `approximate` date is an advisory that shrinks size
  but never blocks: blocking on a guess sits out the wrong day *and clears the
  right one*, so the agent trades into the print believing it is protected.
- A **surprise** is `actual − consensus`, standardised by the median absolute
  deviation of that series' own past surprises — computed only from releases
  published before the decision, with the event excluded from its own
  denominator.
- The bundled schedule works offline. It is patterns, not confirmed dates; a
  live feed wires in through `LiveCalendarSource` without touching this module.

---

## `sentinel/research` — the laboratory

- `labeling.py` — triple-barrier labels, average uniqueness, effective sample size
- `cv.py` — purging + embargo, Combinatorial Purged CV
- `stats.py` — PBO via CSCV, Deflated Sharpe, MinTRL, Clark-West, Hansen SPA,
  White's Reality Check, Benjamini–Hochberg
- `factors.py` — dollar / carry / momentum attribution with Newey–West HAC errors
- `metrics.py` — performance statistics
- `verdicts.py` — **the registry**

> A degenerate-input guard worth knowing about: the standard deviation of a
> perfectly flat equity curve is not 0 in floating point, it is ~2e-19, which
> yields a Sharpe ratio of 7.3e16. `_degenerate()` catches this with a relative
> epsilon in both `stats.py` and `metrics.py`.

### The verdict registry is the root of trading authority

A configuration file can *claim* a strategy is accepted. The registry decides
whether that claim is true. `config_fingerprint(instruments, params, timeframe)`
binds a verdict to the exact configuration that earned it, so changing a
parameter silently revokes acceptance.

`enforce_config_authority()` runs at **both** `bootstrap.build_runtime` and
`Runtime.start`, repairs unbacked badges **downward**, bumps the config
version (one version number must not denote two configurations), and records
`updated_by="startup-authority"`.

A registry that cannot be read raises `RegistryUnreadable` and the process
**refuses to start**. It does not treat "unreadable" as "empty", because the
repair for empty is irreversible: it would demote every accepted strategy and
persist that demotion, destroying the acceptance history.

---

## `sentinel/brokers` — one interface, four venues

`base.py` defines the contract: instrument specs, prices, account, positions,
`submit`, `query_order`, `modify_position`, `close_position`,
`fetch_closed_trades`. Errors are classified `TransientError` (retry) vs
`PermanentError` (do not).

- **`paper.py`** — an *adversarial* simulator, not an optimistic one. Intrabar
  path is direction-aware (a long visits the low first, a short the high), so
  an ambiguous bar is scored against you rather than for you. Spreads widen by
  session; slippage, partial fills, rejects and requotes are modelled;
  financing accrues. Costs are consumed proportionally on partial closes so a
  scale-out cannot double-charge commission.
- **`oanda.py`** — the reference live adapter. `positions()` reads
  `/openTrades` and attaches the **tightest** stop, because a reconciler that
  cannot see stops will "helpfully" widen them every 30 seconds.
  `modify_position` raises `PermanentError` on any widening.
- **`mt5.py`**, **`ccxt_adapter.py`** — same contract, different venues.

---

## `sentinel/api` + `dashboard` — the control surface

See `SECURITY.md` for the authentication model. Architecturally the important
property is that the API is **read-only by default**: every mutating endpoint
requires `require_write` plus a fresh TOTP in `X-TOTP`, and the five that can
change what the system is allowed to do — mode, kill/release, resume, config,
proposal review — additionally require the `owner` role.

The dashboard is a Vite + React + TypeScript SPA with a hand-written SVG chart
kit (no charting dependency), Persian RTL layout, the Doran typeface, and
light/dark themes derived from the refero monochrome reference. Its demo data
is deliberately unflattering — Sharpe below 1, a real drawdown, a **failing**
acceptance verdict — because a dashboard that only knows how to render success
teaches you nothing.

---

## What is deliberately absent

- No "confidence score" that is not a calibrated probability.
- No auto-apply of learned parameters to live trading.
- No backtest result presented without its overfitting probability.
- No override flag on the risk engine.
- No promise about returns anywhere in the codebase or the UI.
