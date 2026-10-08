#!/usr/bin/env python3
"""Статистика поставки для завершённого эпика и связанных с ним тикетов.

Читает только локальные данные: транскрипты сессий агентов, историю git данного репозитория и
CLI трекера задач. Никакие данные никуда не отправляются. Любое число, которое инструмент не может
получить из источников, помечается как отсутствующее, а не заменяется нулём, а приблизительные
оценки сопровождаются соответствующей пометкой.
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import NamedTuple, Protocol, cast
from urllib.parse import quote

MIN_PYTHON = (3, 9)
if sys.version_info < MIN_PYTHON:
    sys.stderr.write(
        "[ERROR] delivery_stats requires Python {}+ (found {}).\n".format(
            ".".join(map(str, MIN_PYTHON)), sys.version.split()[0]
        )
    )
    raise SystemExit(1)

# `harness/bin/harness.py`'s package_files() copies this file verbatim into target projects as
# `.harness/reporting/delivery_stats.py` -- a different directory name than the source tree's
# `harness/`. Alias `harness` to whichever of the two this file actually lives under so
# `from harness...` resolves the same way in both places. See docs/adr/0001.
_HARNESS_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _HARNESS_ROOT.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if _HARNESS_ROOT.name != "harness" or not (_HARNESS_ROOT / "__init__.py").is_file():
    _spec = importlib.machinery.ModuleSpec("harness", None, is_package=True)
    _spec.submodule_search_locations = [str(_HARNESS_ROOT)]
    sys.modules["harness"] = importlib.util.module_from_spec(_spec)

# Keep these explicit re-exports for callers of the installed delivery_stats.py script.
from harness.errors import HarnessError, print_and_exit  # noqa: I001
from harness.health.project_tracker import ProjectTracker, resolve_project_tracker
from harness.reporting.baseline import (
    _provider_delta as _provider_delta,  # noqa: PLC0414
    baseline_snapshot,
    compare_baseline,
    load_baseline as load_baseline,  # noqa: PLC0414
    save_baseline as save_baseline,  # noqa: PLC0414
)
from harness.reporting.common import (
    BASELINE_SCHEMA_VERSION as BASELINE_SCHEMA_VERSION,  # noqa: PLC0414
    CLAUDE_FIELDS,
    CODEX_FIELDS,
    MISSING as MISSING,  # noqa: PLC0414
    JsonObject,
    StatsError as StatsError,  # noqa: PLC0414
    _int,
)
from harness.reporting.cost import estimate_cost as estimate_cost  # noqa: PLC0414
from harness.reporting.cost import load_rates as load_rates  # noqa: PLC0414
from harness.reporting.terminal import render_terminal as render_terminal  # noqa: PLC0414


class _LedgerInstance(Protocol):
    """Интерфейс экземпляра журнала LifecycleLedger."""

    def records_root_lenient(self) -> Path | None:
        """Получить путь к корню записей журнала в мягком режиме (без строгой валидации)."""
        ...


class LedgerClass(Protocol):
    """Интерфейс фабрики/класса LifecycleLedger, загружаемого динамически.

    Класс загружается по пути к файлу из анализируемого репозитория (см. _load_ledger_class),
    поэтому не может быть статически импортирован.
    """

    def __call__(self, root: Path) -> _LedgerInstance:
        """Создать экземпляр журнала по пути к корню состояния."""
        ...

    def read_record_lenient(self, path: Path) -> JsonObject | None:
        """Прочитать запись журнала в мягком режиме (без строгой валидации)."""
        ...


# Pseudo-models the runtime writes for locally generated messages; never billed.
NON_BILLABLE_MODELS = {"<synthetic>"}
# Matches coordinator.py's own default STATE_REL: the backend-orchestration ledger this project's
# coordinator writes, read here only for structural metrics -- never re-validated as strictly as the
# coordinator itself does, since a missing or unreadable record must degrade to "missing", not abort.
ORCHESTRATION_STATE_REL = Path(".harness/orchestration/state")
CONTINUATION_DECISIONS = {"continue", "continue-automatic"}


def _run(
    command: list[str], cwd: Path | None = None, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Выполнить внешнюю команду в подпроцессе и вернуть код завершения, stdout и stderr."""
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except OSError as exc:
        return 127, "", str(exc)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _git(repo: Path, *arguments: str) -> str:
    """Выполнить команду git в репозитории и вернуть stdout либо возбудить StatsError."""
    code, out, err = _run(["git", "-C", str(repo), *arguments])
    if code != 0:
        raise StatsError(
            f"git {' '.join(arguments)} failed: {err or out or 'unknown error'}",
            remedy=f"inspect the git error above and fix the repository state before retrying 'git {' '.join(arguments)}'",
        )
    return out


def _read_jsonl(path: Path) -> Iterator[JsonObject]:
    """Прочитать JSON-объекты из файла JSONL, пропуская повреждённые строки."""
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    yield value
    except OSError:
        return


def _moment(value: object) -> datetime | None:
    """Распарсить метку времени ISO в объект datetime с зоной UTC."""
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _same_path(
    recorded: object, repo: Path, _cache: dict[tuple[str, str], bool] | None = None
) -> bool:
    """Проверить, совпадает ли записанная рабочая директория с путём к репозиторию.

    Обычного сравнения строк недостаточно: Windows может сохранять короткий путь 8.3 ("RUNNER~1"),
    а регистр символов может различаться. Результат кэшируется, так как в рамках одной сессии
    повторяется один и тот же набор директорий.
    """
    if _cache is None:
        _cache = {}
    if not isinstance(recorded, str) or not recorded:
        return False
    key = (recorded, str(repo))
    hit = _cache.get(key)
    if hit is None:
        if recorded.replace("\\", "/").lower() == str(repo).replace("\\", "/").lower():
            hit = True
        else:
            try:
                hit = Path(recorded).resolve() == repo
            except OSError:
                hit = False
            _cache[key] = hit
    return hit


# --------------------------------------------------------------------------------------- scope


def _project_config(repo: Path) -> JsonObject:
    """Загрузить конфигурацию проекта из .harness/project.json."""
    path = repo / ".harness/project.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise StatsError(
            ".harness/project.json is not valid JSON",
            remedy="fix the JSON syntax in .harness/project.json",
        ) from exc
    return value if isinstance(value, dict) else {}


