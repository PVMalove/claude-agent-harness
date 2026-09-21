#!/usr/bin/env python3
"""Deterministic, offline Repo Map for a pinned Git commit."""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import subprocess
import sys
from collections import deque
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from token_estimator import TOKEN_ESTIMATOR_VERSION, estimate_tokens

DEFAULT_MAX_TOKENS = 4000
DEFAULT_MAX_FILES = 10_000
DEFAULT_MAX_FILE_BYTES = 2_000_000
DEFAULT_TIMEOUT_SECONDS = 10
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
    parser_status: Literal["ok", "syntax_error", "invalid_encoding", "too_large"]


class EdgeRecord(TypedDict):
    source: str
    target: str
    kind: str
    confidence: str


class Diagnostic(TypedDict):
    code: Literal["syntax_error", "invalid_encoding", "file_too_large"]
    path: str


@dataclass(frozen=True)
class RepoMapPolicy:
    allow_paths: tuple[str, ...] = ()
    deny_paths: tuple[str, ...] = ()
    redact_paths: tuple[str, ...] = ()
    max_files: int = DEFAULT_MAX_FILES
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_tokens: int | None = None
    sha256: str | None = None

    @property
    def enforced(self) -> bool:
        return self.sha256 is not None


def _git(repo: Path, timeout_seconds: int, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"git {' '.join(args)} timed out after {timeout_seconds} seconds") from exc
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(path, pattern) for pattern in patterns)


def _allowed(path: str, policy: RepoMapPolicy) -> bool:
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
    if policy.allow_paths and not _matches(path, policy.allow_paths):
        return False
    if _matches(path, policy.deny_paths) or _matches(path, policy.redact_paths):
        return False
    return not any(name.endswith(suffix) for suffix in EXCLUDED_SUFFIXES)


def _string_patterns(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"repo_map_policy.{field} must be a list of non-empty path globs")
    return tuple(sorted(set(value)))


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"repo_map_policy.{field} must be a positive integer")
    return value


