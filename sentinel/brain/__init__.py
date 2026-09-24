"""The agent's adaptive layer: it learns from every signal, and can only shrink.

``sentinel.agent`` already learns from CLOSED TRADES (post-mortems, lessons,
proposals). That is a thin stream -- a strategy that trades twice a week gives
a hundred observations a year -- and it says nothing about the signals the
agent did NOT take. The brain widens the stream and uses it carefully:

* ``shadow``  -- every signal is recorded (executed, vetoed, skipped) and
  resolved later against the bars that followed, with the same stop, target
  and horizon: 5-10x more evidence, and an honest scorecard of every veto and
  filter ("the news blackout skipped 14 signals that would have lost 6R").
* ``layers``  -- the statistics: CUSUM drift, the equity-curve filter,
  Bayesian allocation across strategy x regime cells, nearest-neighbour
  memory of similar situations, loss-streak cooldowns.
* ``lab``     -- a nightly research run on the broker's own stored bars:
  which strategies are alive, what each pending proposal would have changed,
  and a meta-label filter trained and validated out of sample.
* ``report``  -- a weekly self-evaluation, including whether each layer of
  the brain itself helped or cost money.

The one rule every piece obeys: **risk goes down fast and automatically; it
goes up only slowly, with statistical evidence and the owner's approval.**
"""

from .service import Brain

__all__ = ["Brain"]
