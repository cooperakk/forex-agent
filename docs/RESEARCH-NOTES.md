# Research notes: what the evidence says, and what Sentinel does about it

This file records the published evidence and the documented market events
behind Sentinel's design, and it maps each one to the code it shaped. It is
written for the owner and for anyone auditing the system.

It is not investment advice. A citation here means "this shaped a design
decision". It does not mean "this strategy will make money".

---

## 1. Where FX returns have come from (and gone)

| Finding | Source | What Sentinel does |
|---|---|---|
| **Time-series momentum.** A futures contract's own past 12-month return predicts its next month's return, across 58 markets including currencies. The strategy did well in extreme markets such as 2008. | Moskowitz, Ooi & Pedersen (2012), *Time series momentum*, JFE 104(2) 228–250 | `ts_momentum`, `vol_target_trend`, `donchian_trend` in the strategy library |
| **Currency momentum.** Cross-sectional FX momentum earns excess returns, but they are concentrated in less liquid currencies and eroded by transaction costs and limits to arbitrage. | Menkhoff, Sarno, Schmeling & Schrimpf (2012), *Currency momentum strategies*, JFE 106(3) 660–684 | `xs_momentum`. Costs are charged in every test, and the lab re-tests every strategy at **2× cost** |
| **Carry.** High-yield currencies earn a premium that compensates for crash risk in global downturns. | Lustig, Roussanov & Verdelhan (2011), *Common risk factors in currency markets*, RFS 24(11) 3731–3777 | `carry_tilt`, `carry_momentum`, `carry_vol_filter` |
| **Carry crashes.** Carry returns are negatively skewed. Crashes arrive when funding liquidity dries up and positions unwind together (AUD/JPY fell about 40% in the second half of 2008). | Brunnermeier, Nagel & Pedersen (2008), *Carry trades and currency crashes*, NBER Macroeconomics Annual 23 313–347 | `carry_vol_filter`. The **gap-stress budget** (`risk/stress.py`) caps yen-cross notional at about 3.5× equity by default |
| **Carry and FX volatility.** Global FX volatility explains carry returns: carry loses when volatility spikes. | Menkhoff, Sarno, Schmeling & Schrimpf (2012), *Carry trades and global foreign exchange volatility*, JF 67(2) 681–718 | The regime detector and the volatility-scaled strategies |
| **Value and momentum everywhere.** Both premia exist across asset classes and are negatively correlated with each other. | Asness, Moskowitz & Pedersen (2013), JF 68(3) 929–985 | Diversifying across strategy families, and the correlated-risk limits in the risk engine |
| **Volatility management.** Scaling exposure inversely to recent volatility improves risk-adjusted returns for many factors. | Moreira & Muir (2017), *Volatility-managed portfolios*, JF 72(4) 1611–1644; Harvey et al. (2018), *The impact of volatility targeting*, JPM 45(1) | ATR-based stops (the stop distance scales size), `vol_target_trend` |

What this means in practice: every documented FX premium is **small**, **time-varying** and
**easy to erase with costs**. None supports a claim of consistent large monthly returns.

## 2. Why most backtests lie

| Finding | Source | What Sentinel does |
|---|---|---|
| **Multiple testing.** With hundreds of factors tested, a t-statistic of 2 is no longer significant; the hurdle should be about 3. | Harvey, Liu & Zhu (2016), *…and the cross-section of expected returns*, RFS 29(1) 5–68 | Every trial is recorded in the trial registry. The acceptance protocol accounts for the number of trials |
| **Deflated Sharpe ratio.** A Sharpe ratio has to be corrected for how many variations were tried and for non-normal returns. | Bailey & López de Prado (2014), JPM 40(5) 94–107 | DSR gate in `scripts/run_acceptance.py` |
| **Probability of backtest overfitting (PBO).** Combinatorially symmetric cross-validation estimates how often the in-sample best is below the median out of sample. | Bailey, Borwein, López de Prado & Zhu (2017), *The probability of backtest overfitting*, J. Computational Finance 20(4) | PBO gate in the acceptance protocol |
| **Triple-barrier labels, meta-labeling, purging and embargo, CPCV.** | López de Prado (2018), *Advances in Financial Machine Learning*, Wiley, chapters 3, 7 and 12 | The brain's shadow book resolves signals with triple barriers. The lab's meta-label filter **purges** training labels that overlap the holdout. The acceptance protocol uses CPCV |
| **Dependent data.** Resampling single trades understates uncertainty when losses cluster. | Künsch (1989), *The jackknife and the bootstrap for general stationary observations*, Ann. Stat. 17(3) 1217–1241 | The lab reports block-bootstrap intervals, not naive ones |