def load_policy(path: Path | None, *, explicit: bool) -> RepoMapPolicy:
    if path is None or not path.is_file():
        if explicit:
            raise ValueError(f"policy file does not exist: {path}")
        return RepoMapPolicy()
    raw = path.read_bytes()
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid policy JSON: {exc.msg}") from exc
    if not isinstance(decoded, dict):
        raise ValueError("policy JSON must be an object")
    section = decoded.get("repo_map_policy")
    if section is None:
        if explicit:
            raise ValueError("policy JSON must define repo_map_policy")
        return RepoMapPolicy()
    if not isinstance(section, dict):
        raise ValueError("repo_map_policy must be an object")
    allowed = {
        "allow_paths", "deny_paths", "redact_paths", "max_files", "max_file_bytes",
        "timeout_seconds", "max_tokens",
    }
    unknown = sorted(str(key) for key in section.keys() - allowed)
    if unknown:
        raise ValueError(f"repo_map_policy has unknown fields: {', '.join(unknown)}")
    patterns: dict[str, tuple[str, ...]] = {}
    for field in ("allow_paths", "deny_paths", "redact_paths"):
        value = section.get(field, [])
        patterns[field] = _string_patterns(value, field)
    numeric: dict[str, int] = {}
    for field, default in (
        ("max_files", DEFAULT_MAX_FILES),
        ("max_file_bytes", DEFAULT_MAX_FILE_BYTES),
        ("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
    ):
        value = section.get(field, default)
        numeric[field] = _positive_int(value, field)
    max_tokens_value = section.get("max_tokens")
    max_tokens = (
        _positive_int(max_tokens_value, "max_tokens") if max_tokens_value is not None else None
    )
    return RepoMapPolicy(
        allow_paths=patterns["allow_paths"],
        deny_paths=patterns["deny_paths"],
        redact_paths=patterns["redact_paths"],
        max_files=numeric["max_files"],
        max_file_bytes=numeric["max_file_bytes"],
        timeout_seconds=numeric["timeout_seconds"],
        max_tokens=max_tokens,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


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


def build_map(
    repo: Path,
    commit: str,
    max_tokens: int,
    seeds: list[str],
    policy: RepoMapPolicy | None = None,
) -> str:
    effective_policy = policy or RepoMapPolicy()
    if max_tokens < 1:
        raise ValueError("--max-tokens must be positive")
    if effective_policy.max_tokens is not None and max_tokens > effective_policy.max_tokens:
        raise ValueError("--max-tokens exceeds repo_map_policy.max_tokens")
    pinned = (
        _git(repo, effective_policy.timeout_seconds, "rev-parse", "--verify", f"{commit}^{{commit}}")
        .decode()
        .strip()
    )
    raw_paths = _git(repo, effective_policy.timeout_seconds, "ls-tree", "-rz", "--name-only", pinned)
    paths = sorted(
        path.decode("utf-8", "surrogateescape")
        for path in raw_paths.split(b"\0")
        if path
    )
    paths = [path for path in paths if _allowed(path, effective_policy)]
    if len(paths) > effective_policy.max_files:
        raise ValueError(
            f"Repo Map has {len(paths)} policy-approved files, above repo_map_policy.max_files={effective_policy.max_files}"
        )
    python_paths = {path for path in paths if path.endswith(".py")}
    module_paths = _module_paths(python_paths)
    files: dict[str, FileRecord] = {}
    edges: list[EdgeRecord] = []
    diagnostics: list[Diagnostic] = []
    for path in paths:
        # Git object reads keep the working tree, including untracked files, outside the input.
        object_name = f"{pinned}:{path}"
        size = int(
            _git(repo, effective_policy.timeout_seconds, "cat-file", "-s", object_name)
            .decode()
            .strip()
        )
        if size > effective_policy.max_file_bytes:
            files[path] = {"path": path, "signatures": [], "parser_status": "too_large"}
            diagnostics.append({"code": "file_too_large", "path": path})
            continue
        content = _git(repo, effective_policy.timeout_seconds, "show", object_name)
        if b"\0" in content:
            continue
        signatures: list[str] = []
        parser_status: Literal["ok", "syntax_error", "invalid_encoding", "too_large"] = "ok"
        if path.endswith(".py"):
            try:
                tree = ast.parse(content.decode("utf-8"), filename=path)
            except UnicodeDecodeError:
                parser_status = "invalid_encoding"
                diagnostics.append({"code": "invalid_encoding", "path": path})
            except SyntaxError:
                parser_status = "syntax_error"
                diagnostics.append({"code": "syntax_error", "path": path})
            else:
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
        files[path] = {"path": path, "signatures": signatures, "parser_status": parser_status}

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
        "parser_provenance": {
            "policy_mode": "enforced" if effective_policy.enforced else "portable",
            "policy_sha256": effective_policy.sha256,
            "max_file_bytes": effective_policy.max_file_bytes,
            "max_files": effective_policy.max_files,
        },
        "files": [],
        "edges": [],
        "diagnostics": [],
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
        payload["diagnostics"] = [
            diagnostic for diagnostic in diagnostics if diagnostic["path"] in candidate
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
            payload["diagnostics"] = [
                diagnostic for diagnostic in diagnostics if diagnostic["path"] in selected
            ]
    return encoded


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--commit", required=True)
    parser.add_argument("--seed", action="append", default=[])
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--policy", type=Path)
    args = parser.parse_args()
    try:
        policy_path = args.policy or args.repo / ".harness" / "orchestration.json"
        policy = load_policy(policy_path, explicit=args.policy is not None)
        max_tokens = args.max_tokens or policy.max_tokens or DEFAULT_MAX_TOKENS
        sys.stdout.write(build_map(args.repo, args.commit, max_tokens, args.seed, policy))
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
