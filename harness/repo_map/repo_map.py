#!/usr/bin/env python3
"""Deterministic, offline Repo Map for a pinned Git commit."""

from __future__ import annotations

import argparse
import ast
import base64
import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from collections import deque
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal, TypedDict, cast

# `harness/bin/harness` copies this file verbatim into target projects as
# `.harness/repo_map/repo_map.py`. Alias `harness` to whichever of the two this
# file actually lives under so this standalone CLI has the same imports in both
# source and installed layouts. See docs/adr/0018.
_HARNESS_ROOT: Path = Path(__file__).resolve().parents[1]
_REPO_ROOT: Path = _HARNESS_ROOT.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if _HARNESS_ROOT.name != "harness":
    _spec = importlib.util.spec_from_file_location(
        "harness",
        _HARNESS_ROOT / "__init__.py",
        submodule_search_locations=[str(_HARNESS_ROOT)],
    )
    assert _spec is not None and _spec.loader is not None
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules["harness"] = _pkg
    _spec.loader.exec_module(_pkg)

from harness.errors import HarnessError, PolicyError, print_and_exit
from harness.repo_map import parser_bundle
from harness.token_estimator import TOKEN_ESTIMATOR_VERSION, estimate_tokens

DEFAULT_MAX_TOKENS = 4000
DEFAULT_MAX_FILES = 10_000
DEFAULT_MAX_FILE_BYTES = 2_000_000
DEFAULT_MAX_PATH_LENGTH = 4_096
DEFAULT_MAX_SYMBOL_LENGTH = 256
DEFAULT_MAX_SIGNATURE_LENGTH = 2_048
DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_PARSER_BUNDLE_TIMEOUT_SECONDS = 30
DEFAULT_PARSER_BUNDLE_MAX_OUTPUT_BYTES = 10_000_000
# Names defined in this many files are too common to provide useful references.
DEFINITION_FILE_FANOUT_THRESHOLD = 5
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
    redact_symbols: tuple[str, ...] = ()
    max_files: int = DEFAULT_MAX_FILES
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_path_length: int = DEFAULT_MAX_PATH_LENGTH
    max_symbol_length: int = DEFAULT_MAX_SYMBOL_LENGTH
    max_signature_length: int = DEFAULT_MAX_SIGNATURE_LENGTH
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_tokens: int | None = None
    tier: Literal["minimal", "reduced", "full"] = "reduced"
    parser_bundle_registry_paths: tuple[str, ...] = ()
    parser_bundle_timeout_seconds: int = DEFAULT_PARSER_BUNDLE_TIMEOUT_SECONDS
    parser_bundle_max_output_bytes: int = DEFAULT_PARSER_BUNDLE_MAX_OUTPUT_BYTES
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
        raise ValueError(
            f"git {' '.join(args)} timed out after {timeout_seconds} seconds"
        ) from exc
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
        marker in part.casefold() for part in parts for marker in SENSITIVE_PATH_PARTS
    ):
        return False
    if ".generated." in name.casefold():
        return False
    if len(path) > policy.max_path_length:
        return False
    if policy.allow_paths and not _matches(path, policy.allow_paths):
        return False
    if _matches(path, policy.deny_paths) or _matches(path, policy.redact_paths):
        return False
    return not any(name.endswith(suffix) for suffix in EXCLUDED_SUFFIXES)


