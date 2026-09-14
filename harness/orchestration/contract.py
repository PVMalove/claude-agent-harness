"""Portable policy for backend-orchestration manifests, assignments and dispatch briefs.

Runtime entry points deliberately adapt this module instead of restating role authority.  The
module has no dependency on a coordinator state store or a particular runtime, so health checks,
in-process handoff and Orca receive the same policy outcome for the same project inputs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


ROLE_MODES = {"write", "read-only"}
ROLE_TRANSPORTS = {"orca", "in-process"}
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")
SENSITIVE_KEY = re.compile(r"(?:api[_-]?key|credential|password|secret|token)", re.IGNORECASE)
ROLE_FIELDS = frozenset({"name", "mode", "required_capabilities", "risk_triggers"})
CONFIG_REQUIRED_FIELDS = (
    "provider_profiles", "assignment_plans", "backend_zones", "concurrency_budget", "verification_commands",
)
CONFIG_ALLOWED_FIELDS = frozenset(CONFIG_REQUIRED_FIELDS) | {"$schema", "developer_verification_commands"}
CODE_REVIEW_REQUIRED_RISK_TRIGGERS = frozenset(
    {
        "api-public-contract", "schema-change", "data-migration", "outbox", "queues",
        "message-schema-routing", "transactions", "authorization-security", "concurrency-retry", "retry-dlq",
    }
)


class ContractError(Exception):
    """A manifest, assignment or immutable brief violates portable role policy."""


def non_empty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def string_list(value: object) -> bool:
    return isinstance(value, list) and all(non_empty(item) for item in value)


def reject_sensitive(value: Any, location: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ContractError(f"{location} contains a non-string key")
            if SENSITIVE_KEY.search(key):
                raise ContractError(f"{location} contains secret-shaped field {key!r}")
            reject_sensitive(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_sensitive(child, f"{location}[{index}]")


def load_role_manifest(path: Path) -> dict[str, Any]:
    """Parse the deliberately small role frontmatter without a YAML dependency."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractError(f"role manifest {path.name!r} cannot be read") from exc
    match = re.match(r"\A---\r?\n(?P<body>.*?)\r?\n---(?:\r?\n|$)", text, re.DOTALL)
    if not match:
        raise ContractError(f"role manifest {path.name!r} has no valid frontmatter")
    metadata: dict[str, Any] = {}
    current: str | None = None
    for line in match.group("body").splitlines():
        if not line.strip():
            continue
        item = re.fullmatch(r"\s+-\s+(.+?)\s*", line)
        if item:
            if current is None:
                raise ContractError(f"role manifest {path.name!r} has an orphan list item")
            metadata.setdefault(current, []).append(item.group(1))
            continue
        field = re.fullmatch(r"([a-z_]+):\s*(.*?)\s*", line)
        if not field:
            raise ContractError(f"role manifest {path.name!r} has invalid frontmatter")
        key, value = field.groups()
        current = key if not value else None
        metadata[key] = [] if not value else value
    return metadata


def _inside(path: str, boundary: str) -> bool:
    prefix = boundary[:-2] if boundary.endswith("**") else boundary
    return path == boundary or path.startswith(prefix)


def _valid_model(value: object) -> str:
    if not non_empty(value) or MODEL_ID.fullmatch(value.strip()) is None:
        raise ContractError("assignment model must be a CLI model ID or alias without spaces")
    return value.strip()


