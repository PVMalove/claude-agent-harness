"""Explicit bounded tracker ingestion; only sanitized immutable records persist."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import threading
from typing import BinaryIO
from pathlib import Path
from urllib.parse import quote

from .adapters import sanitized, scalar
from .index import context, refresh, writer_lock
from .policy import Policy
from .sources import (
    matches,
    safe_source,
    SNAPSHOT,
    snapshot_manifest,
    snapshot_kind_allowed,
)

MAX_PAGES = 20
MAX_RECORDS = 1000
MAX_RESPONSE = 4 * 1024 * 1024
MAX_REQUESTS = 200
TIMEOUT = 10
MARKER = re.compile(r"^## Completion report\s*\n```json\s*\n(.*?)\n```\s*$", re.S)


def fetch_page(argv: list[str], repo: Path) -> list[dict[str, object]]:
    """Bound subprocess time and output without retaining raw diagnostics."""
    cmd: list[str] | str = list(argv)
    if sys.platform == "win32":
        resolved = shutil.which(argv[0])
        if resolved and resolved.lower().endswith((".cmd", ".bat")):
            # cmd.exe splits an unquoted `&` in the query string into separate commands.
            line = " ".join(f'"{part}"' for part in (resolved, *argv[1:]))
            cmd = f'cmd.exe /d /s /c "{line}"'
        elif resolved:
            cmd = [resolved, *argv[1:]]
    try:
        process = subprocess.Popen(
            cmd, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except OSError:
        raise ValueError(f"memory sync: {argv[0]} unavailable") from None
    assert process.stdout is not None and process.stderr is not None
    output = bytearray()
    exceeded = threading.Event()
    guard = threading.Lock()
    size = [0]

    def drain(stream: BinaryIO, retain: bool) -> None:
        while chunk := stream.read(65536):
            with guard:
                size[0] += len(chunk)
                if size[0] > MAX_RESPONSE:
                    exceeded.set()
                    return
                if retain:
                    output.extend(chunk)

    readers = [
        threading.Thread(target=drain, args=(process.stdout, True), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, False), daemon=True),
    ]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + TIMEOUT
    try:
        while process.poll() is None or any(reader.is_alive() for reader in readers):
            if exceeded.is_set():
                raise ValueError(f"memory sync: {argv[0]} response limit")
            if time.monotonic() > deadline:
                raise ValueError(f"memory sync: {argv[0]} timeout")
            time.sleep(0.01)
        if exceeded.is_set():
            raise ValueError(f"memory sync: {argv[0]} response limit")
        if process.returncode:
            raise ValueError(
                f"memory sync: {argv[0]} failed (exit {process.returncode}); check access and network"
            )
        try:
            value = json.loads(output)
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
        for reader in readers:
            reader.join(timeout=0.1)


def inventory(
    tool: str, endpoint: str, repo: Path, budget: list[int]
) -> list[dict[str, object]]:
    """An empty final page proves the bounded inventory is complete."""
    records: list[dict[str, object]] = []
    for page in range(1, MAX_PAGES + 1):
        budget[0] += 1
        if budget[0] > MAX_REQUESTS:
            raise ValueError("memory sync: request budget exceeded")
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


def record(
    kind: str, identifier: int, value: dict[str, object], policy: Policy
) -> tuple[str, bytes]:
    """Project allowlisted fields before any persistent write, including metadata."""

    def clean(item: object, default: str = "") -> str:
        return (
            scalar(sanitized(item, policy), default)
            if isinstance(item, str)
            else default
        )

    completion = kind == "completion_report"
    fields = (
        ("output", "risks", "blockers", "lessons")
        if completion
        else ("body", "description")
    )
    text: list[str] = [
        f"{key}: {clean(value[key])}"
        for key in ("ticket", "role", "outcome")
        if completion and isinstance(value.get(key), str)
    ]
    for key in fields:
        field = value.get(key)
        values = field[:20] if isinstance(field, list) else [field]
        text.extend(clean(item) for item in values if isinstance(item, str))
    payload = {
        "record_kind": kind,
        "title": clean(value.get("title"), f"{kind} {identifier}"),
        "status": "не подтверждено человеком"
        if completion
        else "merged"
        if kind == "pull_request"
        and isinstance(value.get("merged_at"), str)
        and value["merged_at"]
        else clean(value.get("state"), "closed"),
        "date": clean(value.get("date"), clean(value.get("updated_at"), "unknown")),
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
        and snapshot_kind_allowed(kind, policy)
    }
    if not permitted:
        return {"status": "disabled", "synced": 0}
    prior = snapshot_manifest(canonical)
    tool, slug = tracker(canonical)
    root = f"repos/{slug}" if tool == "gh" else f"projects/{quote(slug, safe='')}"
    records: dict[str, bytes] = {}
    skipped = 0
    budget = [0]
    for kind, collection in (
        ("ticket", "issues"),
        ("pull_request", "pulls" if tool == "gh" else "merge_requests"),
    ):
        if kind not in permitted and "completion_report" not in permitted:
            continue
        states = (
            ["closed", "merged"]
            if tool == "glab" and kind == "pull_request"
            else ["closed"]
        )
        for state in states:
            for item in inventory(
                tool, f"{root}/{collection}?state={state}", canonical, budget
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
                for comment in inventory(tool, comments, canonical, budget):
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
                        or not any(
                            key in report
                            for key in ("output", "risks", "blockers", "lessons")
                        )
                        or any(
                            key in report and not isinstance(report[key], str)
                            for key in (
                                "output",
                                "ticket",
                                "role",
                                "outcome",
                                "date",
                                "status",
                            )
                        )
                        or any(
                            key in report
                            and not (
                                isinstance(report[key], str)
                                and key != "lessons"
                                or isinstance(report[key], list)
                                and all(isinstance(part, str) for part in report[key])
                            )
                            for key in ("risks", "blockers", "lessons")
                        )
                    ):
                        skipped += 1
                        continue
                    report_status = scalar(report.get("status")).strip().lower()
                    if report_status in {
                        "superseded",
                        "заменён",
                        "заменен",
                    } or re.match(r"superseded\s+by\s+\S+", report_status):
                        continue
                    path, raw = record("completion_report", report_id, report, policy)
                    if matches(path, policy.allow_paths):
                        records[path] = raw
                if len(records) > MAX_RECORDS:
                    raise ValueError("memory sync: snapshot exceeds 1000 records")
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
