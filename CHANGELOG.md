# Changelog

## 1.8.1 -- 2026-09-25

### Fixed -- the engine did not stay up on Windows

- `deploy/windows/run-engine.ps1` ran the engine as `& $python @argv *>> $log`
  under `$ErrorActionPreference = "Stop"`. The scheduled task runs Windows
  PowerShell 5.1, which turns redirected native stderr into ErrorRecords. The
  engine's first log line on stderr (uvicorn's "Started server process") was
  therefore a terminating error. The consequences:
  - the supervisor exited with code 1 about a second after start, and the
    engine went down with it;
  - the API never answered on port 8088;
  - the log never said why;
  - the dead-man watchdog engaged the kill switch 180 s later.

  This affected every Windows install since the supervisor was introduced.
- The native call now runs under `Continue`. Every output line is appended to
  the log as UTF-8 text, where the redirection wrote UTF-16, and PowerShell
  reads Python's output as UTF-8.
- A regression test pins this.
- After updating, release the kill switch the watchdog engaged, from the
  dashboard (owner + code) or with `Kill-Switch.ps1 -Release`.

### Fixed -- the kill switch could not be released from the dashboard

- The API has always accepted a release (`POST /api/control/kill/release`,
  owner + second factor), and the guides and Diagnose pointed owners to the
  dashboard for it. But no page called it.
- While the switch is engaged, the header now offers «برداشتن توقف اضطراری»,
  and a banner shows who engaged it and why.

### Changed -- first-run help for non-technical owners

- The enrolment file now spells out the authenticator setup key in English
  and Persian under the `otpauth://` URI. "Enter a setup key" in an
  authenticator app wants the key, and a text file cannot be scanned.
- A short Persian Windows guide, `docs/WINDOWS-GUIDE-FA.md`, ships at the top
  of the package as `00-WINDOWS-GUIDE-FA.html`.

## 1.8.0 -- 2026-09-24

### Added -- the US dollar index (`sentinel/data/dxy.py`)

- The index is rebuilt from the broker's own bars with ICE's published formula
  (six components, constant 50.14348112).
  - The agent refreshes the components on its own thread through the feed.
  - A server without USD/SEK gets a flagged proxy (`complete=False`) whose
    returns track the index.
- Causal features on every signal: `dxy_mom` (20-bar move in units of its own
  noise), `dxy_z`, `usd_side` and `dxy_align`.

### Added -- CFTC Commitments of Traders (`sentinel/data/cot.py`)

- Legacy futures-only positioning for EUR, JPY, GBP, CHF, CAD, AUD, NZD and
  MXN, the dollar index, gold and silver.
- Sources:
  - the CFTC Public Reporting API, at a fixed host, with no redirects and a
    size cap, refreshed from the background worker;
  - or the CFTC's yearly zip files, via `scripts/cot_data.py --import`, for
    servers that cannot reach the API.
- `scripts/cot_data.py --selftest` tests the live API from the server.
- No look-ahead: each report is usable only from its Friday release
  (Tuesday + 3 days, 21:00 UTC), live and in the lab alike.
- Positioning index over 156 weeks, and centred features `cot_base`,
  `cot_quote` and `cot_with_trade`.
- A weekly `macro.cot` summary goes to Telegram and Bale.

### Added -- the macro desk (`sentinel/data/macro.py`) and two shrink-only layers

- `cot_crowding` (×0.75): the trade is on the side of a crowded speculative
  position (index ≥ 90 / ≤ 10). The basis is Brunnermeier, Nagel & Pedersen
  (2008) on positioning and crash risk.
- `dxy_headwind` (×0.75): the trade is against a statistically strong dollar
  move (|momentum| ≥ 2).
- Both are recorded in `brain_layers`, so the brain's scorecard measures them,
  and both can be switched off.
- The macro features join every signal's record. The nightly lab joins them to
  its training rows as of each row's own time, so a meta-label filter can learn
  from them out of sample.
- Dashboard page «دلار و COT (بازار کلان)»: the DXY chart and momentum, a COT
  positioning table with crowded zones, and settings.
- API: `GET /api/macro`, plus owner+TOTP `POST /api/macro/settings` and
  `POST /api/macro/cot/refresh`.

### Added -- MetaTrader terminal watchdog (`sentinel/ops/terminal_watchdog.py`)

