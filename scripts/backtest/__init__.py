"""Backtest harness for AI Origination.

MEMORY CONTRACT -- read before adding an import here.

This package runs on a 512 MB Lightsail box (~414 MB usable) alongside a live
trading app holding ~106 MB RSS. That leaves ~145 MB.

  * numpy only. Never pandas. `import pandas` costs 50-80 MB before it loads a
    single row; numpy is ~15 MB and does everything this needs.
  * Nothing in this package may be imported by app.main or anything in its
    import graph. It is reachable only as a standalone entry point. A stray
    top-level numpy import in the live path is 15 MB the box does not have.

INDICATOR EQUIVALENCE -- the reason this lives in the repo at all.

The indicator functions come from app.indicators, the same code the live
engine runs. They are pure-Python over Bar lists rather than vectorised numpy,
which looks like the wrong choice for a backtest until you notice they run
ONCE per index (37k bars, well under a second) and the results are then reused
for every parameter combination. Vectorising them would mean maintaining a
second implementation, and a subtly different Wilder smoothing produces
plausible numbers with no error -- exactly the failure the 0-mismatch
equivalence check was built to rule out.
"""
