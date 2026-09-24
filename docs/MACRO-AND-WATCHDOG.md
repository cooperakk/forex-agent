# The dollar index, COT positioning, and the MetaTrader watchdog (1.8.0)

## 1. The US dollar index (`sentinel/data/dxy.py`)

The index is rebuilt from the broker's own bars, using ICE's published
definition:

```
DXY = 50.14348112 × EURUSD^-0.576 × USDJPY^0.136 × GBPUSD^-0.119
                  × USDCAD^0.091  × USDSEK^0.042 × USDCHF^0.036
```

**Why rebuild it.** Computing the index here keeps it on the same clock, the
same bars and the same data-quality checks as every instrument the agent
trades. The agent refreshes the six components on its own thread through the
feed (`macro.dxy_timeframe`, default H1), because MetaTrader tolerates calls
from one thread only.

**Missing components.** Many retail servers do not list USD/SEK. The index is
then rebuilt from the components that exist, with their weights rescaled, and
flagged `complete=False`. Its level is no longer the published number, but its
returns track the published index closely, and returns are all the features
use. Without EUR/USD, or with less than 75% of the weight available, there is
no index at all.

**Features.** Each is computed causally: a value "as of" a time uses only bars
that had closed by then.

| Feature | Meaning |
|---|---|
| `dxy_mom` | 20-bar log return divided by (1-bar volatility × √20); ±2 is a statistically strong move |
| `dxy_z` | distance from the 50-bar mean, in standard deviations |
| `usd_side` | +1 if the trade is long USD, −1 if short, 0 if USD is not a leg |
| `dxy_align` | `usd_side × dxy_mom`; negative means the trade is against the dollar's move |

## 2. CFTC Commitments of Traders (`sentinel/data/cot.py`)

**Source.** The CFTC's legacy futures-only report for the CME currency
futures, the ICE dollar index and COMEX gold and silver. It comes from the
Public Reporting API (`publicreporting.cftc.gov`, dataset `6dca-aqww`) at a
fixed host, with no redirects and a size cap. A server that cannot reach the
API can instead import the CFTC's yearly zip files:

```
python scripts/cot_data.py --selftest
python scripts/cot_data.py --import deacot2025.zip --state var
python scripts/cot_data.py --show --state var
```

**No look-ahead.** A report describes Tuesday but is public on Friday
afternoon. Each report carries `available_ns` = Tuesday + 3 days at 21:00 UTC,
and every query uses only reports available at the query time. That applies
in the live agent and in the lab's training data alike.

**Positioning index.** `(net − min) / (max − min) × 100` over the last
`cot_lookback_weeks` (default 156) weekly reports. At least `cot_min_weeks`
(52) reports are needed before the index has a value.

**What it is used for, and why.** Speculators' net position moves with the
exchange rate more reliably than it predicts it (Klitgaard & Weir 2004), so
the index is **not** used for direction. Brunnermeier, Nagel & Pedersen (2008)
find that speculators' net futures positions predict currency **crash risk**.
The index is therefore used for caution only.

| Feature | Meaning |
|---|---|
| `cot_base`, `cot_quote` | index of each leg, centred: −1 = most short, +1 = most long |
| `cot_with_trade` | + means speculators lean the same way as the trade |

## 3. The two shrink-only layers

| Layer | When | Default multiplier |
|---|---|---|
| `cot_crowding` | the trade is on the side of a crowded leg: index ≥ `cot_extreme` (90) for the side bought, or ≤ 10 for the side sold | 0.75 |
| `dxy_headwind` | `usd_side × dxy_mom ≤ −dxy_headwind_score` (2.0) | 0.75 |

Both layers join the caution product. They are recorded in the decision's
`brain_layers`, so the brain's scorecard credits or charges them like any
other layer. Both can be switched off in `config.macro`, or on the dashboard
page «دلار و COT».

The features enter every signal's record:

* **Similar-situation memory:** it can use them at once.
* **Nightly lab's meta-label candidate:** training rows get the same features
  as of each row's own time (`ResearchLab._join_macro`). A filter that learns
  from them is still offered only after passing its out-of-sample gate, and is
  used only once the owner approves it.

The `macro` section sits outside the verdict's runtime policy, for the same
reason as the brain: it can never enlarge a position.

## 4. The MetaTrader terminal watchdog (`sentinel/ops/terminal_watchdog.py`)

The watchdog runs at the top of every agent cycle, on the agent's thread. It
reads the terminal's own state:

| State | Meaning |
|---|---|
| `ok` | connected, signed in, right account |
| `terminal_down` | not running, or not answering IPC |
| `broker_disconnected` | running, but with no link to the trade server |
| `not_logged_in` | at the login dialog |
| `wrong_account` | signed in to another account |

**The ladder.**

1. **Grace:** `grace_checks` unhealthy readings in a row before acting.
2. **Reconnect:** shut the session down, then call `MetaTrader5.initialize()`.
   That call starts the terminal if it is not running, and with a stored
   credential it signs in to this service's account. The password is fetched
   from the encrypted store at that moment and never held on the adapter.
3. **Back-off:** 30 s, 60 s, 120 s … up to `backoff_max_sec`.
4. **End a frozen terminal:** after `kill_hung_after_failures` failed attempts
   on a terminal that does not answer at all, the process is ended so the next
   attempt starts it clean. Only the process whose executable is exactly the
   configured `terminal_path` is ended, and only on a local Windows host (never
   over the bridge). The path is passed to PowerShell in an environment
   variable, never in the command text.

**Wrong account.**

* **Attach mode** (no stored credential): the terminal is shared with a
  person, so the watchdog only alerts. The account binding already refuses
  every order.
* **Sign-in mode** with `restore_account`: the watchdog signs the terminal
  back in to this service's account.

**Algo Trading.** Algo Trading switched off in the terminal is reported once.
It cannot be switched on by software.

**What it never does.** It never opens, modifies or closes a position.

* Open positions keep their stop-loss at the broker throughout.
* New entries stay blocked by the connectivity veto while the account cannot
  be read.
* After a recovery the agent reconciles its book with the broker in the same
  cycle.

**Journal and notifications.** Every step is journalled as `ops.mt5_watchdog`
and sent to Telegram and Bale. Each outage has its own message key, so a
second outage is never mistaken for a repeat of the first.

| Method | Path | Who |
|---|---|---|
| GET | `/api/terminal` | any signed-in user |
| POST | `/api/terminal/settings` `{patch}` | owner + TOTP |
| GET | `/api/macro` | any signed-in user |
| POST | `/api/macro/settings` `{patch}` | owner + TOTP |
| POST | `/api/macro/cot/refresh` | owner + TOTP |

## References

* ICE Futures U.S., *U.S. Dollar Index contract specifications* (the index
  formula and weights).
* Klitgaard, T. & Weir, L. (2004). *Exchange rate changes and net positions of
  speculators in the futures market.* FRBNY Economic Policy Review 10(1).
* Brunnermeier, M., Nagel, S. & Pedersen, L. (2008). *Carry trades and currency
  crashes.* NBER Macroeconomics Annual 23, 313–347.
* Lustig, H., Roussanov, N. & Verdelhan, A. (2014). *Countercyclical currency
  risk premia.* JFE 111(3), 527–553 (the dollar factor).
* CFTC, *Commitments of Traders: explanatory notes* (report timing and trader
  categories).