- It runs at the top of every cycle, on the agent's thread. It detects:
  - a closed or frozen terminal;
  - a lost broker link;
  - the login dialog;
  - a wrong account;
  - Algo Trading switched off.
- The ladder: grace checks, then reconnect. `MetaTrader5.initialize()` starts
  the terminal and signs in with the stored credential, fetched at that moment.
- Retries back off (30 s up to 10 min).
- After repeated failures it ends a frozen terminal at the configured path
  only, locally only, with the path passed in an environment variable.
- A wrong account is only reported in attach mode, and restored in sign-in
  mode.
- It never touches positions. After a recovery the agent reconciles in the
  same cycle.
- Every step is journalled as `ops.mt5_watchdog` and notified, with one message
  key per outage.
- `MT5Broker.health()`, `reconnect()` and `kill_terminal()`, with a
  `credential_fn` from bootstrap. The watchdog reads through the account
  binding.
- Dashboard: a watchdog card on «بروکر و اتصال» and a banner while the
  terminal is down. API: `GET /api/terminal` and owner+TOTP
  `POST /api/terminal/settings`.

### Changed

- Environment checks probe the CFTC API. Backups include `macro.db`.
- Docs: [`docs/MACRO-AND-WATCHDOG.md`](docs/MACRO-AND-WATCHDOG.md); the Persian
  install and Alpari guides cover the watchdog and COT import.
- API version 1.8.0.

## 1.7.0 -- 2026-09-24

### Added -- the brain (`sentinel/brain`), shrink-only learning

- **Shadow book.** Every signal the agent considers is recorded once per
  `(strategy, instrument, side, bar)` in `var/brain.db`. That covers executed,
  vetoed, skipped and proposed signals. Each is later resolved against the bars
  that followed:
  - triple barrier with the stop counted first;
  - a gap fills at the open;
  - cost is charged in R;
  - expiry after three horizons.
- **Veto scorecard and layer attribution.** Each rule is marked
  helped/hurt/unclear/insufficient from the outcomes of what it stopped. Each
  brain layer is credited `-(1-m)*R` on the trades it shrank.
- **Loss-streak cooldowns.** 3 consecutive losses rest the account for 4h, and
  4 rest one strategy for 24h. They are enforced by the risk engine as
  `loss_streak_cooldown`, manual tickets included. The owner can lift one early
  (journalled).
- **CUSUM drift detection.** Page 1954, with k=0.5 and h=4. It measures against
  the lab's baseline and uses hysteresis. On alarm the strategy trades at half
  size (`brain.drift`).
- **Equity-curve filter**, **Bayesian allocation** (normal-normal shrinkage per
  strategy × regime) and **similar-situation memory** (kNN over resolved shadow
  signals).
- **Nightly research lab.**
  - Every enabled strategy is re-tested on the broker's stored bars at 1× and
    2× cost, with block-bootstrap intervals, and labelled
    alive/weak/dead/insufficient.
  - The lab result becomes the drift baseline.
  - Pending `risk.*` proposals are A/B-tested on the same bars.
- **Meta-label candidate.** Trained on the first 60% of the lab window, with
  labels that overlap the holdout purged, and judged on the last 40%.
  - It is offered only with holdout AUC ≥ `meta_min_auc` and positive
    expected value.
  - It is active only after owner approval, and its SHA-256 is verified on
    every load.
- **Weekly self-report** (`brain.report`).
- Dashboard page «مغز ربات (یادگیری)»: cooldowns, per-strategy health with the
  CUSUM gauge, the veto scorecard, models, the lab, weekly reports and settings.
- API: `GET /api/brain` plus owner+TOTP endpoints for settings, the lab run,
  model approve/retire and cooldown clear.

### Added -- gap stress (`sentinel/risk/stress.py`)

- Per-currency worst recorded gaps: CHF 30%, GBP 9%, JPY/AUD 7%, EUR/USD 2%,
  and 3% for any other currency.
- A new entry is shrunk until the whole book's stress loss fits
  `brain.stress_loss_limit_pct` (default 25% of equity) (`stress_shrunk`). It is
  refused only if even the minimum lot cannot fit (`stress_gap`).

### Added -- Telegram and Bale notifications (`sentinel/notify`)

- Driven by the audit journal through the new `AuditLog.add_listener`. Every
  notification corresponds to a recorded event.
- Persian messages in six categories (critical, trades, proposals, learning,
  security, daily).
