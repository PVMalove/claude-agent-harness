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

import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

from ..context import HealthContext
from ..labels_table import parse_canonical_labels
from ..model import CheckResult, Fix

GROUP = "tracker"

_ONLINE_TIMEOUT_SECONDS = 10
_TRIAGE_LABELS_REL = Path("docs/agents/triage-labels.md")

# GitLab access_level thresholds (see GitLab's own Members API): Developer (30) can push and open
# MRs; Reporter (20) cannot push but can still manage labels. This mirrors GitHub's push/triage
# split one level down, per ticket #346's brief.
_GITLAB_PUSH_ACCESS_LEVEL = 30
_GITLAB_LABELS_ACCESS_LEVEL = 20

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


# --- tracker.permissions --------------------------------------------------------------------------


def _github_permissions(
    executable: str, slug: str, cwd: Path
) -> tuple[bool, bool] | None:
    """(push, triage-or-above) from `gh api repos/{owner}/{repo}`'s `.permissions`, or None on
    any failure to run/parse it. Only these two booleans ever leave this function."""
    result = _run(
        [executable, "api", f"repos/{slug}", "--jq", ".permissions"], cwd=cwd
    )
    if result is None or result.returncode != 0:
        return None
    try:
        permissions = json.loads(result.stdout)
    except ValueError:
        return None
    if not isinstance(permissions, dict):
        return None
    push = bool(permissions.get("push"))
    triage = push or bool(permissions.get("triage")) or bool(permissions.get("maintain")) or bool(
        permissions.get("admin")
    )
    return push, triage


def _gitlab_permissions(
    executable: str, slug: str, cwd: Path
) -> tuple[bool, bool] | None:
    """(push, triage-equivalent) from `glab api projects/:id`'s `.permissions`, mapped from the
    higher of project_access/group_access's access_level. Developer (30) and up ~ push; Reporter
    (20) and up ~ labels-only. None on any failure to run/parse it."""
    result = _run([executable, "api", f"projects/{quote(slug, safe='')}"], cwd=cwd)
    if result is None or result.returncode != 0:
        return None
    try:
        project = json.loads(result.stdout)
    except ValueError:
        return None
    if not isinstance(project, dict):
        return None
    permissions = project.get("permissions")
    if not isinstance(permissions, dict):
        return None
    levels = []
    for key in ("project_access", "group_access"):
        access = permissions.get(key)
        if isinstance(access, dict) and isinstance(access.get("access_level"), int):
            levels.append(access["access_level"])
    if not levels:
        return None
    access_level = max(levels)
    return (
        access_level >= _GITLAB_PUSH_ACCESS_LEVEL,
        access_level >= _GITLAB_LABELS_ACCESS_LEVEL,
    )


def check_permissions(context: HealthContext) -> CheckResult:
    check_id = "tracker.permissions"
    tracker, slug, early = _offline_or_local(check_id, context)
    if early is not None:
        return early
    tool = _tracker_tool(tracker)
    executable = shutil.which(tool)
    if executable is None:
        return CheckResult(
            id=check_id,
            group=GROUP,
            status="warn",
            message=f"{tool} не найден в PATH: права доступа не проверены",
        )
    if slug is None:
        return CheckResult(
            id=check_id,
            group=GROUP,
            status="warn",
            message="не удалось разобрать owner/repo из git remote -v: права доступа не проверены",
        )
    permissions = (
        _github_permissions(executable, slug, context.repo)
        if tracker == "github"
        else _gitlab_permissions(executable, slug, context.repo)
    )
    if permissions is None:
        return CheckResult(
            id=check_id,
            group=GROUP,
            status="warn",
            message=f"не удалось получить права доступа через {tool}",
        )
    push, triage = permissions
    if push and triage:
        return CheckResult(
            id=check_id,
            group=GROUP,
            status="ok",
            message="прав достаточно для PR/комментариев и меток",
        )
    parts = []
    parts.append(
        "PR и комментарии доступны" if push else "недостаточно прав для PR/комментариев"
    )
    parts.append("метки доступны" if triage else "недостаточно прав для меток")
    return CheckResult(id=check_id, group=GROUP, status="warn", message="; ".join(parts))


# --- tracker.labels --------------------------------------------------------------------------------


