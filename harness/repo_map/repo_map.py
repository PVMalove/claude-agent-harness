#!/usr/bin/env python3
"""Deterministic, offline Repo Map for a pinned Git commit."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import posixpath
import subprocess
import sys
import tempfile
from collections import deque
from dataclasses import asdict, dataclass
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
from harness.storage import storage_path
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
JS_EXTENSIONS = frozenset({".ts", ".tsx", ".js", ".jsx"})
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
    tier: Literal["minimal", "full"] = "full"
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
    tier = section.get("tier", "full")
    if not isinstance(tier, str) or tier not in {"minimal", "full"}:
        raise _policy_error(
            "repo_map_policy.tier must be one of: full, minimal",
            "set repo_map_policy.tier to 'full' or 'minimal'",
        )
    tier = cast(Literal["minimal", "full"], tier)
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


def _visible_signatures(
    signatures: list[parser_bundle.SignatureFact], policy: RepoMapPolicy
) -> list[str]:
    """Keep a signature only when every symbol it serializes passes symbol policy."""
    return [
        signature["text"]
        for signature in signatures
        if len(signature["text"]) <= policy.max_signature_length
        and all(_symbol_visible(symbol, policy) for symbol in signature["symbols"])
    ]


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


def _import_modules(fact: parser_bundle.ImportFact, module: str) -> list[str]:
    """Candidate dotted modules for one import: the base module and each `from` name under it."""
    level = fact["level"]
    parent = module.rsplit(".", level)[0] if level else ""
    base = ".".join(part for part in (parent, fact["module"]) if part)
    if not base:
        return []
    return [base, *(f"{base}.{name}" for name in fact["names"] if name != "*")]


def _module_visible(module: str, policy: RepoMapPolicy) -> bool:
    """Apply symbol length/redaction to a dotted import before exposing its edge."""
    parts = tuple(part for part in module.split(".") if part)
    return _symbol_visible(module, policy) and all(
        _symbol_visible(part, policy) for part in parts
    )


def _js_import_target(source: str, specifier: str, paths: set[str]) -> str | None:
    """Resolve a tracked relative source import; never infer packages or TS aliases."""
    if not specifier.startswith(("./", "../")):
        return None
    base = posixpath.normpath(posixpath.join(posixpath.dirname(source), specifier))
    if base == ".." or base.startswith("../") or base.startswith("/"):
        return None
    candidates = [base]
    if Path(base).suffix not in JS_EXTENSIONS:
        candidates.extend(f"{base}{extension}" for extension in (".ts", ".tsx", ".js", ".jsx"))
        candidates.extend(f"{base}/index{extension}" for extension in (".ts", ".tsx", ".js", ".jsx"))
    return next((candidate for candidate in candidates if candidate in paths), None)


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


@dataclass(frozen=True)
class _BundleParse:
    """The outcome of the `full` tier: parsed file records and facts, or a degradation reason."""

    reason: str
    provenance: dict[str, object]
    records: dict[str, dict[str, object]]
    facts: dict[str, parser_bundle.FileFacts]
    diagnostics: list[Diagnostic]

    @property
    def applied(self) -> bool:
        return self.reason == "parser bundle applied"


def _parse_with_bundle(
    repo: Path, commit: str, paths: list[str], policy: RepoMapPolicy
) -> _BundleParse:
    """Parse every supported file with the verified offline parser bundle, or degrade.

    Every language, Python included, goes through the bundle's tree-sitter worker: there is no
    in-process parser to fall back to, so any failure yields a reason for the `minimal` tier.
    Never touches the network: every step is a local filesystem read, an offline
    `uv pip install --offline --no-index --target`, or a bounded local subprocess into the bundle's worker
    script (see harness/repo_map/parser_bundle.py). The bundle is located before any file content
    is read, so a degraded run reads only paths.
    """
    bundle = parser_bundle.acquire_bundle(
        repo=repo,
        registry_paths=policy.parser_bundle_registry_paths,
        python_executable=sys.executable,
        timeout_seconds=policy.parser_bundle_timeout_seconds,
    )
    if isinstance(bundle, str):
        provenance = parser_bundle.build_provenance(
            None,
            bundle_mode="degraded",
            bundle_source="none",
            python_tag=None,
            platform_tag=None,
        )
        return _BundleParse(bundle, provenance, {}, {}, [])
    records: dict[str, dict[str, object]] = {}
    diagnostics: list[Diagnostic] = []
    request_paths: dict[str, str] = {}
    for path in paths:
        # Git object reads keep the working tree, including untracked files, outside the input.
        object_name = f"{commit}:{path}"
        size = int(
            _git(repo, policy.timeout_seconds, "cat-file", "-s", object_name)
            .decode()
            .strip()
        )
        if size > policy.max_file_bytes:
            records[path] = {
                "path": path,
                "signatures": [],
                "parser_status": "too_large",
            }
            diagnostics.append({"code": "file_too_large", "path": path})
            continue
        content = _git(repo, policy.timeout_seconds, "show", object_name)
        if b"\0" in content:
            continue
        records[path] = {"path": path, "signatures": [], "parser_status": "ok"}
        if Path(path).suffix in bundle.worker_extensions:
            request_paths[path] = base64.b64encode(content).decode("ascii")
    parse_result = parser_bundle.run_bundle_parser(
        sys.executable,
        bundle.worker_script,
        bundle.install_dir,
        {"paths": request_paths},
        timeout_seconds=policy.parser_bundle_timeout_seconds,
        max_output_bytes=policy.parser_bundle_max_output_bytes,
        expected_script_sha256=bundle.lock.script_sha256,
    )
    applied = not isinstance(parse_result, str)
    provenance = parser_bundle.build_provenance(
        bundle.lock,
        bundle_mode="applied" if applied else "degraded",
        bundle_source=bundle.bundle_source,
        python_tag=bundle.python_tag,
        platform_tag=bundle.platform_tag,
    )
    if isinstance(parse_result, str):
        return _BundleParse(parse_result, provenance, {}, {}, [])
    facts = {
        path: record
        for path, record in parse_result["files"].items()
        if path in request_paths
    }
    return _BundleParse(
        "parser bundle applied", provenance, records, facts, diagnostics
    )


def _graph_from_facts(
    records: dict[str, dict[str, object]],
    facts: dict[str, parser_bundle.FileFacts],
    diagnostics: list[Diagnostic],
    policy: RepoMapPolicy,
) -> list[EdgeRecord]:
    """Apply symbol policy to worker facts, fill in file records, and build the edge list."""
    module_paths = _module_paths({path for path in records if path.endswith(".py")})
    edges: set[tuple[str, str, str, str]] = set()
    definitions: dict[tuple[str, str], set[str]] = {}
    js_paths = {path for path in records if Path(path).suffix in JS_EXTENSIONS}
    for path in sorted(facts):
        record = facts[path]
        status = record["parser_status"]
        records[path]["parser_status"] = status
        if status != "ok":
            diagnostics.append({"code": status, "path": path})
        records[path]["signatures"] = _visible_signatures(record["signatures"], policy)
        family = "python" if path.endswith(".py") else "js"
        for name in record["definitions"]:
            if _symbol_visible(name, policy):
                definitions.setdefault((family, name), set()).add(path)
        if path in js_paths:
            for import_fact in record["imports"]:
                specifier = import_fact["module"]
                if not _symbol_visible(specifier, policy) or not all(
                    _symbol_visible(part, policy)
                    for part in posixpath.splitext(specifier)[0].split("/")
                    if part not in {".", ".."}
                ):
                    continue
                target = _js_import_target(path, specifier, js_paths)
                if target is not None and target != path:
                    edges.add((path, target, "import", "high"))
            continue
        if not path.endswith(".py"):
            continue
        current_module = path.removesuffix(".py").replace("/", ".")
        for import_fact in record["imports"]:
            for imported_module in _import_modules(import_fact, current_module):
                if not _module_visible(imported_module, policy):
                    continue
                target = module_paths.get(imported_module)
                if (
                    target
                    and target != path
                    and _module_visible(
                        target.removesuffix(".py").replace("/", "."), policy
                    )
                ):
                    edges.add((path, target, "import", "high"))
    for path in sorted(facts):
        family = "python" if path.endswith(".py") else "js"
        for name in set(facts[path]["references"]):
            if not _symbol_visible(name, policy):
                continue
            definition_paths = definitions.get((family, name), set())
            if len(definition_paths) >= DEFINITION_FILE_FANOUT_THRESHOLD:
                continue
            targets = definition_paths - {path}
            kind, confidence = (
                ("unique-name-ref", "medium")
                if len(targets) == 1
                else ("ambiguous-name-ref", "low")
            )
            for target in targets:
                edges.add((path, target, kind, confidence))
    diagnostics.sort(key=lambda item: (item["path"], item["code"]))
    return [
        {"source": source, "target": target, "kind": kind, "confidence": confidence}
        for source, target, kind, confidence in sorted(edges)
    ]


def _cache_key(
    pinned: str,
    seeds: list[str],
    max_tokens: int,
    policy: RepoMapPolicy,
) -> str:
    """Hash every input that can affect a serialized Repo Map."""
    parser_identity = hashlib.sha256(
        Path(__file__).read_bytes()
        + Path(parser_bundle.__file__).read_bytes()
        + (_HARNESS_ROOT / "repo_map" / "tree_sitter_worker.py").read_bytes()
    ).hexdigest()
    identity = {
        "commit": pinned,
        "seeds": seeds,
        "max_tokens": max_tokens,
        "policy": asdict(policy),
        "parser_identity": parser_identity,
        "token_estimator_version": TOKEN_ESTIMATOR_VERSION,
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _read_cache(cache_dir: Path, key: str) -> str | None:
    """Return a verified entry; malformed or tampered entries are cache misses."""
    try:
        envelope = json.loads((cache_dir / f"{key}.json").read_text(encoding="utf-8"))
        if not isinstance(envelope, dict):
            return None
        payload = envelope.get("payload")
        digest = envelope.get("sha256")
        if not isinstance(payload, str) or not isinstance(digest, str):
            return None
        if hashlib.sha256(payload.encode("utf-8")).hexdigest() != digest:
            return None
        return payload
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _write_cache(cache_dir: Path, key: str, payload: str) -> None:
    """Best-effort atomic cache write: the cache must never become authoritative."""
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        envelope = json.dumps(
            {
                "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                "payload": payload,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        descriptor, temporary = tempfile.mkstemp(
            dir=cache_dir, prefix=f".{key}.", suffix=".tmp"
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(envelope)
        Path(temporary).replace(cache_dir / f"{key}.json")
    except OSError:
        return


def _build_map(
    repo: Path,
    pinned: str,
    paths: list[str],
    max_tokens: int,
    seeds: list[str],
    effective_policy: RepoMapPolicy,
) -> str:
    files: dict[str, dict[str, object]] = {path: {"path": path} for path in paths}
    edges: list[EdgeRecord] = []
    diagnostics: list[Diagnostic] = []
    tier: Literal["minimal", "full"] = "minimal"
    parser: Literal["path-only", "bundle"] = "path-only"
    degradation_reason = "policy requested minimal tier"
    bundle_provenance: dict[str, object] = {}
    if effective_policy.tier == "full":
        parsed = _parse_with_bundle(repo, pinned, paths, effective_policy)
        degradation_reason = parsed.reason
        bundle_provenance = parsed.provenance
        if parsed.applied:
            tier, parser = "full", "bundle"
            files = parsed.records
            diagnostics = parsed.diagnostics
            edges = _graph_from_facts(
                files, parsed.facts, diagnostics, effective_policy
            )
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


def build_map(
    repo: Path,
    commit: str,
    max_tokens: int,
    seeds: list[str],
    policy: RepoMapPolicy | None = None,
    *,
    cache_dir: Path | None = None,
) -> str:
    """Build a Repo Map, reusing a verified local content-addressed entry when possible."""
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
    normalized_seeds = sorted(set(seeds) & set(paths))
    root = cache_dir if cache_dir is not None else storage_path(repo, ".cache", "repo_map", "results")
    key = _cache_key(pinned, normalized_seeds, max_tokens, effective_policy)
    cached = _read_cache(root, key)
    if cached is not None:
        return cached
    result = _build_map(
        repo, pinned, paths, max_tokens, normalized_seeds, effective_policy
    )
    _write_cache(root, key, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--commit", required=True)
    parser.add_argument("--seed", action="append", default=[])
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--cache-dir", type=Path)
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
            build_map(
                args.repo,
                args.commit,
                max_tokens,
                args.seed,
                policy,
                cache_dir=args.cache_dir,
            )
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
