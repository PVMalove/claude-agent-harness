"""Project configuration and policy of the orchestration coordinator.

Reads `.harness/project.json`, `.harness/orchestration.json` and the role manifests, and resolves
every policy a batch or dispatch runs under.  Resolution is deliberately tolerant field by field: a
project that authored no orchestration config still gets a working zone and the documented
defaults, and a partially specified policy keeps the defaults for the fields it omits.

The module reads configuration only — it never opens the ledger and never advances state.
"""

from __future__ import annotations

import json
from fnmatch import fnmatchcase
from pathlib import Path
from typing import cast

from harness.orchestration import extensions
from harness.orchestration.contract import (
    COMMUNICATION_POLICY_FIELDS, ContractError, health_problems, load_role_manifest,
)
from harness.orchestration.core.constants import (
    DEFAULT_ADAPTIVE_CONTINUATION_POLICY, DEFAULT_ATTENTION_POLICY, DEFAULT_COMMUNICATION_POLICY,
    DEFAULT_CONTEXT_PACKAGE_POLICY, DEFAULT_CONTINUATION_POLICY, DEFAULT_PREFLIGHT_POLICY, DEFAULT_RETRY_POLICY,
    DEFAULT_TEST_PATH_PATTERNS, DEFAULT_ZONE, SENSITIVE_KEY, ZERO_ALLOWED_POLICY_FIELDS,
)
from harness.orchestration.core.utils import (
    CoordinatorError, JsonObject, _non_empty, _read_object, _strings,
)