def resolve_assignment(
    config: dict[str, Any], role: dict[str, Any], role_name: str, zone_name: str, runtime_name: str,
) -> dict[str, Any]:
    """Resolve one configured role assignment while preserving manifest authority."""
    assignments = config.get("assignment_plans")
    zones = config.get("backend_zones")
    profiles = config.get("provider_profiles")
    if not isinstance(assignments, dict) or not isinstance(zones, dict) or not isinstance(profiles, dict):
        raise ContractError("project orchestration config has invalid assignments, zones or profiles")
    if role.get("name") != role_name or role.get("mode") not in ROLE_MODES:
        raise ContractError(f"role manifest {role_name!r} has invalid name or mode")
    required = role.get("required_capabilities")
    if not isinstance(required, list) or not all(non_empty(item) for item in required):
        raise ContractError(f"role manifest {role_name!r} has no required capabilities")
    plan = assignments.get(role_name)
    if not isinstance(plan, dict) or plan.get("zone") != zone_name:
        raise ContractError(f"role {role_name!r} is not assigned to zone {zone_name!r}")
    transport = plan.get("transport", "in-process")
    if transport not in ROLE_TRANSPORTS:
        raise ContractError(f"role {role_name!r} has an invalid transport")
    zone = zones.get(zone_name)
    paths = zone.get("paths") if isinstance(zone, dict) else None
    if not isinstance(paths, list) or not paths or not all(non_empty(item) for item in paths):
        raise ContractError(f"backend zone {zone_name!r} is invalid")
    write_paths = plan.get("write_paths", paths)
    if not isinstance(write_paths, list) or not write_paths or not all(non_empty(item) for item in write_paths):
        raise ContractError(f"role {role_name!r} has invalid write_paths")
    if not all(any(_inside(path, boundary) for boundary in paths) for path in write_paths):
        raise ContractError(f"role {role_name!r} write_paths must remain inside backend zone {zone_name!r}")
    runtimes = plan.get("runtimes")
    if not isinstance(runtimes, dict):
        raise ContractError(f"role {role_name!r} has no runtime assignments")
    runtime = runtimes.get(runtime_name)
    if not isinstance(runtime, dict):
        raise ContractError(f"role {role_name!r} is not assigned to runtime {runtime_name!r}")
    profile_ids = runtime.get("profiles")
    if not isinstance(profile_ids, list) or not profile_ids or not all(non_empty(profile_id) for profile_id in profile_ids):
        raise ContractError(f"role {role_name!r} has no provider profile")
    profile_id = profile_ids[0]
    for candidate_id in profile_ids:
        profile = profiles.get(candidate_id)
        if not isinstance(profile, dict):
            raise ContractError(f"provider profile {candidate_id!r} is invalid")
        capabilities = profile.get("capabilities")
        if not isinstance(capabilities, list) or not set(required).intersection(capabilities):
            raise ContractError(f"provider profile {candidate_id!r} is incompatible with role {role_name!r}")
    model = _valid_model(runtime.get("model"))
    effort = runtime.get("effort")
    if not non_empty(effort):
        raise ContractError(f"assignment plan for role {role_name!r} has an invalid effort")
    return {
        "role": role,
        "zone": {"paths": [] if role["mode"] == "read-only" else list(write_paths)},
        "profile_id": profile_id,
        "model": model,
        "effort": effort.strip(),
        "transport": transport,
        "runtime_plan": runtime,
    }


