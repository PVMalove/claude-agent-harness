"""Normalize config enums only, then validate their canonical contract."""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from pathlib import Path

from harness.configuration import parse_object, reject_secrets
from harness.diagnostics import Diagnostic
from harness.json_types import JsonObject
from harness.configuration_schema import schema_diagnostics
from harness.orchestration.contract import configuration_problems, fallback_cycles
from harness.orchestration.core.system_fields import SystemFieldsError, load_registry

NORMALIZATION_VERSION = "normalization.v1"
# Exact path segments; '*' matches one project-owned key, never its spelling.
NORMALIZATION_FIELDS = {
    ("approval_policy",): "config.approval_policy",
    ("human_approval_gate",): "config.human_approval_gate",
    ("auto_retry_min_severity",): "config.auto_retry_min_severity",
    (
        "communication_policy",
        "agent_to_agent_language",
    ): "config.communication_language",
    (
        "communication_policy",
        "coordinator_report_language",
    ): "config.communication_language",
    ("assignment_plans", "*", "transport"): "role.transport",
    ("assignment_plans", "*", "runtimes", "*", "effort"): "assignment.effort",
    ("repo_map_policy", "tier"): "repo_map.tier",
    ("repo_map_policy", "min_tier"): "repo_map.tier",
    ("repo_map_policy", "min_tier_by_role", "*"): "repo_map.tier",
    ("access_policy", "defaults", "mode"): "access.mode",
    ("access_policy", "roles", "*", "mode"): "access.mode",
    ("access_policy", "operations", "*", "mode"): "access.mode",
}
# Only documented aliases per field. No generic separator/fuzzy conversion or null default.
ALIASES: dict[str, dict[str, str]] = {
    "role.transport": {"in_process": "in-process"},
    "config.approval_policy": {"manual-all": "manual_all", "low-risk": "low_risk"},
}


@dataclass(frozen=True)
class ConfigurationResult:
    document: JsonObject
    diagnostics: tuple[Diagnostic, ...]
    source_sha256: str

    @property
    def valid(self) -> bool:
        return all(item.severity != "error" for item in self.diagnostics)

    def to_dict(self) -> JsonObject:
        return {
            "valid": self.valid,
            "normalizer": NORMALIZATION_VERSION,
            "document": self.document if self.valid else None,
            "source_sha256": self.source_sha256,
            "diagnostics": [item.to_dict() for item in self.diagnostics[:200]],
            "diagnostics_total": len(self.diagnostics),
            "diagnostics_truncated": len(self.diagnostics) > 200,
        }


def normalize_configuration(
    document: JsonObject,
) -> tuple[JsonObject, list[Diagnostic]]:
    """Return a copy and warnings; do not normalize IDs, keys, commands or reports."""
    result = copy.deepcopy(document)
    diagnostics: list[Diagnostic] = []
    registry = load_registry()
    missing = set(NORMALIZATION_FIELDS.values()) - set(registry.fields)
    if missing:
        raise SystemFieldsError(
            "normalizer registry is incomplete: " + ", ".join(sorted(missing)),
            remedy="restore system_fields.v1.json from the pinned harness installation",
        )

    def walk(value: JsonObject, path: tuple[str, ...]) -> None:
        for key, item in value.items():
            parts = (*path, key)
            field_id = next(
                (
                    field
                    for pattern, field in NORMALIZATION_FIELDS.items()
                    if len(pattern) == len(parts)
                    and all(a == "*" or a == b for a, b in zip(pattern, parts))
                ),
                None,
            )
            if field_id and isinstance(item, str):
                allowed = registry.fields[field_id].allowed
                folded = item.strip().casefold()
                candidates = [
                    choice for choice in allowed if choice.casefold() == folded
                ]
                alias = ALIASES.get(field_id, {}).get(folded)
                if alias in allowed and alias not in candidates:
                    candidates.append(alias)
                if len(candidates) == 1 and candidates[0] != item:
                    value[key] = candidates[0]
                    diagnostics.append(
                        Diagnostic(
                            "config-normalized",
                            "orchestration.json",
                            parts,
                            f"{NORMALIZATION_VERSION}:{field_id}: normalized config enum",
                            allowed,
                            item,
                            candidates[0],
                            "warning",
                            True,
                            rule_id=field_id,
                            rule_version=NORMALIZATION_VERSION,
                        )
                    )
                elif len(candidates) > 1:
                    diagnostics.append(
                        Diagnostic(
                            "config-ambiguous",
                            "orchestration.json",
                            parts,
                            "normalization has more than one canonical target",
                            allowed,
                            item,
                        )
                    )
            elif isinstance(item, dict):
                walk(item, parts)

    walk(result, ())
    return result, diagnostics


