"""Переносимая политика манифестов, назначений и заданий диспетчеризации для бэкенд-оркестрации.

Точки входа рантайма намеренно адаптируют этот модуль вместо повторного определения ролевых полномочий.
Модуль не зависит от хранилища состояния координатора или конкретного рантайма, благодаря чему проверки
состояния (health checks) и внутрипроцессные передачи получают одинаковый результат применения политик
для одних и тех же входных данных проекта.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TypeGuard, cast

from ..errors import INTERNAL_INVARIANT_REMEDY, HarnessError
from .extensions import DEFAULT_EXTENSION, EXTENSION_KINDS, EXTENSION_NAME

ROLE_MODES = {"write", "read-only"}
ROLE_TRANSPORTS = {"in-process", "external"}
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")
# ``input_tokens`` and friends are accounting fields, not secrets.  Match token-shaped
# credentials precisely so policy can safely validate token budgets and provider telemetry.
SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|credential|password|secret|(?:access|auth|refresh|id|bearer)[_-]?token|(?:^|[_-])token(?:$|[_-](?:id|value|secret|key)$))",
    re.IGNORECASE,
)
EFFORT_LEVELS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)
ROLE_FIELDS = frozenset({"name", "mode", "required_capabilities", "risk_triggers"})
CONFIG_REQUIRED_FIELDS = (
    "provider_profiles",
    "assignment_plans",
    "concurrency_budget",
    "verification_commands",
)
CONFIG_ALLOWED_FIELDS = frozenset(CONFIG_REQUIRED_FIELDS) | {
    "$schema",
    # Legacy zone model: still accepted so an existing project config keeps validating, never required.
    "backend_zones",
    "developer_verification_commands",
    "review_verification_commands",
    "qa_preparation",
    "qa_environment_probes",
    "qa_project_file_checks",
    "test_path_patterns",
    "adaptive_continuation_policy",
    "approval_policy",
    "low_risk_zones",
    "low_risk_paths",
    "context_package_policy",
    "repo_map_policy",
    "continuation_policy",
    "retry_policy",
    "preflight_policy",
    "worker_attestation_required",
    "communication_policy",
    "human_approval_gate",
    "tool_policy",
    "attention_policy",
    "execution_policy",
    "approval_ttl_seconds",
    "extensions",
    "access_policy",
    "infrastructure_retry_policy",
}
# The working set a brief records for a role when the project states no `tool_policy`. It is the
# role's own set, not a deny-list: global runtime tools stay available whatever a brief records.
DEFAULT_ALLOWED_TOOLS = {
    "read-only": ("Read", "Grep", "Glob", "Bash"),
    "write": ("Read", "Grep", "Glob", "Bash", "Edit", "Write"),
}
TOOL_POLICY_SECTIONS = ("modes", "roles")
APPROVAL_POLICIES = {"manual_all", "milestone", "low_risk", "auto"}
# `auto` takes every path decision by policy (issue #643), so it needs a project that can trust
# what a worker reports about its own runtime and that does not demand a human on a terminal.
AUTO_TTY_PROBLEM = "orchestration approval_policy 'auto' cannot be combined with human_approval_gate 'tty'"
AUTO_ATTESTATION_PROBLEM = (
    "orchestration approval_policy 'auto' requires worker_attestation_required true"
)
HUMAN_APPROVAL_GATES = {"trusted", "tty"}
COMMUNICATION_POLICY_FIELDS = frozenset(
    {"agent_to_agent_language", "coordinator_report_language"}
)
COMMUNICATION_LANGUAGES = {"en", "ru"}
CODE_REVIEW_REQUIRED_RISK_TRIGGERS = frozenset(
    {
        "api-public-contract",
        "schema-change",
        "data-migration",
        "outbox",
        "queues",
        "message-schema-routing",
        "transactions",
        "authorization-security",
        "concurrency-retry",
        "retry-dlq",
    }
)


class ContractError(HarnessError):
    """Манифест, назначение или неизменяемое задание нарушают переносимую ролевую политику."""


# Role manifests and resolved assignments are dynamic, JSON-shaped documents that the coordinator and
# adapters consume as ``dict[str, Any]``; this is the one intentional dynamic boundary of the module.
JsonObject = dict[str, Any]  # type: ignore[explicit-any]


def non_empty(value: object) -> TypeGuard[str]:
    """Проверить, что значение является непустой строкой без пробельных символов по краям."""
    return isinstance(value, str) and bool(value.strip())


def string_list(value: object) -> TypeGuard[list[str]]:
    """Проверить, что значение является списком непустых строк."""
    return isinstance(value, list) and all(non_empty(item) for item in value)


def _is_int(value: object) -> TypeGuard[int]:
    """Проверить, что значение является целым числом и не является булевым флагом."""
    return isinstance(value, int) and not isinstance(value, bool)


def reject_sensitive(value: object, location: str) -> None:
    """Рекурсивно отклонить структуры данных, содержащие имена полей, похожие на секреты или токены доступа."""
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ContractError(
                    f"{location} contains a non-string key",
                    remedy=f"use only string keys in {location}",
                )
            if SENSITIVE_KEY.search(key):
                raise ContractError(
                    f"{location} contains secret-shaped field {key!r}",
                    remedy=f"remove the secret-shaped field {key!r} from {location}; credentials never belong in this config",
                )
            reject_sensitive(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_sensitive(child, f"{location}[{index}]")


def load_role_manifest(path: Path) -> JsonObject:
    """Разобрать компактный frontmatter манифеста роли без зависимости от сторонних YAML-библиотек."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractError(
            f"role manifest {path.name!r} cannot be read",
            remedy=f"fix the file-system error above for {path}",
        ) from exc
    match = re.match(r"\A---\r?\n(?P<body>.*?)\r?\n---(?:\r?\n|$)", text, re.DOTALL)
    if not match:
        raise ContractError(
            f"role manifest {path.name!r} has no valid frontmatter",
            remedy=f"start {path.name} with a '---'-delimited YAML-like frontmatter block",
        )
    metadata: dict[str, str | list[str]] = {}
    current: str | None = None
    for line in match.group("body").splitlines():
        if not line.strip():
            continue
        item = re.fullmatch(r"\s+-\s+(.+?)\s*", line)
        if item:
            if current is None:
                raise ContractError(
                    f"role manifest {path.name!r} has an orphan list item",
                    remedy=f"in {path.name}, put each '- item' line under a preceding 'key:' line",
                )
            cast(list[str], metadata.setdefault(current, [])).append(item.group(1))
            continue
        field = re.fullmatch(r"([a-z_]+):\s*(.*?)\s*", line)
        if not field:
            raise ContractError(
                f"role manifest {path.name!r} has invalid frontmatter",
                remedy=f"in {path.name}, use only 'key: value' or '  - item' lines in the frontmatter block",
            )
        key, value = field.groups()
        current = key if not value else None
        metadata[key] = [] if not value else value
    return metadata


