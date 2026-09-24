# TradingView: an independent reference price, ratings and research history

Sentinel-FX can read market data from TradingView. It is **off by default**,
it never trades on TradingView prices, and when it is unavailable the engine
behaves exactly as if it did not exist.

## What it is used for

| Use | Effect on trading | Where |
|---|---|---|
| **Reference-price check**: compare the broker's mid with TradingView's for every instrument the engine trades | Can only **shrink** (default x0.5) or **block** a NEW entry. Never opens, enlarges, closes or modifies a position. Exits and protection are never gated. | `sentinel/data/reference.py`, risk veto `reference_divergence` |
| **Technical ratings** (TradingView's "Recommend.All / MA / Other" summaries, 15m to 1W) | **None.** Shown on the dashboard only. | dashboard -> قیمت مرجع (TradingView) |
| **Historical bars** for research | **None** at runtime. A dataset built from them is labelled `third-party` by the acceptance protocol and can never promote a strategy on its own. | `scripts/tv_history.py` |

### Why a second price source

Every risk number starts from the broker's bid/ask: the stop distance, the
size, the spread veto, the reward-to-risk ratio. The feed already refuses a
quote that is *old*. Nothing refused a quote that is *fresh and wrong*:

* a feed that stopped moving while its timestamp kept advancing;
* a bad tick;
* a symbol mapped to a different contract (a futures-based CFD, a mini lot
  series, a renamed symbol after a broker migration);
* a server that lost its upstream and keeps publishing its last price.

Each is invisible from inside one feed and obvious next to an independent one.
Two independent FX feeds normally agree to well under a pip.

### Why the ratings are not a signal

A summary of 26 textbook indicators has no demonstrated edge, and the
acceptance protocol is the only door through which a signal reaches money. The
ratings are context for the owner. If you believe they carry information, test
that belief the way every other idea is tested: as a strategy, through
`scripts/run_acceptance.py`.

## The check, precisely

Divergence is measured in **basis points of price** (1 bp = 0.01%), so one
setting works for EUR/USD, USD/JPY and gold. It is never tighter than a
multiple of the broker's own spread, so a wide-spread exotic is not flagged
for being an exotic:

```
shrink when |broker_mid - reference_mid| > max(shrink_bp, 2 x broker_spread_bp)
block  when |broker_mid - reference_mid| > max(block_bp,  4 x broker_spread_bp)
```

Defaults: `shrink_bp = 5`, `block_bp = 12`, `shrink_multiplier = 0.5`. For
EUR/USD near 1.10 that is about 5.5 and 13 pips.

The check runs on the **same quotes the entry is priced from**: the decision
cycle, a manual ticket and an accepted proposal each re-assess on their own
snapshot.

### When the reference is absent, nothing happens

| Status | Meaning | Effect |
|---|---|---|
| `ok` | the prices agree | none |
| `shrink` | above the shrink threshold | size x `shrink_multiplier` |
| `block` | above the block threshold | the entry is vetoed |
| `stale` | the reference price has not moved for `max_age_sec` (90 s) | none |
| `delayed` | TradingView marks the symbol as delayed (`update_mode`) | none |
| `closed` | the reference session is not `market` (weekend) | none |
| `unavailable` | no connection, no quote yet, or a symbol error | none |
| `unmapped` | no valid TradingView symbol for the instrument | none |
| `no_broker_quote` | no usable broker quote (the risk engine already vetoes) | none |

The source is unofficial and can disappear at any time. Letting its absence
stop trading would hand the account's uptime to a website's protocol, and its
absence says nothing about the broker's price.

A block and its clearing are journalled in the audit chain as
`data.divergence`.

### Reading a persistent gap

The dashboard shows the **median signed gap** per instrument. A steady
non-zero value (for example gold on a futures-based CFD) is a basis, not a
fault: map a better reference symbol in `reference.symbol_map` rather than
widening the thresholds until they stop meaning anything.

## Configuration

`config.json`, section `reference` (dashboard: owner only, second factor
required, every change journalled):

```json
"reference": {
  "enabled": false,
  "provider": "tradingview",
  "exchange": "OANDA",
  "symbol_map": {"XAU_USD": "OANDA:XAUUSD"},
  "shrink_bp": 5.0,
  "block_bp": 12.0,
  "spread_multiple_shrink": 2.0,
  "spread_multiple_block": 4.0,
  "shrink_multiplier": 0.5,
  "max_age_sec": 90,
  "ta_ratings": true,
  "ta_every_min": 15
}
```

* Instruments without an explicit mapping use `exchange`:
  `EUR_USD -> OANDA:EURUSD`. `OANDA` and `FX_IDC` are free and real-time for FX.
* The stream follows every instrument an enabled strategy trades, every
  instrument with an open position, and every instrument in `symbol_map`
  (at most 40).
* The section is deliberately **outside the verdict's runtime policy**: like
  the kill switch, it acts on a data fault that no validated strategy depends
  on, so switching it on does not invalidate an acceptance verdict.

## Operating it

* **Test from the server first:**
  `python scripts/tv_history.py --selftest` (Windows:
  `.venv\Scripts\python.exe scripts\tv_history.py --selftest`). It opens the
  websocket, reads two live quotes, fetches a few bars and the ratings, and
  names the part that failed. The environment checks
  (`deploy/scripts/check-environment.sh`, `deploy\windows\Check-Environment.ps1`)
  also probe the hosts.
* **Network:** outbound HTTPS/WSS to `data.tradingview.com` (the websocket)
  and `scanner.tradingview.com` (ratings); `symbol-search.tradingview.com` for
  the dashboard's symbol search. Proxies from `HTTPS_PROXY` are honoured.
* **Research history:**
  `python scripts/tv_history.py --instruments EUR_USD,GBP_USD --timeframe H4 --count 5000 --out data/tradingview/H4`
  writes one CSV per instrument in the layout `scripts/run_acceptance.py`
  reads. Bars still forming are dropped. Daily bars follow TradingView's
  session boundary, which may differ from the broker's.

## Security properties

* **No account.** The connection uses the protocol's anonymous token. Signing
  in would put a TradingView password or session cookie on a trading server and
  put that account at risk of suspension, for data that is already free for FX.
* Symbols are validated against `^[A-Z0-9_]{1,24}:[A-Z0-9._!&-]{1,40}$` before
  they are sent or stored; the configuration refuses anything else.
* Inbound frames are capped at 2 MB, parsed as JSON only, and every price is
  checked finite, positive and uncrossed before it is stored (a crossed book
  drops both sides). A malformed packet is counted and dropped, never fatal.
* HTTPS requests follow **no redirects** and are size- (1 MB) and time-capped.
* The stream runs on its own daemon thread with jittered exponential
  reconnects (5 s to 5 min) and a silence timeout; the decision thread only
  reads the latest quote from memory.
* The dashboard's symbol search is owner-only and rate-limited (10 a minute).

## Caveats the owner must accept

* The interface is **unofficial and reverse-engineered** (the packet shapes
  follow the open-source project
  [TradingView-API](https://github.com/Mathieu2301/TradingView-API), ISC
  licence; no code is copied). TradingView's terms of use restrict automated
  access to its data. Using it is the owner's decision; that is why it ships
  switched off.
* It can break without notice when TradingView changes its protocol. The
  design makes that a loss of a safety net, never a loss of trading.
* TradingView's prices come from its data providers, not from your broker. A
  small, steady gap between them is normal and is what the thresholds absorb.

## Protocol notes (for maintainers)

* Frames: `~m~<length>~m~<body>`; heartbeats `~h~<n>` must be echoed.
  Bodies are sent as ASCII-only JSON so the length prefix is unambiguous.
* Quote session: `set_auth_token ["unauthorized_user_token"]`,
  `quote_create_session [qs]`, `quote_set_fields [qs, ...fields]`,
  `quote_add_symbols [qs, '={"session":"regular","symbol":"OANDA:EURUSD"}']`;
  updates arrive as `qsd [qs, {n, s, v}]`.
* Chart session: `chart_create_session [cs]`, `resolve_symbol [cs, "ser_1", ...]`,
  `create_series [cs, "$prices", "s1", "ser_1", resolution, count]`, bars in
  `timescale_update`/`du` under `$prices.s[].v = [time, o, h, l, c, v]`, each
  round closed by `series_completed`; `request_more_data` for older bars.
* Ratings: `POST https://scanner.tradingview.com/global/scan` with
  `Recommend.All|<tf>` columns (the daily column has no suffix).
