"""Read task titles through the configured tracker without changing local state."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import json
import os
from pathlib import Path
import re
from time import monotonic
from urllib.parse import quote

from harness.health.process import run_tool
from harness.health.project_tracker import resolve_project_tracker

TaskTitlesLoader = Callable[[Path, Sequence[str]], dict[str, str]]


def load_task_titles(
    repo: Path, tickets: Sequence[str], *, clock: Callable[[], float] = monotonic
) -> dict[str, str]:
    """Return confirmed titles, leaving unavailable or unsupported tickets absent."""
    issues: dict[str, list[str]] = {}
    for ticket in tickets:
        match = re.fullmatch(r"#?([1-9][0-9]*)", ticket.strip())
        if match:
            issues.setdefault(match[1], []).append(ticket)
    if not issues:
        return {}
    tracker = resolve_project_tracker(repo).effective
    if tracker.type == "local" or not tracker.host or not tracker.project:
        return {}
    titles: dict[str, str] = {}
    deadline = clock() + 10
    for number, aliases in issues.items():
        remaining = deadline - clock()
        if remaining <= 0:
            break
        environment = dict(os.environ)
        if tracker.type == "github":
            argv = [
                "gh",
                "issue",
                "view",
                number,
                "--repo",
                f"{tracker.host}/{tracker.project}",
                "--json",
                "title",
            ]
        else:
            argv = [
                "glab",
                "api",
                f"projects/{quote(tracker.project, safe='')}/issues/{number}",
            ]
            environment["GITLAB_HOST"] = tracker.host
        result = run_tool(
            argv,
            cwd=repo,
            env=environment,
            timeout=remaining,
        )
        if result is None or result.returncode:
            continue
        try:
            payload: object = json.loads(result.stdout)
        except ValueError:
            continue
        title = payload.get("title") if isinstance(payload, dict) else None
        if isinstance(title, str) and title.strip():
            titles.update((alias, title) for alias in aliases)
    return titles