def is_clean_path_pattern(path: str) -> bool:
    """Whether a path or glob is repo-relative in its canonical form.

    A leading ``/``, a backslash, or an empty, ``.`` or ``..`` segment (``./src``, ``src//x``,
    ``src/../x``) is never canonical: such a form would be compared literally here and matched
    differently by the glob that checks reported files.
    """
    if not path or path.startswith("/") or "\\" in path:
        return False
    return all(segment not in {"", ".", ".."} for segment in path.split("/"))


def _inside(path: str, boundary: str) -> bool:
    """Проверить, находится ли путь внутри границы (по сегментам пути, а не по префиксу строки).

    Граница ``**`` принимает любой путь, ``dir/**`` - всё строго под ``dir``, любая другая граница -
    только ровно себя. Неканоническая форма пути или границы никогда не считается внутри.
    """
    if not is_clean_path_pattern(path) or not is_clean_path_pattern(boundary):
        return False
    if boundary == "**":
        return True
    if boundary.endswith("/**"):
        prefix = boundary.removesuffix("/**").split("/")
        parts = path.split("/")
        return len(parts) > len(prefix) and parts[: len(prefix)] == prefix
    return path == boundary


def paths_inside(paths: list[str], boundaries: list[str]) -> bool:
    """Whether every path stays inside at least one boundary pattern."""
    return all(
        any(_inside(path, boundary) for boundary in boundaries) for path in paths
    )


def low_risk_patterns(config: Mapping[str, object]) -> list[str]:
    """The path patterns whose batches may continue under ``approval_policy: low_risk``.

    ``low_risk_paths`` is the zone-free declaration. A legacy ``low_risk_zones`` list maps to the
    paths of the zones it names, so an existing configuration keeps exactly the authority it had.
    """
    patterns: list[str] = []
    declared = config.get("low_risk_paths")
    if string_list(declared):
        patterns.extend(declared)
    zones = config.get("backend_zones")
    legacy = config.get("low_risk_zones")
    if string_list(legacy) and isinstance(zones, dict):
        for name in legacy:
            zone = zones.get(name)
            paths = zone.get("paths") if isinstance(zone, dict) else None
            if string_list(paths):
                patterns.extend(paths)
    return patterns


def low_risk_eligible(
    config: Mapping[str, object], batch: Mapping[str, object]
) -> bool:
    """Whether a batch's explicit scope lies entirely inside the project's low-risk paths.

    Nothing is eligible until the project names low-risk paths (or legacy zones). A batch recorded
    before explicit scopes existed has no ``allowed_paths``; it keeps its historical rule: its zone
    must be one of ``low_risk_zones``.
    """
    patterns = low_risk_patterns(config)
    if not patterns:
        return False
    allowed = batch.get("allowed_paths")
    if allowed is None:
        zone = batch.get("zone")
        legacy = config.get("low_risk_zones")
        return non_empty(zone) and string_list(legacy) and zone in legacy
    return string_list(allowed) and bool(allowed) and paths_inside(allowed, patterns)


def _valid_model(value: object) -> str:
    """Проверить и вернуть корректный идентификатор модели CLI без пробелов."""
    if not non_empty(value) or MODEL_ID.fullmatch(value.strip()) is None:
        raise ContractError(
            "assignment model must be a CLI model ID or alias without spaces",
            remedy="set the assignment's model to a non-empty CLI model ID/alias with no spaces",
        )
    return value.strip()


def _valid_effort(value: object) -> str:
    """Проверить и вернуть допустимый уровень рассуждений (effort)."""
    if not non_empty(value) or value.strip() not in EFFORT_LEVELS:
        raise ContractError(
            "assignment effort must be one of: " + ", ".join(sorted(EFFORT_LEVELS)),
            remedy="set the assignment's effort to one of: "
            + ", ".join(sorted(EFFORT_LEVELS)),
        )
    return value.strip()


def resolve_runtime_name(plan: Mapping[str, object], requested: object) -> str:
    """Разрешить имя рантайма роли без неявного глобального значения по умолчанию провайдера.

    План с одним рантаймом однозначен. План с несколькими рантаймами должен либо указывать
    собственный default_runtime проекта, либо получать явный выбор из CLI. Сохранение этого
    решения в переносимом контракте позволяет preflight, координатору и адаптерам согласовывать
    рантайм до формирования задания.
    """
    runtimes = plan.get("runtimes")
    if not isinstance(runtimes, dict) or not runtimes:
        raise ContractError(
            "assignment plan has no runtime assignments",
            remedy="add at least one entry under the assignment plan's runtimes",
        )
    if non_empty(requested):
        runtime = requested.strip()
        if runtime not in runtimes:
            raise ContractError(
                f"role is not assigned to runtime {runtime!r}",
                remedy=f"pass --runtime as one of the assigned runtimes: {', '.join(sorted(runtimes))}",
            )
        return runtime
    if len(runtimes) == 1:
        return cast(str, next(iter(runtimes)))
    default = plan.get("default_runtime")
    if not non_empty(default) or default.strip() not in runtimes:
        raise ContractError(
            "role has multiple runtimes; configure default_runtime or pass --runtime explicitly",
            remedy=f"set default_runtime to one of {', '.join(sorted(runtimes))} in the assignment plan, or pass --runtime explicitly",
        )
    return default.strip()


def role_write_ceiling(
    config: Mapping[str, object], plan: Mapping[str, object], role_name: str
) -> list[str]:
    """The widest path set a role may ever be handed: the role's own authority limit.

    ``write_paths`` states it. Without it the ceiling is the whole repository, or -- for a legacy
    plan that still names a zone -- that zone's paths. A batch narrows it with its explicit scope.
    """
    zones = config.get("backend_zones")
    zone = zones.get(plan.get("zone")) if isinstance(zones, dict) else None
    zone_paths = zone.get("paths") if isinstance(zone, dict) else None
    boundaries = zone_paths if string_list(zone_paths) and zone_paths else ["**"]
    write_paths = plan.get("write_paths", boundaries)
    if not string_list(write_paths) or not write_paths:
        raise ContractError(
            f"role {role_name!r} has invalid write_paths",
            remedy=f"set assignment_plans[{role_name!r}].write_paths to a non-empty list of strings",
        )
    if not paths_inside(write_paths, boundaries):
        raise ContractError(
            f"role {role_name!r} write_paths must remain inside backend zone {plan.get('zone')!r}",
            remedy=f"narrow assignment_plans[{role_name!r}].write_paths so every path stays inside backend_zones[{plan.get('zone')!r}].paths",
        )
    return list(write_paths)