## 3. Detecting that an edge has died

* **CUSUM.** Page (1954), *Continuous inspection schemes*, Biometrika 41(1/2)
  100–115. Sentinel runs a one-sided CUSUM on live trade R against the lab's
  baseline.
  * With `k = 0.5`, `h = 4`, a healthy strategy raises a false alarm about once
    every 170 trades, and a one-sigma drop is caught in about 8 trades.
  * On alarm the strategy trades at half size (`brain.drift_multiplier`).
  * The alarm clears only after the statistic halves (hysteresis).
  * It sits alongside the older performance guard, which suspends a strategy
    outright once its interval is wholly below zero.
* **Shrinkage.** Efron & Morris (1977), *Stein's paradox in statistics*,
  Scientific American 236(5). A cell with few trades is pulled toward the prior
  before it is allowed to change anything: the brain's Bayesian allocation.

## 4. How people lose money (behaviour)

| Finding | Source | What Sentinel does |
|---|---|---|
| Most retail CFD and FX accounts lose money: **74–89%**, as stated by ESMA when it restricted leverage in 2018. | ESMA product intervention decisions, 2018 | Low default risk per trade (0.5%), leverage ceilings far below what brokers allow |
| Fewer than 1% of day traders are predictably profitable after fees. | Barber, Lee, Liu & Odean (2014), *The cross-section of speculator skill*, J. Financial Markets 18 1–24 | Real money only after the acceptance protocol; paper trading and advisory mode first |
| Trading more makes individual investors poorer. | Barber & Odean (2000), *Trading is hazardous to your wealth*, JF 55(2) 773–806 | Trade-frequency caps and the annual cost budget in the risk engine |
| Professional traders take **more risk after losses** (afternoon risk-taking after morning losses). | Coval & Shumway (2005), *Do behavioral biases affect prices?*, JF 60(1) 1–34 | **Loss-streak cooldowns** (`brain.loss_streak_*`), which apply to manual tickets too |
| Investors sell winners too early and hold losers too long (the disposition effect). | Shefrin & Statman (1985), JF 40(3) 777–790 | Stops are placed at the broker and cannot be widened by the agent; post-mortems flag `gave_back_open_profit` |

## 5. Position sizing: why not "bet big when confident"

The Kelly criterion (Kelly 1956, *A new interpretation of information rate*,
BSTJ 35(4); Thorp 2006, *The Kelly criterion in blackjack, sports betting and
the stock market*) maximises long-run growth **when the edge is known
exactly**.

For a strategy with mean trade R of `mu` and dispersion `sigma`, the
growth-optimal risk per trade is about `mu / (sigma² + mu²)`. With
`sigma = 1.2`:

| True edge | Kelly risk per trade | Sentinel default |
|---|---|---|
| +0.05R | 3.5% | 0.5% |
| +0.10R | 6.9% | 0.5% |
| +0.20R | 13.5% | 0.5% |

The edge is never known exactly. Its 95% interval after 50 trades is roughly
±0.33R, which usually includes zero. Betting Kelly on an **estimated** edge
over-bets, and over-betting is far more costly than under-betting: at twice
Kelly, expected growth is zero. Sentinel therefore risks a small fixed
fraction, and the brain is **shrink-only**. It reduces size when the evidence
weakens and never increases it when the evidence looks strong.

A Monte Carlo run with Sentinel's own `stats.probability_of_drawdown` (240
trades a year, sigma 1.2R) gives the probability of a drawdown of at least 20%
within one year:

| Risk per trade | No edge | +0.1R | +0.2R |
|---|---|---|---|
| 0.5% | 3% | ~0% | ~0% |
| 1% | 44% | 11% | 2% |
| 2% | 93% | 72% | 40% |
| 5% | 100% | 100% | 100% |