def _list_repo_labels(
    tracker: Tracker, executable: str, slug: str, cwd: Path
) -> list[tuple[str, str]] | None:
    """Existing repository labels as (name, "#rrggbb") pairs, or None on any failure.

    Uses the raw paginated REST/API endpoint rather than `label list` (gh's own subcommand
    defaults to 30 labels and has no flag to fetch every page in one call; glab's caps out at a
    single page too), so a repository with more labels than that default is not misread as
    missing them all past the cutoff - which would otherwise make `--fix` try to recreate labels
    that already exist."""
    if tracker == "github":
        argv = [executable, "api", "--paginate", f"repos/{slug}/labels"]
    else:
        argv = [
            executable,
            "api",
            "--paginate",
            f"projects/{quote(slug, safe='')}/labels",
        ]
    result = _run(argv, cwd=cwd)
    if result is None or result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    labels: list[tuple[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name, color = item.get("name"), item.get("color")
        if isinstance(name, str) and isinstance(color, str) and color:
            normalized = color if color.startswith("#") else f"#{color}"
            labels.append((name, normalized.lower()))
    return labels


def _canonical_labels(context: HealthContext) -> list[tuple[str, str]] | None:
    """Canonical (name, color) pairs parsed from the target project's own
    docs/agents/triage-labels.md (`context.repo` is the project being checked, never this harness
    repository's own copy), or None when that file is missing."""
    labels_path = context.repo / _TRIAGE_LABELS_REL
    if not labels_path.is_file():
        return None
    return parse_canonical_labels(labels_path.read_text(encoding="utf-8"))


def _label_diff(
    context: HealthContext, tracker: Tracker, executable: str, slug: str
) -> tuple[list[tuple[str, str]], list[str]] | None:
    """(missing, mismatched-by-color) against the canonical table, or None when the canonical
    table or the repository's own label list could not be read."""
    canonical = _canonical_labels(context)
    if not canonical:
        return None
    existing = _list_repo_labels(tracker, executable, slug, context.repo)
    if existing is None:
        return None
    existing_by_name = {name.lower(): color for name, color in existing}
    missing = [
        (name, color) for name, color in canonical if name.lower() not in existing_by_name
    ]
    mismatched = [
        name
        for name, color in canonical
        if name.lower() in existing_by_name and existing_by_name[name.lower()] != color
    ]
    return missing, mismatched


def check_labels(context: HealthContext) -> CheckResult:
    check_id = "tracker.labels"
    tracker, slug, early = _offline_or_local(check_id, context)
    if early is not None:
        return early
    if not (context.repo / _TRIAGE_LABELS_REL).is_file():
        return CheckResult(
            id=check_id,
            group=GROUP,
            status="warn",
            message=f"нет {_TRIAGE_LABELS_REL.as_posix()}: канонические метки не проверены",
        )
    tool = _tracker_tool(tracker)
    executable = shutil.which(tool)
    if executable is None:
        return CheckResult(
            id=check_id,
            group=GROUP,
            status="warn",
            message=f"{tool} не найден в PATH: метки не проверены",
        )
    if slug is None:
        return CheckResult(
            id=check_id,
            group=GROUP,
            status="warn",
            message="не удалось разобрать owner/repo из git remote -v: метки не проверены",
        )
    diff = _label_diff(context, tracker, executable, slug)
    if diff is None:
        return CheckResult(
            id=check_id,
            group=GROUP,
            status="warn",
            message=f"не удалось получить список меток через {tool}, либо канонические таблицы не распознаны",
        )
    missing, mismatched = diff
    if not missing and not mismatched:
        return CheckResult(
            id=check_id,
            group=GROUP,
            status="ok",
            message="все канонические метки присутствуют и совпадают по цвету",
        )
    parts = []
    if missing:
        parts.append("отсутствуют: " + ", ".join(name for name, _color in missing))
    if mismatched:
        parts.append(
            "цвет расходится с канонической таблицей (метка не перекрашивается): "
            + ", ".join(mismatched)
        )
    fix = (
        Fix(
            text="создайте отсутствующие метки с каноническими цветами",
            command="harness health <repo> --online --fix",
        )
        if missing
        else None
    )
    return CheckResult(
        id=check_id, group=GROUP, status="warn", message="; ".join(parts), fix=fix
    )
