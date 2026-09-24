"""Strategy families.

Each module here is one family: a group of strategies that share an economic
premise. Dropping a new module into this directory is all it takes to add
strategies -- ``registry.discover_builtin()`` walks the package and registers
every ``Strategy`` subclass it finds, so there is no central list to edit and
no way to add a strategy that the trial ledger does not know about.

The grouping is not cosmetic. ``research/trials.py`` charges a candidate with
the search effort spent on its entire family, because choosing the best of six
trend systems is one search over six trials rather than six independent
discoveries. Putting a strategy in the wrong family therefore understates the
multiple-testing correction applied to it, which is the one direction of error
this system is built to prevent.
"""

FAMILIES = (
    "trend",
    "mean_reversion",
    "breakout",
    "momentum",
    "carry",
    "volatility",
    "session",
    "pattern",
)