## 6. Failures on record, and the rule each one wrote

| Event | What happened | Rule in Sentinel |
|---|---|---|
| **LTCM, 1998** | Balance-sheet leverage of about 25:1 and derivatives notional above $1 trillion. It lost about $4.6bn in under four months; the Fed brokered a $3.6bn recapitalisation by 14 banks. | Gross-leverage ceiling, correlated-risk and currency-exposure limits, and a drawdown ladder that cuts size as equity falls |
| **Amaranth, 2006** | About $6.6bn lost on concentrated natural-gas spreads within weeks. | Per-instrument and per-currency exposure limits |
| **2008 carry unwind** | AUD/JPY fell about 40% from July to October; carry books were wiped out. | Carry strategies carry a volatility filter; yen crosses carry a 7% stress scenario |
| **Knight Capital, 1 Aug 2012** | A deployment error revived dead code. About $440m was lost in 45 minutes, and the firm was sold. | File-based kill switch that works without a password or network; dead-man watchdog; duplicate-order guard; reconciliation that blocks entries on any mismatch |
| **Swiss franc, 15 Jan 2015** | The SNB removed the EUR/CHF 1.20 floor. EUR/CHF traded near 0.85 within minutes, and stops filled hundreds of pips away. FXCM clients ran negative balances and the firm needed a $300m rescue; **Alpari (UK)** and Excel Markets became insolvent. | **Gap stress** with a 30% CHF scenario. The shadow book fills gaps at the open, not at the stop. The broker profile says to verify the entity and complete a withdrawal before scaling (gate L0.6) |
| **French election, 23 Apr 2017** | EUR/USD opened the week about 2% above Friday's close. | Weekend flatten rule; 2% EUR/USD scenario |
| **GBP flash crash, 7 Oct 2016** | GBP/USD fell about 6% in two minutes of thin Asian trading (BIS Markets Committee report, Jan 2017). | 9% GBP scenario; session windows; spread veto |
| **JPY flash crash, 3 Jan 2019** | AUD/JPY fell about 7% and USD/JPY about 4% in minutes during a holiday-thinned session. | 7% JPY and AUD scenarios; spread and data-quality vetoes |
| **Archegos, March 2021** | Concentrated positions held through total-return swaps. About $20bn of family capital was lost, and banks lost over $10bn (Credit Suisse about $5.5bn). | Hidden leverage is still leverage: exposure is computed per currency across all positions, and multi-account group limits exist |

## 7. Success on record, and what it shares

* **Trend followers in 2008.** Managed-futures indices ended 2008 with strongly
  positive returns while equities fell by about 40%. This is the "crisis alpha"
  that Moskowitz, Ooi & Pedersen document.
  * Shared trait: many markets, small risk per position, cut losses, let winners
    run.
  * The Turtle experiment (Dennis and Eckhardt, 1983) taught exactly that:
    Donchian breakouts sized by volatility (the "N" unit), with strict loss
    limits. It is the ancestor of `donchian_trend`.
* **What does not transfer.**
  * Renaissance's Medallion fund: capacity-limited, secret and built by a large
    research team.
  * Soros's 1992 sterling trade: discretionary, with rare conviction.

  Neither is a template for a retail robot, and a system that claims to be one
  is making a marketing claim.

The common thread between the survivors and the failures is not the entry
signal. It is **sizing, diversification and what happens on the worst day**.
That is where Sentinel spends most of its code.

## 8. What changed in 1.7.0 because of this

* Every signal is scored by what happened next: the shadow book, with triple
  barriers, adverse-first ordering and fills at the open.
* The veto scorecard shows whether each safety rule earned its keep.
* CUSUM drift detection sets the size of a decaying strategy, measured against
  a lab-measured baseline.
* Loss-streak cooldowns counter the loss-chasing behaviour in Coval & Shumway.
* Bayesian shrinkage per strategy × regime; similar-situation memory.
* A meta-label filter with purged training, an out-of-sample gate and owner
  approval.
* Gap stress drawn from the documented crises above.
* Cent-account conversion, so small accounts can keep risk per trade small.