def _string_patterns(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise PolicyError(
            f"repo_map_policy.{field} must be a list of non-empty path globs",
            remedy=f"set repo_map_policy.{field} to a list of non-empty glob strings",
        )
    return tuple(sorted(set(value)))


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PolicyError(
            f"repo_map_policy.{field} must be a positive integer",
            remedy=f"set repo_map_policy.{field} to a positive integer in the project orchestration config",
        )
    return value


def _policy_error(message: str, remedy: str) -> PolicyError:
    return PolicyError(message, remedy=remedy)


def load_policy(path: Path | None, *, explicit: bool) -> RepoMapPolicy:
    if path is None or not path.is_file():
        if explicit:
            raise _policy_error(
                f"policy file does not exist: {path}",
                "create the policy file or omit --policy to use portable defaults",
            )
        return RepoMapPolicy()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _policy_error(
            f"policy file cannot be read: {path}",
            "fix the policy file permissions or path, then retry",
        ) from exc
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _policy_error(
            f"invalid policy JSON: {exc.msg}",
            "fix the JSON syntax in the policy file and retry",
        ) from exc
    if not isinstance(decoded, dict):
        raise _policy_error(
            "policy JSON must be an object",
            "rewrite the policy file as a JSON object containing repo_map_policy",
        )
    section = decoded.get("repo_map_policy")
    if section is None:
        if explicit:
            raise _policy_error(
                "policy JSON must define repo_map_policy",
                "add a repo_map_policy object or omit --policy to use portable defaults",
            )
        return RepoMapPolicy()
    if not isinstance(section, dict):
        raise _policy_error(
            "repo_map_policy must be an object",
            "set repo_map_policy to an object that follows orchestration.schema.json",
        )
    allowed = {
        "allow_paths",
        "deny_paths",
        "redact_paths",
        "redact_symbols",
        "max_files",
        "max_file_bytes",
        "max_path_length",
        "max_symbol_length",
        "max_signature_length",
        "timeout_seconds",
        "max_tokens",
        "tier",
        "parser_bundle_registry_paths",
        "parser_bundle_timeout_seconds",
        "parser_bundle_max_output_bytes",
    }
    unknown = sorted(str(key) for key in section.keys() - allowed)
    if unknown:
        raise _policy_error(
            f"repo_map_policy has unknown fields: {', '.join(unknown)}",
            "remove unknown repo_map_policy fields and follow orchestration.schema.json",
        )
    patterns: dict[str, tuple[str, ...]] = {}
    for field in ("allow_paths", "deny_paths", "redact_paths", "redact_symbols"):
        value = section.get(field, [])
        patterns[field] = _string_patterns(value, field)
    registry_paths_value = section.get("parser_bundle_registry_paths", [])
    if not isinstance(registry_paths_value, list) or any(
        not isinstance(item, str) or not item for item in registry_paths_value
    ):
        raise PolicyError(
            "repo_map_policy.parser_bundle_registry_paths must be a list of non-empty strings",
            remedy="set repo_map_policy.parser_bundle_registry_paths to a list of non-empty path strings",
        )
    registry_paths = tuple(registry_paths_value)
    numeric: dict[str, int] = {}
    for field, default in (
        ("max_files", DEFAULT_MAX_FILES),
        ("max_file_bytes", DEFAULT_MAX_FILE_BYTES),
        ("max_path_length", DEFAULT_MAX_PATH_LENGTH),
        ("max_symbol_length", DEFAULT_MAX_SYMBOL_LENGTH),
        ("max_signature_length", DEFAULT_MAX_SIGNATURE_LENGTH),
        ("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
        ("parser_bundle_timeout_seconds", DEFAULT_PARSER_BUNDLE_TIMEOUT_SECONDS),
        ("parser_bundle_max_output_bytes", DEFAULT_PARSER_BUNDLE_MAX_OUTPUT_BYTES),
    ):
        value = section.get(field, default)
        numeric[field] = _positive_int(value, field)
    max_tokens_value = section.get("max_tokens")
    max_tokens = (
        _positive_int(max_tokens_value, "max_tokens")
        if max_tokens_value is not None
        else None
    )
    tier = section.get("tier", "reduced")
    if not isinstance(tier, str) or tier not in {"minimal", "reduced", "full"}:
        raise _policy_error(
            "repo_map_policy.tier must be one of: minimal, reduced, full",
            "set repo_map_policy.tier to 'minimal', 'reduced', or 'full'",
        )
    tier = cast(Literal["minimal", "reduced", "full"], tier)
    return RepoMapPolicy(
        allow_paths=patterns["allow_paths"],
        deny_paths=patterns["deny_paths"],
        redact_paths=patterns["redact_paths"],
        redact_symbols=patterns["redact_symbols"],
        parser_bundle_registry_paths=registry_paths,
        parser_bundle_timeout_seconds=numeric["parser_bundle_timeout_seconds"],
        parser_bundle_max_output_bytes=numeric["parser_bundle_max_output_bytes"],
        max_files=numeric["max_files"],
        max_file_bytes=numeric["max_file_bytes"],
        max_path_length=numeric["max_path_length"],
        max_symbol_length=numeric["max_symbol_length"],
        max_signature_length=numeric["max_signature_length"],
        timeout_seconds=numeric["timeout_seconds"],
        max_tokens=max_tokens,
        tier=tier,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _symbol_visible(name: str, policy: RepoMapPolicy) -> bool:
    return len(name) <= policy.max_symbol_length and not _matches(
        name, policy.redact_symbols
    )


def _signature_parts_visible(
    parts: tuple[ast.AST | None, ...], policy: RepoMapPolicy
) -> bool:
    """Reject a signature when any serialized AST name would bypass symbol policy."""
    for part in parts:
        if part is None:
            continue
        for node in ast.walk(part):
            if isinstance(node, ast.arg) and not _symbol_visible(node.arg, policy):
                return False
            if isinstance(node, ast.Name) and not _symbol_visible(node.id, policy):
                return False
            if isinstance(node, ast.Attribute) and not _symbol_visible(
                node.attr, policy
            ):
                return False
            if (
                isinstance(node, ast.keyword)
                and node.arg is not None
                and not _symbol_visible(node.arg, policy)
            ):
                return False
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if not _symbol_visible(node.value, policy):
                    return False
    return True


def _signatures(tree: ast.Module, policy: RepoMapPolicy) -> list[str]:
    found: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not _symbol_visible(node.name, policy):
                continue
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            annotation = f" -> {ast.unparse(node.returns)}" if node.returns else ""
            args = copy.deepcopy(node.args)
            args.defaults = [ast.Constant(value=Ellipsis) for _ in args.defaults]
            args.kw_defaults = [
                ast.Constant(value=Ellipsis) if item is not None else None
                for item in args.kw_defaults
            ]
            if not _signature_parts_visible((args, node.returns), policy):
                continue
            signature = f"{prefix} {node.name}({ast.unparse(args)}){annotation}"
            if len(signature) <= policy.max_signature_length:
                found.append(signature)
        elif isinstance(node, ast.ClassDef):
            if not _symbol_visible(node.name, policy):
                continue
            if not _signature_parts_visible(
                tuple(node.bases) + tuple(node.keywords), policy
            ):
                continue
            bases = ", ".join(ast.unparse(base) for base in node.bases)
            signature = f"class {node.name}({bases})" if bases else f"class {node.name}"
            if len(signature) <= policy.max_signature_length:
                found.append(signature)
    return found


def _defined_names(tree: ast.Module, policy: RepoMapPolicy) -> set[str]:
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and _symbol_visible(node.name, policy)
    }


def _referenced_names(tree: ast.Module, policy: RepoMapPolicy) -> set[str]:
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and _symbol_visible(node.id, policy)
    }


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


def _module_visible(module: str, policy: RepoMapPolicy) -> bool:
    """Apply symbol length/redaction to a dotted import before exposing its edge."""
    parts = tuple(part for part in module.split(".") if part)
    return _symbol_visible(module, policy) and all(
        _symbol_visible(part, policy) for part in parts
    )


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


def _apply_parser_bundle(
    repo: Path,
    policy: RepoMapPolicy,
    files: dict[str, dict[str, object]],
    bundle_candidate_content: dict[str, bytes],
) -> tuple[
    Literal["minimal", "reduced", "full"],
    Literal["path-only", "ast-only", "bundle"],
    str,
    dict[str, object],
]:
    """Try the opt-in `full` tier's offline parser bundle; degrade to `reduced`/`ast-only` on any failure.

    Never raises and never touches the network: every branch below is either a local filesystem
    read, an offline `pip install --no-index --target` subprocess, or a bounded local subprocess
    call into the bundle's own worker script (see harness/repo_map/parser_bundle.py).
    """
    bundle_result = parser_bundle.acquire_bundle(
        repo=repo,
        registry_paths=policy.parser_bundle_registry_paths,
        python_executable=sys.executable,
        timeout_seconds=policy.parser_bundle_timeout_seconds,
    )
    if isinstance(bundle_result, str):
        provenance = parser_bundle.build_provenance(
            None,
            bundle_mode="degraded",
            bundle_source="none",
            python_tag=None,
            platform_tag=None,
        )
        return "reduced", "ast-only", bundle_result, provenance
    eligible = {
        path: content
        for path, content in bundle_candidate_content.items()
        if Path(path).suffix in bundle_result.worker_extensions
    }
    request: dict[str, object] = {
        "paths": {
            path: base64.b64encode(content).decode("ascii")
            for path, content in eligible.items()
        }
    }
    parse_result = parser_bundle.run_bundle_parser(
        sys.executable,
        bundle_result.worker_script,
        bundle_result.install_dir,
        request,
        timeout_seconds=policy.parser_bundle_timeout_seconds,
        max_output_bytes=policy.parser_bundle_max_output_bytes,
        expected_script_sha256=bundle_result.lock.script_sha256,
    )
    if isinstance(parse_result, str):
        provenance = parser_bundle.build_provenance(
            bundle_result.lock,
            bundle_mode="degraded",
            bundle_source=bundle_result.bundle_source,
            python_tag=bundle_result.python_tag,
            platform_tag=bundle_result.platform_tag,
        )
        return "reduced", "ast-only", parse_result, provenance
    for path, record in parse_result["files"].items():
        if path in files:
            files[path] = {"path": path, **record}
    provenance = parser_bundle.build_provenance(
        bundle_result.lock,
        bundle_mode="applied",
        bundle_source=bundle_result.bundle_source,
        python_tag=bundle_result.python_tag,
        platform_tag=bundle_result.platform_tag,
    )
    return "full", "bundle", "parser bundle applied", provenance


def build_map(
    repo: Path,
    commit: str,
    max_tokens: int,
    seeds: list[str],
    policy: RepoMapPolicy | None = None,
) -> str:
    effective_policy = policy or RepoMapPolicy()
    if max_tokens < 1:
        raise PolicyError(
            "--max-tokens must be positive",
            remedy="pass a positive integer to --max-tokens",
        )
    if (
        effective_policy.max_tokens is not None
        and max_tokens > effective_policy.max_tokens
    ):
        raise PolicyError(
            "--max-tokens exceeds repo_map_policy.max_tokens",
            remedy="lower --max-tokens or raise repo_map_policy.max_tokens in the project config",
        )
    pinned = (
        _git(
            repo,
            effective_policy.timeout_seconds,
            "rev-parse",
            "--verify",
            f"{commit}^{{commit}}",
        )
        .decode()
        .strip()
    )
    raw_paths = _git(
        repo, effective_policy.timeout_seconds, "ls-tree", "-rz", "--name-only", pinned
    )
    paths = sorted(
        path.decode("utf-8", "surrogateescape")
        for path in raw_paths.split(b"\0")
        if path
    )
    paths = [path for path in paths if _allowed(path, effective_policy)]
    if len(paths) > effective_policy.max_files:
        raise PolicyError(
            f"Repo Map has {len(paths)} policy-approved files, above repo_map_policy.max_files={effective_policy.max_files}",
            remedy="narrow repo_map_policy.allow_paths or raise repo_map_policy.max_files deliberately",
        )
    files: dict[str, dict[str, object]] = {}
    edges: list[EdgeRecord] = []
    diagnostics: list[Diagnostic] = []
    parsed_trees: dict[str, ast.Module] = {}
    bundle_candidate_content: dict[str, bytes] = {}
    if effective_policy.tier in ("reduced", "full"):
        python_paths = {path for path in paths if path.endswith(".py")}
        module_paths = _module_paths(python_paths)
        for path in paths:
            # Git object reads keep the working tree, including untracked files, outside the input.
            object_name = f"{pinned}:{path}"
            size = int(
                _git(
                    repo,
                    effective_policy.timeout_seconds,
                    "cat-file",
                    "-s",
                    object_name,
                )
                .decode()
                .strip()
            )
            if size > effective_policy.max_file_bytes:
                files[path] = {
                    "path": path,
                    "signatures": [],
                    "parser_status": "too_large",
                }
                diagnostics.append({"code": "file_too_large", "path": path})
                continue
            content = _git(repo, effective_policy.timeout_seconds, "show", object_name)
            if b"\0" in content:
                continue
            signatures: list[str] = []
            parser_status: Literal[
                "ok", "syntax_error", "invalid_encoding", "too_large"
            ] = "ok"
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
                    parsed_trees[path] = tree
                    signatures = _signatures(tree, effective_policy)
                    current_module = path.removesuffix(".py").replace("/", ".")
                    for node in ast.walk(tree):
                        if not isinstance(node, ast.stmt):
                            continue
                        for imported_module in _import_modules(node, current_module):
                            if not _module_visible(imported_module, effective_policy):
                                continue
                            target = module_paths.get(imported_module)
                            target_module = (
                                target.removesuffix(".py").replace("/", ".")
                                if target
                                else ""
                            )
                            if (
                                target
                                and target != path
                                and _module_visible(target_module, effective_policy)
                            ):
                                import_edge: EdgeRecord = {
                                    "source": path,
                                    "target": target,
                                    "kind": "import",
                                    "confidence": "high",
                                }
                                if import_edge not in edges:
                                    edges.append(import_edge)
            elif effective_policy.tier == "full":
                bundle_candidate_content[path] = content
            files[path] = {
                "path": path,
                "signatures": signatures,
                "parser_status": parser_status,
            }

        definitions: dict[str, set[str]] = {}
        for path, tree in parsed_trees.items():
            for name in _defined_names(tree, effective_policy):
                definitions.setdefault(name, set()).add(path)
        for path, tree in parsed_trees.items():
            for name in _referenced_names(tree, effective_policy):
                definition_paths = definitions.get(name, set())
                if len(definition_paths) >= DEFINITION_FILE_FANOUT_THRESHOLD:
                    continue
                targets = sorted(definition_paths - {path})
                if not targets:
                    continue
                kind: Literal["unique-name-ref", "ambiguous-name-ref"]
                confidence: Literal["medium", "low"]
                kind, confidence = (
                    ("unique-name-ref", "medium")
                    if len(targets) == 1
                    else ("ambiguous-name-ref", "low")
                )
                for target in targets:
                    reference_edge: EdgeRecord = {
                        "source": path,
                        "target": target,
                        "kind": kind,
                        "confidence": confidence,
                    }
                    if reference_edge not in edges:
                        edges.append(reference_edge)

        edges.sort(
            key=lambda edge: (
                edge["source"],
                edge["target"],
                edge["kind"],
                edge["confidence"],
            )
        )
    else:
        files = {path: {"path": path} for path in paths}
    indegree = {path: 0 for path in files}
    for edge in edges:
        if edge["confidence"] != "low" and edge["target"] in indegree:
            indegree[edge["target"]] += 1
    effective_seeds = sorted(set(seeds) & files.keys())
    distances: dict[str, int] = {}
    queue = deque(effective_seeds)
    for path in queue:
        distances[path] = 0
    neighbors: dict[str, set[str]] = {path: set() for path in files}
    for edge in edges:
        if (
            edge["confidence"] != "low"
            and edge["source"] in neighbors
            and edge["target"] in neighbors
        ):
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
        if effective_seeds
        else (lambda path: (-indegree[path], path)),
    )
    tier: Literal["minimal", "reduced", "full"] = effective_policy.tier
    parser: Literal["path-only", "ast-only", "bundle"] = (
        "path-only" if tier == "minimal" else "ast-only"
    )
    degradation_reason = (
        "policy requested minimal tier"
        if tier == "minimal"
        else "offline parser bundle unavailable"
    )
    bundle_provenance: dict[str, object] = {}
    if effective_policy.tier == "full":
        tier, parser, degradation_reason, bundle_provenance = _apply_parser_bundle(
            repo, effective_policy, files, bundle_candidate_content
        )
    payload: dict[str, object] = {
        "schema_version": 1,
        "commit": pinned,
        "tier": tier,
        "parser": parser,
        "degradation_reason": degradation_reason,
        "token_estimator_version": TOKEN_ESTIMATOR_VERSION,
        "parser_provenance": {
            "policy_mode": "enforced" if effective_policy.enforced else "portable",
            "policy_sha256": effective_policy.sha256,
            "policy_tier": effective_policy.tier,
            "max_file_bytes": effective_policy.max_file_bytes,
            "max_files": effective_policy.max_files,
            "max_path_length": effective_policy.max_path_length,
            "max_symbol_length": effective_policy.max_symbol_length,
            "max_signature_length": effective_policy.max_signature_length,
            "timeout_seconds": effective_policy.timeout_seconds,
            "max_tokens": max_tokens,
            **bundle_provenance,
        },
        "files": [],
        "edges": [],
        "diagnostics": [],
        "estimated_tokens": 0,
    }
    encoded, size = _sized(payload)
    if size > max_tokens:
        raise PolicyError(
            "--max-tokens is too small for Repo Map metadata",
            remedy="raise --max-tokens or use a smaller policy tier and limits",
        )
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
                diagnostic
                for diagnostic in diagnostics
                if diagnostic["path"] in selected
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
        if args.max_tokens is not None:
            max_tokens = args.max_tokens
        elif policy.max_tokens is not None:
            max_tokens = policy.max_tokens
        else:
            max_tokens = DEFAULT_MAX_TOKENS
        sys.stdout.write(
            build_map(args.repo, args.commit, max_tokens, args.seed, policy)
        )
    except HarnessError as exc:
        return print_and_exit(exc)
    except ValueError as exc:
        return print_and_exit(
            PolicyError(
                str(exc),
                remedy="inspect the Repo Map input and project policy, then retry",
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
