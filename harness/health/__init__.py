"""harness/health: a stdlib-only registry of `harness health` checks (see model.py for the result
shape and registry.py for how checks are wired in).

`harness/bin/harness.py` already puts the canonical repository root on `sys.path` before importing
this package, so `import harness.health` works today without the block below. That block only
matters once this package is copied byte-for-byte into an installed project's `.harness/health/`
(the way CAPABILITIES.json resources already copy `harness/repo_map/repo_map.py`) and later
launched there without the canonical `harness/` package alongside it on `sys.path` - the harness
console (#348) is the first consumer of that path. It mirrors the bootstrap alias
`harness/repo_map/repo_map.py` uses today; see docs/adr/0001.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_HARNESS_ROOT: Path = Path(__file__).resolve().parent.parent
_REPO_ROOT: Path = _HARNESS_ROOT.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if _HARNESS_ROOT.name != "harness" and "harness" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "harness",
        _HARNESS_ROOT / "__init__.py",
        submodule_search_locations=[str(_HARNESS_ROOT)],
    )
    assert _spec is not None and _spec.loader is not None
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules["harness"] = _pkg
    _spec.loader.exec_module(_pkg)