def validate_brief_policy(
    brief: dict[str, Any], project: dict[str, Any], config: dict[str, Any], roles_root: Path,
    *, expected_transport: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate the policy-owned portion of an approved immutable handoff brief."""
    reject_sensitive(brief, "dispatch brief")
    approval = brief.get("coordinator_approval")
    if not isinstance(approval, dict) or set(approval) != {"approved_by", "approved_at"} or not all(
        non_empty(value) for value in approval.values()
    ):
        raise ContractError("dispatch brief requires coordinator_approval with approved_by and approved_at")
    if expected_transport is not None and brief.get("resolved_transport", expected_transport) != expected_transport:
        raise ContractError(f"dispatch brief selected a non-{expected_transport} transport")
    for field in (
        "ticket", "role", "zone", "branch", "worktree", "definition_of_done", "prohibited_changes",
        "verification_commands", "required_gates", "dependencies",
    ):
        if field not in brief:
            raise ContractError(f"dispatch brief is missing {field!r}")
    if not non_empty(brief["ticket"]) or not non_empty(brief["role"]):
        raise ContractError("dispatch brief ticket and role must be non-empty strings")
    if not non_empty(brief["zone"]) or not non_empty(brief["branch"]):
        raise ContractError("dispatch brief zone and branch must be non-empty strings")
    if not non_empty(brief["worktree"]):
        raise ContractError("dispatch brief worktree must be a non-empty string")
    if not isinstance(brief["definition_of_done"], list) or not all(non_empty(item) for item in brief["definition_of_done"]):
        raise ContractError("dispatch brief definition_of_done must be a non-empty list of strings")
    if not isinstance(brief["prohibited_changes"], list) or not all(non_empty(item) for item in brief["prohibited_changes"]):
        raise ContractError("dispatch brief prohibited_changes must be a non-empty list of strings")
    for field in ("verification_commands", "required_gates", "dependencies"):
        if not isinstance(brief[field], list) or not all(non_empty(item) for item in brief[field]):
            raise ContractError(f"dispatch brief {field} must be a list of strings")
    expected_commands = config.get("developer_verification_commands", config.get("verification_commands")) \
        if brief.get("role") == "developer" and brief.get("purpose") == "work" else config.get("verification_commands")
    if brief["verification_commands"] != expected_commands:
        raise ContractError("dispatch brief verification_commands must exactly match its project role configuration")
    branch = brief["branch"]
    pattern = project.get("branch_pattern", r"^feature/issue-[0-9]+-.+")
    base = project.get("base_branch", "master")
    try:
        valid_branch = isinstance(pattern, str) and re.fullmatch(pattern, branch) is not None
    except re.error as exc:
        raise ContractError("project config has an invalid branch_pattern") from exc
    if branch.startswith("integration/") or branch == base or not valid_branch:
        raise ContractError("dispatch brief branch must be an issue branch and never a protected or integration branch")
    role_name = brief["role"]
    role = load_role_manifest(roles_root / f"{role_name}.md")
    assignment = resolve_assignment(config, role, role_name, brief["zone"], brief.get("resolved_runtime", "codex"))
    if brief.get("access") != role["mode"]:
        raise ContractError(f"dispatch brief access must be {role['mode']!r} for role {role_name!r}")
    expected_paths = assignment["zone"]["paths"]
    if role["mode"] == "write" and brief.get("write_paths") != expected_paths:
        raise ContractError("write dispatch paths must exactly match its role assignment")
    if role["mode"] == "read-only" and brief.get("write_paths"):
        raise ContractError("read-only role cannot receive write paths")
    for field, expected in (
        ("resolved_provider_profile", assignment["profile_id"]),
        ("resolved_model", assignment["model"]),
        ("resolved_effort", assignment["effort"]),
        ("resolved_transport", assignment["transport"]),
    ):
        if field in brief and brief[field] != expected:
            raise ContractError(f"dispatch brief {field} does not match the project assignment")
    return role, assignment


def health_problems(config_path: Path, roles_root: Path) -> list[str]:
    """Return health diagnostics without changing project state."""
    problems: list[str] = []
    if not config_path.is_file():
        return problems
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
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
        problems.append(f"{config_path.as_posix()} missing required field(s): {', '.join(missing)}")
    extra = sorted(set(config) - CONFIG_ALLOWED_FIELDS)
    if extra:
        problems.append(f"{config_path.as_posix()} has unknown field(s): {', '.join(extra)}")
    roles: dict[str, dict[str, Any]] = {}
    if not roles_root.is_dir():
        problems.append("missing .harness/orchestration/roles")
    else:
        for path in sorted(roles_root.glob("*.md")):
            if path.stem == "_common":
                continue
            try:
                role = load_role_manifest(path)
            except ContractError as exc:
                problems.append(str(exc).replace("role manifest", "orchestration role manifest"))
                continue
            unknown = sorted(set(role) - ROLE_FIELDS)
            if unknown:
                problems.append(f"orchestration role manifest {path.name} has unknown field(s): {', '.join(unknown)}")
            if role.get("name") != path.stem:
                problems.append(f"orchestration role manifest {path.name} name must be {path.stem!r}")
            if role.get("mode") not in ROLE_MODES:
                problems.append(f"orchestration role manifest {path.name} mode must be 'write' or 'read-only'")
            if not string_list(role.get("required_capabilities")):
                problems.append(f"orchestration role manifest {path.name} required_capabilities must be a non-empty list")
            if not string_list(role.get("risk_triggers")):
                problems.append(f"orchestration role manifest {path.name} risk_triggers must be a non-empty list")
            if isinstance(role.get("name"), str):
                if role["name"] in roles:
                    problems.append(f"duplicate orchestration role manifest: {role['name']}")
                roles[role["name"]] = role
    review = roles.get("code-review")
    if review is not None:
        if review.get("mode") != "read-only":
            problems.append("code-review role manifest must remain read-only")
        if "code-review" not in set(review.get("required_capabilities", [])):
            problems.append("code-review role manifest must require the code-review capability")
        missing_triggers = sorted(CODE_REVIEW_REQUIRED_RISK_TRIGGERS - set(review.get("risk_triggers", [])))
        if missing_triggers:
            problems.append("code-review role manifest is missing required risk trigger(s): " + ", ".join(missing_triggers))
    profiles = config.get("provider_profiles")
    profile_capabilities: dict[str, set[str]] = {}
    profile_fallbacks: dict[str, list[str]] = {}
    if not isinstance(profiles, dict):
        problems.append("orchestration provider_profiles must be an object")
        profiles = {}
    for profile_id, profile in profiles.items():
        if not non_empty(profile_id):
            problems.append("orchestration provider profile IDs must be non-empty strings")
            continue
        if not isinstance(profile, dict):
            problems.append(f"provider profile {profile_id!r} must be an object")
            continue
        extra_profile = sorted(set(profile) - {"capabilities", "agent", "fallback", "known_limitations"})
        if extra_profile:
            problems.append(
                f"provider profile {profile_id!r} has unknown field(s): {', '.join(extra_profile)}; "
                "credentials and role-policy overrides are not allowed"
            )
        for field in ("capabilities", "fallback", "known_limitations"):
            if field not in profile:
                problems.append(f"provider profile {profile_id!r} missing required field: {field}")
        capabilities = profile.get("capabilities")
        if not string_list(capabilities) or not capabilities:
            problems.append(f"provider profile {profile_id!r} capabilities must be a non-empty list")
            capabilities = []
        profile_capabilities[profile_id] = set(capabilities)
        agent = profile.get("agent")
        if agent is not None and not non_empty(agent):
            problems.append(f"provider profile {profile_id!r} agent must be a non-empty string")
        if not string_list(profile.get("fallback")):
            problems.append(f"provider profile {profile_id!r} fallback must be a list of profile IDs")
            profile_fallbacks[profile_id] = []
        else:
            profile_fallbacks[profile_id] = profile["fallback"]
        if not string_list(profile.get("known_limitations")):
            problems.append(f"provider profile {profile_id!r} known_limitations must be a list of strings")
    for profile_id, fallbacks in profile_fallbacks.items():
        for fallback_id in fallbacks:
            if fallback_id not in profiles:
                problems.append(f"provider profile {profile_id!r} references unknown fallback profile {fallback_id!r}")
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
            problems.append(f"backend zone {zone_id!r} has unknown field(s): {', '.join(extra_zone)}")
        if not string_list(zone.get("paths")) or not zone["paths"]:
            problems.append(f"backend zone {zone_id!r} paths must be a non-empty list")
    assignments = config.get("assignment_plans")
    if isinstance(assignments, dict):
        if assignments and "code-review" not in assignments:
            problems.append("orchestration assignment_plans must include code-review for the mandatory high-risk role gate")
        for role_name, plan in assignments.items():
            if role_name not in roles:
                problems.append(f"unknown role in orchestration assignment_plans: {role_name}")
                continue
            if not isinstance(plan, dict):
                problems.append(f"assignment plan for role {role_name!r} must be an object")
                continue
            extra_plan = sorted(set(plan) - {"zone", "write_paths", "runtimes", "transport"})
            if extra_plan:
                problems.append(f"assignment plan for role {role_name!r} cannot override role manifest fields: {', '.join(extra_plan)}")
            if "transport" in plan and plan["transport"] not in ROLE_TRANSPORTS:
                problems.append(
                    f"assignment plan for role {role_name!r} transport must be one of: "
                    + ", ".join(sorted(ROLE_TRANSPORTS))
                )
            zone_id = plan.get("zone")
            if not non_empty(zone_id):
                problems.append(f"assignment plan for role {role_name!r} zone must be a non-empty string")
            elif zone_id not in zones:
                problems.append(f"unknown backend zone {zone_id!r} for role {role_name!r}")
            write_paths = plan.get("write_paths")
            if write_paths is not None and (not string_list(write_paths) or not write_paths):
                problems.append(f"assignment plan for role {role_name!r} write_paths must be a non-empty list when provided")
            elif write_paths is not None and isinstance(zone_id, str) and isinstance(zones.get(zone_id), dict):
                boundaries = zones[zone_id].get("paths")
                if string_list(boundaries) and any(
                    not any(_inside(path, boundary) for boundary in boundaries) for path in write_paths
                ):
                    problems.append(f"assignment plan for role {role_name!r} write_paths must remain inside zone {zone_id!r}")
            runtimes = plan.get("runtimes")
            if not isinstance(runtimes, dict) or not runtimes:
                problems.append(f"assignment plan for role {role_name!r} runtimes must be a non-empty object")
                continue
            for runtime_name in runtimes:
                runtime = runtimes[runtime_name]
                if not non_empty(runtime_name) or not isinstance(runtime, dict):
                    problems.append(f"assignment runtime for role {role_name!r} must be a named object")
                    continue
                extra_runtime = sorted(set(runtime) - {"profiles", "model", "effort"})
                if extra_runtime:
                    problems.append(f"assignment runtime {runtime_name!r} for role {role_name!r} has unknown field(s): {', '.join(extra_runtime)}")
                profile_ids = runtime.get("profiles")
                if not string_list(profile_ids) or not profile_ids:
                    problems.append(f"assignment runtime {runtime_name!r} for role {role_name!r} profiles must be a non-empty list")
                    profile_ids = []
                for field in ("model", "effort"):
                    if not non_empty(runtime.get(field)):
                        problems.append(f"assignment runtime {runtime_name!r} for role {role_name!r} {field} must be a non-empty string")
                model = runtime.get("model")
                if non_empty(model) and MODEL_ID.fullmatch(model.strip()) is None:
                    problems.append(f"assignment runtime {runtime_name!r} for role {role_name!r} model must be a CLI model ID or alias without spaces")
                for profile_id in profile_ids:
                    if profile_id not in profiles:
                        problems.append(f"unknown provider profile {profile_id!r} for role {role_name!r}")
                    elif not set(roles[role_name].get("required_capabilities", [])).intersection(profile_capabilities.get(profile_id, set())):
                        problems.append(f"provider profile {profile_id!r} has incompatible capability for role {role_name!r}; requires one of {', '.join(sorted(roles[role_name].get('required_capabilities', [])))}")
                try:
                    resolve_assignment(config, roles[role_name], role_name, plan.get("zone"), runtime_name)
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
        problems.append("orchestration developer_verification_commands must be a list of strings when provided")
    return problems