def _gh(repo: Path, *arguments: str) -> JsonObject | list[JsonObject] | None:
    """Выполнить команду GitHub CLI (gh) и распарсить её JSON-вывод."""
    code, out, err = _run(["gh", *arguments], cwd=repo)
    if code == 127:
        raise StatsError(
            "the gh CLI is required to resolve an epic and is not available",
            remedy="install the GitHub CLI (gh) and ensure it is on PATH, or pass --tickets to run offline",
        )
    if code != 0:
        raise StatsError(
            f"gh {' '.join(arguments)} failed: {err or out or 'unknown error'}",
            remedy=f"inspect the gh error above and fix authentication/permissions before retrying 'gh {' '.join(arguments)}'",
        )
    try:
        return cast(JsonObject | list[JsonObject], json.loads(out)) if out else None
    except ValueError as exc:
        raise StatsError(
            "gh returned output that is not valid JSON",
            remedy=f"retry 'gh {' '.join(arguments)}'; if it keeps failing, check the gh CLI version",
        ) from exc


GLAB_AUTH_FAILURE = re.compile(r"auth login|\b401\b[^\n]*unauthorized", re.IGNORECASE)


class GitLabProject(NamedTuple):
    """Явная адресация проекта GitLab (docs/agents/issue-tracker.md → GitLab → Conventions)."""

    host: str
    # The `-R` value of every glab command.
    url: str
    # `projects/<URL-encoded project path>`, the REST path prefix of `GITLAB_HOST=<host> glab api`.
    api_prefix: str


def _gitlab_project(tracker: ProjectTracker) -> GitLabProject:
    """Адресация проекта GitLab из тройки резолвера трекера проекта."""
    if tracker.host is None or tracker.project is None:
        raise StatsError(
            "the GitLab tracker host or project is unknown",
            remedy='set "tracker": {"type": "gitlab", "host": ..., "project": ...} in .harness/project.json',
        )
    return GitLabProject(
        tracker.host,
        f"https://{tracker.host}/{tracker.project}",
        f"projects/{quote(tracker.project, safe='')}",
    )


def _glab(repo: Path, gitlab: GitLabProject, *arguments: str) -> object:
    """Выполнить команду glab и распарсить её JSON-вывод; страницы `--paginate`, напечатанные
    подряд, склеиваются в один список."""
    # glab rejects a port in `api --hostname` ("invalid hostname"); GITLAB_HOST keeps the port
    # and wins over the remote of the working directory.
    env = (
        {**os.environ, "GITLAB_HOST": gitlab.host}
        if arguments[:1] == ("api",)
        else None
    )
    prefix = f"GITLAB_HOST={gitlab.host} " if env else ""
    command_text = f"{prefix}glab {' '.join(arguments)}"
    code, out, err = _run(["glab", *arguments], cwd=repo, env=env)
    if code == 127:
        raise StatsError(
            "the glab CLI is required for a GitLab project and is not available",
            remedy="install the GitLab CLI (glab) and ensure it is on PATH, or pass --tickets to run offline",
        )
    if code != 0 and GLAB_AUTH_FAILURE.search(f"{err}\n{out}"):
        raise StatsError(
            f"glab is not authenticated on {gitlab.host}",
            remedy=f"run 'glab auth login --hostname {gitlab.host}', or pass --tickets to run offline",
        )
    if code != 0:
        raise StatsError(
            f"{command_text} failed: {err or out or 'unknown error'}",
            remedy=f"fix the cause named in the glab error (project access, host, glab version) before retrying '{command_text}', or pass --tickets to run offline",
        )
    decoder = json.JSONDecoder()
    values: list[object] = []
    index = 0
    try:
        while index < len(out):
            value, index = decoder.raw_decode(out, index)
            values.append(value)
            index = len(out) - len(out[index:].lstrip())
    except ValueError as exc:
        raise StatsError(
            "glab returned output that is not valid JSON",
            remedy=f"retry '{command_text}'; if it keeps failing, check the glab CLI version",
        ) from exc
    if len(values) <= 1:
        return values[0] if values else None
    return [item for page in values if isinstance(page, list) for item in page]


def _glab_issue(repo: Path, gitlab: GitLabProject, iid: object) -> JsonObject:
    """Задача проекта GitLab по её iid."""
    return cast(
        JsonObject,
        _glab(
            repo,
            gitlab,
            "issue",
            "view",
            str(iid),
            "-R",
            gitlab.url,
            "--output",
            "json",
        )
        or {},
    )


def _glab_api_list(repo: Path, gitlab: GitLabProject, path: str) -> list[JsonObject]:
    """Все страницы списка из REST API проекта GitLab."""
    return cast(
        list[JsonObject],
        _glab(
            repo,
            gitlab,
            "api",
            "--paginate",
            f"{gitlab.api_prefix}/{path}",
        )
        or [],
    )


ISSUE_BRANCH = re.compile(r"^[a-z]+/issue-(\d+)-")


def ticket_of_branch(branch: object) -> int | None:
    """Извлечь номер тикета из имени ветки вида issue-<number>-..."""
    if not isinstance(branch, str):
        return None
    match = ISSUE_BRANCH.match(branch)
    return int(match.group(1)) if match else None


def resolve_scope(repo: Path, epic: int) -> JsonObject:
    """Определить область охвата: эпик и все связанные с ним дочерние тикеты через GitHub CLI.

    Область охвата представляет собой набор номеров тикетов, а не живых веток: обычно эпик
    анализируется после слияния, когда ветки задач уже удалены. Все последующие шаги сопоставляют
    данные по номеру тикета, закодированному в имени ветки, который сохраняется в pull request и в транскриптах.
    """
    parent = cast(
        JsonObject,
        _gh(
            repo,
            "issue",
            "view",
            str(epic),
            "--json",
            "number,title,state,createdAt,closedAt,url",
        ),
    )
    children = cast(
        JsonObject, _gh(repo, "issue", "view", str(epic), "--json", "subIssues") or {}
    )
    nodes = ((children.get("subIssues") or {}).get("nodes")) or []
    tickets = [
        {
            "number": parent["number"],
            "title": parent["title"],
            "state": parent["state"],
            "role": "epic",
        }
    ]
    for node in nodes:
        tickets.append(
            {
                "number": node["number"],
                "title": node.get("title", ""),
                "state": node.get("state", ""),
                "role": "child",
            }
        )
    return {
        "epic": {
            "number": parent["number"],
            "title": parent["title"],
            "state": parent["state"],
            "url": parent.get("url", ""),
            "created_at": parent.get("createdAt"),
            "closed_at": parent.get("closedAt"),
        },
        "tickets": tickets,
        "numbers": {ticket["number"] for ticket in tickets},
    }


