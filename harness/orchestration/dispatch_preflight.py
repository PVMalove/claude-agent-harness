"""Детерминированная подготовка передачи задач координатора без побочных эффектов.

Координатор вызывает этот модуль перед записью неизменяемого задания. Модуль намеренно
не модифицирует реестр жизненного цикла и не запускает транспорт, благодаря чему утверждение
всегда распространяется на те же разрешённые рантайм, рабочее дерево (worktree) и снимок,
которые будут использоваться итоговой диспетчеризацией.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from fnmatch import fnmatchcase
from pathlib import Path

from ..errors import HarnessError
from ..token_estimator import estimate_tokens, estimate_tokens_for_bytes
from . import runtime_access
from .contract import ContractError, paths_inside, resolve_runtime_name, string_list
from .core.config import _adaptive_continuation_policy
from .core.constants import RUNTIME_PATH_PREFIXES, TOOLING_BLOCKER_UNCOMMITTED_FIELD
from .ledger import JsonObject, JsonValue

# Structured handoff fields a compacted developer-retry start keeps: no report prose, no quoted
# finding evidence or logs.
_HANDOFF_REPORT_FIELDS = (
    "dispatch_id",
    "outcome",
    "commit_sha",
    "changed_files",
    "commit_map",
)
_HANDOFF_DECISION_FIELDS = ("dispatch_id", "role", "route", "reason_category")
_HANDOFF_FINDING_FIELDS = ("axis", "severity", "summary")


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
    retry_start: JsonObject | None = None

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


def _optional_text(value: object, label: str) -> str | None:
    """Вернуть непустую строку из состояния проекта либо None, если поле не задано."""
    return None if value is None else _text(value, label)


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


def _tokens(value: JsonValue) -> int:
    return estimate_tokens(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _pick(value: JsonValue, fields: tuple[str, ...]) -> JsonObject:
    source = value if isinstance(value, dict) else {}
    return {key: source[key] for key in fields if key in source}


def _retry_findings(handoff: JsonObject) -> list[JsonValue]:
    decision = handoff.get("retry_decision")
    findings = decision.get("findings") if isinstance(decision, dict) else None
    return findings if isinstance(findings, list) else []


def _structured_handoff(handoff: JsonObject) -> JsonObject:
    """The handoff reduced to its structured fields: findings lose their quoted evidence, and a
    tooling blocker keeps only the uncommitted files the restart inherits."""
    report = _pick(handoff.get("developer_report"), _HANDOFF_REPORT_FIELDS)
    uncommitted: list[JsonValue] = list(_uncommitted_files(handoff))
    if uncommitted:
        report["tooling_blocker"] = {TOOLING_BLOCKER_UNCOMMITTED_FIELD: uncommitted}
    return {
        "context_package_id": handoff.get("context_package_id"),
        "commit_plan": handoff.get("commit_plan"),
        "developer_report": report,
        "retry_decision": {
            **_pick(handoff.get("retry_decision"), _HANDOFF_DECISION_FIELDS),
            "findings": [
                _pick(finding, _HANDOFF_FINDING_FIELDS)
                for finding in _retry_findings(handoff)
            ],
        },
    }


def _uncommitted_files(handoff: JsonObject) -> list[str]:
    """The files a developer, blocked at its commit, listed as left uncommitted (issue #502)."""
    report = handoff.get("developer_report")
    blocker = report.get("tooling_blocker") if isinstance(report, dict) else None
    if not isinstance(blocker, dict):
        return []
    files = blocker.get(TOOLING_BLOCKER_UNCOMMITTED_FIELD)
    if not isinstance(files, list):
        return []
    return [path for path in files if isinstance(path, str)]


def _is_tooling_restart(handoff: JsonObject) -> bool:
    decision = handoff.get("retry_decision")
    return isinstance(decision, dict) and decision.get("route") == "tooling-retry"


def _check_restart_worktree(
    worktree: Path,
    config: JsonObject,
    zone: str | None,
    allowed_paths: JsonValue,
    handoff: JsonObject,
) -> None:
    """A tooling restart inherits exactly the uncommitted changes its blocked report listed.

    HEAD is already pinned to the developer's last commit; here the worktree's uncommitted paths
    must equal the report's list, every one inside the batch's explicit ``allowed_paths`` (a batch
    recorded before explicit scopes falls back to its zone's paths). A clean worktree with an empty
    list passes as before; anything else is refused with the discrepancy named.
    """
    paths: list[str]
    explicit = False
    if string_list(allowed_paths) and allowed_paths:
        explicit = True
        paths = allowed_paths
        scope = "the batch allowed_paths"
    else:
        zones = config.get("backend_zones")
        declared = zones.get(zone) if isinstance(zones, dict) and zone else None
        zone_paths = declared.get("paths") if isinstance(declared, dict) else None
        if not string_list(zone_paths) or not zone_paths:
            raise PreflightError(
                f"project config declares no paths for zone {zone!r}",
                remedy=f"declare backend_zones[{zone!r}].paths in the project orchestration config",
            )
        paths = zone_paths
        scope = f"the batch zone {zone!r}"
    git_output = "\0".join(
        (
            _git(worktree, "diff", "--name-only", "--no-renames", "-z", "HEAD"),
            _git(worktree, "ls-files", "--others", "--exclude-standard", "-z"),
        )
    )
    actual = {
        path
        for path in git_output.split("\0")
        if path and not path.startswith(RUNTIME_PATH_PREFIXES)
    }
    expected = set(_uncommitted_files(handoff))
    outside = sorted(
        path
        for path in actual | expected
        if not (
            paths_inside([path], paths)
            if explicit
            else any(fnmatchcase(path, pattern) for pattern in paths)
        )
    )
    extra = sorted(actual - expected - set(outside))
    missing = sorted(expected - actual - set(outside))
    if not (outside or extra or missing):
        return
    discrepancies = [
        (label, fix, group)
        for label, fix, group in (
            (f"outside {scope}", "revert or move out", outside),
            ("not listed in the blocked report", "revert", extra),
            ("missing from the worktree", "restore from the blocked session", missing),
        )
        if group
    ]
    found = [f"{label}: {', '.join(group)}" for label, _, group in discrepancies]
    fixes = [f"{fix} {', '.join(group)}" for _, fix, group in discrepancies]
    raise PreflightError(
        "tooling restart worktree does not match the blocked developer report's "
        f"uncommitted files ({'; '.join(found)})",
        remedy=f"{'; '.join(fixes)}, so the uncommitted files equal the blocked report's "
        "tooling_blocker uncommitted_files, or decide the batch again",
    )


def _named_in(path: str, text: str) -> bool:
    """Whether ``text`` names ``path`` as a whole path, not as part of a longer one."""
    return re.search(rf"(?<![\w/.-]){re.escape(path)}(?![\w/.-])", text) is not None


def _starting_file_tokens(worktree: Path, item: JsonObject) -> int:
    """A starting file costs its section index when it is a large document, else its bytes."""
    sections = item.get("sections")
    if sections:
        return _tokens(sections)
    path = worktree / str(item.get("path", ""))
    return estimate_tokens_for_bytes(path.stat().st_size) if path.is_file() else 0


def _retry_start(
    config: JsonObject,
    worktree: Path,
    brief: JsonObject,
    package: JsonObject | None,
    handoff: JsonObject,
) -> JsonObject:
    """The starting context of a developer retry, compacted when it leaves the smart zone.

    The estimate covers the brief, the Context Package and the handoff against
    ``context_warn_ratio × context_limit``. Above it, the handoff keeps only structured fields and
    the starting files narrow to those a finding names or the developer changed, while a large
    document stays as its section index. The dispatch is never blocked: a compact that cannot reach
    the threshold records a warning.
    """
    policy = _adaptive_continuation_policy(config)
    threshold = round(policy["context_limit"] * policy["context_warn_ratio"])
    seeds = (package or {}).get("starting_files")
    files = (
        [item for item in seeds if isinstance(item, dict)]
        if isinstance(seeds, list)
        else []
    )
    package_tokens = (package or {}).get("estimated_tokens", 0)
    if not isinstance(package_tokens, int):
        package_tokens = _tokens(package)
    brief_tokens = _tokens(brief)
    before = brief_tokens + package_tokens + _tokens(handoff)
    after, compacted = before, before > threshold
    if compacted:
        report = handoff.get("developer_report")
        changed = report.get("changed_files") if isinstance(report, dict) else None
        keep = {
            path
            for path in (changed if isinstance(changed, list) else [])
            if isinstance(path, str)
        } | set(_uncommitted_files(handoff))
        findings = json.dumps(_retry_findings(handoff), ensure_ascii=False)
        kept = [
            item
            for item in files
            if item.get("sections")
            or item.get("path") in keep
            or _named_in(str(item.get("path")), findings)
        ]
        dropped = sum(
            _starting_file_tokens(worktree, item) for item in files if item not in kept
        )
        files, handoff = kept, _structured_handoff(handoff)
        after = brief_tokens + max(package_tokens - dropped, 0) + _tokens(handoff)
    return {
        "handoff": handoff,
        "starting_files": list(files),
        "context_estimate": {
            "threshold": threshold,
            "before": before,
            "after": after,
            "compacted": compacted,
        },
        "warning": None
        if after <= threshold
        else f"developer-retry starting context estimate {after} tokens stays above the "
        f"smart-zone threshold {threshold} after the compact; the dispatch proceeds",
    }


def prepare(
    ticket: str, role: str, project_state: Mapping[str, JsonValue]
) -> PreparedDispatch:
    """Разрешить и валидировать потенциальную диспетчеризацию без её фактического создания.

    ``project_state`` намеренно представляет собой простые данные, чтобы CLI, адаптер или тесты могли
    вызывать одну и ту же детерминированную функцию. Обязательные поля: ``repo``, ``config``, ``branch``,
    ``worktree`` и ``base_sha``; ``zone`` — необязательная историческая метка, ``candidate_sha`` требуется только при явной фиксации кандидата.
    Для developer-retry ``retry_handoff`` и ``retry_package`` дают компактный старт ``retry_start``.
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
    if not isinstance(plan, dict) and plans:
        raise PreflightError(
            f"project_state has no assignment plan for role {role!r}",
            remedy=f"add an assignment_plans[{role!r}] object to the project config",
        )
    try:
        runtime = (
            resolve_runtime_name(plan, project_state.get("runtime"))
            if isinstance(plan, dict)
            else str(project_state.get("runtime") or "session")
        )
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
        "zone": _optional_text(project_state.get("zone"), "zone"),
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
    # Authored requirements follow the runtime's role manifest, including specialist writers.
    from .core.config import _role

    role_access = (
        _role(repo, role)["mode"] if "access_policy" in config else "read-only"
    )
    access_plan = runtime_access.resolve_plan(
        repo,
        worktree,
        config,
        role,
        str(role_access),
        operation=runtime_access.dispatch_operation(
            role, _optional_text(project_state.get("purpose"), "purpose") or "work"
        ),
    )
    verification, _ = runtime_access.verify_plan(
        access_plan,
        str(plan.get("transport", "in-process"))
        if isinstance(plan, dict)
        else "in-process",
    )
    preview["runtime_access"] = access_plan
    packet["runtime_access"] = {"plan": access_plan, "verification": verification}
    handoff = project_state.get("retry_handoff")
    retry_package = project_state.get("retry_package")
    if (
        role == "developer"
        and isinstance(handoff, dict)
        and _is_tooling_restart(handoff)
    ):
        _check_restart_worktree(
            worktree,
            config,
            _optional_text(project_state.get("zone"), "zone"),
            project_state.get("allowed_paths"),
            handoff,
        )
    retry_start = (
        _retry_start(
            config,
            worktree,
            preview,
            retry_package if isinstance(retry_package, dict) else None,
            handoff,
        )
        if role == "developer" and isinstance(handoff, dict)
        else None
    )
    if retry_start is not None:
        # The approval packet carries the retry's smart-zone evidence; it never gates the dispatch.
        packet["retry_context_estimate"] = retry_start["context_estimate"]
        packet["retry_context_warning"] = retry_start["warning"]
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
        retry_start=retry_start,
    )