def reference_diagnostics(document: JsonObject) -> list[Diagnostic]:
    """Pointers for semantic graph constraints which JSON Schema cannot express."""
    diagnostics: list[Diagnostic] = []
    profiles = document.get("provider_profiles", {})
    if not isinstance(profiles, dict):
        return diagnostics
    graph: dict[str, list[str]] = {}
    for name, profile in profiles.items():
        if not isinstance(profile, dict) or not isinstance(
            profile.get("fallback"), list
        ):
            continue
        graph[name] = [
            target for target in profile["fallback"] if isinstance(target, str)
        ]
        for index, target in enumerate(profile["fallback"]):
            if isinstance(target, str) and target not in profiles:
                diagnostics.append(
                    Diagnostic(
                        "config-reference",
                        "orchestration.json",
                        ("provider_profiles", name, "fallback", index),
                        "fallback must reference an existing provider profile",
                        list(profiles),
                        target,
                    )
                )
    for name in fallback_cycles(graph):
        diagnostics.append(
            Diagnostic(
                "config-cycle",
                "orchestration.json",
                ("provider_profiles", name, "fallback"),
                "fallback chain contains a cycle",
                "acyclic fallback references",
                graph[name],
            )
        )
    plans = document.get("assignment_plans", {})
    if not isinstance(plans, dict):
        return diagnostics
    for role, plan in plans.items():
        runtimes = plan.get("runtimes") if isinstance(plan, dict) else None
        if not isinstance(runtimes, dict):
            continue
        default = plan.get("default_runtime")
        if default is not None and (
            not isinstance(default, str) or default not in runtimes
        ):
            diagnostics.append(
                Diagnostic(
                    "config-reference",
                    "orchestration.json",
                    ("assignment_plans", role, "default_runtime"),
                    "default_runtime must name an authored runtime",
                    list(runtimes),
                    default,
                )
            )
        for runtime, assignment in runtimes.items():
            targets = (
                assignment.get("profiles") if isinstance(assignment, dict) else None
            )
            if not isinstance(targets, list):
                continue
            for index, target in enumerate(targets):
                if isinstance(target, str) and target not in profiles:
                    diagnostics.append(
                        Diagnostic(
                            "config-reference",
                            "orchestration.json",
                            (
                                "assignment_plans",
                                role,
                                "runtimes",
                                runtime,
                                "profiles",
                                index,
                            ),
                            "assignment must reference an existing provider profile",
                            list(profiles),
                            target,
                        )
                    )
    return diagnostics


def inspect_configuration(
    repo: Path,
    raw: bytes | None,
    *,
    canonical: bool = False,
    roles_root: Path | None = None,
) -> ConfigurationResult:
    """A public read-only boundary with safe diagnostics and canonical output."""
    digest = hashlib.sha256(raw or b"").hexdigest()
    try:
        authored = {} if raw is None else parse_object(raw, "orchestration.json")
        reject_secrets(authored)
    except ValueError as exc:
        return ConfigurationResult(
            {},
            (
                Diagnostic(
                    "config-invalid",
                    "orchestration.json",
                    (),
                    str(exc),
                    "bounded, unambiguous JSON object without secrets",
                ),
            ),
            digest,
        )
    try:
        document, diagnostics = normalize_configuration(authored)
    except SystemFieldsError as exc:
        return ConfigurationResult(
            {},
            (
                Diagnostic(
                    "config-contract",
                    "orchestration/system_fields.v1.json",
                    (),
                    f"{exc.message}; {exc.remedy}",
                    "complete systemFields.v1 registry",
                ),
            ),
            digest,
        )
    if canonical:
        diagnostics = [
            Diagnostic(
                "config-noncanonical",
                item.file,
                item.parts,
                "write the suggested canonical enum",
                item.expected,
                item.received,
                item.suggestion,
            )
            for item in diagnostics
        ]
    effective = (
        {
            "provider_profiles": {},
            "assignment_plans": {},
            "concurrency_budget": 1,
            "verification_commands": [],
            **document,
        }
        if not any(
            document.get(key)
            for key in ("assignment_plans", "provider_profiles", "backend_zones")
        )
        else document
    )
    problems = configuration_problems(
        effective, roles_root or repo / ".harness/orchestration/roles"
    )
    try:
        diagnostics.extend(schema_diagnostics(repo, "orchestration.json", effective))
    except (ValueError, OSError):
        diagnostics.append(
            Diagnostic(
                "config-schema",
                "orchestration/orchestration.schema.json",
                (),
                "schema is unavailable or invalid; restore the installed schema from the pinned harness",
                "readable supported orchestration schema",
            )
        )
    diagnostics.extend(reference_diagnostics(effective))
    diagnostics.extend(
        Diagnostic("config-contract", "orchestration.json", (), problem)
        for problem in problems
    )
    return ConfigurationResult(document, tuple(diagnostics), digest)