- Repeats are deduplicated for 10 minutes, sends are rate-limited per channel,
  and bursts of failed sign-ins are reported.
- Fixed hosts: `api.telegram.org` and `tapi.bale.ai`. Redirects are not
  followed, the token is scrubbed from every error, and tokens are encrypted in
  the broker secret store.
- Optional phone commands: `/status` and `/stop` only, and only from the
  configured chat. Commands older than 5 minutes are ignored, and the offset is
  persisted so a restart does not replay them. There is no command that
  releases the kill switch or takes risk.
- Dashboard page «اعلان‌ها (تلگرام و بله)» with a guided setup, including chat
  discovery. `position.open` is now journalled on every fill.

### Added -- MetaTrader 5 onboarding

- `scripts/mt5_check.py` and `deploy/windows/Check-MT5.cmd`: a read-only
  connection check with a Persian report.
  - Covers terminal and Python bitness, login errors, investor vs trading
    password, Algo Trading, demo/real, cent accounts, symbol suffix and a live
    quote.
  - For error -6 it gives an Alpari-specific checklist.
- **Cent accounts.** When no direct `USD→USC` symbol exists, the MT5 adapter
  derives the conversion from the terminal's own
  `tick_value / (tick_size × contract_size)`.
- `docs/ALPARI-MT5-FA.md`, `docs/BRAIN.md`, `docs/RESEARCH-NOTES.md`.
- The environment checks probe the Telegram and Bale APIs.

### Changed

- The orchestrator always computes the signal's feature context once, for the
  meta filter, the similarity memory and the shadow book alike. A filter named
  in the configuration still wins over one approved in the brain.
- API version 1.7.0.

## 1.6.0 -- 2026-09-24

### Added -- an independent reference price (TradingView)