def resolve_assignment(
    config: Mapping[str, object],
    role: Mapping[str, object],
    role_name: str,
    runtime_name: object,
) -> JsonObject:
    """Разрешить сконфигурированное назначение роли с сохранением авторитета манифеста."""
    assignments = config.get("assignment_plans")
    profiles = config.get("provider_profiles")
    if not isinstance(assignments, dict) or not isinstance(profiles, dict):
        raise ContractError(
            "project orchestration config has invalid assignments or profiles",
            remedy="set assignment_plans and provider_profiles to objects in the project orchestration config",
        )
    if role.get("name") != role_name or role.get("mode") not in ROLE_MODES:
        raise ContractError(
            f"role manifest {role_name!r} has invalid name or mode",
            remedy=f"set the role manifest's name to {role_name!r} and mode to one of {sorted(ROLE_MODES)}",
        )
    required = role.get("required_capabilities")
    if not isinstance(required, list) or not all(non_empty(item) for item in required):
        raise ContractError(
            f"role manifest {role_name!r} has no required capabilities",
            remedy=f"add a non-empty required_capabilities list to the {role_name!r} role manifest",
        )
    plan = assignments.get(role_name)
    if not isinstance(plan, dict):
        raise ContractError(
            f"role {role_name!r} has no assignment plan",
            remedy=f"add assignment_plans[{role_name!r}] to the project orchestration config",
        )
    transport = plan.get("transport", "in-process")
    if transport not in ROLE_TRANSPORTS:
        raise ContractError(
            f"role {role_name!r} has an invalid transport",
            remedy=f"set assignment_plans[{role_name!r}].transport to one of {sorted(ROLE_TRANSPORTS)}",
        )
    write_paths = role_write_ceiling(config, plan, role_name)
    runtimes = plan.get("runtimes")
    if not isinstance(runtimes, dict):
        raise ContractError(
            f"role {role_name!r} has no runtime assignments",
            remedy=f"add a runtimes object to assignment_plans[{role_name!r}]",
        )
    resolved_runtime = resolve_runtime_name(plan, runtime_name)
    runtime = runtimes.get(resolved_runtime)
    if not isinstance(runtime, dict):
        raise ContractError(
            f"role {role_name!r} is not assigned to runtime {runtime_name!r}",
            remedy=f"add assignment_plans[{role_name!r}].runtimes[{runtime_name!r}], or select an assigned runtime",
        )
    profile_ids = runtime.get("profiles")
    if (
        not isinstance(profile_ids, list)
        or not profile_ids
        or not all(non_empty(profile_id) for profile_id in profile_ids)
    ):
        raise ContractError(
            f"role {role_name!r} has no provider profile",
            remedy=f"add a non-empty profiles list to assignment_plans[{role_name!r}].runtimes[{runtime_name!r}]",
        )
    profile_id = profile_ids[0]
    for candidate_id in profile_ids:
        profile = profiles.get(candidate_id)
        if not isinstance(profile, dict):
            raise ContractError(
                f"provider profile {candidate_id!r} is invalid",
                remedy=f"define provider_profiles[{candidate_id!r}] as an object",
            )
        capabilities = profile.get("capabilities")
        if not isinstance(capabilities, list) or not set(required).intersection(
            capabilities
        ):
            raise ContractError(
                f"provider profile {candidate_id!r} is incompatible with role {role_name!r}",
                remedy=f"add one of the role's required_capabilities to provider_profiles[{candidate_id!r}].capabilities",
            )
    model = _valid_model(runtime.get("model"))
    effort = _valid_effort(runtime.get("effort"))
    return {
        "role": role,
        "write_ceiling": [] if role["mode"] == "read-only" else write_paths,
        "profile_id": profile_id,
        "model": model,
        "effort": effort,
        "transport": transport,
        "runtime_plan": runtime,
    }


