"""Bounded local Markdown ingestion after explicit policy authorization."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .policy import Policy

MAX_SOURCE_BYTES = 1024 * 1024
MAX_SOURCES = 1000


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
    """ADR directories/filename prefixes identify ADRs; other allowed Markdown is glossary."""
    parts = Path(path).parts
    return (
        "adr"
        if "adr" in [p.lower() for p in parts[:-1]]
        or Path(path).stem.lower().startswith("adr")
        else "glossary"
    )


def safe_source(repo: Path, relative: str) -> Path:
    """Reject symlink components before touching file contents."""
    path = repo
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"memory source must not be a symlink: {relative}")
    if not path.resolve().is_relative_to(repo.resolve()):
        raise ValueError("memory source escaped project")
    return path


def read_source(repo: Path, relative: str, policy: Policy) -> Source | None:
    """Read one authorized file, redact before retaining metadata or searchable text."""
    path = safe_source(repo, relative)
    if not path.is_file() or path.suffix.lower() != ".md":
        return None
    with path.open("rb") as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError(f"memory source exceeds 1 MiB: {relative}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"memory source must be UTF-8: {relative}") from None
    def metadata(names: str, default: str) -> str:
        match = re.search(
            rf"(?im)^\s*(?:\*\*)?(?:{names})(?:\*\*)?\s*:\s*([^\n]+?)\s*$", text
        )
        if not match:
            match = re.search(rf"(?im)^##\s+(?:{names})\s*\n\s*([^\n]+)", text)
        return match.group(1).strip(" *\"'") if match else default

    status = metadata("status|статус", "unknown")
    date = metadata("date|дата", "unknown")
    superseded_by = metadata("superseded[-_]by|заменён на|заменен на", "")
    if status.lower() in {"superseded", "заменён", "заменен"}:
        return None
    for rule in policy.redact_rules:
        if re.search(rule, relative):
            raise ValueError("memory source path cannot be safely retained")
        text = re.sub(rule, "[REDACTED]", text)
        status = re.sub(rule, "[REDACTED]", status)
        date = re.sub(rule, "[REDACTED]", date)
        superseded_by = re.sub(rule, "[REDACTED]", superseded_by)
    status = status.lower().replace("[redacted]", "[REDACTED]")
    title_match = re.search(r"(?m)^#\s+(.+?)\s*$", text)
    title = title_match.group(1) if title_match else "untitled"
    return Source(
        source_type(relative),
        title,
        status,
        date,
        superseded_by,
        relative,
        hashlib.sha256(raw).hexdigest(),
        text,
    )


def allowed_paths(repo: Path, policy: Policy) -> list[str]:
    """Expand only explicit allow_paths; sorted deduplicated paths have stable identities."""
    if not policy.active:
        return []
    paths: set[str] = set()
    for pattern in policy.allow_paths:
        for path in repo.glob(pattern):
            relative = path.relative_to(repo).as_posix()
            if (
                path.suffix.lower() != ".md"
                or source_type(relative) not in policy.source_types
            ):
                continue
            paths.add(relative)
            if len(paths) > MAX_SOURCES:
                raise ValueError("memory source count exceeds 1000")
    return sorted(paths)


def collect_sources(repo: Path, policy: Policy) -> list[Source]:
    """Read only the bounded, explicitly allowed corpus."""
    documents: list[Source] = []
    for relative in allowed_paths(repo, policy):
        document = read_source(repo, relative, policy)
        if document is not None:
            documents.append(document)
    return documents
