# The brain (`sentinel/brain`)

The brain is the agent's adaptive layer. It learns from **every signal the
agent considers**, not only from the trades it takes, and it uses what it
learns in exactly one direction: **more caution**. It can shrink a position,
rest the account after a losing streak, or refuse an entry. It can never
enlarge a position beyond what the risk engine already allows, and it can never
change a risk limit. That is why `brain` sits outside the verdict's
`runtime_policy`: with the brain on, a validated strategy trades the same
signals with equal or less exposure.

Every layer measures itself. The shadow book records what each layer did, and
the scorecard shows whether that helped **this** account. Any layer can be
switched off from the dashboard ("مغز ربات") or in `config.brain`.

```
signal ──► meta-label filter ──► similarity ──► strategy layers ──► risk engine ──► venue
  │          (owner-approved)     (kNN)          drift / equity /     cooldown veto
  │                                              allocation           gap-stress shrink/veto
  └──────────────────────────► shadow book ◄── bars that follow ──► scorecard, lab, reports
```

## 1. The shadow book

`BrainStore` (SQLite `var/brain.db`, mode 0600) holds one row per
`(strategy, instrument, side, decision bar)`. Each row stores the action
(executed / vetoed / skipped / proposed), the first vetoing rule, the size
multiplier and the per-layer multipliers, the meta-label probability, the
feature vector, the regime and the round-trip cost in R. If the same signal is
executed later, the row is upgraded to `executed`.

`Brain.resolve` scores open rows against the bars that **followed** the signal
bar, using the backtester's own rules (`shadow.resolve_path`):

* the path starts at the first bar after the signal bar;
* if one bar touches both barriers, the **stop counts first** (adverse-first);
* a bar that **opens** beyond the stop fills at the open (a gap is worse than 1R);
* cost is charged in R;
* at the horizon, the trade is marked at the last close;
* a signal whose data never arrives expires after three horizons.

This is a triple-barrier label (López de Prado 2018, ch. 3). It is honest
because the prices exist whether or not the agent traded.

## 2. The scorecard

`shadow.scorecard(rows)`:

* **per rule:** the outcomes of the signals each rule stopped. A rule
  **helped** when the 95% interval of those outcomes lies below zero, and
  **hurt** when it lies above zero. With fewer than 15 observations the answer
  is `insufficient`, never a guess.
* **per layer:** a layer that shrank a taken trade by multiplier *m* is
  credited `-(1 - m) * R`. That is positive when the trade lost (the shrink
  saved money) and negative when it won.
* **per strategy:** taken vs not-taken outcomes.

## 3. Loss-streak cooldowns

After `loss_streak_limit` consecutive losses (default 3) the whole account
rests for `loss_streak_cooldown_hours` (default 4). After
`strategy_loss_streak_limit` (default 4) one strategy rests for
`strategy_cooldown_hours` (default 24). A win resets the streak.

The risk engine vetoes with `loss_streak_cooldown`, and that includes manual
tickets. The rationale is behavioural. Traders measurably increase risk after
losses (Coval & Shumway 2005, CBOT proprietary traders), and "win it back" is
the most common path from a bad day to a blown account. The owner can lift a
rest early; that action is journalled.

## 4. Drift: one-sided CUSUM

For each strategy, live trade R is compared with a baseline:

* the mean and standard deviation measured by the nightly lab on the broker's
  own bars; or
* `drift_expected_r` with a standard deviation of 1 until the lab has measured
  one.

The statistic is Page's (1954) CUSUM:

```
S_t = max(0, S_{t-1} + (mu0 - r_t) / sigma - k),   alarm when S_t > h
```

With `k = 0.5` and `h = 4`:

* the in-control average run length is about 170 trades;
* a one-sigma drop in mean R is detected in about eight trades.

The alarm uses hysteresis. It is raised above `h` and cleared only once
`S <= h/2`, so a statistic near the line cannot flap the size on every trade.
While the alarm stands, the strategy trades at `drift_multiplier` (default
0.5). The alarm and its clearing are journalled (`brain.drift`) and sent to
Telegram and Bale.

## 5. Equity-curve filter

A strategy whose cumulative-R curve is below its own `equity_filter_window`
moving average trades at `equity_filter_multiplier`. The evidence on this
technique is mixed: it helps when losses cluster and costs return when they do
not. It therefore only halves size, never stops a strategy, and the scorecard
reports whether it earns its keep on this account.

## 6. Bayesian allocation (strategy × regime)

Each strategy × regime cell's mean R is estimated with a normal-normal
posterior:

* prior `N(allocation_prior_mean_r, allocation_prior_sd²)`;
* noise scale is the sample standard deviation, floored at 0.5R.

The size multiplier is 1 while `P(mean R > 0) >= 0.5`, and
`max(floor, 2 * P)` below that. Shrinkage toward the prior is what keeps five
lucky trades from looking like skill (Efron & Morris 1977). The multiplier
never exceeds 1.

## 7. Similar-situation memory

