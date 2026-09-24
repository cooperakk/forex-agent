# Sentinel-FX — Acceptance protocol

> **The only path from "an idea" to "trading real money" runs through this
> document.** There is no other. `lifecycle: accepted` in a configuration file
> is a claim; the verdict registry is what makes it true, and a claim without
> a matching verdict is repaired downward at every startup.

```bash
python scripts/run_acceptance.py --strategy donchian_trend
```

## Why the protocol is this strict

Given 200 strategy variants tested on the same history, the best one has an
excellent backtest **whether or not any of them has an edge**. That is not a
risk to be managed; it is the default outcome. Every gate below exists to make
selection bias visible rather than to make a strategy look good.

The thresholds are **fixed in `ResearchConfig` before any run**, and the
number of trials you have declared (`max_total_trials_declared`) feeds
directly into the Deflated Sharpe calculation. Raising a threshold after
seeing a result is the specific act this whole apparatus exists to prevent.

Default thresholds:

| Parameter | Default | Meaning |
|---|---|---|
| `alpha` | 0.01 | significance level, not 0.05 |
| `pbo_max` | 0.20 | maximum probability of backtest overfitting |
| `min_dsr` | 0.95 | minimum Deflated Sharpe |
| `cpcv_positive_path_fraction` | 0.70 | share of CPCV paths that must be positive |
| `cost_stress_multiple` | 2.0 | survive doubled costs |
| `latency_stress_multiple` | 2.0 | survive doubled latency |
| `declared_prior` | 0.03 | honest prior that a given hypothesis holds |

> That last one is worth sitting with. With a 3 % prior, a p = 0.05
> "discovery" is **more likely false than true**. This is why `alpha` is 0.01
> and why several independent gates must pass together.

---

## Stage 0 — Environment gates

Run **before a line of strategy code**. Each can end a project in a week, and
each costs almost nothing to check. A blocking failure here means the family
of strategies should be *abandoned*, not optimised.

| Gate | Test | Threshold |
|---|---|---|
| **L0.1** | Cost barrier — break-even win rate `p = (S+c)/(T+S)` | **< 60 %** |
| **L0.2** | Capital granularity — the 0.01-lot floor must not distort the risk budget by > 20 % | `equity ≥ min_equity_for_granularity(...)` |
| **L0.3** | Venue idempotency — venue-deduplicated client order id | supported *(warning, not blocking)* |
| **L0.4** | Venue-side stop | **required** |
| **L0.5** | Connectivity — measured over weeks, before any real trade | **≥ 99.0 % uptime** |
| **L0.6** | Full capital cycle — deposit → hold → **withdraw** completed | **required** |

**L0.1 closes the scalping family by arithmetic.** At `T = S = 3` pips with a
1.0-pip round trip, the required win rate is 66.7 %. Nothing in liquid FX
delivers that consistently.

**L0.4 is not negotiable.** A stop held only in our process does not exist
during a disconnection — and a disconnection is exactly when you need it.

**L0.6 is about counterparty risk, not strategy.** No edge compensates for a
broker that will not return your money. Withdraw the minimum deposit
successfully before anything else is discussed. From Iran specifically,
connectivity (L0.5) and the withdrawal path (L0.6) are first-class project
risks, not footnotes.

---

## Stage 1 — Statistical gates

| Gate | Test | Pass condition |
|---|---|---|
| **L1** | Beats the baselines | beats buy-and-hold, random-entry and always-flat, **and** Sharpe > 0 |
| **L2** | Directional content vs a random walk | the strategy's own calls, scored on the return that followed over its horizon: mean signed return > 0 with **Newey–West** errors, p < α on ≥ 30 signals |
| **L3** | Alpha after factor controls | significant α after dollar / carry / momentum, **Newey–West** HAC errors |
| **L4** | Backtest overfitting | **PBO** (via CSCV) ≤ 0.20 |
| **L5.1** | Deflated Sharpe | **DSR** ≥ 0.95, given the effective trial count |
| **L5.2** | Superior predictive ability | **Hansen SPA** p < α across the variant family |
| **L6** | Stability across CPCV paths | ≥ 70 % of combinatorial purged paths positive |
| **L7** | Stress | survives 2× costs **and** 2× latency |
| **L8** | Drawdown ceiling | worst drawdown ≤ the configured ceiling |
| **L9** | Statistical power | `n_obs ≥ MinTRL` — enough observations for the observed Sharpe to be distinguishable from luck |
| **L10** | Data provenance | live-quality broker data, not synthetic or vendor-reconstructed |