def resolve_gitlab_scope(repo: Path, epic: int, gitlab: GitLabProject) -> JsonObject:
    """Определить область охвата эпика на GitLab Free, где нет sub-issues.

    Дочерний тикет — задача того же проекта, связанная с эпиком через `relates_to`, в описании
    которой есть маркер `## Parent: #<epic>`: связь не имеет направления, поэтому маркер отсекает
    посторонние связи (docs/agents/issue-tracker.md → Wayfinding operations).
    """
    parent = _glab_issue(repo, gitlab, epic)
    marker = re.compile(rf"^## Parent: #{epic}\b", re.MULTILINE)
    tickets = [
        {
            "number": parent["iid"],
            "title": parent["title"],
            "state": parent["state"],
            "role": "epic",
        }
    ]
    for link in _glab_api_list(repo, gitlab, f"issues/{epic}/links"):
        if link.get("project_id") != parent.get("project_id"):
            continue
        child = _glab_issue(repo, gitlab, link["iid"])
        if not marker.search(child.get("description") or ""):
            continue
        tickets.append(
            {
                "number": child["iid"],
                "title": child.get("title", ""),
                "state": child.get("state", ""),
                "role": "child",
            }
        )
    return {
        "epic": {
            "number": parent["iid"],
            "title": parent["title"],
            "state": parent["state"],
            "url": parent.get("web_url", ""),
            "created_at": parent.get("created_at"),
            "closed_at": parent.get("closed_at"),
        },
        "tickets": tickets,
        "numbers": {ticket["number"] for ticket in tickets},
    }


def offline_scope(epic: int, tickets: str) -> JsonObject:
    """Сформировать область охвата из переданного списка тикетов без обращения к трекеру.

    В этом режиме CLI трекера не опрашивается, поэтому заголовки и статусы тикетов считаются неизвестными
    и отображаются как отсутствующие. Объём кода в этом случае берётся только из локальных git-ссылок.
    """
    numbers = []
    for chunk in tickets.replace(",", " ").split():
        if not chunk.isdigit():
            raise StatsError(
                f"--tickets expects issue numbers, got {chunk!r}",
                remedy="pass --tickets as a comma/space-separated list of issue numbers only",
            )
        numbers.append(int(chunk))
    if epic not in numbers:
        numbers.insert(0, epic)
    return {
        "epic": {
            "number": epic,
            "title": MISSING,
            "state": MISSING,
            "url": "",
            "created_at": None,
            "closed_at": None,
        },
        "tickets": [
            {
                "number": number,
                "title": MISSING,
                "state": MISSING,
                "role": "epic" if number == epic else "child",
            }
            for number in numbers
        ],
        "numbers": set(numbers),
        "offline": True,
    }


def pull_requests(repo: Path, numbers: set[int]) -> list[JsonObject]:
    """Получить pull request, исходные ветки которых относятся к тикетам из области охвата.

    GitHub сохраняет статистику изменений (diffstat) объединённого pull request даже после удаления ветки,
    поэтому PR является надёжным источником объёма кода; локальные ссылки служат лишь резервом.
    """
    listed = cast(
        list[JsonObject],
        _gh(
            repo,
            "pr",
            "list",
            "--state",
            "all",
            "--limit",
            "200",
            "--json",
            "number,headRefName,state,additions,deletions,changedFiles,mergedAt,url",
        )
        or [],
    )
    matched = []
    for entry in listed:
        ticket = ticket_of_branch(entry.get("headRefName"))
        if ticket in numbers:
            matched.append(
                {
                    **entry,
                    "ticket": ticket,
                    **_pull_request_commits(repo, entry["number"]),
                }
            )
    return matched


def _pull_request_commits(repo: Path, number: int) -> JsonObject:
    """Число коммитов pull request и пути ADR, добавленных его собственными коммитами."""
    detail = cast(
        JsonObject,
        _gh(repo, "pr", "view", str(number), "--json", "commits,files") or {},
    )
    commit_list = detail.get("commits") or []
    oids = {item.get("oid") for item in commit_list if item.get("oid")}
    adr = []
    for item in detail.get("files") or []:
        path = item.get("path", "")
        if _is_adr(path) and _added_by(repo, path) in oids:
            adr.append(path)
    return {"commits": len(commit_list), "adr_added": adr}


def gitlab_merge_requests(
    repo: Path, gitlab: GitLabProject, numbers: set[int]
) -> list[JsonObject]:
    """Получить merge request GitLab, исходные ветки которых относятся к тикетам из области охвата,
    в форме pull request из `pull_requests`."""
    listed = cast(
        list[JsonObject],
        _glab(
            repo,
            gitlab,
            "mr",
            "list",
            "-R",
            gitlab.url,
            "--all",
            "--per-page",
            "100",
            "--output",
            "json",
        )
        or [],
    )
    matched = []
    for entry in listed:
        ticket = ticket_of_branch(entry.get("source_branch"))
        if ticket in numbers:
            matched.append(
                {
                    "number": entry["iid"],
                    "headRefName": entry["source_branch"],
                    "state": entry.get("state"),
                    "mergedAt": entry.get("merged_at"),
                    "url": entry.get("web_url", ""),
                    "ticket": ticket,
                    **_merge_request_changes(repo, gitlab, entry["iid"]),
                }
            )
    return matched


def _merge_request_changes(repo: Path, gitlab: GitLabProject, iid: int) -> JsonObject:
    """Коммиты, diffstat и добавленные ADR одного merge request по REST API GitLab.

    Список MR не несёт diffstat, поэтому строки считаются по полю `diff` каждого файла: GitLab
    отдаёт в нём только hunk'и, без заголовков `---`/`+++`.
    """
    commits = _glab_api_list(repo, gitlab, f"merge_requests/{iid}/commits")
    diffs = _glab_api_list(repo, gitlab, f"merge_requests/{iid}/diffs")
    diff_lines = [
        line for item in diffs for line in (item.get("diff") or "").splitlines()
    ]
    return {
        "commits": len(commits),
        "additions": sum(1 for line in diff_lines if line.startswith("+")),
        "deletions": sum(1 for line in diff_lines if line.startswith("-")),
        "changedFiles": len(diffs),
        "adr_added": [
            item["new_path"]
            for item in diffs
            if item.get("new_file") and _is_adr(item.get("new_path", ""))
        ],
    }


# ------------------------------------------------------------------------------------ git volume