- **Reference-price guard** (`sentinel/data/reference.py`). The broker's mid is
  compared with TradingView's for every instrument the engine trades. Above
  `reference.shrink_bp` (or 2x the broker's spread) a new entry is sized at
  `shrink_multiplier`; above `reference.block_bp` (or 4x the spread) the risk
  engine vetoes it (`reference_divergence`). A missing, delayed, stale or
  closed reference changes nothing. The check runs on the snapshot each entry
  is priced from -- cycle, manual ticket and accepted proposal alike. Blocks
  and their clearing are journalled as `data.divergence`.
- **TradingView client** (`sentinel/data/tradingview.py`): a read-only Python
  port of the websocket protocol documented by the open-source TradingView-API
  project -- quote stream with validated prices, chart history with closed
  bars only, technical ratings, symbol search. Anonymous, no redirects, size
  and time caps, jittered reconnects.
- **Dashboard page** "قیمت مرجع (TradingView)": connection state, broker vs
  reference per instrument with a divergence sparkline and median gap,
  technical ratings (display only), and owner settings with symbol search.
- `scripts/tv_history.py`: research history in the acceptance CSV layout
  (labelled `third-party` by the protocol) and a `--selftest` of the live
  connection from the server.
- Environment checks probe TradingView and the Jev API.
- `Runtime.update_config(..., replace=...)`: a mapping edited as a table can
  now actually lose a row.
- Docs: [`docs/TRADINGVIEW.md`](docs/TRADINGVIEW.md).

Off by default. The interface is unofficial; TradingView's terms restrict
automated access, and the owner decides whether to enable it.

### Changed -- Jev earns its authority

- **Shadow mode by default.** Jev's news classifications are recorded and
  shown but change nothing until the owner promotes it to `shrink_only` (can
  halve size, cannot block) or `active` (can also block). In shadow, a
  configured text model feeds the filter and both opinions are compared.
- **`active` requires evidence** on the answering version: 20 owner-labelled
  headlines, >= 90% decision accuracy on contradictions and >= 85% on
  corrections. The dashboard reports reliability bands, Brier score, skill
  against the base rate and AUC (`sentinel/ai/calibration.py`).
- **Version guard.** The served model version is read from every answer and
  journalled; a change demotes Jev to shadow until the owner accepts the new
  version, whose calibration starts from zero.
- **Fixed: a bare pick was read as certainty.** An answer without a
  probability distribution now uses its stated `confidence`, or is recorded as
  unknown confidence and cannot block.
- **Rate-limit breaker** for every provider: 429/529 back off 1 min -> 1 h with
  no traffic, cleared by the next success.
- API: `GET /api/ai/jev`, `POST /api/ai/jev/mode`, `POST /api/ai/jev/labels`
  (batch, one code), `POST /api/ai/jev/accept-version`.

## 1.5.0 -- 2026-09-24

An end-to-end audit of 1.4.0 followed by the features the owner asked for.
Every defect below has a regression test that fails against 1.4.0.

### Fixed -- capital preservation

- **Accepting a proposal discarded the agent's shrinkage.** The risk engine
  sizes from the full per-trade budget and that size was sent as-is, so a human
  click could place up to 1/caution times the agent's own proposal. Lessons,
  news and the meta-label scale are now re-applied at acceptance, never looser
  than when the proposal was made. The strategy's horizon (time stop) travels
  with the proposal.
- A strategy **suspended by the performance guard** could still trade through a
  queued proposal.
- **The licence was checked once, at boot**: an expiry during a long uptime
  kept authorising live entries. It is now asked every cycle and on every human
  entry path; exits and protection are never gated.
- The **rolling 24 h loss window** covered ~7 h at short decision intervals and
  was forgotten by restarts.
- The **break-even stop** used default costs instead of the configured
  commission and slippage.
- A zero period baseline read the whole account as today's profit.

### Fixed -- correctness and availability

- The Linux installer shipped with **CRLF line endings** and could not run at
  all ("bash\r: No such file or directory"). Line endings are now pinned by
  `.gitattributes` and tested.
- The Linux installer used Python 3.10 on Ubuntu 22.04 (3.11+ is required).
- 48 API handlers were `async` while doing blocking I/O, so one slow broker
  froze the whole API **including the kill switch**.
- `/api/audit` returned the oldest records and re-hashed the whole journal on
  every poll.
- Meta-label diagnostics were erased before reaching the dashboard; trade-id
  trimming kept old ids; manual closes produced spurious `phantom` mismatches;
  malformed `Content-Length` escaped the error handler.
- Paths the process loads or executes (the pickled meta model, the plugin
  directory, data/backup/ledger locations) could be changed from the dashboard.
- The performance-guard suspension could not be seen or released from the
  console.

### Security -- licensing

- The vendor key is **embedded** at release time (`licensegen.py embed-key`);
  the environment can no longer switch licensing off or substitute a key.
- A distributed build **requires** its signed manifest; the manifest covers the
  files that wire the gate in.
- **Online activation** (`activation_url`) is implemented: Ed25519-signed,
  nonce-bound leases; offline tolerance until the lease expires; revocation
  and seat limits; server time as a rollback reference. Reference server:
  `scripts/license_server.py`.
- **Protected builds** (`scripts/build_protected.py`): vendor tools removed,
  keys embedded, protected modules compiled to native code with Cython and
  their sources deleted, manifest signed over the binaries.
- Malformed licences raise `LicenseInvalid` (never a bare exception); licence
  ids are unique.

### Added

- **AI assistants** (`sentinel/ai`): Claude, ChatGPT, Gemini, DeepSeek, Kimi,
  **Jev (TypeSafe AI System One, used for probabilistic news classification)**
  and any OpenAI-compatible endpoint; sealed write-only keys; fallback chain;
  budgets; journalled calls. Used for official-news extraction (shrink/block
  only), the **trade coach** (plain-Persian review of each closed trade), and a
  **daily brief**. See `docs/AI-PROVIDERS.md`.
- **Live news**: confirmed economic calendar and official central-bank feeds;
  blackout windows are now real.
- **Manual trading**: a ticket with a mandatory stop, sized by the risk budget
  and judged by every veto; preview mode; real money off by default
  (`agent.manual_trading_live`).
- **Dashboard**: "معامله دستی" and "هوش مصنوعی و اخبار" pages, guard release,
  meta probability and caution on each decision, licence and guard banners.
- **Windows Server** deployment (`deploy/windows`): installer, supervisor,
  environment check, diagnostics with support bundle, kill switch, backup,
  update with rollback, uninstall. See `docs/WINDOWS-SERVER.md`.
- **Linux** `check-environment.sh`; broader backup coverage;
  `scripts/backup_state.py` (portable backup).

Tests: 961 -> 1086 passing (+2 platform-specific skips).

## 1.4.0 and earlier

See `docs/HANDOFF-1.4.0-FA.md`.
