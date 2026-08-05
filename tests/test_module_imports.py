"""Every app module must import cleanly.

WHY THIS EXISTS
---------------
`app/option_chain.py` once referenced `os.getenv` at module level without
importing `os`. That is a NameError at IMPORT time, not at call time, so it
does not fail when the collector runs -- it fails when anything imports the
module. `app.main` imports it, so the entire application refused to start.

Nothing caught it before deployment. The repo has tests for behaviour but
nothing that simply proves each module can be loaded, and the deploy model is a
manual push to a live trading server, so "does it import" was being answered by
the production box.

This is the cheapest possible guard: import every module and fail if any
raises. It catches missing imports, typos in module-level constants, circular
imports, and anything else that executes at load time -- including the numpy
isolation rule, since a stray `import numpy` inside app/ would be visible here.

app.main is deliberately excluded. Importing it constructs the SmartAPI client,
the trade managers and the scheduler as module-level side effects, which is too
much machinery for a test whose only job is to prove modules parse and resolve
their names. Every module app.main pulls in IS covered individually, which is
what would have caught the original bug.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pytest

import app

# Constructs live objects at import time -- see the module docstring.
EXCLUDED = {"app.main"}


def _module_names() -> list[str]:
    root = Path(app.__file__).parent
    found = []
    for info in pkgutil.walk_packages([str(root)], prefix="app."):
        if info.name in EXCLUDED:
            continue
        found.append(info.name)
    return sorted(found)


@pytest.mark.parametrize("module_name", _module_names())
def test_module_imports(module_name: str):
    """Import the module. Any exception here is a module that cannot load.

    Parametrized rather than looped so a failure names the offending module
    directly instead of stopping at the first one.
    """
    importlib.import_module(module_name)


def test_numpy_stays_out_of_the_app_import_graph():
    """The memory contract, enforced rather than documented.

    scripts/backtest/ is numpy-based and must never be reachable from the live
    app: numpy is ~15 MB on a 414 MB box already running ~106 MB of trading
    application. A stray top-level import inside app/ would not fail anything
    obvious -- it would just quietly cost memory the box does not have, which
    is precisely the kind of regression that goes unnoticed until an OOM.
    """
    import sys

    for module_name in _module_names():
        importlib.import_module(module_name)
    assert "numpy" not in sys.modules, (
        "numpy was pulled into the app import graph. Find the offending module and move "
        "the import inside the function that needs it, or into scripts/."
    )
