"""Group 'tracker': online checks of the tracker (GitHub/GitLab) this repository is hosted on
(ticket #346) - authentication, effective permissions, network reachability, and repository labels
against the canonical taxonomy in a target project's docs/agents/triage-labels.md.

Every check here is a no-op without `harness health --online`: it reports `skipped` with reason
"offline" instead of making any network call, so a plain `harness health` stays local-only. A
local (non-GitHub/GitLab) tracker is `skipped` the same way once detected, even online. Every
external call gets a 10 second timeout (`_ONLINE_TIMEOUT_SECONDS`), separate from the 60 second
budget checks/environment.py gives local tool invocations. A missing `gh`/`glab` executable is
`warn`, never `fail`: it also makes every check that depends on it warn or skip in turn.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from ..context import HealthContext
from ..model import CheckResult

GROUP = "tracker"

_ONLINE_TIMEOUT_SECONDS = 10

Tracker = str  # "github" | "gitlab" | "local"

_GITHUB_REMOTE = re.compile(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.\s]+)")
_GITLAB_REMOTE = re.compile(r"gitlab\.[^/:\s]+[:/](?P<owner>[^/]+)/(?P<repo>[^/.\s]+)")


def _run(
    argv: list[str], *, cwd: Path | None = None
) -> subprocess.CompletedProcess[str] | None:
    """Run one tracker CLI/git call with the tracker-check timeout; None when it cannot be
    started or does not finish in time. Never raises: a missing tool or a stalled network call
    becomes a `warn`/`fail` CheckResult, not an exception (see registry.py's isolation)."""
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_ONLINE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def detect_tracker(context: HealthContext) -> tuple[Tracker, str | None]:
    """Classify origin from `git remote -v`, the same GitHub/GitLab URL heuristic
    docs/agents/issue-tracker.md and check-branch-name.sh use. Returns (tracker, "owner/repo") -
    the slug is None whenever it cannot be parsed out of the URL, even for a recognized host."""
    git = shutil.which("git")
    if git is None:
        return "local", None
    result = _run([git, "remote", "-v"], cwd=context.repo)
    if result is None or result.returncode != 0:
        return "local", None
    origin_url = None
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "origin":
            origin_url = parts[1]
            break
    if not origin_url:
        return "local", None
    github_match = _GITHUB_REMOTE.search(origin_url)
    if github_match:
        return "github", f"{github_match['owner']}/{github_match['repo']}"
    gitlab_match = _GITLAB_REMOTE.search(origin_url)
    if gitlab_match:
        return "gitlab", f"{gitlab_match['owner']}/{gitlab_match['repo']}"
    return "local", None


def _skip(check_id: str, message: str) -> CheckResult:
    return CheckResult(id=check_id, group=GROUP, status="skipped", message=message)


def _tracker_tool(tracker: Tracker) -> str:
    return "gh" if tracker == "github" else "glab"


def _offline_or_local(
    check_id: str, context: HealthContext
) -> tuple[Tracker, str | None, CheckResult | None]:
    """Shared early-exit ladder every tracker.* check starts with: offline, then a non-hosted
    (local) tracker. Returns the detected tracker/slug plus a skip result when the caller should
    stop; the caller proceeds only when the third element is None."""
    if not context.online:
        return "local", None, _skip(
            check_id, "офлайн: без --online проверки трекера не выполняются"
        )
    tracker, slug = detect_tracker(context)
    if tracker == "local":
        return tracker, slug, _skip(
            check_id, "локальный трекер задач: онлайн-проверки не применимы"
        )
    return tracker, slug, None


# --- tracker.auth --------------------------------------------------------------------------------


def check_auth(context: HealthContext) -> CheckResult:
    check_id = "tracker.auth"
    tracker, _slug, early = _offline_or_local(check_id, context)
    if early is not None:
        return early
    tool = _tracker_tool(tracker)
    executable = shutil.which(tool)
    if executable is None:
        return CheckResult(
            id=check_id,
            group=GROUP,
            status="warn",
            message=f"{tool} не найден в PATH: авторизация и права {tracker} не проверены",
        )
    result = _run([executable, "auth", "status"], cwd=context.repo)
    if result is None:
        return CheckResult(
            id=check_id,
            group=GROUP,
            status="fail",
            message=f"{tool} auth status не завершился за {_ONLINE_TIMEOUT_SECONDS} с",
        )
    if result.returncode != 0:
        # Never surface result.stdout/result.stderr here: `gh`/`glab` auth output can include
        # token-adjacent details, so only the return code crosses into the report (ticket #346).
        return CheckResult(
            id=check_id,
            group=GROUP,
            status="fail",
            message=f"{tool} не аутентифицирован (auth status вернул код {result.returncode})",
        )
    return CheckResult(
        id=check_id, group=GROUP, status="ok", message=f"{tool} аутентифицирован"
    )


# --- tracker.reachability ------------------------------------------------------------------------


def check_reachability(context: HealthContext) -> CheckResult:
    check_id = "tracker.reachability"
    _tracker, _slug, early = _offline_or_local(check_id, context)
    if early is not None:
        return early
    git = shutil.which("git")
    if git is None:
        return CheckResult(
            id=check_id,
            group=GROUP,
            status="warn",
            message="git не найден в PATH: достижимость origin не проверена",
        )
    result = _run([git, "ls-remote", "origin"], cwd=context.repo)
    if result is None:
        return CheckResult(
            id=check_id,
            group=GROUP,
            status="fail",
            message=f"git ls-remote origin не завершился за {_ONLINE_TIMEOUT_SECONDS} с",
        )
    if result.returncode != 0:
        return CheckResult(
            id=check_id,
            group=GROUP,
            status="fail",
            message="origin недостижим: git ls-remote origin завершился с ошибкой",
        )
    return CheckResult(
        id=check_id, group=GROUP, status="ok", message="origin достижим (git ls-remote)"
    )