A note on each of the ones that are commonly skipped elsewhere:

**L2 (directional content).** The Meese–Rogoff null — "tomorrow's rate is
today's" — asked of the thing a rule-based strategy actually produces: a
direction. Every signal the backtest raised, filled or not, is scored by the
side it called times the return that followed over the rule's own horizon.
The gate is on the *mean* of that, not the hit rate: a rule that is right 70 %
of the time on tiny moves and wrong 30 % on large ones has a fine hit rate and
loses money. (An earlier version ran Clark–West on the strategy's equity
returns against half its own lagged equity return. Neither series is a
forecast of the exchange rate, so passing it said nothing. Clark–West remains
in `research/stats.py` for models that emit a numeric forecast; `news/lap.py`
uses it correctly.)

**L3 (factor attribution).** A strategy that is long carry during a carry
rally has no alpha; it has beta you did not measure. Newey–West errors because
overlapping returns are autocorrelated and naive t-statistics are inflated.

**L4 (PBO).** The single most informative number in the whole report. It
estimates the probability that a strategy selected as best in-sample
underperforms the median out-of-sample.

**L5.1 (Deflated Sharpe).** Adjusts the observed Sharpe for the number of
trials, for non-normal returns (skew and kurtosis), and for sample length.
This is where the honest trial count matters: understate it and the gate
becomes meaningless.

The count is therefore not taken on faith. It is the **largest** of four
figures, each of which is a lower bound on how much searching happened:

| source | what it knows |
|---|---|
| `--declared-trials` | what you say you tried, including work this machine never saw |
| the run itself | how many variants this acceptance run evaluated |
| the **trial ledger** | every configuration ever backtested, for this strategy's whole *family* |
| a floor of 2 | `expected_max_sharpe` returns 0 below 2, which would switch the gate off |

### The trial ledger

`sentinel/research/trials.py` keeps a persistent, hash-keyed record in
`var/trials.db` of every `(strategy, parameters, instruments, timeframe, data
window)` combination that has been backtested. `run_backtest` records itself,
so the count rises whether or not anyone remembers to declare it, and
re-running an identical configuration does **not** double-count — it produced
the same number and revealed nothing new.

Two design choices are worth knowing:

* **A candidate is charged with its family's count, not its own.** Choosing the
  best of six trend systems is one search over six trials. Without this, adding
  strategies would raise the chance of a flattering winner while leaving the
  bar exactly where it was — a bigger library would be a better overfitting
  machine. With it, adding a strategy raises the bar for every sibling.
* **Validation runs are recorded but not counted.** A CPCV fold, a stress pass
  or a baseline re-runs a configuration already counted; charging for them
  would penalise thorough validation.

The ledger is a **floor, never the truth**. It cannot see runs made before it
existed, on another machine, in a notebook, or by eye on a chart — and ideas
discarded after a glance are real trials selected on the same data. The
acceptance report prints that caveat every time. If you know a larger number,
declare it: the larger figure is the one that is used.

**L6 (CPCV).** Combinatorial purged cross-validation of the **selection**,
not of one fixed configuration. The variant family (every parameter perturbed
±15 % and ±30 %, validated by constructing the strategy) is backtested once
each; for every combination of test blocks the best variant is chosen on the
purged, embargoed train rows and scored on the test rows; the pieces are
reassembled into complete out-of-sample paths. What L6 measures is whether
"pick the parameters that worked" survives out of sample. (An earlier version
backtested the candidate on six contiguous slices and called them CPCV paths.
That measures era-by-era consistency, which L8 still reads; it does not
measure selection, and it purges nothing, because nothing was selected.) A
family that collapses to one distinct configuration is reported as such and
L6 is marked *not evaluated*, which is a failure.