For each new signal, the brain finds the `similarity_k` resolved shadow
signals with the nearest feature vectors. It uses z-scored Euclidean distance
over the same features the meta-label filter uses. If the upper end of the 90%
interval of their outcomes is below zero, the signal trades at
`similarity_multiplier`. The memory needs `similarity_min_samples` resolved
signals before it has an opinion, and it has several times more observations
than closed trades, because vetoed signals count too.

## 7b. Macro layers (1.8.0)

Two further shrink-only layers come from the macro desk:

* `cot_crowding`: speculators are at a multi-year extreme on the side of the
  trade;
* `dxy_headwind`: the dollar is moving hard against the trade.

Both are recorded in `brain_layers` and measured by the same scorecard. Their
features join every shadow-book record and the lab's training rows, as of each
row's own time. See [`MACRO-AND-WATCHDOG.md`](MACRO-AND-WATCHDOG.md).

## 8. Gap stress (`sentinel/risk/stress.py`)

The sizing layer bounds each trade's loss at its stop. Gap stress asks a
different question: what if the worst historically recorded gap hit every open
position and the new one at the same time?

Each position's gap is the larger of its two currencies' scenarios. The
defaults are:

| Scenario | Gap |
|---|---|
| CHF (15 Jan 2015) | 30% |
| GBP (7 Oct 2016) | 9% |
| JPY and AUD (3 Jan 2019) | 7% |
| EUR and USD (23 Apr 2017 weekend gap) | 2% |
| Any other currency (`*`) | 3% |

If the book's stress loss would exceed `stress_loss_limit_pct` of equity
(default 25%), the new position is **shrunk** until it fits
(`stress_shrunk` warning). It is **refused** (`stress_gap`) only if even the
minimum lot does not fit.

In practice this caps the notional the book can carry:

* about 12× equity in EUR/USD, so a $100 standard account can hold one 0.01-lot
  position;
* about 3.5× in yen crosses;
* under 1× in franc crosses.

## 9. The meta-label filter and the nightly lab

Every night at `lab_hour_utc`, `ResearchLab` does three things within a time
budget, on a worker thread.

1. **Re-tests every enabled strategy on the broker's stored bars.** Each test
   runs at normal cost and at twice the cost. The mean trade R gets a
   block-bootstrap interval (Künsch 1989), because losses cluster. Each
   strategy is then labelled:
   * `alive`: the whole interval is above zero and the strategy is still
     positive at 2× cost;
   * `dead`: the whole interval is below zero;
   * `weak`: anything in between;
   * `insufficient`: fewer than 30 trades.

   The measured mean and standard deviation become the drift baseline.
2. **A/B-tests every pending `risk.*` parameter proposal** on the same bars.
3. **Trains a meta-label candidate** (López de Prado 2018, ch. 3.6):
   * a random forest with isotonic calibration, trained on the signal logs of
     the first 60% of the window;
   * training rows whose label window reaches into the holdout are **purged**
     (ch. 7.4);
   * it is judged on the last 40%, which it never saw.

   It becomes a candidate only if its holdout AUC is at least `meta_min_auc`
   and its expected value at the chosen threshold is positive after cost.

A candidate changes nothing until the owner approves it in the dashboard. The
approved file is stored under `var/brain-models/` and is verified by SHA-256
every time it is loaded. A mismatched or unreadable file passes every signal
through; it never blocks trading silently. Live calibration of the deployed
filter (Brier score, skill, AUC) is shown next to it.

None of this is a promotion verdict. Real money still requires
`scripts/run_acceptance.py` with its full battery (CPCV, PBO, DSR, baselines).

## 10. Weekly self-report

Every `weekly_report_dow` (default Sunday) at `weekly_report_hour_utc`, the
brain stores and announces (`brain.report`):

* the week's trades, overall and per strategy, with intervals;
* the veto scorecard;
* the cooldowns and drift alarms raised;
* live calibration of the meta filter;
* the number of active lessons.

## 11. Failure modes

| Failure | Result |
|---|---|
| Brain raises in any hook | multiplier 1.0, no veto; the error is journalled and shown on the page |
| Memory or store unreadable | no layer effect |
| Model file tampered with | filter off, reason shown |
| Lab crashes | the report records the error; nothing else changes |
| Brain disabled | the agent behaves exactly as 1.6.0 |

## 12. Configuration

All fields are listed in `sentinel/core/config.py::BrainConfig`, with their
bounds. The dashboard edits them through `POST /api/brain/settings`, which
requires the owner and a TOTP code. The request body is `{"patch": {...}}`,
and `stress_scenarios` is replaced as a whole table rather than merged.

## 13. API

| Method | Path | Who |
|---|---|---|
| GET | `/api/brain` | any signed-in user |
| POST | `/api/brain/settings` | owner + TOTP |
| POST | `/api/brain/lab/run` | owner + TOTP |
| POST | `/api/brain/model/approve` `{model_id}` | owner + TOTP |
| POST | `/api/brain/model/retire` | owner + TOTP |
| POST | `/api/brain/cooldown/clear` `{scope}` | owner + TOTP |

References are collected in [`RESEARCH-NOTES.md`](RESEARCH-NOTES.md).