def validate_brief_policy(
    brief: Mapping[str, object],
    project: Mapping[str, object],
    config: Mapping[str, object],
    roles_root: Path,
    *,
    expected_transport: str | None = None,
) -> tuple[JsonObject, JsonObject]:
    """Валидировать относящуюся к политикам часть утверждённого неизменяемого задания диспетчеризации."""
    reject_sensitive(brief, "dispatch brief")
    from .runtime_access import AccessError, validate_binding

    try:
        validate_binding(brief)
    except AccessError as exc:
        raise ContractError(exc.message, remedy=exc.remedy) from exc
    from .infrastructure_retry import pinned
    from .core.utils import CoordinatorError

    try:
        pinned(brief)
    except CoordinatorError as exc:
        raise ContractError(exc.message, remedy=exc.remedy) from exc
    approval = brief.get("coordinator_approval")
    # `transition_digest` binds the approval to the exact transition it was given for; a brief
    # written before that field existed carries the historical two-field approval.
    if (
        not isinstance(approval, dict)
        or set(approval)
        not in (
            {"approved_by", "approved_at"},
            {"approved_by", "approved_at", "transition_digest"},
        )
        or not all(non_empty(value) for value in approval.values())
    ):
        raise ContractError(
            "dispatch brief requires coordinator_approval with approved_by and approved_at",
            remedy="have the coordinator record coordinator_approval.approved_by and .approved_at before this brief is used",
        )
    if (
        expected_transport is not None
        and brief.get("resolved_transport", expected_transport) != expected_transport
    ):
        raise ContractError(
            f"dispatch brief selected a non-{expected_transport} transport",
            remedy=f"send this dispatch through the {expected_transport} adapter, or drop expected_transport if another transport is intended",
        )
    for field in (
        "ticket",
        "role",
        "branch",
        "worktree",
        "definition_of_done",
        "prohibited_changes",
        "verification_commands",
        "required_gates",
        "dependencies",
    ):
        if field not in brief:
            raise ContractError(
                f"dispatch brief is missing {field!r}", remedy=INTERNAL_INVARIANT_REMEDY
            )
    if not non_empty(brief["ticket"]) or not non_empty(brief["role"]):
        raise ContractError(
            "dispatch brief ticket and role must be non-empty strings",
            remedy=INTERNAL_INVARIANT_REMEDY,
        )
    if not non_empty(brief["branch"]) or (
        brief.get("zone") is not None and not non_empty(brief["zone"])
    ):
        raise ContractError(
            "dispatch brief branch must be a non-empty string and zone, when recorded, too",
            remedy=INTERNAL_INVARIANT_REMEDY,
        )
    if not non_empty(brief["worktree"]):
        raise ContractError(
            "dispatch brief worktree must be a non-empty string",
            remedy=INTERNAL_INVARIANT_REMEDY,
        )
    attestation_required = config.get("worker_attestation_required", False)
    if not isinstance(attestation_required, bool):
        raise ContractError(
            "project worker_attestation_required must be a boolean",
            remedy="set worker_attestation_required to true or false in the project orchestration config",
        )
    if brief.get("worker_attestation_required", False) is not attestation_required:
        raise ContractError(
            "dispatch brief worker_attestation_required does not match project policy",
            remedy=INTERNAL_INVARIANT_REMEDY,
        )
    if attestation_required and (not non_empty(brief.get("snapshot_commit"))):
        raise ContractError(
            "attested dispatch brief requires a non-empty snapshot_commit",
            remedy=INTERNAL_INVARIANT_REMEDY,
        )
    if not isinstance(brief["definition_of_done"], list) or not all(
        non_empty(item) for item in brief["definition_of_done"]
    ):
        raise ContractError(
            "dispatch brief definition_of_done must be a non-empty list of strings",
            remedy=INTERNAL_INVARIANT_REMEDY,
        )
    if not isinstance(brief["prohibited_changes"], list) or not all(
        non_empty(item) for item in brief["prohibited_changes"]
    ):
        raise ContractError(
            "dispatch brief prohibited_changes must be a non-empty list of strings",
            remedy=INTERNAL_INVARIANT_REMEDY,
        )
    for field in ("verification_commands", "required_gates", "dependencies"):
        entries = brief[field]
        if not isinstance(entries, list) or not all(
            non_empty(item) for item in entries
        ):
            raise ContractError(
                f"dispatch brief {field} must be a list of strings",
                remedy=INTERNAL_INVARIANT_REMEDY,
            )
    accepted_commands = accepted_verification_commands(
        config, str(brief.get("role")), str(brief.get("purpose"))
    )
    if brief["verification_commands"] not in accepted_commands:
        raise ContractError(
            "dispatch brief verification_commands must exactly match its project role configuration",
            remedy="regenerate this brief so verification_commands matches the project's role verification_commands",
        )
    branch = brief["branch"]
    pattern = project.get("branch_pattern", r"^feature/issue-[0-9]+-.+")
    base = project.get("base_branch", "master")
    try:
        valid_branch = (
            isinstance(pattern, str) and re.fullmatch(pattern, branch) is not None
        )
    except re.error as exc:
        raise ContractError(
            "project config has an invalid branch_pattern",
            remedy="fix the branch_pattern regular expression in the project config",
        ) from exc
    if branch.startswith("integration/") or branch == base or not valid_branch:
        raise ContractError(
            "dispatch brief branch must be an issue branch and never a protected or integration branch",
            remedy=f"use an issue branch matching {pattern!r}, never {base!r} or an integration/* branch",
        )
    role_name = brief["role"]
    role = load_role_manifest(roles_root / f"{role_name}.md")
    assignment = resolve_assignment(
        config, role, role_name, brief.get("resolved_runtime")
    )
    if brief.get("access") != role["mode"]:
        raise ContractError(
            f"dispatch brief access must be {role['mode']!r} for role {role_name!r}",
            remedy=INTERNAL_INVARIANT_REMEDY,
        )
    scope = brief.get("write_paths")
    if role["mode"] == "write" and (
        not string_list(scope)
        or not scope
        or not paths_inside(scope, assignment["write_ceiling"])
    ):
        raise ContractError(
            "write dispatch paths must be an explicit scope inside its role write ceiling",
            remedy=INTERNAL_INVARIANT_REMEDY,
        )
    if role["mode"] == "read-only" and brief.get("write_paths"):
        raise ContractError(
            "read-only role cannot receive write paths",
            remedy=INTERNAL_INVARIANT_REMEDY,
        )
    for field, expected in (
        ("resolved_provider_profile", assignment["profile_id"]),
        ("resolved_model", assignment["model"]),
        ("resolved_effort", assignment["effort"]),
        ("resolved_transport", assignment["transport"]),
    ):
        if field in brief and brief[field] != expected:
            raise ContractError(
                f"dispatch brief {field} does not match the project assignment",
                remedy=INTERNAL_INVARIANT_REMEDY,
            )
    return role, assignment


def _policy_problem(
    config: Mapping[str, object],
    key: str,
    fields: set[str],
    *,
    booleans: set[str] | None = None,
    minimum: int = 1,
) -> list[str]:
    """Валидировать небольшие числовые карты политик без зависимости от JSON-schema в рантайме."""
    value = config.get(key)
    if value is None:
        return []
    if not isinstance(value, dict):
        return [f"orchestration {key} must be an object"]
    problems: list[str] = []
    unknown = sorted(set(value) - fields - (booleans or set()))
    if unknown:
        problems.append(
            f"orchestration {key} has unknown field(s): {', '.join(unknown)}"
        )
    for field in fields:
        if field not in value:
            continue
        item = value[field]
        if isinstance(item, bool) or not isinstance(item, int) or item < minimum:
            qualifier = (
                "a non-negative integer" if minimum == 0 else "a positive integer"
            )
            problems.append(f"orchestration {key}.{field} must be {qualifier}")
    for field in booleans or set():
        if field in value and not isinstance(value[field], bool):
            problems.append(f"orchestration {key}.{field} must be a boolean")
    return problems


REPO_MAP_TIER_ORDER = ("minimal", "full")
REPO_MAP_POLICY_ROLES = ("architect", "developer", "code-review")
# Work roles that own no verification gate: the architect runs only decision-specific checks
# (roles/architect.md), so it must never receive the batch's full QA suite in its brief.
NO_GATE_WORK_ROLES = frozenset({"architect"})


def role_verification_commands(
    source: Mapping[str, object], role: str, purpose: str
) -> object:
    """Вернуть список проверок, положенный роли в brief.

    ``source`` — конфиг оркестрации или замороженный batch: developer и code-review получают свои
    фокусные списки с откатом на ``verification_commands``, architect — пустой список, остальные
    роли и не-``work`` цели — полный ``verification_commands``.
    """
    fallback = source.get("verification_commands")
    if purpose != "work":
        return fallback
    if role in NO_GATE_WORK_ROLES:
        return []
    if role == "developer":
        return source.get("developer_verification_commands", fallback)
    if role == "code-review":
        return source.get("review_verification_commands", fallback)
    return fallback


def accepted_verification_commands(
    source: Mapping[str, object], role: str, purpose: str
) -> list[object]:
    """Вернуть допустимые списки проверок для уже записанного brief роли.

    Brief architect, созданный до исключения роли из полного gate, ещё несёт полный
    ``verification_commands``; такие записи остаются валидными, чтобы незавершённые batch
    продолжались без ручной правки ledger.
    """
    accepted = [role_verification_commands(source, role, purpose)]
    if purpose == "work" and role in NO_GATE_WORK_ROLES:
        accepted.append(source.get("verification_commands"))
    return accepted