def git_volume(
    repo: Path, prs: list[JsonObject], extra_branches: set[str], base: str
) -> JsonObject:
    """Подсчитать объём изменений кода по тикетам (сначала из pull request, затем из локальных ссылок)."""
    entries = []
    totals = {
        "commits": 0,
        "insertions": 0,
        "deletions": 0,
        "files": 0,
        "pull_requests": 0,
    }
    covered: set[str] = set()

    adr: set[str] = set()
    for pr in prs:
        count = pr["commits"]
        adr.update(pr["adr_added"])
        entries.append(
            {
                "source": "pull-request",
                "ticket": pr["ticket"],
                "branch": pr["headRefName"],
                "pull_request": pr["number"],
                "state": pr.get("state"),
                "status": "ok",
                "commits": count,
                "insertions": _int(pr.get("additions")),
                "deletions": _int(pr.get("deletions")),
                "files": _int(pr.get("changedFiles")),
            }
        )
        covered.add(pr["headRefName"])
        totals["pull_requests"] += 1
        totals["commits"] += count
        totals["insertions"] += _int(pr.get("additions"))
        totals["deletions"] += _int(pr.get("deletions"))
        totals["files"] += _int(pr.get("changedFiles"))

    for branch in sorted(extra_branches - covered):
        ref = branch if _ref_exists(repo, branch) else f"origin/{branch}"
        if not _ref_exists(repo, ref):
            entries.append(
                {
                    "source": "branch",
                    "branch": branch,
                    "ticket": ticket_of_branch(branch),
                    "status": MISSING,
                    "reason": "ветка удалена, pull request не найден",
                }
            )
            continue
        try:
            merge_base = _git(repo, "merge-base", base, ref)
            numstat = _git(repo, "diff", "--numstat", merge_base, ref)
            commits = [
                line
                for line in _git(repo, "rev-list", f"{merge_base}..{ref}").splitlines()
                if line
            ]
        except StatsError:
            entries.append(
                {
                    "source": "branch",
                    "branch": branch,
                    "ticket": ticket_of_branch(branch),
                    "status": MISSING,
                    "reason": "не сравнить с базовой веткой",
                }
            )
            continue
        insertions = deletions = 0
        files: set[str] = set()
        for line in numstat.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            added, removed, path = parts
            insertions += int(added) if added.isdigit() else 0
            deletions += int(removed) if removed.isdigit() else 0
            files.add(path)
        entries.append(
            {
                "source": "branch",
                "ticket": ticket_of_branch(branch),
                "branch": branch,
                "status": "ok",
                "commits": len(commits),
                "insertions": insertions,
                "deletions": deletions,
                "files": len(files),
            }
        )
        totals["commits"] += len(commits)
        totals["insertions"] += insertions
        totals["deletions"] += deletions
        totals["files"] += len(files)
        for path in _added_adr(repo, base, ref):
            adr.add(path)
    return {
        "base": base,
        "entries": entries,
        "totals": totals,
        "adr_added": sorted(adr),
    }


def _is_adr(path: str) -> bool:
    """Проверить, является ли путь архитектурным решением (ADR в docs/adr/)."""
    return (
        path.startswith("docs/adr/")
        and path.endswith(".md")
        and not path.endswith("template.md")
    )


def _added_by(repo: Path, path: str) -> str | None:
    """Определить хеш коммита, впервые создавшего файл в репозитории.

    Pull request, который лишь редактирует существующую запись решения, не должен учитываться как
    добавивший её. Поэтому решающим является вхождение коммита в список собственных коммитов PR.
    """
    # --all: the adding commit may live on a branch that is not the current HEAD, which is the
    # normal case when the epic is measured from a different worktree or before its merge.
    code, out, _ = _run(
        [
            "git",
            "-C",
            str(repo),
            "log",
            "--all",
            "--diff-filter=A",
            "--format=%H",
            "--",
            path,
        ]
    )
    if code != 0 or not out:
        return None
    return out.splitlines()[-1].strip()


def _added_adr(repo: Path, base: str, ref: str) -> list[str]:
    """Найти пути всех файлов ADR, добавленных в ветке ref относительно base."""
    try:
        merge_base = _git(repo, "merge-base", base, ref)
        names = _git(repo, "diff", "--name-only", "--diff-filter=A", merge_base, ref)
    except StatsError:
        return []
    return [path for path in names.splitlines() if _is_adr(path)]


def _local_issue_branches(repo: Path) -> set[str]:
    """Собрать имена локальных и отслеживаемых удалённых веток тикетов в репозитории."""
    names: set[str] = set()
    for scope in ("refs/heads", "refs/remotes/origin"):
        code, out, _ = _run(
            ["git", "-C", str(repo), "for-each-ref", "--format=%(refname:short)", scope]
        )
        if code != 0:
            continue
        for name in out.splitlines():
            names.add(
                name.split("origin/", 1)[-1] if scope.endswith("origin") else name
            )
    return {name for name in names if ticket_of_branch(name) is not None}


def _ref_exists(repo: Path, ref: str) -> bool:
    """Проверить существование git-ссылки (коммита) в репозитории."""
    code, _, _ = _run(
        [
            "git",
            "-C",
            str(repo),
            "rev-parse",
            "--verify",
            "--quiet",
            f"{ref}^{{commit}}",
        ]
    )
    return code == 0


# --------------------------------------------------------------------------------- claude usage


CWD_PROBE_TRANSCRIPTS = 5
CWD_PROBE_RECORDS = 200


def _slug_variants(repo: Path) -> list[str]:
    """Сформировать возможные варианты имени директории проекта в хранилище транскриптов."""
    text = str(repo)
    variants = [re.sub(r"[\\/:]", "-", text), re.sub(r"[^A-Za-z0-9-]", "-", text)]
    seen = []
    for variant in variants:
        if variant not in seen:
            seen.append(variant)
    return seen


def _has_transcripts(directory: Path) -> bool:
    """Проверить, содержит ли директория непустые файлы транскриптов (*.jsonl)."""
    try:
        return any(
            path.is_file() and path.stat().st_size > 0
            for path in directory.glob("*.jsonl")
        )
    except OSError:
        return False


