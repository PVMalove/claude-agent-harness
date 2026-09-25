#!/usr/bin/env python3
"""Assemble an offline Repo Map parser bundle directory from an already-downloaded wheelhouse.

Build-time only: it copies wheels and the tree-sitter worker, hashes them, and writes
`parser_bundle.lock.json` in the format `harness/repo_map/parser_bundle.py` verifies. It never
downloads, resolves, or installs anything -- the wheelhouse must already hold exactly the pinned
wheels below for one interpreter/platform pair (ADR 0024). Pins change only with a harness release.

Usage: build_parser_bundle.py --wheelhouse DIR --out DIR [--pair cpXY-platform]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "harness" / "repo_map" / "tree_sitter_worker.py"
CORE_DISTRIBUTION = "tree_sitter"
CORE_VERSION = "0.26.0"
CORE_ABI_RANGE = "13-15"


@dataclass(frozen=True)
class GrammarPin:
    name: str
    distribution: str
    version: str
    abi: int
    extensions: tuple[str, ...]


GRAMMARS = (
    GrammarPin("python", "tree_sitter_python", "0.25.0", 15, (".py",)),
    GrammarPin("typescript", "tree_sitter_typescript", "0.23.2", 14, (".ts",)),
    GrammarPin("tsx", "tree_sitter_typescript", "0.23.2", 14, (".tsx",)),
    GrammarPin("javascript", "tree_sitter_javascript", "0.25.0", 15, (".js", ".jsx")),
    GrammarPin("go", "tree_sitter_go", "0.25.0", 15, (".go",)),
    GrammarPin("java", "tree_sitter_java", "0.23.5", 14, (".java",)),
    GrammarPin("csharp", "tree_sitter_c_sharp", "0.23.5", 15, (".cs",)),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _running_pair() -> str:
    script = (
        "import sys, sysconfig;"
        "print(f'cp{sys.version_info[0]}{sys.version_info[1]}-'"
        " + sysconfig.get_platform().replace('-', '_').replace('.', '_'))"
    )
    return subprocess.check_output([sys.executable, "-c", script], text=True).strip()


def _single_wheel(wheelhouse: Path, distribution: str, version: str) -> Path:
    matches = sorted(wheelhouse.glob(f"{distribution}-{version}-*.whl"))
    if len(matches) != 1:
        raise SystemExit(
            f"expected exactly one {distribution}=={version} wheel in {wheelhouse}, "
            f"found {len(matches)}"
        )
    return matches[0]


def build(wheelhouse: Path, out: Path, pair: str) -> Path:
    core = _single_wheel(wheelhouse, CORE_DISTRIBUTION, CORE_VERSION)
    grammar_wheels = {
        pin.name: _single_wheel(wheelhouse, pin.distribution, pin.version) for pin in GRAMMARS
    }
    target = out / "wheelhouse" / pair
    target.mkdir(parents=True, exist_ok=True)
    artifacts = []
    for wheel in dict.fromkeys((core, *grammar_wheels.values())):
        shutil.copyfile(wheel, target / wheel.name)
        artifacts.append({"filename": wheel.name, "sha256": _sha256(wheel)})
    shutil.copyfile(WORKER, out / WORKER.name)
    lock = {
        "core_version": CORE_VERSION,
        "core_abi_range": CORE_ABI_RANGE,
        "worker_script": WORKER.name,
        "script_sha256": _sha256(out / WORKER.name),
        "grammars": [
            {
                "name": pin.name,
                "distribution": pin.distribution,
                "version": pin.version,
                "abi": pin.abi,
                "sha256": _sha256(grammar_wheels[pin.name]),
                "extensions": list(pin.extensions),
            }
            for pin in GRAMMARS
        ],
        "wheelhouses": {pair: artifacts},
    }
    lock_path = out / "parser_bundle.lock.json"
    lock_path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return lock_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--pair", help="interpreter/platform pair; defaults to this interpreter")
    args = parser.parse_args(argv)
    lock_path = build(args.wheelhouse, args.out, args.pair or _running_pair())
    print(lock_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