def _reject_sensitive(value: object, location: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise CoordinatorError(f"{location} contains a non-string key", remedy=f"use only string keys in {location}")
            if SENSITIVE_KEY.search(key):
                raise CoordinatorError(f"{location} contains secret-shaped field {key!r}", remedy=f"remove the secret-shaped field {key!r} from {location}; credentials never belong in this config")
            _reject_sensitive(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive(child, f"{location}[{index}]")


def _project(repo: Path) -> JsonObject:
    return _read_object(repo / ".harness/project.json", "project config")


def _configured(repo: Path) -> bool:
    """Whether the project actually states an orchestration configuration.

    `harness init` seeds an intentionally empty template. A present-but-empty file states nothing,
    yet taking the configured path on it makes every zone unknown and every role unassigned — a
    freshly initialised project would be unable to start a batch at all, while deleting the file
    would fix it. An empty template therefore means the same as no file: use the documented defaults.
    """
    path = repo / ".harness/orchestration.json"
    if not path.is_file():
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True  # let the real loader report the parse failure
    if not isinstance(value, dict):
        return True
    return bool(value.get("backend_zones")) or bool(value.get("assignment_plans"))


def _default_config(repo: Path) -> JsonObject:
    """Zero-configuration fallback.  A project that has not authored an orchestration config still
    gets one working zone — the whole repository — and takes the role runtime from the invoking
    session, so the gated pipeline is available before any assignment plan exists."""
    commands = _project(repo).get("qa_gate_commands")
    if not isinstance(commands, list) or not all(_non_empty(item) for item in commands):
        commands = []
    return {
        "provider_profiles": {},
        "assignment_plans": {},
        "backend_zones": {DEFAULT_ZONE: {"paths": ["**"]}},
        "concurrency_budget": 1,
        "developer_verification_commands": list(commands),
        "verification_commands": list(commands),
    }


def _config(repo: Path) -> JsonObject:
    if not _configured(repo):
        return _default_config(repo)
    value = _read_object(repo / ".harness/orchestration.json", "project orchestration config")
    _reject_sensitive(value, "project orchestration config")
    problems = health_problems(repo / ".harness/orchestration.json", repo / ".harness/orchestration/roles")
    if problems:
        raise CoordinatorError("invalid project orchestration config: " + "; ".join(problems), remedy="fix the listed project orchestration config problem(s) before retrying")
    return value


def _adaptive_continuation_policy(config: JsonObject) -> JsonObject:
    """Adaptive-policy thresholds for a planned-trigger continuation. Project-configurable per
    AC5; an absent or partially-specified `adaptive_continuation_policy` falls back to the
    documented defaults field by field, the same tolerance `_default_config` gives every other
    zero-configuration project."""
    policy = config.get("adaptive_continuation_policy")
    resolved = dict(DEFAULT_ADAPTIVE_CONTINUATION_POLICY)
    if isinstance(policy, dict):
        for key in resolved:
            if key == "context_warn_ratio":
                continue
            value = policy.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
                resolved[key] = value
        ratio = policy.get("context_warn_ratio")
        if (
            isinstance(ratio, (int, float)) and not isinstance(ratio, bool)
            and 0 < ratio <= 1
        ):
            resolved["context_warn_ratio"] = ratio
    return resolved


def _context_advisory(config: JsonObject, observed: int | None) -> JsonObject:
    """Advisory-only read of an observed token count against `context_warn_ratio` of
    `context_limit`. Never authorizes a checkpoint or changes state — a coordinator decision does."""
    policy = _adaptive_continuation_policy(config)
    limit = policy["context_limit"]
    warn_at = round(limit * policy["context_warn_ratio"])
    if observed is None or observed < warn_at:
        level = "ok"
    elif observed < limit:
        level = "warn"
    else:
        level = "over"
    return {"level": level, "limit": limit, "warn_at": warn_at, "observed": observed}


def _numeric_policy(config: JsonObject, key: str, defaults: dict[str, int]) -> dict[str, int]:
    """Resolve a small project policy after config validation, retaining safe defaults.

    This second guard makes direct coordinator use safe even if a caller bypasses ``harness
    health``.  ``retry_policy.max_developer_retries`` alone permits zero to explicitly disable
    retries; all other policy values are positive.
    """
    resolved = dict(defaults)
    configured = config.get(key)
    if not isinstance(configured, dict):
        return resolved
    for name, default in defaults.items():
        value = configured.get(name)
        minimum = 0 if (key, name) in ZERO_ALLOWED_POLICY_FIELDS else 1
        if isinstance(value, int) and not isinstance(value, bool) and value >= minimum:
            resolved[name] = value
    return resolved


def _context_package_policy(config: JsonObject) -> dict[str, int]:
    policy = _numeric_policy(config, "context_package_policy", DEFAULT_CONTEXT_PACKAGE_POLICY)
    if policy["reserved_prompt_tokens"] >= policy["context_window_tokens"]:
        raise CoordinatorError("context_package_policy reserved_prompt_tokens must be below context_window_tokens", remedy="lower context_package_policy.reserved_prompt_tokens below context_window_tokens")
    available = policy["context_window_tokens"] - policy["reserved_prompt_tokens"]
    if policy["max_tokens"] > available:
        raise CoordinatorError("context_package_policy max_tokens exceeds available context after prompt headroom", remedy="lower context_package_policy.max_tokens so it fits inside context_window_tokens minus reserved_prompt_tokens")
    return policy


def _continuation_policy(config: JsonObject) -> dict[str, int]:
    return _numeric_policy(config, "continuation_policy", DEFAULT_CONTINUATION_POLICY)


def _retry_policy(config: JsonObject) -> dict[str, int]:
    return _numeric_policy(config, "retry_policy", DEFAULT_RETRY_POLICY)


def _attention_policy(config: JsonObject) -> dict[str, int]:
    return _numeric_policy(config, "attention_policy", DEFAULT_ATTENTION_POLICY)


def _approval_ttl(config: JsonObject) -> int | None:
    """Seconds an explicit approval stays valid; ``None`` (the default) means it does not expire."""
    value = config.get("approval_ttl_seconds")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else None


def _extension_names(config: JsonObject) -> dict[str, str]:
    try:
        return extensions.selected(config)
    except extensions.ExtensionError as exc:
        raise CoordinatorError(exc.message, remedy=exc.remedy) from exc


def _orchestration_policy(config: JsonObject) -> JsonObject:
    """The operational policy this brief runs under. Recorded in the immutable brief so a later edit
    to `.harness/orchestration.json` never changes what an in-flight dispatch was approved under."""
    adaptive = _adaptive_continuation_policy(config)
    return {
        "approval_ttl_seconds": _approval_ttl(config),
        "attention": dict(_attention_policy(config)),
        "context_pressure": {"context_limit": adaptive["context_limit"], "warning_ratio": adaptive["context_warn_ratio"]},
        "extensions": _extension_names(config),
    }


def _preflight_policy(config: JsonObject) -> JsonObject:
    policy: JsonObject = dict(DEFAULT_PREFLIGHT_POLICY)
    configured = config.get("preflight_policy")
    if not isinstance(configured, dict):
        return policy
    for name, default in DEFAULT_PREFLIGHT_POLICY.items():
        value = configured.get(name)
        if name == "require_estimates":
            if isinstance(value, bool):
                policy[name] = value
        elif isinstance(value, int) and not isinstance(value, bool) and value >= 1:
            policy[name] = value
    return policy


def _test_path_patterns(config: JsonObject) -> list[str]:
    patterns = config.get("test_path_patterns")
    if isinstance(patterns, list) and patterns and all(_non_empty(item) for item in patterns):
        return list(patterns)
    return list(DEFAULT_TEST_PATH_PATTERNS)


def _is_test_path(path: str, patterns: list[str]) -> bool:
    return any(fnmatchcase(path, pattern) for pattern in patterns)


def _role(repo: Path, name: str) -> JsonObject:
    try:
        return load_role_manifest(repo / ".harness/orchestration/roles" / f"{name}.md")
    except ContractError as exc:
        raise CoordinatorError(exc.message, remedy=exc.remedy) from exc


def _verification_commands(config: JsonObject) -> list[str]:
    commands = config.get("verification_commands")
    return _strings(commands, "verification_commands", allow_empty=True)


def _developer_verification_commands(config: JsonObject) -> list[str]:
    """Focused developer proof, with the historical full-QA list as a safe fallback.

    Older project configs have one verification list.  Keeping that as the fallback preserves their
    existing approval contract, while a project can opt into a narrow developer loop without
    weakening the clean-room QA commands stored separately in ``verification_commands``.
    """
    commands = config.get("developer_verification_commands")
    if commands is None:
        return _verification_commands(config)
    return _strings(commands, "developer_verification_commands", allow_empty=True)


def _worker_attestation_required(config: JsonObject) -> bool:
    value = config.get("worker_attestation_required", False)
    if not isinstance(value, bool):
        raise CoordinatorError("worker_attestation_required must be a boolean", remedy="set worker_attestation_required to true or false in the project orchestration config")
    return value


def _communication_policy(config: JsonObject) -> dict[str, str]:
    value = config.get("communication_policy", DEFAULT_COMMUNICATION_POLICY)
    if not isinstance(value, dict) or set(value) != COMMUNICATION_POLICY_FIELDS:
        raise CoordinatorError(
            "communication_policy must contain agent_to_agent_language and coordinator_report_language",
            remedy="set communication_policy to an object with agent_to_agent_language and coordinator_report_language",
        )
    if value != DEFAULT_COMMUNICATION_POLICY:
        raise CoordinatorError(
            "communication_policy must use English for agent communication and Russian for coordinator reports",
            remedy="set communication_policy.agent_to_agent_language to 'en' and coordinator_report_language to 'ru'",
        )
    return dict(value)


def _human_approval_gate(config: JsonObject) -> str:
    gate = config.get("human_approval_gate", "trusted")
    if gate not in {"trusted", "tty"}:
        raise CoordinatorError("human_approval_gate must be trusted or tty", remedy="set human_approval_gate to 'trusted' or 'tty' in the project orchestration config")
    return cast(str, gate)


def _approval_policy(config: JsonObject) -> str:
    policy = config.get("approval_policy", "manual_all")
    if policy not in {"manual_all", "milestone", "low_risk"}:
        raise CoordinatorError("approval_policy must be manual_all, milestone or low_risk", remedy="set approval_policy to 'manual_all', 'milestone' or 'low_risk' in the project orchestration config")
    return cast(str, policy)