def _repo_map_policy_problems(config: Mapping[str, object]) -> list[str]:
    """Валидировать политику Repo Map без импорта её базовых ресурсов возможностей."""
    value = config.get("repo_map_policy")
    if value is None:
        return []
    if not isinstance(value, dict):
        return ["orchestration repo_map_policy must be an object"]
    numeric = {
        "max_files",
        "max_file_bytes",
        "max_path_length",
        "max_symbol_length",
        "max_signature_length",
        "timeout_seconds",
        "max_tokens",
        "parser_bundle_timeout_seconds",
        "parser_bundle_max_output_bytes",
    }
    patterns = {
        "allow_paths",
        "deny_paths",
        "redact_paths",
        "redact_symbols",
        "parser_bundle_registry_paths",
    }
    enum_values = {
        "tier": set(REPO_MAP_TIER_ORDER),
        "min_tier": set(REPO_MAP_TIER_ORDER),
    }
    structured = {"min_tier_by_role"}
    unknown = sorted(set(value) - numeric - patterns - set(enum_values) - structured)
    problems: list[str] = []
    if unknown:
        problems.append(
            f"orchestration repo_map_policy has unknown field(s): {', '.join(unknown)}"
        )
    for field in numeric:
        if field in value and (
            not _is_int(value[field]) or cast(int, value[field]) < 1
        ):
            problems.append(
                f"orchestration repo_map_policy.{field} must be a positive integer"
            )
    for field in patterns:
        if field in value:
            item = value[field]
            if not isinstance(item, list) or any(
                not isinstance(entry, str) or not entry for entry in item
            ):
                problems.append(
                    f"orchestration repo_map_policy.{field} must be a list of non-empty path globs"
                )
    for field, choices in enum_values.items():
        if field in value and (
            not isinstance(value[field], str) or value[field] not in choices
        ):
            problems.append(
                f"orchestration repo_map_policy.{field} must be one of: "
                + ", ".join(sorted(choices))
            )
    if "min_tier_by_role" in value:
        by_role = value["min_tier_by_role"]
        if not isinstance(by_role, dict):
            problems.append(
                "orchestration repo_map_policy.min_tier_by_role must be an object"
            )
        else:
            for role_name, tier in by_role.items():
                if role_name not in REPO_MAP_POLICY_ROLES:
                    problems.append(
                        f"orchestration repo_map_policy.min_tier_by_role names unknown role {role_name!r}"
                    )
                if not isinstance(tier, str) or tier not in REPO_MAP_TIER_ORDER:
                    problems.append(
                        f"orchestration repo_map_policy.min_tier_by_role.{role_name} must be one of: "
                        + ", ".join(sorted(REPO_MAP_TIER_ORDER))
                    )
    return problems


def resolve_min_repo_map_tier(
    config: Mapping[str, object], role_name: str
) -> str | None:
    """Минимальный уровень Repo Map, которому должен соответствовать Context Package роли при допуске к диспетчеризации.

    Порядок разрешения: запись роли в repo_map_policy.min_tier_by_role, затем общерепозиторный
    repo_map_policy.min_tier, иначе None (без ограничений). Без repo_map_policy или без обоих
    полей всегда возвращает None, поэтому проект без этой настройки никогда не блокируется деградацией Repo Map.
    """
    policy = config.get("repo_map_policy")
    if not isinstance(policy, dict):
        return None
    by_role = policy.get("min_tier_by_role")
    if isinstance(by_role, dict):
        role_value = by_role.get(role_name)
        if isinstance(role_value, str) and role_value in REPO_MAP_TIER_ORDER:
            return role_value
    default_value = policy.get("min_tier")
    if isinstance(default_value, str) and default_value in REPO_MAP_TIER_ORDER:
        return default_value
    return None


def resolve_allowed_tools(
    config: Mapping[str, object], role_name: str, mode: str
) -> list[str]:
    """Инструменты, фиксируемые в brief роли: запись роли в проекте, иначе запись для режима, иначе встроенный дефолт режима манифеста."""
    policy = config.get("tool_policy")
    if isinstance(policy, dict):
        for section, key in (("roles", role_name), ("modes", mode)):
            entries = policy.get(section)
            if isinstance(entries, dict) and key in entries:
                return list(entries[key])
    return list(DEFAULT_ALLOWED_TOOLS[mode])


def valid_tool_list(value: object) -> TypeGuard[list[str]]:
    """Проверить, что значение является непустым списком уникальных имён инструментов."""
    return string_list(value) and bool(value) and len(set(value)) == len(value)


def _tool_policy_problems(
    config: Mapping[str, object], role_names: set[str]
) -> list[str]:
    """Валидировать структуру и допустимые значения конфигурации tool_policy."""
    if "tool_policy" not in config:
        return []
    policy = config["tool_policy"]
    if not isinstance(policy, dict):
        return ["orchestration tool_policy must be an object"]
    problems: list[str] = []
    unknown = sorted(set(policy) - set(TOOL_POLICY_SECTIONS))
    if unknown:
        problems.append(
            f"orchestration tool_policy has unknown field(s): {', '.join(unknown)}"
        )
    for section, known in (("modes", ROLE_MODES), ("roles", role_names)):
        if section not in policy:
            continue
        entries = policy[section]
        if not isinstance(entries, dict):
            problems.append(f"orchestration tool_policy.{section} must be an object")
            continue
        for name, tools in entries.items():
            if name not in known:
                problems.append(
                    f"orchestration tool_policy.{section} names unknown entry {name!r}"
                )
            if not valid_tool_list(tools):
                problems.append(
                    f"orchestration tool_policy.{section}.{name} must be a non-empty list of unique tool names"
                )
    return problems


ATTENTION_POLICY_FIELDS = {
    "retry_queue_seconds": 1,
    "max_infrastructure_retries": 0,
    "stale_dispatch_seconds": 1,
    "heartbeat_interval_seconds": 1,
}


def _operational_policy_problems(config: Mapping[str, object]) -> list[str]:
    """Валидировать параметры операционного цикла: attention_policy, approval_ttl_seconds и extensions."""
    problems: list[str] = []
    if "infrastructure_retry_policy" in config:
        retry = config["infrastructure_retry_policy"]
        if (
            not isinstance(retry, dict)
            or set(retry) != {"enabled"}
            or not isinstance(retry.get("enabled"), bool)
        ):
            problems.append(
                "orchestration infrastructure_retry_policy must contain only enabled (boolean)"
            )
    attention = config.get("attention_policy")
    if attention is not None:
        if not isinstance(attention, dict):
            problems.append("orchestration attention_policy must be an object")
        else:
            unknown = sorted(set(attention) - set(ATTENTION_POLICY_FIELDS))
            if unknown:
                problems.append(
                    f"orchestration attention_policy has unknown field(s): {', '.join(unknown)}"
                )
            for field, minimum in ATTENTION_POLICY_FIELDS.items():
                value = attention.get(field)
                if field in attention and (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < minimum
                ):
                    qualifier = (
                        "a non-negative integer"
                        if minimum == 0
                        else "a positive integer"
                    )
                    problems.append(
                        f"orchestration attention_policy.{field} must be {qualifier}"
                    )
    ttl = config.get("approval_ttl_seconds")
    if ttl is not None and (
        isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 1
    ):
        problems.append("orchestration approval_ttl_seconds must be a positive integer")
    selected = config.get("extensions")
    if selected is not None:
        if not isinstance(selected, dict):
            problems.append("orchestration extensions must be an object")
        else:
            unknown = sorted(set(selected) - set(EXTENSION_KINDS))
            if unknown:
                problems.append(
                    f"orchestration extensions has unknown interface(s): {', '.join(unknown)}"
                )
            for kind, name in selected.items():
                if kind in EXTENSION_KINDS and (
                    not isinstance(name, str)
                    or (
                        name != DEFAULT_EXTENSION
                        and EXTENSION_NAME.fullmatch(name) is None
                    )
                ):
                    problems.append(
                        f"orchestration extensions.{kind} must be 'none' or a name like 'module:factory'"
                    )
    return problems


