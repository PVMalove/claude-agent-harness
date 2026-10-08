#!/usr/bin/env python3
"""Issue First для branch-name hooks: есть ли issue с ID из имени ветки в трекере проекта.

Трекер и проект берутся из резолвера `.harness/health/project_tracker.py` (docs/adr/0011), и
`gh`/`glab` получают явный `-R`, поэтому self-hosted GitLab с портом и подгруппами адресуется
верно. Код 2 с сообщением в stderr - отказ; код 0 - issue найдена или проверить нечем: нет
резолвера, трекер локальный, путь проекта неизвестен или CLI трекера не установлен.

Вызов: tracker-issue.py <каталог проекта> <ID> <имя ветки или worktree>
"""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal

if TYPE_CHECKING:
    from harness.health.project_tracker import TrackerResolution

    Run = Callable[[list[str]], subprocess.CompletedProcess[str] | None]

TIMEOUT_SECONDS = 10
RULE = "Issue First (docs/agents/git-workflow.md)"
# Tracker type -> (CLI, the -R address format).
TRACKERS = {
    "github": ("gh", "{host}/{project}"),
    "gitlab": ("glab", "https://{host}/{project}"),
}
Reason = Literal["auth", "disabled", "missing", "project"]


def load_tracker_tools(
    repo: Path,
) -> tuple[Callable[[Path], TrackerResolution], Run] | None:
    """Резолвер трекера и run_tool из поставленного пакета `.harness/health`; None без него."""
    root = repo / ".harness"
    try:
        spec = importlib.machinery.ModuleSpec("harness", None, is_package=True)
        spec.submodule_search_locations = [str(root)]
        sys.modules["harness"] = importlib.util.module_from_spec(spec)
        tracker = importlib.import_module("harness.health.project_tracker")
        process = importlib.import_module("harness.health.process")
    except Exception:
        return None
    resolve: Callable[[Path], TrackerResolution] = tracker.resolve_project_tracker

    def run(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
        result: subprocess.CompletedProcess[str] | None = process.run_tool(
            argv, timeout=TIMEOUT_SECONDS, cwd=repo
        )
        return result

    return resolve, run


def classify(output: str) -> Reason | None:
    """Причина отказа по выводу gh/glab: auth, disabled, missing, project; None - не распознана."""
    text = output.lower()
    if "auth login" in text or re.search(
        r"\b401\b[^\n]*(unauthorized|bad credentials)", text
    ):
        return "auth"
    if "has disabled issues" in text:
        return "disabled"
    if "could not resolve to an issue" in text or re.search(
        r"\b404\b[^\n]*not found", text
    ):
        return "missing"
    if "could not resolve to a repository" in text:
        return "project"
    return None


def gitlab_404_reason(run: Run, executable: str, address: str) -> Reason:
    """GitLab отвечает 404 и на отсутствующую issue, и на проект с отключёнными issues: различить
    их можно только по issues_access_level проекта."""
    result = run([executable, "repo", "view", "-R", address, "-F", "json"])
    if result is None or result.returncode != 0:
        return "project"
    try:
        project = json.loads(result.stdout)
    except ValueError:
        return "missing"
    if isinstance(project, dict) and project.get("issues_access_level") == "disabled":
        return "disabled"
    return "missing"


def main(repo_dir: str, issue_id: str, name: str) -> int:
    repo = Path(repo_dir)
    tools = load_tracker_tools(repo)
    if tools is None:
        return 0
    resolve, run = tools
    tracker = resolve(repo).effective
    kind, host, project = tracker.type, tracker.host, tracker.project
    if kind not in TRACKERS or host is None or project is None:
        return 0
    tool, address_format = TRACKERS[kind]
    executable = shutil.which(tool)
    if executable is None:
        return 0
    address = address_format.format(host=host, project=project)
    argv = [executable, "issue", "view", issue_id, "-R", address]
    result = run(argv)
    if result is not None and result.returncode == 0:
        return 0

    command = f"{tool} issue view {issue_id} -R {address}"
    subject = f"issue #{issue_id} из имени '{name}'"
    reason = None if result is None else classify(result.stdout + result.stderr)
    if reason == "missing" and tool == "glab":
        reason = gitlab_404_reason(run, executable, address)
    messages: dict[Reason, str] = {
        "missing": f"{subject} не найдена в {address} — сначала заведи её "
        f"('{tool} issue create' или /to-spec, /to-tickets).",
        "disabled": f"в {address} отключены issues, поэтому {subject} не проверить — включи "
        "issues в проекте или укажи нужный трекер в поле tracker .harness/project.json.",
        "auth": f"{tool} не авторизован на {host}, поэтому {subject} не проверить — выполни "
        f"'{tool} auth login --hostname {host}'.",
        "project": f"проект {address} не найден или недоступен, поэтому {subject} не проверить — "
        f"проверь '{tool} auth status --hostname {host}' и поле tracker в .harness/project.json.",
    }
    if result is None:
        message = f"'{command}' не завершилась за {TIMEOUT_SECONDS} с, {subject} не проверена."
    elif reason is None:
        message = (
            f"'{command}' завершилась с кодом {result.returncode}, {subject} не проверена — "
            "запусти команду вручную, чтобы увидеть причину."
        )
    else:
        message = messages[reason]
    sys.stderr.write(f"{RULE}: {message}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]))
