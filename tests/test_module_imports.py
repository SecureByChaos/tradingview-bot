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


# Importing the app and then inspecting sys.modules IN THIS PROCESS does not
# work, and failing to notice that produced two wrong results in a row.
#
# pytest imports every test module during collection. tests/test_premium_buckets.py
# imports scripts.backtest.premium, which imports numpy. So by the time any
# assertion here runs, both are already in sys.modules -- put there by a TEST,
# not by app code. The check then passes or fails depending on which other test
# files were collected, which makes it worthless: run alone it passed, run as
# part of the suite it failed, and neither answer was about the app.
#
# So the probe runs in a clean subprocess that imports only app modules. Slower,
# and worth it: this is the only way the question "what does importing the app
# actually load" gets an answer that does not depend on the test runner.
_PROBE_SOURCE = """
import importlib, json, pkgutil, sys
import app
for info in pkgutil.walk_packages(list(app.__path__), prefix="app."):
    if info.name in {excluded!r}:
        continue
    importlib.import_module(info.name)
print(json.dumps(sorted(sys.modules)))
"""


def _app_import_graph() -> set[str]:
    """Every module loaded by importing the app, measured in a fresh interpreter."""
    import json
    import subprocess
    import sys

    root = Path(app.__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-c", _PROBE_SOURCE.format(excluded=EXCLUDED)],
        capture_output=True, text=True, cwd=str(root),
    )
    if result.returncode != 0:
        pytest.fail(f"probe failed to import the app:\n{result.stderr}")
    return set(json.loads(result.stdout.strip().splitlines()[-1]))


def test_backtest_package_is_not_reachable_from_the_app():
    """The isolation rule that is actually true and worth enforcing.

    scripts/backtest/ must never be imported by app code. It exists to be run
    standalone, and pulling it in would drag the fitting machinery into a live
    trading process for no benefit -- app/premium_model.py reads the fitted
    coefficients from JSON precisely so this boundary holds.
    """
    leaked = sorted(name for name in _app_import_graph() if name.startswith("scripts."))
    assert not leaked, f"app modules imported from scripts/: {leaked}"


# Heavy third-party packages already present in the app's import graph, with
# where they come from. Pinned as a set so a NEW one gets caught, rather than
# asserting an absence that is not true.
KNOWN_HEAVY_DEPENDENCIES = {
    # app/option_finder.py:11 -- used only to filter the instrument master.
    "pandas",
    # Pulled in BY pandas, not imported by any app module directly.
    "numpy",
}


def test_no_new_heavy_dependency_enters_the_app_graph():
    """Pins the memory cost of importing the app.

    CORRECTS A CLAIM THIS REPO MADE FOR MONTHS. Both CLAUDE.md and
    app/premium_model.py stated that nothing in app.main's graph may pull numpy
    in. That was never true: app/option_finder.py imports pandas at module
    level and pandas imports numpy, so both have always been loaded in the live
    process. The first version of this test asserted the documented rule and
    failed immediately, which is how the discrepancy surfaced.

    The rule was worth wanting -- pandas is 50-80 MB on a 414 MB box already
    running ~106 MB of application. option_finder uses it only for DataFrame
    filtering of the instrument master, which app/option_chain.py does with
    plain dicts for exactly this reason. Replacing it is a real memory saving,
    but it sits in the live strike-selection path, so it is an opportunity
    rather than a cleanup.

    Until then, this pins the status quo so the cost does not grow silently.

    Measured in a subprocess for the same reason as the test above: another
    test file importing scipy would otherwise be blamed on the app.
    """
    heavy = {"pandas", "numpy", "scipy", "matplotlib", "sklearn", "torch"}
    present = heavy & _app_import_graph()
    unexpected = present - KNOWN_HEAVY_DEPENDENCIES
    assert not unexpected, (
        f"New heavy dependency in the app import graph: {sorted(unexpected)}. On a 414 MB "
        "box this is real memory. Move the import inside the function that needs it, or "
        "add it to KNOWN_HEAVY_DEPENDENCIES with a note on where it comes from and why."
    )
