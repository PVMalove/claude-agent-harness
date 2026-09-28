"""Детерминированная подготовка передачи задач координатора без побочных эффектов.

Координатор вызывает этот модуль перед записью неизменяемого задания. Модуль намеренно
не модифицирует реестр жизненного цикла и не запускает транспорт, благодаря чему утверждение
всегда распространяется на те же разрешённые рантайм, рабочее дерево (worktree) и снимок,
которые будут использоваться итоговой диспетчеризацией.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from ..errors import HarnessError
from .contract import ContractError, resolve_runtime_name, string_list
from .ledger import JsonObject, JsonValue


class PreflightError(HarnessError):
    """Диспетчеризация не может быть безопасно подготовлена на основе переданного состояния проекта."""


@dataclass(frozen=True)
class PreparedDispatch:
    """Структурированные неизменяемые данные подготовленной к диспетчеризации задачи."""

    ticket: str
    role: str
    integration_ref: str
    base_sha: str
    candidate_sha: str | None
    issue_branch: str
    worktree: str
    worktree_sha: str
    runtime: str
    mandatory_checks: list[str]
    context_package: JsonObject
    preview_brief: JsonObject
    decision_packet: JsonObject

    def to_dict(self) -> JsonObject:
        """Преобразовать подготовленные данные диспетчеризации в словарь."""
        return asdict(self)


def _text(value: object, label: str) -> str:
    """Проверить и вернуть непустую строку из состояния проекта."""
    if not isinstance(value, str) or not value.strip():
        raise PreflightError(
            f"project_state requires a non-empty {label}",
            remedy=f"set project_state[{label!r}] to a non-empty string",
        )
    return value.strip()


def _git(path: Path, *args: str) -> str:
    """Выполнить команду Git в указанном каталоге и вернуть stdout либо возбудить PreflightError."""
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise PreflightError(
            f"git {' '.join(args)} failed: {detail or 'unknown error'}",
            remedy=f"inspect the git error above and fix the repository/worktree state before retrying 'git {' '.join(args)}'",
        )
    return result.stdout.strip()


def _worktree_paths(repo: Path) -> set[Path]:
    """Получить множество зарегистрированных путей рабочих деревьев Git."""
    output = _git(repo, "worktree", "list", "--porcelain")
    paths: set[Path] = set()
    for line in output.splitlines():
        if line.startswith("worktree "):
            paths.add(Path(line.removeprefix("worktree ")).resolve())
    return paths


def _role_context(
    role: str, state: Mapping[str, JsonValue], snapshot: str
) -> JsonObject:
    """Компактные метаданные контекстных указателей роли; содержимое пакетов остаётся под управлением реестра."""
    keys = {
        "architect": ("contracts", "neighbour_tickets", "starting_files"),
        "developer": (
            "architecture_decision",
            "affected_symbols",
            "related_tests",
            "starting_files",
        ),
        "code-review": (
            "pinned_diff",
            "prior_findings",
            "verification_commands",
            "starting_files",
        ),
    }.get(role, ("starting_files",))
    included: JsonObject = {"snapshot_sha": snapshot, "role": role}
    for key in keys:
        value = state.get(key)
        if value not in (None, [], ""):
            included[key] = value
    return included


def prepare(
    ticket: str, role: str, project_state: Mapping[str, JsonValue]
) -> PreparedDispatch:
    """Разрешить и валидировать потенциальную диспетчеризацию без её фактического создания.

    ``project_state`` намеренно представляет собой простые данные, чтобы CLI, адаптер или тесты могли
    вызывать одну и ту же детерминированную функцию. Обязательные поля: ``repo``, ``config``, ``branch``,
    ``worktree``, ``zone`` и ``base_sha``; ``candidate_sha`` требуется только при явной фиксации кандидата.
    """
    ticket = _text(ticket, "ticket")
    role = _text(role, "role")
    repo = Path(_text(project_state.get("repo"), "repo")).resolve()
    if not repo.is_dir():
        raise PreflightError(
            "project_state repo does not exist",
            remedy="point project_state['repo'] at an existing directory",
        )
    config = project_state.get("config")
    if not isinstance(config, dict):
        raise PreflightError(
            "project_state config must be an object",
            remedy="set project_state['config'] to a JSON object",
        )
    plans = config.get("assignment_plans")
    plan = plans.get(role) if isinstance(plans, dict) else None
    if not isinstance(plan, dict):
        raise PreflightError(
            f"project_state has no assignment plan for role {role!r}",
            remedy=f"add an assignment_plans[{role!r}] object to the project config",
        )
    try:
        runtime = resolve_runtime_name(plan, project_state.get("runtime"))
    except ContractError as exc:
        raise PreflightError(
            str(exc),
            remedy="fix the runtime/provider selection reported above in the assignment plan or project_state['runtime']",
        ) from exc

    branch = _text(project_state.get("branch"), "branch")
    worktree = Path(_text(project_state.get("worktree"), "worktree")).resolve()
    base_sha = _text(project_state.get("base_sha"), "base_sha")
    raw_candidate = project_state.get("candidate_sha")
    candidate = None if raw_candidate is None else _text(raw_candidate, "candidate_sha")
    integration_ref = _text(
        project_state.get("integration_ref") or "base", "integration_ref"
    )
    if worktree not in _worktree_paths(repo):
        raise PreflightError(
            "worktree is not registered by git worktree",
            remedy=f"run 'git worktree add' for {worktree} or point project_state['worktree'] at a registered worktree",
        )
    if _git(worktree, "rev-parse", "--is-inside-work-tree") != "true":
        raise PreflightError(
            "worktree is not a Git worktree",
            remedy=f"point project_state['worktree'] at a real Git worktree, not {worktree}",
        )
    worktree_sha = _git(worktree, "rev-parse", "--verify", "HEAD^{commit}")
    expected_sha = candidate or _text(
        project_state.get("snapshot_sha") or base_sha, "snapshot_sha"
    )
    if worktree_sha != expected_sha:
        raise PreflightError(
            f"worktree is pinned to {worktree_sha}, expected snapshot {expected_sha}",
            remedy=f"checkout {expected_sha} in the worktree, or update snapshot_sha/candidate_sha to match {worktree_sha}",
        )
    if (
        role in {"architect", "developer"}
        and _git(worktree, "branch", "--show-current") != branch
    ):
        raise PreflightError(
            "write/planning worktree is not on the resolved issue branch",
            remedy=f"checkout branch {branch!r} in the worktree before dispatching this role",
        )

    checks = project_state.get("mandatory_checks", [])
    if not string_list(checks):
        raise PreflightError(
            "project_state mandatory_checks must be a list of commands",
            remedy="set project_state['mandatory_checks'] to a list of non-empty command strings",
        )
    package = _role_context(role, project_state, expected_sha)
    preview: JsonObject = {
        "ticket": ticket,
        "role": role,
        "branch": branch,
        "worktree": str(worktree),
        "zone": _text(project_state.get("zone"), "zone"),
        "resolved_runtime": runtime,
        "base_commit": base_sha,
        "snapshot_commit": expected_sha,
        "candidate_commit": candidate,
        "context_package": package,
        "verification_commands": list(checks),
    }
    packet: JsonObject = {
        "action": f"create and send {role} dispatch",
        "branch": branch,
        "worktree": str(worktree),
        "runtime": runtime,
        "base_sha": base_sha,
        "snapshot_sha": expected_sha,
        "candidate_sha": candidate,
        "checks": list(checks),
        "context_package": package,
        "approval_reason": "the immutable brief will bind this exact runtime, worktree and snapshot",
        "options": ["accept", "retry", "block", "full review", "delta-review"],
    }
    return PreparedDispatch(
        ticket=ticket,
        role=role,
        integration_ref=integration_ref,
        base_sha=base_sha,
        candidate_sha=candidate,
        issue_branch=branch,
        worktree=str(worktree),
        worktree_sha=worktree_sha,
        runtime=runtime,
        mandatory_checks=list(checks),
        context_package=package,
        preview_brief=preview,
        decision_packet=packet,
    )
