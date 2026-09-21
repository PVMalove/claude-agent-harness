#!/usr/bin/env python3
"""Deterministic, offline Repo Map for a pinned Git commit."""

from __future__ import annotations

import argparse
import ast
import copy
import json
import subprocess
import sys
from collections import deque
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from token_estimator import TOKEN_ESTIMATOR_VERSION, estimate_tokens

DEFAULT_MAX_TOKENS = 4000
EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "deps",
        "dependencies",
        "third_party",
        "vendor",
        "dist",
        "build",
        "__pycache__",
        ".cache",
        ".mypy_cache",
        ".pytest_cache",
        "coverage",
        "generated",
        "tmp",
        "temp",
        "logs",
        "media",
        "images",
        "assets",
        "target",
        ".next",
        ".terraform",
        "site-packages",
    }
)
EXCLUDED_NAMES = frozenset({".env", ".env.local", "id_rsa", "id_ed25519"})
SENSITIVE_PATH_PARTS = frozenset({"secret", "credential", "private_key"})
EXCLUDED_SUFFIXES = frozenset(
    {
        ".pyc",
        ".pyo",
        ".min.js",
        ".map",
        ".log",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".mp4",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".lock",
    }
)


class FileRecord(TypedDict):
    path: str
    signatures: list[str]


class EdgeRecord(TypedDict):
    source: str
    target: str
    kind: str
    confidence: str


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=False
    )
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


def _allowed(path: str) -> bool:
    parts = Path(path).parts
    name = parts[-1]
    if any(part in EXCLUDED_DIRS or part.startswith(".env") for part in parts):
        return False
    if name in EXCLUDED_NAMES or name.endswith(("~", ".env", ".pem", ".key")):
        return False
    if any(
        marker in part.casefold()
        for part in parts
        for marker in SENSITIVE_PATH_PARTS
    ):
        return False
    if ".generated." in name.casefold():
        return False
    return not any(name.endswith(suffix) for suffix in EXCLUDED_SUFFIXES)


def _signatures(tree: ast.Module) -> list[str]:
    found: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            annotation = f" -> {ast.unparse(node.returns)}" if node.returns else ""
            args = copy.deepcopy(node.args)
            args.defaults = [ast.Constant(value=Ellipsis) for _ in args.defaults]
            args.kw_defaults = [
                ast.Constant(value=Ellipsis) if item is not None else None
                for item in args.kw_defaults
            ]
            found.append(f"{prefix} {node.name}({ast.unparse(args)}){annotation}")
        elif isinstance(node, ast.ClassDef):
            bases = ", ".join(ast.unparse(base) for base in node.bases)
            found.append(
                f"class {node.name}({bases})" if bases else f"class {node.name}"
            )
    return found


def _module_paths(paths: set[str]) -> dict[str, str]:
    modules: dict[str, str] = {}
    for path in paths:
        stem = path.removesuffix(".py")
        if stem.endswith("/__init__"):
            stem = stem.removesuffix("/__init__")
        module = stem.replace("/", ".")
        if module:
            modules[module] = path
    return modules


def _import_modules(node: ast.stmt, module: str) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if not isinstance(node, ast.ImportFrom):
        return []
    parent = module.rsplit(".", node.level)[0] if node.level else ""
    base = ".".join(part for part in (parent, node.module or "") if part)
    candidates = [base] if base else []
    if base:
        candidates.extend(
            f"{base}.{alias.name}" for alias in node.names if alias.name != "*"
        )
    return candidates


def _encode(payload: dict[str, object]) -> str:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )


def _sized(payload: dict[str, object]) -> tuple[str, int]:
    # The count includes the count field itself. Iterate until its digit width settles.
    size = 0
    for _ in range(8):
        payload["estimated_tokens"] = size
        encoded = _encode(payload)
        next_size = estimate_tokens(encoded)
        if next_size == size:
            return encoded, size
        size = next_size
    raise ValueError("token estimate did not converge")