def access_policy_problems(value: object, roles: set[str]) -> list[str]:
    """Validate authored access independently of role transport and tool policy."""
    from .core.constants import ACCESS_MODES, ACCESS_OPERATIONS, ACCESS_RESOURCES

    errors: list[str] = []
    host_pattern = re.compile(
        r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$"
    )

    def component(item: object, label: str) -> None:
        if not isinstance(item, dict) or set(item) - {"mode", "network", "filesystem"}:
            errors.append(f"{label} must be an access component object with known keys")
            return
        if "mode" in item and item["mode"] not in ACCESS_MODES:
            errors.append(f"{label}.mode must be inherit, sandbox or unsandboxed")
        if "network" in item:
            network = item["network"]
            hosts = network.get("hosts") if isinstance(network, dict) else None
            if (
                not isinstance(network, dict)
                or set(network) != {"hosts"}
                or not isinstance(hosts, list)
                or any(
                    not isinstance(host, str)
                    or host_pattern.fullmatch(host) is None
                    or any(
                        not part
                        or len(part) > 63
                        or part.startswith("-")
                        or part.endswith("-")
                        for part in host.split(".")
                    )
                    for host in hosts
                )
            ):
                errors.append(
                    f"{label}.network must contain explicit DNS hosts without URLs, ports or wildcards"
                )
        if "filesystem" in item:
            filesystem = item["filesystem"]
            if not isinstance(filesystem, list):
                errors.append(f"{label}.filesystem must be a list")
                return
            for requirement in filesystem:
                if (
                    not isinstance(requirement, dict)
                    or set(requirement) - {"resource", "access", "path"}
                    or requirement.get("resource") not in ACCESS_RESOURCES
                    or requirement.get("access") not in ("read", "write")
                    or (
                        requirement.get("resource") == "cache"
                        and (
                            not non_empty(requirement.get("path"))
                            or "\x00" in requirement["path"]
                        )
                    )
                    or (
                        requirement.get("resource") != "cache" and "path" in requirement
                    )
                ):
                    errors.append(
                        f"{label}.filesystem has an invalid resource/access requirement; cache needs an explicit path"
                    )

    if not isinstance(value, dict) or set(value) - {"defaults", "roles", "operations"}:
        return [
            "access_policy must be an object with defaults, roles and operations only"
        ]
    if "defaults" in value:
        component(value["defaults"], "access_policy.defaults")
    for section, names in (("roles", roles), ("operations", set(ACCESS_OPERATIONS))):
        if section not in value:
            continue
        overrides = value[section]
        if not isinstance(overrides, dict):
            errors.append(f"access_policy.{section} must be an object")
            continue
        for name, item in overrides.items():
            if name not in names:
                errors.append(f"access_policy.{section} has unknown name {name!r}")
            component(item, f"access_policy.{section}.{name}")
    return errors


