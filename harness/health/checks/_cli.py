"""Reuse the detection logic already defined in `harness/bin/harness` (the packager CLI entry
point) instead of duplicating it here.

That file has no `.py` suffix - it is a script, not an importable dotted module - so it is loaded
once by path, the same way `tests/test_repo_map_modules.py` already loads it for its own unit
tests. The functions below are unchanged; only the health-check wrapping around their return
values is new (see files.py and repo_map.py in this package).
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path
from types import ModuleType

_CLI_PATH = Path(__file__).resolve().parents[3] / "harness" / "bin" / "harness"
_cli: ModuleType | None = None


def cli() -> ModuleType:
    """Load harness/bin/harness once per process and cache it."""
    global _cli
    if _cli is None:
        loader = importlib.machinery.SourceFileLoader("harness_health_cli_bridge", str(_CLI_PATH))
        spec = importlib.util.spec_from_loader("harness_health_cli_bridge", loader)
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        _cli = module
    return _cli
