"""Portable policy for backend-orchestration manifests, assignments and dispatch briefs.

Runtime entry points deliberately adapt this module instead of restating role authority.  The
module has no dependency on a coordinator state store or a particular runtime, so health checks,
in-process handoff and Orca receive the same policy outcome for the same project inputs.
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
ROLE_TRANSPORTS = {"orca", "in-process"}
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
    "backend_zones",
    "concurrency_budget",
    "verification_commands",
)
CONFIG_ALLOWED_FIELDS = frozenset(CONFIG_REQUIRED_FIELDS) | {
    "$schema",
    "developer_verification_commands",
    "test_path_patterns",
    "adaptive_continuation_policy",
    "approval_policy",
    "low_risk_zones",
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
    "approval_ttl_seconds",
    "extensions",
}
# The working set a brief records for a role when the project states no `tool_policy`. It is the
# role's own set, not a deny-list: global runtime tools stay available whatever a brief records.
DEFAULT_ALLOWED_TOOLS = {
    "read-only": ("Read", "Grep", "Glob", "Bash"),
    "write": ("Read", "Grep", "Glob", "Bash", "Edit", "Write"),
}
TOOL_POLICY_SECTIONS = ("modes", "roles")
APPROVAL_POLICIES = {"manual_all", "milestone", "low_risk"}
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
    """A manifest, assignment or immutable brief violates portable role policy."""


# Role manifests and resolved assignments are dynamic, JSON-shaped documents that the coordinator and
# adapters consume as ``dict[str, Any]``; this is the one intentional dynamic boundary of the module.
JsonObject = dict[str, Any]  # type: ignore[explicit-any]


def non_empty(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and bool(value.strip())


def string_list(value: object) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(non_empty(item) for item in value)


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def reject_sensitive(value: object, location: str) -> None:
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
    """Parse the deliberately small role frontmatter without a YAML dependency."""
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


def _inside(path: str, boundary: str) -> bool:
    prefix = boundary.removesuffix("**")
    return path == boundary or path.startswith(prefix)


def _valid_model(value: object) -> str:
    if not non_empty(value) or MODEL_ID.fullmatch(value.strip()) is None:
        raise ContractError(
            "assignment model must be a CLI model ID or alias without spaces",
            remedy="set the assignment's model to a non-empty CLI model ID/alias with no spaces",
        )
    return value.strip()


def _valid_effort(value: object) -> str:
    if not non_empty(value) or value.strip() not in EFFORT_LEVELS:
        raise ContractError(
            "assignment effort must be one of: " + ", ".join(sorted(EFFORT_LEVELS)),
            remedy="set the assignment's effort to one of: "
            + ", ".join(sorted(EFFORT_LEVELS)),
        )
    return value.strip()


def resolve_runtime_name(plan: Mapping[str, object], requested: object) -> str:
    """Resolve a role runtime without a global, hidden provider default.

    A one-runtime plan is unambiguous. A multi-runtime plan must either name its project-owned
    ``default_runtime`` or receive an explicit CLI selection. Keeping this decision in the
    portable contract lets preflight, the coordinator and adapters agree before a brief exists.
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