def health_problems(config_path: Path, roles_root: Path) -> list[str]:
    """Вернуть диагностические замечания к здоровью конфигурации без изменения состояния проекта."""
    problems: list[str] = []
    if not config_path.is_file():
        return problems
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"{config_path.as_posix()} is not valid JSON: {exc}"]
    if not isinstance(config, dict):
        return [f"{config_path.as_posix()} must contain a JSON object"]
    reject_error: ContractError | None = None
    try:
        reject_sensitive(config, "project orchestration config")
    except ContractError as exc:
        reject_error = exc
    if reject_error:
        problems.append(str(reject_error))
    # Access-only projects keep session assignments; validate authored values before defaults.
    if (
        "access_policy" in config or "infrastructure_retry_policy" in config
    ) and not any(
        config.get(key)
        for key in ("assignment_plans", "backend_zones", "provider_profiles")
    ):
        config = {
            "provider_profiles": {},
            "assignment_plans": {},
            "concurrency_budget": 1,
            "verification_commands": [],
            **config,
        }
    missing = [field for field in CONFIG_REQUIRED_FIELDS if field not in config]
    if missing:
        problems.append(
            f"{config_path.as_posix()} missing required field(s): {', '.join(missing)}"
        )
    extra = sorted(set(config) - CONFIG_ALLOWED_FIELDS)
    if extra:
        problems.append(
            f"{config_path.as_posix()} has unknown field(s): {', '.join(extra)}"
        )
    roles: dict[str, JsonObject] = {}
    if not roles_root.is_dir():
        problems.append("missing .harness/orchestration/roles")
    else:
        for path in sorted(roles_root.glob("*.md")):
            if path.stem == "_common":
                continue
            try:
                role = load_role_manifest(path)
            except ContractError as exc:
                problems.append(
                    str(exc).replace("role manifest", "orchestration role manifest")
                )
                continue
            unknown = sorted(set(role) - ROLE_FIELDS)
            if unknown:
                problems.append(
                    f"orchestration role manifest {path.name} has unknown field(s): {', '.join(unknown)}"
                )
            if role.get("name") != path.stem:
                problems.append(
                    f"orchestration role manifest {path.name} name must be {path.stem!r}"
                )
            if role.get("mode") not in ROLE_MODES:
                problems.append(
                    f"orchestration role manifest {path.name} mode must be 'write' or 'read-only'"
                )
            if not string_list(role.get("required_capabilities")):
                problems.append(
                    f"orchestration role manifest {path.name} required_capabilities must be a non-empty list"
                )
            if not string_list(role.get("risk_triggers")):
                problems.append(
                    f"orchestration role manifest {path.name} risk_triggers must be a non-empty list"
                )
            if isinstance(role.get("name"), str):
                if role["name"] in roles:
                    problems.append(
                        f"duplicate orchestration role manifest: {role['name']}"
                    )
                roles[role["name"]] = role
    review = roles.get("code-review")
    if review is not None:
        if review.get("mode") != "read-only":
            problems.append("code-review role manifest must remain read-only")
        if "code-review" not in set(review.get("required_capabilities", [])):
            problems.append(
                "code-review role manifest must require the code-review capability"
            )
        missing_triggers = sorted(
            CODE_REVIEW_REQUIRED_RISK_TRIGGERS - set(review.get("risk_triggers", []))
        )
        if missing_triggers:
            problems.append(
                "code-review role manifest is missing required risk trigger(s): "
                + ", ".join(missing_triggers)
            )
    if "access_policy" in config:
        problems.extend(access_policy_problems(config["access_policy"], set(roles)))
    profiles = config.get("provider_profiles")
    profile_capabilities: dict[str, set[str]] = {}
    profile_fallbacks: dict[str, list[str]] = {}
    if not isinstance(profiles, dict):
        problems.append("orchestration provider_profiles must be an object")
        profiles = {}
    for profile_id, profile in profiles.items():
        if not non_empty(profile_id):
            problems.append(
                "orchestration provider profile IDs must be non-empty strings"
            )
            continue
        if not isinstance(profile, dict):
            problems.append(f"provider profile {profile_id!r} must be an object")
            continue
        extra_profile = sorted(
            set(profile) - {"capabilities", "fallback", "known_limitations"}
        )
        if extra_profile:
            problems.append(
                f"provider profile {profile_id!r} has unknown field(s): {', '.join(extra_profile)}; "
                "credentials and role-policy overrides are not allowed"
            )
        for field in ("capabilities", "fallback", "known_limitations"):
            if field not in profile:
                problems.append(
                    f"provider profile {profile_id!r} missing required field: {field}"
                )
        capabilities = profile.get("capabilities")
        if not string_list(capabilities) or not capabilities:
            problems.append(
                f"provider profile {profile_id!r} capabilities must be a non-empty list"
            )
            capabilities = []
        profile_capabilities[profile_id] = set(capabilities)
        if not string_list(profile.get("fallback")):
            problems.append(
                f"provider profile {profile_id!r} fallback must be a list of profile IDs"
            )
            profile_fallbacks[profile_id] = []
        else:
            profile_fallbacks[profile_id] = profile["fallback"]
        if not string_list(profile.get("known_limitations")):
            problems.append(
                f"provider profile {profile_id!r} known_limitations must be a list of strings"
            )
    for profile_id, fallbacks in profile_fallbacks.items():
        for fallback_id in fallbacks:
            if fallback_id not in profiles:
                problems.append(
                    f"provider profile {profile_id!r} references unknown fallback profile {fallback_id!r}"
                )
    # backend_zones is a legacy declaration: optional, validated only when a project still states it.
    zones = config.get("backend_zones", {})
    if not isinstance(zones, dict):
        problems.append("orchestration backend_zones must be an object")
        zones = {}
    for zone_id, zone in zones.items():
        if not non_empty(zone_id):
            problems.append("orchestration backend zone IDs must be non-empty strings")
            continue
        if not isinstance(zone, dict):
            problems.append(f"backend zone {zone_id!r} must be an object")
            continue
        extra_zone = sorted(set(zone) - {"paths"})
        if extra_zone:
            problems.append(
                f"backend zone {zone_id!r} has unknown field(s): {', '.join(extra_zone)}"
            )
        if not string_list(zone.get("paths")) or not zone["paths"]:
            problems.append(f"backend zone {zone_id!r} paths must be a non-empty list")
    assignments = config.get("assignment_plans")
    if isinstance(assignments, dict):
        if assignments and "code-review" not in assignments:
            problems.append(
                "orchestration assignment_plans must include code-review for the mandatory high-risk role gate"
            )
        for role_name, plan in assignments.items():
            if role_name not in roles:
                problems.append(
                    f"unknown role in orchestration assignment_plans: {role_name}"
                )
                continue
            if not isinstance(plan, dict):
                problems.append(
                    f"assignment plan for role {role_name!r} must be an object"
                )
                continue
            extra_plan = sorted(
                set(plan)
                - {"zone", "write_paths", "runtimes", "transport", "default_runtime"}
            )
            if extra_plan:
                problems.append(
                    f"assignment plan for role {role_name!r} cannot override role manifest fields: {', '.join(extra_plan)}"
                )
            if "transport" in plan and plan["transport"] not in ROLE_TRANSPORTS:
                problems.append(
                    f"assignment plan for role {role_name!r} transport must be one of: "
                    + ", ".join(sorted(ROLE_TRANSPORTS))
                )
            zone_id = plan.get("zone")
            if "zone" in plan and not non_empty(zone_id):
                problems.append(
                    f"assignment plan for role {role_name!r} zone must be a non-empty string"
                )
            elif "zone" in plan and zone_id not in zones:
                problems.append(
                    f"unknown backend zone {zone_id!r} for role {role_name!r}"
                )
            write_paths = plan.get("write_paths")
            if write_paths is not None and (
                not string_list(write_paths) or not write_paths
            ):
                problems.append(
                    f"assignment plan for role {role_name!r} write_paths must be a non-empty list when provided"
                )
            elif (
                write_paths is not None
                and isinstance(zone_id, str)
                and isinstance(zones.get(zone_id), dict)
            ):
                boundaries = zones[zone_id].get("paths")
                if string_list(boundaries) and any(
                    not any(_inside(path, boundary) for boundary in boundaries)
                    for path in write_paths
                ):
                    problems.append(
                        f"assignment plan for role {role_name!r} write_paths must remain inside zone {zone_id!r}"
                    )
            runtimes = plan.get("runtimes")
            if not isinstance(runtimes, dict) or not runtimes:
                problems.append(
                    f"assignment plan for role {role_name!r} runtimes must be a non-empty object"
                )
                continue
            default_runtime = plan.get("default_runtime")
            if default_runtime is not None and (
                not non_empty(default_runtime)
                or default_runtime.strip() not in runtimes
            ):
                problems.append(
                    f"assignment plan for role {role_name!r} default_runtime must name one configured runtime"
                )
            for runtime_name in runtimes:
                runtime = runtimes[runtime_name]
                if not non_empty(runtime_name) or not isinstance(runtime, dict):
                    problems.append(
                        f"assignment runtime for role {role_name!r} must be a named object"
                    )
                    continue
                extra_runtime = sorted(set(runtime) - {"profiles", "model", "effort"})
                if extra_runtime:
                    problems.append(
                        f"assignment runtime {runtime_name!r} for role {role_name!r} has unknown field(s): {', '.join(extra_runtime)}"
                    )
                profile_ids = runtime.get("profiles")
                if not string_list(profile_ids) or not profile_ids:
                    problems.append(
                        f"assignment runtime {runtime_name!r} for role {role_name!r} profiles must be a non-empty list"
                    )
                    profile_ids = []
                for field in ("model", "effort"):
                    if not non_empty(runtime.get(field)):
                        problems.append(
                            f"assignment runtime {runtime_name!r} for role {role_name!r} {field} must be a non-empty string"
                        )
                model = runtime.get("model")
                if non_empty(model) and MODEL_ID.fullmatch(model.strip()) is None:
                    problems.append(
                        f"assignment runtime {runtime_name!r} for role {role_name!r} model must be a CLI model ID or alias without spaces"
                    )
                effort = runtime.get("effort")
                if non_empty(effort) and effort.strip() not in EFFORT_LEVELS:
                    problems.append(
                        f"assignment runtime {runtime_name!r} for role {role_name!r} effort must be one of: "
                        + ", ".join(sorted(EFFORT_LEVELS))
                    )
                for profile_id in profile_ids:
                    if profile_id not in profiles:
                        problems.append(
                            f"unknown provider profile {profile_id!r} for role {role_name!r}"
                        )
                    elif not set(
                        roles[role_name].get("required_capabilities", [])
                    ).intersection(profile_capabilities.get(profile_id, set())):
                        problems.append(
                            f"provider profile {profile_id!r} has incompatible capability for role {role_name!r}; requires one of {', '.join(sorted(roles[role_name].get('required_capabilities', [])))}"
                        )
                try:
                    resolve_assignment(
                        config,
                        roles[role_name],
                        role_name,
                        runtime_name,
                    )
                except ContractError as exc:
                    problems.append(str(exc))
    elif assignments is not None:
        problems.append("orchestration assignment_plans must be an object")
    budget = config.get("concurrency_budget")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        problems.append("orchestration concurrency_budget must be a positive integer")
    if not string_list(config.get("verification_commands")):
        problems.append("orchestration verification_commands must be a list of strings")
    developer_commands = config.get("developer_verification_commands")
    if developer_commands is not None and not string_list(developer_commands):
        problems.append(
            "orchestration developer_verification_commands must be a list of strings when provided"
        )
    review_commands = config.get("review_verification_commands")
    if review_commands is not None and not string_list(review_commands):
        problems.append(
            "orchestration review_verification_commands must be a list of strings when provided"
        )
    for qa_key in ("qa_preparation", "qa_environment_probes", "qa_project_file_checks"):
        qa_commands = config.get(qa_key)
        if qa_commands is not None and not string_list(qa_commands):
            problems.append(
                f"orchestration {qa_key} must be a list of strings when provided"
            )
    approval_policy = config.get("approval_policy", "manual_all")
    if approval_policy not in APPROVAL_POLICIES:
        problems.append(
            "orchestration approval_policy must be one of: "
            + ", ".join(sorted(APPROVAL_POLICIES))
        )
    human_gate = config.get("human_approval_gate", "trusted")
    if human_gate not in HUMAN_APPROVAL_GATES:
        problems.append(
            "orchestration human_approval_gate must be one of: "
            + ", ".join(sorted(HUMAN_APPROVAL_GATES))
        )
    if approval_policy == "auto":
        if human_gate == "tty":
            problems.append(AUTO_TTY_PROBLEM)
        if config.get("worker_attestation_required", False) is not True:
            problems.append(AUTO_ATTESTATION_PROBLEM)
    low_risk_zones = config.get("low_risk_zones")
    if low_risk_zones is not None and (
        not string_list(low_risk_zones) or not set(low_risk_zones).issubset(zones)
    ):
        problems.append(
            "orchestration low_risk_zones must name configured backend zones"
        )
    low_risk_paths = config.get("low_risk_paths")
    if low_risk_paths is not None and (
        not string_list(low_risk_paths) or not low_risk_paths
    ):
        problems.append(
            "orchestration low_risk_paths must be a non-empty list of path patterns when provided"
        )
    elif string_list(low_risk_paths) and not all(
        is_clean_path_pattern(pattern) for pattern in low_risk_paths
    ):
        problems.append(
            "orchestration low_risk_paths entries must be repo-relative patterns such as 'src/**' without './', '//' or '..' segments"
        )
    attestation_required = config.get("worker_attestation_required")
    if attestation_required is not None and not isinstance(attestation_required, bool):
        problems.append(
            "orchestration worker_attestation_required must be a boolean when provided"
        )
    communication_policy = config.get("communication_policy")
    if communication_policy is not None:
        if (
            not isinstance(communication_policy, dict)
            or set(communication_policy) != COMMUNICATION_POLICY_FIELDS
        ):
            problems.append(
                "orchestration communication_policy must contain exactly agent_to_agent_language and coordinator_report_language"
            )
        elif any(
            value not in COMMUNICATION_LANGUAGES
            for value in communication_policy.values()
        ):
            problems.append(
                "orchestration communication_policy languages must be en or ru"
            )
        elif communication_policy["agent_to_agent_language"] != "en":
            problems.append(
                "orchestration communication_policy agent_to_agent_language must be en"
            )
        elif communication_policy["coordinator_report_language"] != "ru":
            problems.append(
                "orchestration communication_policy coordinator_report_language must be ru"
            )
    problems.extend(_tool_policy_problems(config, set(roles)))
    problems.extend(_operational_policy_problems(config))
    problems.extend(
        _policy_problem(
            config,
            "context_package_policy",
            {
                "max_tokens",
                "context_window_tokens",
                "reserved_prompt_tokens",
                "symbol_graph_depth",
                "max_related_tests",
                "min_starting_files",
                "max_starting_files",
                "section_index_min_tokens",
            },
        )
    )
    problems.extend(_repo_map_policy_problems(config))
    context_policy = config.get("context_package_policy")
    if isinstance(context_policy, dict):
        minimum_files = context_policy.get("min_starting_files")
        maximum_files = context_policy.get("max_starting_files")
        if (
            _is_int(minimum_files)
            and _is_int(maximum_files)
            and minimum_files > maximum_files
        ):
            problems.append(
                "orchestration context_package_policy.min_starting_files must not exceed max_starting_files"
            )
        maximum = context_policy.get("max_tokens")
        window = context_policy.get("context_window_tokens")
        reserve = context_policy.get("reserved_prompt_tokens")
        if (
            _is_int(maximum)
            and _is_int(window)
            and _is_int(reserve)
            and maximum > window - reserve
        ):
            problems.append(
                "orchestration context_package_policy.max_tokens must fit inside context_window_tokens minus reserved_prompt_tokens"
            )
    problems.extend(
        _policy_problem(
            config,
            "continuation_policy",
            {"max_continuations", "max_rate_limit_resumes"},
        )
    )
    problems.extend(
        _policy_problem(
            config,
            "execution_policy",
            {
                "dispatch_wait_timeout_seconds",
                "dispatch_poll_interval_seconds",
                "qa_lease_seconds",
                "rate_limit_retry_seconds",
            },
        )
    )
    problems.extend(
        _policy_problem(config, "retry_policy", {"max_developer_retries"}, minimum=0)
    )
    problems.extend(
        _policy_problem(
            config,
            "preflight_policy",
            {
                "max_definition_of_done_items",
                "max_dependencies",
                "max_expected_files",
                "max_expected_services",
                "max_expected_changed_lines",
                "max_expected_context_tokens",
                "estimated_tokens_per_changed_line",
                "estimated_tokens_per_file",
            },
            booleans={"require_estimates"},
        )
    )
    return problems