def build_map(repo: Path, commit: str, max_tokens: int, seeds: list[str]) -> str:
    if max_tokens < 1:
        raise ValueError("--max-tokens must be positive")
    pinned = (
        _git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
    )
    raw_paths = _git(repo, "ls-tree", "-rz", "--name-only", pinned)
    paths = sorted(
        path.decode("utf-8", "surrogateescape")
        for path in raw_paths.split(b"\0")
        if path
    )
    paths = [path for path in paths if _allowed(path)]
    python_paths = {path for path in paths if path.endswith(".py")}
    module_paths = _module_paths(python_paths)
    files: dict[str, FileRecord] = {}
    edges: list[EdgeRecord] = []
    for path in paths:
        # Git object reads keep the working tree, including untracked files, outside the input.
        content = _git(repo, "show", f"{pinned}:{path}")
        if b"\0" in content or len(content) > 2_000_000:
            continue
        signatures: list[str] = []
        if path.endswith(".py"):
            try:
                tree = ast.parse(content.decode("utf-8", "replace"), filename=path)
            except SyntaxError:
                continue
            signatures = _signatures(tree)
            current_module = path.removesuffix(".py").replace("/", ".")
            for node in ast.walk(tree):
                for imported_module in _import_modules(node, current_module):
                    target = module_paths.get(imported_module)
                    if target and target != path:
                        edge: EdgeRecord = {
                            "source": path,
                            "target": target,
                            "kind": "import",
                            "confidence": "high",
                        }
                        if edge not in edges:
                            edges.append(edge)
        files[path] = {"path": path, "signatures": signatures}

    edges.sort(key=lambda edge: (edge["source"], edge["target"], edge["kind"]))
    indegree = {path: 0 for path in files}
    for edge in edges:
        if edge["target"] in indegree:
            indegree[edge["target"]] += 1
    distances: dict[str, int] = {}
    queue = deque(sorted(set(seeds) & files.keys()))
    for path in queue:
        distances[path] = 0
    neighbors: dict[str, set[str]] = {path: set() for path in files}
    for edge in edges:
        if edge["source"] in neighbors and edge["target"] in neighbors:
            neighbors[edge["source"]].add(edge["target"])
            neighbors[edge["target"]].add(edge["source"])
    while queue:
        path = queue.popleft()
        for neighbor in sorted(neighbors[path]):
            if neighbor not in distances:
                distances[neighbor] = distances[path] + 1
                queue.append(neighbor)
    ordered = sorted(
        files,
        key=(lambda path: (distances.get(path, 10**9), path))
        if seeds
        else (lambda path: (-indegree[path], path)),
    )
    payload: dict[str, object] = {
        "schema_version": 1,
        "commit": pinned,
        "tier": "reduced",
        "parser": "ast-only",
        "degradation_reason": "offline parser bundle unavailable",
        "token_estimator_version": TOKEN_ESTIMATOR_VERSION,
        "files": [],
        "edges": [],
        "estimated_tokens": 0,
    }
    encoded, size = _sized(payload)
    if size > max_tokens:
        raise ValueError("--max-tokens is too small for Repo Map metadata")
    selected: set[str] = set()
    for path in ordered:
        candidate = selected | {path}
        payload["files"] = [files[item] for item in ordered if item in candidate]
        payload["edges"] = [
            edge
            for edge in edges
            if edge["source"] in candidate and edge["target"] in candidate
        ]
        trial, trial_size = _sized(payload)
        if trial_size <= max_tokens:
            selected = candidate
            encoded = trial
        else:
            payload["files"] = [files[item] for item in ordered if item in selected]
            payload["edges"] = [
                edge
                for edge in edges
                if edge["source"] in selected and edge["target"] in selected
            ]
    return encoded


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--commit", required=True)
    parser.add_argument("--seed", action="append", default=[])
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    args = parser.parse_args()
    try:
        sys.stdout.write(build_map(args.repo, args.commit, args.max_tokens, args.seed))
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
