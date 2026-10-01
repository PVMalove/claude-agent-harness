"""Bounded local ingestion after explicit type and path authorization."""

from __future__ import annotations

import fnmatch
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .adapters import STATE, baseline, classify, project_json, selected_generation
from .policy import Policy

MAX_SOURCE_BYTES = 1024 * 1024
MAX_SOURCES = 1000
MAX_ENTRIES = 10000
EXCLUDED = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "vendor",
    "dist",
    "build",
    "logs",
    "qa-artifacts",
    "cache",
}
STATE_SOURCE_TYPES = {"ledger", "qa_finding", "completion_report"}


@dataclass(frozen=True)
class Source:
    """Sanitized cache document with original-byte provenance."""

    source_type: str
    title: str
    status: str
    date: str
    superseded_by: str
    path: str
    source_hash: str
    body: str


def source_type(path: str) -> str:
    """Classify reserved namespaces before generic Markdown."""
    return classify(path)


def safe_source(repo: Path, relative: str) -> Path:
    """Reject traversal and symlink components before touching contents."""
    if Path(relative).is_absolute() or any(
        p in {".", ".."} for p in relative.split("/")
    ):
        raise ValueError("memory source escaped project")
    path = repo
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("memory source must not be a symlink")
    if not path.resolve().is_relative_to(repo.resolve()):
        raise ValueError("memory source escaped project")
    return path


def raw_bytes(repo: Path, relative: str) -> bytes:
    """Read bounded original bytes for parsing and content-hash provenance."""
    with safe_source(repo, relative).open("rb") as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("memory source exceeds 1 MiB")
    return raw


def metadata(text: str, names: str, default: str) -> str:
    """Explicit line fields take priority over named sections; never infer prose."""
    match = re.search(
        rf"(?im)^\s*(?:\*\*)?(?:{names})(?:\*\*)?\s*:\s*([^\n]+?)\s*$", text
    )
    if not match:
        match = re.search(rf"(?im)^##\s+(?:{names})\s*\n\s*([^\n]+)", text)
    return match.group(1).strip(" *\"'") if match else default


def read_source(
    repo: Path, relative: str, policy: Policy, *, raw: bytes | None = None
) -> Source | None:
    """Project and sanitize an authorized source before retaining any text."""
    kind = classify(relative)
    completion = kind == "qa_finding" and "completion_report" in policy.source_types
    if (kind not in policy.source_types and not completion) or not matches(
        relative, policy.allow_paths
    ):
        return None
    if not safe_source(repo, relative).is_file():
        return None
    raw = raw_bytes(repo, relative) if raw is None else raw
    if kind in STATE_SOURCE_TYPES:
        projection = None
        if completion:
            projection = project_json(
                raw, "completion_report", include_qa="qa_finding" in policy.source_types
            )
            if projection is not None:
                kind = "completion_report"
        if projection is None and kind in policy.source_types:
            projection = project_json(raw, kind)
        if projection is None:
            return None
        title, status, date, superseded, text = projection
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("memory source must be UTF-8") from None
        status = metadata(text, "status|статус", "unknown")
        date = metadata(text, "date|дата", "unknown")
        superseded = metadata(text, "superseded[-_]by|заменён на|заменен на", "")
        match = re.search(r"(?m)^#\s+(.+?)\s*$", text)
        title = match.group(1) if match else "untitled"
    if (
        status.lower() in {"superseded", "заменён", "заменен"}
        or re.match(r"superseded\s+by\s+\S+", status, flags=re.IGNORECASE)
    ):
        return None

    def sanitize(value: str) -> str:
        if kind in STATE_SOURCE_TYPES:
            value = baseline(value)
        for rule in policy.redact_rules:
            value = re.sub(rule, "[REDACTED]", value)
        return value

    # Identity must remain a usable pointer; deny a path that sanitization would alter.
    if baseline(relative) != relative or sanitize(relative) != relative:
        raise ValueError("memory source path cannot be safely retained")
    return Source(
        kind,
        sanitize(title),
        sanitize(status).lower().replace("[redacted]", "[REDACTED]"),
        sanitize(date),
        sanitize(superseded),
        relative,
        hashlib.sha256(raw).hexdigest(),
        sanitize(text),
    )


def matches(relative: str, patterns: tuple[str, ...]) -> bool:
    """Match POSIX glob components, allowing ** to consume zero or more directories."""
    parts = relative.split("/")

    def match(items: list[str], pattern: list[str]) -> bool:
        if not pattern:
            return not items
        if pattern[0] == "**":
            return (
                match(items, pattern[1:]) or bool(items) and match(items[1:], pattern)
            )
        return (
            bool(items)
            and fnmatch.fnmatchcase(items[0], pattern[0])
            and match(items[1:], pattern[1:])
        )

    return any(match(parts, pattern.split("/")) for pattern in patterns)


def allowed_paths(repo: Path, policy: Policy) -> list[str]:
    """Bound discovery before parsing, restricted to selected authorized namespaces."""
    if not policy.active:
        return []
    generation = (
        selected_generation(repo)
        if policy.active and STATE_SOURCE_TYPES & set(policy.source_types)
        else ""
    )
    paths: set[str] = set()
    visited: set[str] = set()

    def walk(path: Path) -> None:
        relative = path.relative_to(repo).as_posix()
        if relative in visited:
            return
        visited.add(relative)
        if len(visited) > MAX_ENTRIES:
            raise ValueError("memory traversal exceeds 10000 entries")
        if path.is_symlink():
            raise ValueError("memory source must not be a symlink")
        if path.is_dir():
            if path.name in EXCLUDED:
                return
            if relative == ".harness" or relative.startswith(".harness/"):
                if not generation or not (
                    generation.startswith(relative + "/")
                    or relative == generation
                    or relative.startswith(generation + "/")
                ):
                    return
                if relative.startswith(generation + "/") and path.name not in {
                    "reports",
                    "batches",
                    "dispatches",
                    "dispatch-status",
                }:
                    return
            for child in sorted(path.iterdir()):
                walk(child)
        elif matches(relative, policy.allow_paths) and (
            classify(relative) in policy.source_types
            or classify(relative) == "qa_finding"
            and "completion_report" in policy.source_types
        ):
            if relative.startswith(STATE + "/") and not relative.startswith(
                generation + "/"
            ):
                return
            sanitized = baseline(relative)
            for rule in policy.redact_rules:
                sanitized = re.sub(rule, "[REDACTED]", sanitized)
            if sanitized != relative:
                raise ValueError("memory source path cannot be safely retained")
            paths.add(relative)
            if len(paths) > MAX_SOURCES:
                raise ValueError("memory source count exceeds 1000")

    for pattern in policy.allow_paths:
        prefix = []
        for part in pattern.split("/"):
            if any(c in part for c in "*?["):
                break
            prefix.append(part)
        start = repo.joinpath(*prefix)
        safe_source(repo, start.relative_to(repo).as_posix())
        if start.exists():
            walk(start)
    if generation and selected_generation(repo) != generation:
        raise ValueError("memory ledger generation changed during discovery; retry")
    return sorted(paths)


def collect_sources(repo: Path, policy: Policy) -> list[Source]:
    """Read only the bounded, explicitly authorized corpus."""
    documents = []
    generation = (
        selected_generation(repo)
        if policy.active and STATE_SOURCE_TYPES & set(policy.source_types)
        else ""
    )
    for relative in allowed_paths(repo, policy):
        document = read_source(repo, relative, policy)
        if document is not None:
            documents.append(document)
    if generation and selected_generation(repo) != generation:
        raise ValueError("memory ledger generation changed during ingestion; retry")
    return documents
