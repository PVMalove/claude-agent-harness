"""Read PR creation times through the configured tracker CLI, without ledger writes.

The local ledger reader remains offline. This enrichment runs in the Reports worker,
on entry and manual refresh; failed/partial queries keep PR timing explicitly unknown.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from time import monotonic
from typing import cast
from urllib.parse import quote, urlencode

from harness.health.project_tracker import ProjectTracker, resolve_project_tracker
from harness.orchestration.ledger.lifecycle import JsonObject
from .reports import LedgerView, load_ledger_view, timestamp

_LIMIT = 100


def _pull_requests(
    repo: Path, tracker: ProjectTracker, branch: str, timeout: float
) -> list[JsonObject] | None:
    if not tracker.host or not tracker.project:
        return None
    environment = dict(os.environ)
    if tracker.type == "github":
        argv = [
            "gh",
            "pr",
            "list",
            "--repo",
            f"{tracker.host}/{tracker.project}",
            "--head",
            branch,
            "--state",
            "all",
            "--limit",
            str(_LIMIT),
            "--json",
            "headRefName,createdAt,url,isCrossRepository",
        ]
    elif tracker.type == "gitlab":
        query = urlencode({"source_branch": branch, "state": "all", "per_page": _LIMIT})
        argv = [
            "glab",
            "api",
            f"projects/{quote(tracker.project, safe='')}/merge_requests?{query}",
        ]
        environment["GITLAB_HOST"] = tracker.host
    else:
        return None
    try:
        result = subprocess.run(
            argv,
            cwd=repo,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        payload: object = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(payload, list) or len(payload) >= _LIMIT:
        return None
    if any(not isinstance(item, dict) for item in payload):
        return None
    records = cast(list[JsonObject], payload)
    matching: list[JsonObject] = []
    for item in records:
        if tracker.type == "github":
            if (
                item.get("headRefName") != branch
                or item.get("isCrossRepository") is not False
            ):
                continue
            at, url = item.get("createdAt"), item.get("url")
        else:
            source, target = (
                item.get("source_project_id"),
                item.get("target_project_id"),
            )
            if (
                item.get("source_branch") != branch
                or not isinstance(source, int)
                or source != target
            ):
                continue
            at, url = item.get("created_at"), item.get("web_url")
        if not isinstance(at, str) or timestamp(at) is None or not isinstance(url, str):
            return None
        matching.append({"created_at": at, "url": url})
    return matching if matching or not records else None


def load_reports_view(repo: Path) -> LedgerView:
    """Load local reports and confirmed PR creation dates for each exact batch branch."""
    view = load_ledger_view(repo)
    branches = dict.fromkeys(batch.branch for batch in view.batches if batch.branch)
    if not branches:
        return view
    tracker = resolve_project_tracker(repo).effective
    deadline = monotonic() + 10
    observations: dict[str, list[JsonObject] | None] = {}
    for branch in branches:
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        observations[branch] = _pull_requests(repo, tracker, branch, remaining)
    for index, batch in enumerate(view.batches):
        records = observations.get(batch.branch)
        if records is None:
            continue
        if not records:
            view.batches[index] = replace(batch, pr_state="missing")
            continue
        start = timestamp(batch.created_at)
        if start is None:
            continue
        eligible = [
            record
            for record in records
            if (at := timestamp(str(record["created_at"]))) is not None and at >= start
        ]
        if eligible:
            first = min(
                eligible,
                key=lambda record: timestamp(str(record["created_at"])) or start,
            )
            view.batches[index] = replace(
                batch,
                pr_state="found",
                pr_created_at=str(first["created_at"]),
                pr_url=str(first["url"]),
            )
    return view
