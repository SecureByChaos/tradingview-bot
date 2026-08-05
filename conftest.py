"""Put the repo root on sys.path for pytest.

Without this, `pytest tests/...` fails with ModuleNotFoundError on `app` and
`scripts`. pytest prepends the test file's own directory (tests/) to sys.path,
not the rootdir, so the packages one level up are invisible. It works when
invoked as `python -m pytest` only because that form adds the cwd -- which
makes the suite pass or fail depending on how it was launched, which is worse
than failing consistently.

A root conftest.py is the fix pytest itself documents: its presence defines the
rootdir AND its directory gets inserted into sys.path under the default
prepend import mode.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