def _records_repo(directory: Path, repo: Path) -> bool:
    """Проверить по записям рабочих директорий в транскриптах, относятся ли они к репозиторию."""
    try:
        transcripts = sorted(
            directory.glob("*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return False
    for transcript in transcripts[:CWD_PROBE_TRANSCRIPTS]:
        for index, record in enumerate(_read_jsonl(transcript)):
            if index >= CWD_PROBE_RECORDS:
                break
            if _same_path(record.get("cwd"), repo):
                return True
    return False


def claude_project_dirs(home: Path, repo: Path) -> list[Path]:
    """Найти все директории с транскриптами Claude Code, принадлежащие этому репозиторию."""
    root = home / ".claude/projects"
    if not root.is_dir():
        return []
    try:
        candidates = [path for path in sorted(root.iterdir()) if path.is_dir()]
    except OSError:
        return []
    named = {root / slug for slug in _slug_variants(repo)}
    found = []
    for candidate in candidates:
        if not _has_transcripts(candidate):
            continue
        if candidate in named or _records_repo(candidate, repo):
            found.append(candidate)
    return found


def _claude_transcripts(directory: Path) -> list[tuple[Path, bool]]:
    """Получить список всех транскриптов проекта: основных сессий и вложенных сессий подагентов."""
    main = [(path, False) for path in sorted(directory.glob("*.jsonl"))]
    sub = [(path, True) for path in sorted(directory.glob("*/subagents/*.jsonl"))]
    return main + sub


def _is_billable_turn(record: JsonObject) -> bool:
    """Проверить, является ли запись тарифицируемым ходом API с реальной моделью."""
    message = record.get("message")
    return (
        record.get("type") == "assistant"
        and isinstance(message, dict)
        and message.get("model") not in NON_BILLABLE_MODELS
        and not record.get("isApiErrorMessage")
    )


def _usage_complete(usage: object) -> bool:
    """Проверить, содержит ли словарь использования токенов все обязательные числовые поля Claude."""
    return isinstance(usage, dict) and not any(
        not isinstance(usage.get(field), int) or isinstance(usage.get(field), bool)
        for field in CLAUDE_FIELDS
    )


def _turn_input(usage: JsonObject) -> int:
    """Вычислить общий входной объём токенов за ход (включая создание и чтение кэша)."""
    return sum(_int(usage.get(f)) for f in CLAUDE_FIELDS[:3])


def claude_usage(project_dirs: list[Path], numbers: set[int]) -> JsonObject:
    """Собрать статистику расхода токенов Claude Code по веткам тикетов из области охвата."""
    if not project_dirs:
        return {
            "status": MISSING,
            "reason": "нет транскриптов Claude Code для этого репозитория",
        }
    branches_seen: set[str] = set()
    models: JsonObject = {}
    sessions: set[str] = set()
    sidechain = {"input_tokens": 0, "output_tokens": 0, "turns": 0}
    thinking = 0
    first: datetime | None = None
    last: datetime | None = None
    quota: JsonObject | None = None
    turns = 0
    synthetic = 0
    incomplete_telemetry = False

    session_stats: dict[str, JsonObject] = {}

    transcripts = [
        entry for directory in project_dirs for entry in _claude_transcripts(directory)
    ]
    for transcript, is_subagent in transcripts:
        for record in _read_jsonl(transcript):
            branch = record.get("gitBranch")
            if not isinstance(branch, str) or ticket_of_branch(branch) not in numbers:
                continue
            branches_seen.add(branch)
            message = record.get("message")
            if record.get("type") != "assistant" or not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not _is_billable_turn(record):
                # Locally generated notices ("you've hit your session limit"), not API turns: their
                # usage is all zeros and no rate card can ever price them. Counting them would add a
                # pseudo-model to the breakdown and to the unpriced list for no reason.
                synthetic += 1
                continue
            if not isinstance(usage, dict) or not _usage_complete(usage):
                incomplete_telemetry = True
                continue
            turns += 1
            # A subagent's transcript replays the parent's sessionId inside its JSON records, so the
            # record alone cannot identify the subagent. Using the filename (transcript.stem) is the
            # established convention here, as the tool generating the logs (e.g. Claude Code) does not
            # currently emit a distinct subagentId field.
            session_id = (
                transcript.stem
                if is_subagent
                else (record.get("sessionId") or transcript.stem)
            )
            sessions.add(session_id)

            turn_input = _turn_input(usage)
            s_bucket = session_stats.setdefault(
                session_id,
                {
                    "branch": branch,
                    "kind": "subagent" if is_subagent else "main",
                    "turns": 0,
                    "total_input": 0,
                    "max_input": 0,
                },
            )
            s_bucket["turns"] += 1
            s_bucket["total_input"] += turn_input
            s_bucket["max_input"] = max(s_bucket["max_input"], turn_input)

            moment = _moment(record.get("timestamp"))
            if moment:
                first = moment if first is None or moment < first else first
                last = moment if last is None or moment > last else last
            bucket = models.setdefault(
                message.get("model") or "unknown",
                {field: 0 for field in CLAUDE_FIELDS} | {"turns": 0},
            )
            bucket["turns"] += 1
            for field in CLAUDE_FIELDS:
                bucket[field] += _int(usage.get(field))
            details = usage.get("output_tokens_details")
            if isinstance(details, dict):
                thinking += _int(details.get("thinking_tokens"))
            if record.get("isSidechain"):
                sidechain["turns"] += 1
                sidechain["input_tokens"] += sum(
                    _int(usage.get(f)) for f in CLAUDE_FIELDS[:3]
                )
                sidechain["output_tokens"] += _int(usage.get("output_tokens"))
            limits = record.get("quotaLimits")
            if isinstance(limits, dict) and limits:
                quota = limits

    if incomplete_telemetry:
        return {
            "status": MISSING,
            "reason": "неполная telemetry Claude Code на ветках этих тикетов",
        }
    if not turns:
        return {
            "status": MISSING,
            "reason": "нет ходов Claude Code на ветках этих тикетов",
        }
    return {
        "status": "ok",
        "attribution": "exact",
        "sources": [str(directory) for directory in project_dirs],
        "branches": sorted(branches_seen),
        "models": models,
        "turns": turns,
        "sessions": len(sessions),
        "thinking_tokens": thinking,
        "non_billable_turns": synthetic,
        "sidechain": sidechain,
        "session_stats": sorted(
            [{"id": k} | v for k, v in session_stats.items()],
            key=lambda x: x["total_input"],
            reverse=True,
        ),
        "first_activity": first.isoformat() if first else None,
        "last_activity": last.isoformat() if last else None,
        "quota": quota if quota else MISSING,
    }


def live_probe(project_dirs: list[Path], branch: str) -> JsonObject:
    """Собрать оперативный снимок активных сессий и подагентов Claude Code на указанной ветке.

    Снимок включает количество ходов, наибольший контекст входа за один ход и размер входа
    последнего хода. Предназначен для наблюдения за происходящим прямо сейчас на ветке,
    а не для итоговой оценки завершённого эпика.
    """
    if not project_dirs:
        return {
            "status": MISSING,
            "reason": "нет транскриптов Claude Code для этого репозитория",
        }
    sessions: dict[str, JsonObject] = {}
    for directory in project_dirs:
        for transcript, is_subagent in _claude_transcripts(directory):
            for record in _read_jsonl(transcript):
                if record.get("gitBranch") != branch:
                    continue
                if not _is_billable_turn(record):
                    continue
                usage = record["message"].get("usage")
                if not isinstance(usage, dict) or not _usage_complete(usage):
                    continue
                session_id = (
                    transcript.stem
                    if is_subagent
                    else (record.get("sessionId") or transcript.stem)
                )
                turn_input = _turn_input(usage)
                bucket = sessions.setdefault(
                    session_id,
                    {
                        "kind": "subagent" if is_subagent else "main",
                        "turns": 0,
                        "max_input": 0,
                        "last_input": 0,
                    },
                )
                bucket["turns"] += 1
                bucket["last_input"] = turn_input
                bucket["max_input"] = max(bucket["max_input"], turn_input)
    if not sessions:
        return {"status": MISSING, "reason": f"нет ходов Claude Code на ветке {branch}"}
    return {
        "status": "ok",
        "branch": branch,
        "sessions": [{"id": k} | v for k, v in sessions.items()],
    }


# ---------------------------------------------------------------------------------- codex usage


def codex_usage(
    sessions_root: Path | None,
    repo: Path,
    window: tuple[datetime | None, datetime | None],
) -> JsonObject:
    """Оценить расход токенов Codex по логам сессий в заданном временном окне репозитория.

    Codex не фиксирует имя ветки, сохраняя лишь рабочую директорию и метку времени.
    Поэтому работа относится к эпику по репозиторию и временному окну активности его веток.
    Это является оценкой, и отчёт отображает её именно как оценку.
    """
    if sessions_root is None or not sessions_root.is_dir():
        return {"status": MISSING, "reason": "на этой машине нет сессий Codex"}
    start, end = window
    if start is None or end is None:
        return {
            "status": MISSING,
            "reason": "нет окна активности, к которому можно отнести работу Codex",
        }
    models: JsonObject = {}
    sessions: set[str] = set()
    rate_limits: JsonObject | None = None
    turns = 0
    incomplete_telemetry = False

    for transcript in sorted(sessions_root.rglob("rollout-*.jsonl")):
        current_model = "unknown"
        in_repo = False
        counted: set[int | str] = set()
        pending: list[tuple[str, JsonObject]] = []
        transcript_incomplete = False
        for record in _read_jsonl(transcript):
            payload = record.get("payload")
            payload = payload if isinstance(payload, dict) else {}
            cwd = payload.get("cwd") or record.get("cwd")
            if _same_path(cwd, repo):
                in_repo = True
            if payload.get("model"):
                current_model = payload["model"]
            if payload.get("type") != "token_count":
                continue
            moment = _moment(record.get("timestamp"))
            if moment is None or not (start <= moment <= end):
                continue
            ordinal = record.get("ordinal")
            if ordinal is not None:
                if ordinal in counted:
                    continue
                counted.add(ordinal)
            info = payload.get("info")
            info = info if isinstance(info, dict) else {}
            delta = info.get("last_token_usage")
            if not isinstance(delta, dict) or any(
                not isinstance(delta.get(field), int)
                or isinstance(delta.get(field), bool)
                for field in CODEX_FIELDS
            ):
                transcript_incomplete = True
                continue
            pending.append((current_model, delta))
            limits = record.get("rate_limits") or payload.get("rate_limits")
            if isinstance(limits, dict) and limits:
                rate_limits = limits
        if not in_repo:
            continue
        if transcript_incomplete:
            incomplete_telemetry = True
            continue
        if not pending:
            continue
        sessions.add(transcript.stem)
        for model, delta in pending:
            turns += 1
            bucket = models.setdefault(
                model,
                {field: 0 for field in CODEX_FIELDS}
                | {"reasoning_output_tokens": 0, "turns": 0},
            )
            bucket["turns"] += 1
            for field in CODEX_FIELDS:
                bucket[field] += _int(delta.get(field))
            bucket["reasoning_output_tokens"] += _int(
                delta.get("reasoning_output_tokens")
            )

    if incomplete_telemetry:
        return {
            "status": MISSING,
            "reason": "неполная telemetry Codex по этому репозиторию внутри окна",
        }
    if not turns:
        return {
            "status": MISSING,
            "reason": "нет ходов Codex по этому репозиторию внутри окна",
        }
    return {
        "status": "ok",
        "attribution": "estimated",
        "attribution_note": (
            "В логах Codex нет ветки: сюда попадает работа по этому репозиторию внутри окна "
            "активности эпика, включая посторонние задачи того же периода."
        ),
        "models": models,
        "turns": turns,
        "sessions": len(sessions),
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "rate_limits": rate_limits if rate_limits else MISSING,
    }


# --------------------------------------------------------------------------- orchestration ledger


def _load_ledger_class(repo: Path) -> LedgerClass | None:
    """Динамически загрузить класс LifecycleLedger из .harness анализируемого репозитория.

    Оркестрация бэкенда является опциональной функциональностью, независимой от данного модуля отчётности.
    Класс загружается по пути к файлу под приватным именем, не оставаясь в sys.modules после импорта.
    Возвращает None, если модуль отсутствует или произошла ошибка импорта.
    """
    orchestration = repo / ".harness" / "orchestration"
    # ledger.py became the ledger/ package's lifecycle.py; a project installed from an older
    # harness still carries the flat module, so accept either layout.
    ledger_path = next(
        (
            path
            for path in (
                orchestration / "ledger" / "lifecycle.py",
                orchestration / "ledger.py",
            )
            if path.is_file()
        ),
        None,
    )
    if ledger_path is None:
        return None
    module_name = f"_delivery_stats_ledger_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, ledger_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001
        return None
    finally:
        sys.modules.pop(module_name, None)
    return cast(LedgerClass | None, getattr(module, "LifecycleLedger", None))


def _fallback_records_root(root: Path) -> Path | None:
    """Резервный алгоритм разрешения корня записей журнала поколений (ledger.json)."""
    pointer_path = root / "ledger.json"
    if not pointer_path.is_file():
        return root if root.is_dir() else None
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    generation = pointer.get("generation") if isinstance(pointer, dict) else None
    if not isinstance(generation, str):
        return None
    candidate = root / "generations" / generation
    return candidate if candidate.is_dir() else None


def _fallback_read_record(path: Path) -> JsonObject | None:
    """Резервный алгоритм чтения JSON-записи журнала."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _lenient_records_root(root: Path, ledger_cls: LedgerClass | None) -> Path | None:
    """Получить корень записей журнала через LifecycleLedger или резервный алгоритм."""
    if ledger_cls is None:
        return _fallback_records_root(root)
    try:
        return ledger_cls(root).records_root_lenient()
    except Exception:  # noqa: BLE001
        # ledger_cls came from a dynamically loaded module (_load_ledger_class): an incompatible
        # or old LifecycleLedger (e.g. missing records_root_lenient()) must degrade to the same
        # fallback as no module at all, never propagate out of orchestration_metrics().
        return _fallback_records_root(root)


def _lenient_read_record(
    path: Path, ledger_cls: LedgerClass | None
) -> JsonObject | None:
    """Прочитать JSON-запись журнала через LifecycleLedger или резервный алгоритм."""
    if ledger_cls is None:
        return _fallback_read_record(path)
    try:
        return ledger_cls.read_record_lenient(path)
    except Exception:  # noqa: BLE001
        # Same rationale as _lenient_records_root above.
        return _fallback_read_record(path)


def _dispatch_record(
    root: Path, dispatch_id: object, ledger_cls: LedgerClass | None
) -> JsonObject | None:
    """Загрузить JSON-запись диспетчеризации (dispatch) по её идентификатору."""
    if not isinstance(dispatch_id, str):
        return None
    return _lenient_read_record(root / "dispatches" / f"{dispatch_id}.json", ledger_cls)


def _developer_write_paths(
    root: Path, batch: JsonObject, ledger_cls: LedgerClass | None
) -> list[str] | None:
    """Определить объявленную зону путей записи разработчика (write_paths) для пакета задач."""
    for entry in reversed(batch.get("dispatches", [])):
        if not isinstance(entry, dict) or entry.get("role") != "developer":
            continue
        dispatch = _dispatch_record(root, entry.get("dispatch_id"), ledger_cls)
        paths = dispatch.get("write_paths") if dispatch else None
        if isinstance(paths, list) and paths:
            return paths
    return None


def _empty_ticket_orchestration() -> JsonObject:
    """Создать пустую структуру метрик оркестрации для тикета."""
    return {
        "batches": [],
        "worker_sessions": [],
        "qa_decided": 0,
        "qa_failed": 0,
        "review_scope": [],
    }


def _accumulate_batch_orchestration(
    root: Path, batch: JsonObject, bucket: JsonObject, ledger_cls: LedgerClass | None
) -> None:
    """Накопить метрики оркестрации из одного пакета (batch) в структуру тикета."""
    bucket["batches"].append(batch.get("batch_id"))
    restarts_by_dispatch: dict[str, list[JsonObject]] = {}
    for decision in batch.get("coordinator_decisions", []):
        if (
            not isinstance(decision, dict)
            or decision.get("decision") not in CONTINUATION_DECISIONS
        ):
            continue
        dispatch_id = decision.get("dispatch_id")
        if not isinstance(dispatch_id, str):
            continue
        restarts_by_dispatch.setdefault(dispatch_id, []).append(
            {
                "decision": decision.get("decision"),
                "reason": decision.get("note") or MISSING,
                "approved_at": decision.get("approved_at", MISSING),
            }
        )
    developer_paths = _developer_write_paths(root, batch, ledger_cls)
    for entry in batch.get("dispatches", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("dispatch_id"), str):
            continue
        dispatch_id = entry["dispatch_id"]
        restarts = restarts_by_dispatch.get(dispatch_id, [])
        bucket["worker_sessions"].append(
            {
                "dispatch_id": dispatch_id,
                "role": entry.get("role", MISSING),
                "sessions": 1 + len(restarts),
                "restarts": restarts,
            }
        )
        role = entry.get("role")
        decision = entry.get("decision")
        if role == "qa" and isinstance(decision, dict):
            bucket["qa_decided"] += 1
            if decision.get("decision") != "accept":
                bucket["qa_failed"] += 1
        if role == "code-review" and developer_paths is not None:
            dispatch = _dispatch_record(root, dispatch_id, ledger_cls)
            scope = dispatch.get("review_scope") if dispatch else None
            if isinstance(scope, list) and scope:
                out_of_scope = [
                    path
                    for path in scope
                    if not any(
                        fnmatchcase(str(path).replace("\\", "/"), pattern)
                        for pattern in developer_paths
                    )
                ]
                bucket["review_scope"].append(
                    {
                        "dispatch_id": dispatch_id,
                        "files_total": len(scope),
                        "files_out_of_scope": len(out_of_scope),
                        "out_of_scope_files": out_of_scope,
                        "share": round(len(out_of_scope) / len(scope), 4),
                    }
                )


def _finalize_ticket_orchestration(bucket: JsonObject) -> JsonObject:
    """Вычислить итоговые доли ошибок QA и превышения зоны ревью для тикета."""
    bucket["qa_failure_rate"] = (
        round(bucket["qa_failed"] / bucket["qa_decided"], 4)
        if bucket["qa_decided"]
        else MISSING
    )
    if not bucket["review_scope"]:
        bucket["review_scope"] = MISSING
    return bucket


def orchestration_metrics(
    repo: Path, numbers: set[int], state_dir: Path | None = None
) -> JsonObject:
    """Собрать структурные метрики оркестрации бэкенда по тикетам из журнала состояний.

    Метрики включают количество сессий воркеров и причины перезапуска/сжатия контекста,
    долю диффа код-ревью за пределами объявленной зоны записи разработчика и процент ошибок QA.
    Все числа берутся из записей координатора; при недоступности журнала метрика помечается как отсутствующая.
    """
    ledger_cls = _load_ledger_class(repo)
    root_dir = state_dir if state_dir is not None else repo / ORCHESTRATION_STATE_REL
    root = _lenient_records_root(root_dir, ledger_cls)
    if root is None:
        return {
            "status": MISSING,
            "reason": "оркестрационный ledger недоступен для этого репозитория",
        }
    batches_dir = root / "batches"
    if not batches_dir.is_dir():
        return {"status": MISSING, "reason": "в ledger нет записей batches"}
    tickets: dict[str, JsonObject] = {}
    for path in sorted(batches_dir.glob("batch-*.json")):
        batch = _lenient_read_record(path, ledger_cls)
        if batch is None:
            continue
        ticket_number = ticket_of_branch(batch.get("branch"))
        if ticket_number not in numbers:
            continue
        bucket = tickets.setdefault(str(ticket_number), _empty_ticket_orchestration())
        _accumulate_batch_orchestration(root, batch, bucket, ledger_cls)
    if not tickets:
        return {
            "status": MISSING,
            "reason": "в ledger нет batch-записей для тикетов этой области",
        }
    return {
        "status": "ok",
        "tickets": {
            number: _finalize_ticket_orchestration(bucket)
            for number, bucket in tickets.items()
        },
    }


# -------------------------------------------------------------------------------------- summary


def cache_split(claude: JsonObject) -> str | JsonObject:
    """Рассчитать процентное распределение входных токенов Claude (чтение кэша, запись кэша, свежий ввод)."""
    if claude.get("status") != "ok":
        return MISSING
    fresh = sum(bucket["input_tokens"] for bucket in claude["models"].values())
    write = sum(
        bucket["cache_creation_input_tokens"] for bucket in claude["models"].values()
    )
    read = sum(
        bucket["cache_read_input_tokens"] for bucket in claude["models"].values()
    )
    total = fresh + write + read
    if total == 0:
        return MISSING
    return {
        "total_input": total,
        "fresh": fresh,
        "cache_write": write,
        "cache_read": read,
        "fresh_percent": round(fresh * 100 / total, 3),
        "cache_write_percent": round(write * 100 / total, 3),
        "cache_read_percent": round(read * 100 / total, 3),
    }


def build_report(args: argparse.Namespace) -> JsonObject:
    """Собрать полный отчёт о статистике поставки по переданным параметрам запуска CLI."""
    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        raise StatsError(
            f"not a git repository: {repo}",
            remedy="pass --repo pointing at a real Git checkout",
        )
    config = _project_config(repo)
    base = args.base or config.get("base_branch") or "main"

    tracker = None if args.tickets else resolve_project_tracker(repo).effective
    gitlab = (
        _gitlab_project(tracker)
        if tracker is not None and tracker.type == "gitlab"
        else None
    )
    if args.tickets:
        scope = offline_scope(args.epic, args.tickets)
    elif gitlab is not None:
        scope = resolve_gitlab_scope(repo, args.epic, gitlab)
    else:
        scope = resolve_scope(repo, args.epic)
    numbers = scope["numbers"]

    home = Path(args.home).expanduser() if args.home else Path.home()
    if args.claude_projects:
        project_dirs = [Path(item) for item in args.claude_projects]
    else:
        project_dirs = claude_project_dirs(home, repo)
    claude = claude_usage(project_dirs, numbers)
    window = (
        _moment(claude.get("first_activity")),
        _moment(claude.get("last_activity")),
    )
    codex_root = (
        Path(args.codex_sessions) if args.codex_sessions else home / ".codex/sessions"
    )
    codex = codex_usage(codex_root, repo, window)

    rates_path = (
        Path(args.rates) if args.rates else repo / ".harness/reporting/rates.json"
    )
    cost = estimate_cost(claude, codex, load_rates(rates_path))

    if scope.get("offline"):
        prs = []
    elif gitlab is not None:
        prs = gitlab_merge_requests(repo, gitlab, numbers)
    else:
        prs = pull_requests(repo, numbers)
    local = {
        name
        for name in _local_issue_branches(repo)
        if ticket_of_branch(name) in numbers
    }
    volume = git_volume(repo, prs, local | set(claude.get("branches") or []), base)
    if (
        not prs
        and volume["totals"]["insertions"] == 0
        and claude.get("status") != "ok"
        and codex.get("status") != "ok"
    ):
        raise StatsError(
            f"epic #{args.epic}: no pull request, branch or session recorded for it or its sub-issues",
            remedy=f"verify epic #{args.epic}'s sub-issue numbers and that its PRs/branches/session logs exist locally, or pass --tickets explicitly",
        )
    tickets = scope["tickets"]
    for ticket in tickets:
        ticket["branches"] = sorted(
            {
                entry["branch"]
                for entry in volume["entries"]
                if entry.get("ticket") == ticket["number"]
            }
        )
    orchestration_state = (
        Path(args.orchestration_state_dir) if args.orchestration_state_dir else None
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "repository": repo.name,
        "epic": scope["epic"],
        "tickets": tickets,
        "tickets_closed": sum(
            1 for t in tickets if str(t.get("state", "")).upper() == "CLOSED"
        ),
        "tickets_total": len(tickets),
        "claude": claude,
        "codex": codex,
        "cache": cache_split(claude),
        "cost": cost,
        "volume": volume,
        "adr_added": len(volume["adr_added"]),
        "orchestration": orchestration_metrics(repo, numbers, orchestration_state),
    }


def main_live_probe(argv: list[str]) -> int:
    """Точка входа подкоманды live-probe для оперативного мониторинга сессий на ветке."""
    parser = argparse.ArgumentParser(
        prog="delivery_stats.py live-probe",
        description="Live snapshot of active Claude Code sessions/subagents on one branch.",
    )
    parser.add_argument("--repo", default=".", help="target project root")
    parser.add_argument("--branch", required=True, help="git branch to probe")
    parser.add_argument("--home", help="home directory holding agent session logs")
    parser.add_argument(
        "--claude-projects",
        action="append",
        help="explicit transcript directory; repeatable when a project has more than one",
    )
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    home = Path(args.home).expanduser() if args.home else Path.home()
    project_dirs = (
        [Path(p) for p in args.claude_projects]
        if args.claude_projects
        else claude_project_dirs(home, repo)
    )
    print(
        json.dumps(
            live_probe(project_dirs, args.branch),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    """Главная точка входа CLI delivery_stats: парсинг аргументов и формирование отчёта."""
    for stream in (sys.stdout, sys.stderr):
        # The report is authored in Russian; a legacy console code page would mangle it.
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass
    if sys.argv[1:2] == ["live-probe"]:
        return main_live_probe(sys.argv[2:])
    parser = argparse.ArgumentParser(
        description="Delivery statistics for one epic and its tickets."
    )
    parser.add_argument("--repo", default=".", help="target project root")
    parser.add_argument("--epic", type=int, required=True, help="epic issue number")
    parser.add_argument(
        "--base",
        help="base ref for diffing issue branches; defaults to project base_branch",
    )
    parser.add_argument(
        "--tickets",
        help="offline mode: comma-separated issue numbers instead of asking the tracker for sub-issues",
    )
    parser.add_argument(
        "--rates", help="rate card JSON; defaults to .harness/reporting/rates.json"
    )
    parser.add_argument(
        "--orchestration-state-dir",
        help="backend-orchestration ledger state directory; defaults to .harness/orchestration/state under --repo",
    )
    parser.add_argument("--home", help="home directory holding agent session logs")
    parser.add_argument(
        "--claude-projects",
        action="append",
        help="explicit transcript directory; repeatable when a project has more than one",
    )
    parser.add_argument("--codex-sessions", help="explicit Codex sessions directory")
    parser.add_argument(
        "--baseline", help="versioned baseline JSON saved by --save-baseline"
    )
    parser.add_argument(
        "--save-baseline", help="write this report's comparable baseline JSON to a path"
    )
    parser.add_argument("--html", help="write a standalone HTML dashboard to this path")
    parser.add_argument(
        "--json", action="store_true", help="print the full report as JSON"
    )
    args = parser.parse_args()

    try:
        report = build_report(args)
        current_baseline = baseline_snapshot(report)
        if args.baseline:
            report["comparison"] = compare_baseline(
                load_baseline(Path(args.baseline)), current_baseline
            )
        if args.save_baseline:
            save_baseline(current_baseline, Path(args.save_baseline))
        if args.html:
            from harness.reporting.render_html import write_dashboard

            destination = Path(args.html)
            write_dashboard(report, destination)
            report["html"] = str(destination)
    except HarnessError as exc:
        return print_and_exit(exc)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_terminal(report))
        if args.html:
            print(f"HTML: {report['html']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