**L9 (MinTRL).** Minimum track record length. A Sharpe of 1.2 over 40 trades
is not evidence.

---

## What the protocol actually does when you run it

`scripts/run_acceptance.py` performs a full, honest run:

1. Backtests the candidate.
2. Backtests **three baselines** — buy-and-hold, random-entry, always-flat.
3. Runs the **stress variant** (2× cost, 2× latency).
4. Backtests the **variant family** — genuine parameter neighbours — for PBO,
   SPA and CPCV, under the same session windows, trading days and weekend
   flatten the agent applies.
5. Runs **combinatorial purged CV** over that family's return matrix.
6. Applies Benjamini–Hochberg across the gate p-values.
7. Records a verdict in `var/verdicts.db`, bound to
   `config_fingerprint(instruments, params, timeframe)`.

It prints a per-gate table with the observed value, the threshold, and the
reason the gate exists.

**The gate list is fixed.** Every gate in L1–L10 appears in every verdict. A
gate whose evidence was not supplied — no baselines, no variant matrix, no
stress run — is recorded as *not evaluated* and **fails**. An earlier version
only evaluated gates whose inputs happened to be present, so a verdict built
from a bare return series and the `live-quality` label came back accepted with
five gates in it.

**The data label is earned, not typed.** A CSV directory is `third-party`
unless every file carries the venue's own `bid` and `ask` columns, in which
case `--data-label live-quality` is honoured. Synthetic data cannot be
relabelled. `carry_bp`, `bid`, `ask`, `swap_*` columns are kept by the
importer — the carry family needs the first and L10 needs the next two.

**The venue is named.** `--broker amarkets` (or the configuration's broker)
supplies the environment gates L0.3 and L0.4 from that profile's declaration
rather than from a constant. The dashboard's connection probe is the
measurement; the verdict records which profile it was evaluated for.

### The shipped demo fails, on purpose

```
L2   beats a random walk .................. FAIL
L3   alpha after factor controls .......... FAIL
L4   backtest overfitting ................. FAIL
L5.2 superior predictive ability .......... FAIL
L9   statistical power .................... FAIL
L10  data provenance ...................... FAIL
VERDICT: NOT ACCEPTED
```

`donchian_trend` on synthetic data does not pass. This is the correct result
and it is left in place deliberately. A system that ships with a passing
verdict has either found something extraordinary or is lying to you, and the
second is overwhelmingly more likely.

---

## Promotion, and how acceptance is revoked

A verdict is bound to a **configuration fingerprint**. Change an instrument, a
parameter or the timeframe and the fingerprint no longer matches — acceptance
is revoked automatically at the next startup, `enforce_config_authority()`
repairs the lifecycle downward, bumps the config version, and writes
`updated_by="startup-authority"` into the audit log.

There is no way to promote from the dashboard without a matching verdict.
`POST /api/config` cannot set `agent.mode`, `execution.venue_mode` or
`execution.broker` at all.

### The sequence that is actually responsible

1. **Paper**, for weeks, on live prices. Read the audit log daily.
2. **Acceptance run.** If it fails, the strategy is not ready — that is the
   protocol working, not a bug to route around.
3. **Advisory mode** on the live account: the system proposes, you execute.
   Compare its proposals to your own judgement for a month.
4. **Semi-auto** inside a narrow envelope (instruments, max lots, max risk).
5. **Autonomous**, at minimum size, with the kill switch tested.
6. Scale only after a **withdrawal** has actually cleared.

Each step is weeks, not days. The system is built to support that pace; the
configuration defaults assume it.

---

## Honest framing

Even a strategy that passes every gate has **not** been proven profitable. It
has been shown to be *not obviously the product of selection bias* on the data
available. That is a much weaker statement, and it is the strongest statement
this or any other apparatus can make about the future.

Roughly **71 %** of retail FX accounts lose money. The realistic value of this
project is that it will tell you the truth about whether you have an edge,
early and cheaply, instead of letting you discover it over two years and a
drawdown.
