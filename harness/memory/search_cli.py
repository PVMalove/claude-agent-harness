#!/usr/bin/env python3
"""Portable read-only equivalent of `harness memory search <repo> <query>`."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path


def main() -> int:
    """Alias the installed package and search without the packager or writer commands."""
    harness_root = Path(__file__).resolve().parents[1]
    if str(harness_root.parent) not in sys.path:
        sys.path.insert(0, str(harness_root.parent))
    if harness_root.name != "harness":
        spec = importlib.util.spec_from_file_location(
            "harness",
            harness_root / "__init__.py",
            submodule_search_locations=[str(harness_root)],
        )
        assert spec is not None and spec.loader is not None
        package = importlib.util.module_from_spec(spec)
        sys.modules["harness"] = package
        spec.loader.exec_module(package)

    from harness.memory.search import search

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument("query")
    args = parser.parse_args()
    try:
        result = search(Path(args.repo).expanduser().resolve(), args.query)
    except (OSError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