def resolve_assignment(
    config: Mapping[str, object],
    role: Mapping[str, object],
    role_name: str,
    zone_name: object,
    runtime_name: object,
) -> JsonObject:
    """Resolve one configured role assignment while preserving manifest authority."""
    assignments = config.get("assignment_plans")
    zones = config.get("backend_zones")
    profiles = config.get("provider_profiles")
    if (
        not isinstance(assignments, dict)
        or not isinstance(zones, dict)
        or not isinstance(profiles, dict)
    ):
        raise ContractError(
            "project orchestration config has invalid assignments, zones or profiles",
            remedy="set assignment_plans, backend_zones and provider_profiles to objects in the project orchestration config",
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
    if not isinstance(plan, dict) or plan.get("zone") != zone_name:
        raise ContractError(
            f"role {role_name!r} is not assigned to zone {zone_name!r}",
            remedy=f"set assignment_plans[{role_name!r}].zone to {zone_name!r} in the project orchestration config",
        )
    transport = plan.get("transport", "in-process")
    if transport not in ROLE_TRANSPORTS:
        raise ContractError(
            f"role {role_name!r} has an invalid transport",
            remedy=f"set assignment_plans[{role_name!r}].transport to one of {sorted(ROLE_TRANSPORTS)}",
        )
    zone = zones.get(zone_name)
    paths = zone.get("paths") if isinstance(zone, dict) else None
    if (
        not isinstance(paths, list)
        or not paths
        or not all(non_empty(item) for item in paths)
    ):
        raise ContractError(
            f"backend zone {zone_name!r} is invalid",
            remedy=f"set backend_zones[{zone_name!r}].paths to a non-empty list of strings",
        )
    write_paths = plan.get("write_paths", paths)
    if (
        not isinstance(write_paths, list)
        or not write_paths
        or not all(non_empty(item) for item in write_paths)
    ):
        raise ContractError(
            f"role {role_name!r} has invalid write_paths",
            remedy=f"set assignment_plans[{role_name!r}].write_paths to a non-empty list of strings",
        )
    if not all(
        any(_inside(path, boundary) for boundary in paths) for path in write_paths
    ):
        raise ContractError(
            f"role {role_name!r} write_paths must remain inside backend zone {zone_name!r}",
            remedy=f"narrow assignment_plans[{role_name!r}].write_paths so every path stays inside backend_zones[{zone_name!r}].paths",
        )
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
        "zone": {"paths": [] if role["mode"] == "read-only" else list(write_paths)},
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
    """Validate the policy-owned portion of an approved immutable handoff brief."""
    reject_sensitive(brief, "dispatch brief")
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
        "zone",
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
    if not non_empty(brief["zone"]) or not non_empty(brief["branch"]):
        raise ContractError(
            "dispatch brief zone and branch must be non-empty strings",
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
    expected_commands = (
        config.get(
            "developer_verification_commands", config.get("verification_commands")
        )
        if brief.get("role") == "developer" and brief.get("purpose") == "work"
        else config.get("verification_commands")
    )
    if brief["verification_commands"] != expected_commands:
        raise ContractError(
            "dispatch brief verification_commands must exactly match its project role configuration",
            remedy="regenerate this brief so verification_commands matches the project's (developer_)verification_commands",
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
        config, role, role_name, brief["zone"], brief.get("resolved_runtime")
    )
    if brief.get("access") != role["mode"]:
        raise ContractError(
            f"dispatch brief access must be {role['mode']!r} for role {role_name!r}",
            remedy=INTERNAL_INVARIANT_REMEDY,
        )
    expected_paths = assignment["zone"]["paths"]
    if role["mode"] == "write" and brief.get("write_paths") != expected_paths:
        raise ContractError(
            "write dispatch paths must exactly match its role assignment",
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
    """Validate small numeric policy maps without a JSON-schema runtime dependency."""
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


def _repo_map_policy_problems(config: Mapping[str, object]) -> list[str]:
    """Validate the Repo Map policy without importing its base-capability resource."""
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
    }
    patterns = {"allow_paths", "deny_paths", "redact_paths", "redact_symbols"}
    enum_values = {"tier": {"minimal", "reduced"}}
    unknown = sorted(set(value) - numeric - patterns - set(enum_values))
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
            if (
                not isinstance(item, list)
                or any(not isinstance(entry, str) or not entry for entry in item)
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
    return problems


def resolve_allowed_tools(
    config: Mapping[str, object], role_name: str, mode: str
) -> list[str]:
    """Tools a dispatch brief records for a role: the project's per-role entry, else its per-mode
    entry, else the built-in default for the role's manifest mode. `harness health` has already
    validated the shape of `tool_policy` for a configured project."""
    policy = config.get("tool_policy")
    if isinstance(policy, dict):
        for section, key in (("roles", role_name), ("modes", mode)):
            entries = policy.get(section)
            if isinstance(entries, dict) and key in entries:
                return list(entries[key])
    return list(DEFAULT_ALLOWED_TOOLS[mode])


def valid_tool_list(value: object) -> TypeGuard[list[str]]:
    return string_list(value) and bool(value) and len(set(value)) == len(value)


def _tool_policy_problems(
    config: Mapping[str, object], role_names: set[str]
) -> list[str]:
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
}


def _operational_policy_problems(config: Mapping[str, object]) -> list[str]:
    """`attention_policy`, `approval_ttl_seconds` and `extensions`: the operational-loop policy of issue #250."""
    problems: list[str] = []
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


def health_problems(config_path: Path, roles_root: Path) -> list[str]:
    """Return health diagnostics without changing project state."""
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
            set(profile) - {"capabilities", "agent", "fallback", "known_limitations"}
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
        agent = profile.get("agent")
        if agent is not None and not non_empty(agent):
            problems.append(
                f"provider profile {profile_id!r} agent must be a non-empty string"
            )
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
    zones = config.get("backend_zones")
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
            if not non_empty(zone_id):
                problems.append(
                    f"assignment plan for role {role_name!r} zone must be a non-empty string"
                )
            elif zone_id not in zones:
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
                        plan.get("zone"),
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
    low_risk_zones = config.get("low_risk_zones")
    if low_risk_zones is not None and (
        not string_list(low_risk_zones) or not set(low_risk_zones).issubset(zones)
    ):
        problems.append(
            "orchestration low_risk_zones must name configured backend zones"
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
            },
        )
    )
    problems.extend(_repo_map_policy_problems(config))
    context_policy = config.get("context_package_policy")
    if isinstance(context_policy, dict):
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
            },
            booleans={"require_estimates"},
        )
    )
    return problems
