"""Explicit bounded tracker ingestion; only sanitized immutable records persist."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

from .adapters import baseline, scalar
from .index import context, refresh, writer_lock
from .policy import Policy
from .sources import matches, safe_source, SNAPSHOT, snapshot_manifest

MAX_PAGES = 20
MAX_RECORDS = 1000
MAX_RESPONSE = 4 * 1024 * 1024
TIMEOUT = 10
MARKER = re.compile(r"^## Completion report\s*\n```json\s*\n(.*?)\n```\s*$", re.S)


def fetch_page(argv: list[str], repo: Path) -> list[dict[str, object]]:
    """Bound subprocess time and output without retaining raw diagnostics."""
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        try:
            process = subprocess.Popen(argv, cwd=repo, stdout=output, stderr=error)
        except OSError:
            raise ValueError(f"memory sync: {argv[0]} unavailable") from None
        deadline = time.monotonic() + TIMEOUT
        try:
            while process.poll() is None:
                if time.monotonic() > deadline:
                    raise ValueError(f"memory sync: {argv[0]} timeout")
                if (
                    os.fstat(output.fileno()).st_size + os.fstat(error.fileno()).st_size
                    > MAX_RESPONSE
                ):
                    raise ValueError(f"memory sync: {argv[0]} response limit")
                time.sleep(0.01)
            if process.returncode:
                raise ValueError(
                    f"memory sync: {argv[0]} failed (exit {process.returncode}); check access and network"
                )
            output.seek(0)
            raw = output.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError("memory sync: response limit")
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise ValueError("memory sync: invalid tracker JSON") from None
            if not isinstance(value, list) or not all(
                isinstance(item, dict) for item in value
            ):
                raise ValueError("memory sync: tracker page must be an object list")
            return value
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def inventory(tool: str, endpoint: str, repo: Path) -> list[dict[str, object]]:
    """An empty final page proves the bounded inventory is complete."""
    records: list[dict[str, object]] = []
    for page in range(1, MAX_PAGES + 1):
        separator = "&" if "?" in endpoint else "?"
        batch = fetch_page(
            [tool, "api", endpoint + f"{separator}per_page=100&page={page}"], repo
        )
        if not batch:
            return records
        records.extend(batch)
        if len(records) > MAX_RECORDS:
            raise ValueError("memory sync: inventory exceeds 1000 records")
    raise ValueError("memory sync: inventory exceeds 20 pages")


def tracker(repo: Path) -> tuple[str, str]:
    """Use existing origin heuristics without importing optional health machinery."""
    try:
        result = subprocess.run(
            ["git", "remote", "-v"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        raise ValueError("memory sync: cannot detect origin") from None
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] != "origin":
            continue
        for host, tool in ((r"github\.com", "gh"), (r"gitlab\.[^/:\s]+", "glab")):
            match = re.search(host + r"[:/]([^/]+)/([^/.\s]+)", parts[1])
            if match:
                slug = f"{match[1]}/{match[2]}"
                if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_-]+", slug):
                    break
                return tool, slug
        break
    raise ValueError("memory sync: unsupported or local tracker")


def sanitized(value: str, policy: Policy) -> str:
    value = baseline(value)
    for rule in policy.redact_rules:
        value = re.sub(rule, "[REDACTED]", value)
    return value


def record(
    kind: str, identifier: int, value: dict[str, object], policy: Policy
) -> tuple[str, bytes]:
    """Project allowlisted fields before any persistent write, including metadata."""
    completion = kind == "completion_report"
    fields = (
        ("output", "risks", "blockers", "lessons")
        if completion
        else ("body", "description")
    )
    text: list[str] = []
    for key in fields:
        field = value.get(key)
        values = field[:20] if isinstance(field, list) else [field]
        text.extend(scalar(item) for item in values if isinstance(item, str))
    payload = {
        "record_kind": kind,
        "title": scalar(value.get("title"), f"Completion report {identifier}"),
        "status": "не подтверждено человеком"
        if completion
        else scalar(value.get("state"), "closed"),
        "date": scalar(value.get("updated_at"), "unknown"),
        "body": "\n".join(text)[:16384],
    }
    for key in ("title", "status", "date", "body"):
        payload[key] = sanitized(payload[key], policy)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    digest = hashlib.sha256(raw).hexdigest()
    path = f"{SNAPSHOT}/records/{kind}-{identifier}-{digest}.json"
    if sanitized(path, policy) != path:
        raise ValueError("memory sync: record path cannot be safely retained")
    return path, raw


def sync(repo: Path) -> dict[str, object]:
    """Fetch completely, atomically select a sanitized snapshot, then refresh offline."""
    canonical, index, policy = context(repo, writer=True)
    permitted = {
        kind
        for kind, source in (
            ("ticket", "task_archive"),
            ("pull_request", "task_archive"),
            ("completion_report", "completion_report"),
        )
        if policy.active
        and source in policy.source_types
        and matches(f"{SNAPSHOT}/records/{kind}-1-{'a' * 64}.json", policy.allow_paths)
    }
    if not permitted:
        return {"status": "disabled", "synced": 0}
    prior = snapshot_manifest(canonical)
    tool, slug = tracker(canonical)
    root = f"repos/{slug}" if tool == "gh" else f"projects/{quote(slug, safe='')}"
    records: dict[str, bytes] = {}
    skipped = 0
    for kind, collection in (
        ("ticket", "issues"),
        ("pull_request", "pulls" if tool == "gh" else "merge_requests"),
    ):
        states = (
            ["closed", "merged"]
            if tool == "glab" and kind == "pull_request"
            else ["closed"]
        )
        for state in states:
            for item in inventory(
                tool, f"{root}/{collection}?state={state}", canonical
            ):
                if tool == "gh" and collection == "issues" and "pull_request" in item:
                    continue
                identifier = item.get("number" if tool == "gh" else "iid")
                if (
                    type(identifier) is not int
                    or identifier < 1
                    or item.get("state") not in {"closed", "merged"}
                ):
                    raise ValueError(
                        "memory sync: invalid terminal record identity/state"
                    )
                if kind in permitted:
                    path, raw = record(kind, identifier, item, policy)
                    if matches(path, policy.allow_paths):
                        records[path] = raw
                if "completion_report" not in permitted:
                    continue
                comments = (
                    f"{root}/issues/{identifier}/comments"
                    if tool == "gh"
                    else f"{root}/{collection}/{identifier}/notes"
                )
                for comment in inventory(tool, comments, canonical):
                    body = comment.get("body")
                    match = (
                        MARKER.fullmatch(body)
                        if isinstance(body, str) and len(body) <= 16384
                        else None
                    )
                    if match is None:
                        skipped += 1
                        continue
                    try:
                        report = json.loads(match[1])
                    except ValueError:
                        skipped += 1
                        continue
                    report_id = comment.get("id")
                    if (
                        not isinstance(report, dict)
                        or type(report_id) is not int
                        or report_id < 1
                    ):
                        skipped += 1
                        continue
                    if report.get("status") == "superseded":
                        continue
                    path, raw = record("completion_report", report_id, report, policy)
                    if matches(path, policy.allow_paths):
                        records[path] = raw
                if len(records) > MAX_RECORDS:
                    raise ValueError("memory sync: snapshot exceeds 1000 records")
    manifest = json.dumps(
        {"version": 1, "records": sorted(records)}, sort_keys=True
    ).encode()
    index.parent.mkdir(parents=True, exist_ok=True)
    with writer_lock(index):
        if (
            context(canonical)[2].fingerprint != policy.fingerprint
            or snapshot_manifest(canonical) != prior
        ):
            raise ValueError(
                "memory sync: policy or snapshot changed during fetch; retry"
            )
        destination = safe_source(canonical, SNAPSHOT + "/manifest.json")
        for relative, raw in records.items():
            target = safe_source(canonical, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if target.read_bytes() != raw:
                    raise ValueError("memory sync: immutable record collision")
            else:
                with target.open("xb") as stream:
                    stream.write(raw)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if prior != manifest:
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=destination.parent, delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(manifest)
                os.replace(temporary, destination)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
    try:
        indexed = refresh(canonical)
    except (ValueError, OSError):
        return {
            "status": "snapshot_synced_index_failed",
            "synced": len(records),
            "diagnostic": "snapshot published; index refresh failed; run harness memory rebuild",
        }
    return {
        "status": "synced",
        "synced": len(records),
        "indexed": indexed["indexed"],
        "skipped_reports": skipped,
    }
