"""Resolve project access without treating settings as runtime permission evidence.

Filesystem permission roots do not replace source write_paths, tool policy or native approval.
No resolver call writes files, probes network hosts or changes a machine profile.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from harness.errors import INTERNAL_INVARIANT_REMEDY, HarnessError
from harness.storage import storage_root

from . import extensions
from .contract import access_policy_problems
from .core.constants import ACCESS_OPERATIONS, ACCESS_RESOURCES
from .core.utils import JsonObject

# Local Git plumbing only; a slow disk or a huge repository is the worst expected case.
GIT_TIMEOUT_SECONDS = 60


class AccessError(HarnessError):
    """The requested role access cannot be safely resolved or verified."""


def _digest(plan: Mapping[str, object]) -> str:
    data = {key: value for key, value in plan.items() if key != "plan_digest"}
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _root(path: Path) -> str:
    resolved = path.expanduser().resolve()
    home = Path.home().resolve()
    if (
        resolved == Path(resolved.anchor)
        or resolved == home
        or resolved in home.parents
    ):
        raise AccessError(
            "access requires an entire filesystem or home directory",
            remedy="select a specific project, Git, storage or cache directory",
        )
    return str(resolved)


def validate_plan(plan: object) -> None:
    """Reject malformed plans before trusting their checksum or consulting a provider."""
    remedy = "create a new dispatch and approval for a valid resolved access plan"
    fields = {
        "mode",
        "network",
        "filesystem",
        "sources",
        "requirements",
        "runtime_extension",
        "plan_digest",
    }
    if not isinstance(plan, dict) or set(plan) != fields:
        raise AccessError("runtime access plan schema mismatch", remedy=remedy)
    components = {key: plan[key] for key in ("mode", "network")}
    if access_policy_problems({"defaults": components}, set()):
        raise AccessError(
            "runtime access plan has invalid mode or network", remedy=remedy
        )
    hosts = plan["network"]["hosts"]
    if hosts != sorted(set(host.lower() for host in hosts)):
        raise AccessError("runtime access hosts are not canonical", remedy=remedy)
    sources = plan["sources"]
    if (
        not isinstance(sources, dict)
        or set(sources) != {"mode", "network", "filesystem"}
        or any(
            not isinstance(value, str)
            or not value
            or (
                value not in ("legacy", "default", "defaults")
                and not value.startswith(("roles.", "operations."))
            )
            for value in sources.values()
        )
        or not isinstance(plan["runtime_extension"], str)
        or not plan["runtime_extension"].strip()
    ):
        raise AccessError(
            "runtime access sources or extension are invalid", remedy=remedy
        )
    for field in ("filesystem", "requirements"):
        entries = plan[field]
        if not isinstance(entries, list):
            raise AccessError(f"runtime access {field} must be a list", remedy=remedy)
        for item in entries:
            if (
                not isinstance(item, dict)
                or set(item) != {"resource", "path", "access"}
                or item["resource"] not in (*ACCESS_RESOURCES, "artifacts")
                or item["access"] not in ("read", "write")
                or not isinstance(item["path"], str)
                or not Path(item["path"]).is_absolute()
                or _root(Path(item["path"])) != item["path"]
            ):
                raise AccessError(
                    f"runtime access {field} has an invalid real path requirement",
                    remedy=remedy,
                )
        if len(
            {(item["resource"], item["path"], item["access"]) for item in entries}
        ) != len(entries):
            raise AccessError(
                f"runtime access {field} has duplicate requirements", remedy=remedy
            )
    legacy = all(value == "legacy" for value in sources.values())
    if legacy:
        if (
            plan["mode"] != "inherit"
            or hosts
            or plan["filesystem"]
            or plan["requirements"]
            or plan["runtime_extension"] != "none"
        ):
            raise AccessError(
                "legacy runtime access must preserve empty inherit", remedy=remedy
            )
    elif (
        any(value == "legacy" for value in sources.values())
        or not {"checkout", "git_common", "shared_storage", "artifacts"}
        <= {item["resource"] for item in plan["requirements"]}
        or any(item not in plan["requirements"] for item in plan["filesystem"])
    ):
        raise AccessError(
            "runtime access plan omits operational requirements", remedy=remedy
        )
    if plan["plan_digest"] != _digest(plan):
        raise AccessError("runtime access plan digest mismatch", remedy=remedy)


def validate_binding(brief: Mapping[str, object]) -> None:
    """A historical brief has neither field; a new one binds the entire canonical plan."""
    transition = brief.get("transition")
    bound = (
        transition.get("runtime_access_sha256")
        if isinstance(transition, dict)
        else None
    )
    if "runtime_access" not in brief and bound is None:
        return
    validate_plan(brief.get("runtime_access"))
    plan = cast(JsonObject, brief["runtime_access"])
    if bound != plan["plan_digest"]:
        raise AccessError(
            "runtime access plan is not bound to its approval transition",
            remedy="propose and approve a new dispatch for this access plan",
        )
    if plan["requirements"]:
        checkout = [
            {
                "resource": "checkout",
                "path": str(Path(str(brief.get("worktree"))).resolve()),
                "access": "write" if brief.get("access") == "write" else "read",
            }
        ]
        # A QA brief names a read-only role, yet the coordinator that runs its operation writes
        # the clean-room checkout, Git metadata and shared storage: the role boundary never binds it.
        if checkout[0] not in plan["requirements"] or (
            brief.get("access") == "read-only"
            and brief.get("role") != "qa"
            and any(
                item["resource"] in ("checkout", "git_common", "shared_storage")
                and item["access"] == "write"
                for item in plan["requirements"]
            )
        ):
            raise AccessError(
                "runtime access violates the brief checkout or read-only boundary",
                remedy="propose and approve a plan matching the role manifest and worktree",
            )


def dispatch_operation(role: str, purpose: str) -> str | None:
    """The coordinator operation whose access override a dispatch selects, if any.

    Only the operations a dispatch itself performs are mapped; ``git`` has no dispatch of its own.
    """
    if purpose == "publish":
        return "publish"
    return "qa" if role == "qa" else None


def resolve_plan(
    repo: Path,
    worktree: Path,
    config: Mapping[str, object],
    role: str | None,
    access: str,
    *,
    operation: str | None = None,
) -> JsonObject:
    """Replace specified components, resolve real roots and retain mandatory artifacts.

    A plan without authored access preserves historical inherit behavior. An authored policy
    also exposes operational requirements that an empty filesystem override cannot remove.

    An ``operation`` is executed by the coordinator itself, never by a role adapter: it takes the
    defaults and its own override only, so a role override cannot replace its choice, and the
    coordinator needs write access to Git metadata and shared storage whatever the role's mode.
    """
    if operation is not None and operation not in ACCESS_OPERATIONS:
        raise AccessError(
            f"unknown coordinator operation {operation!r}",
            remedy=f"select one of: {', '.join(ACCESS_OPERATIONS)}",
        )
    policy = config.get("access_policy")
    if "access_policy" not in config:
        plan: JsonObject = {
            "mode": "inherit",
            "network": {"hosts": []},
            "filesystem": [],
            "sources": {key: "legacy" for key in ("mode", "network", "filesystem")},
            "requirements": [],
            "runtime_extension": "none",
        }
        plan["plan_digest"] = _digest(plan)
        return plan
    problems = access_policy_problems(policy, {role} if role else set())
    # Other role overrides are validated by the config contract, not this selected-role view.
    problems = [
        problem
        for problem in problems
        if "access_policy.roles has unknown name" not in problem
    ]
    if problems:
        raise AccessError(
            "; ".join(problems),
            remedy="fix access_policy and repeat preflight before approval",
        )
    if not isinstance(policy, dict):
        raise AccessError(
            "access_policy passed validation without being an object",
            remedy=INTERNAL_INVARIANT_REMEDY,
        )
    selected: JsonObject = {
        "mode": "inherit",
        "network": {"hosts": []},
        "filesystem": [],
    }
    sources: JsonObject = {key: "default" for key in selected}
    layers = [("defaults", policy.get("defaults", {}))]
    for section, name in (
        ("roles", None if operation else role),
        ("operations", operation),
    ):
        overrides = policy.get(section, {})
        if name is not None and isinstance(overrides, dict) and name in overrides:
            layers.append((f"{section}.{name}", overrides[name]))
    for label, layer in layers:
        if not isinstance(layer, dict):
            raise AccessError(
                f"access_policy {label} passed validation without being an object",
                remedy=INTERNAL_INVARIANT_REMEDY,
            )
        for key in selected:
            if key in layer:
                selected[key] = layer[key]
                sources[key] = label
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise AccessError(
            f"cannot resolve shared Git metadata: {exc}",
            remedy="make git available and responsive in the worktree and repeat preflight",
        ) from exc
    if result.returncode:
        raise AccessError(
            "cannot resolve shared Git metadata",
            remedy="prepare a registered Git worktree and repeat preflight",
        )
    common = Path(result.stdout.strip())
    roots = {
        "checkout": worktree,
        "git_common": common if common.is_absolute() else worktree / common,
        "shared_storage": storage_root(repo, require_main_checkout=True),
    }
    filesystem: list[JsonObject] = []
    for item in selected["filesystem"]:
        resource = item["resource"]
        path = (
            worktree / Path(item["path"]).expanduser()
            if resource == "cache"
            else roots[resource]
        )
        if (
            access == "read-only"
            and item["access"] == "write"
            and resource != "cache"
            and operation is None
        ):
            raise AccessError(
                f"read-only role cannot request write access to {resource}",
                remedy="keep source and Git roots read-only; operational scratch is added separately",
            )
        filesystem.append(
            {"resource": resource, "access": item["access"], "path": _root(path)}
        )
    requirements = [
        {
            "resource": "checkout",
            "path": _root(worktree),
            "access": "write" if access == "write" else "read",
        },
        {
            "resource": "git_common",
            "path": _root(roots["git_common"]),
            "access": "write" if access == "write" or operation else "read",
        },
        {
            "resource": "shared_storage",
            "path": _root(roots["shared_storage"]),
            "access": "write" if operation else "read",
        },
        {
            "resource": "artifacts",
            "path": _root(roots["shared_storage"] / ".sandboxes/scratch"),
            "access": "write",
        },
    ]
    for item in filesystem:
        if item not in requirements:
            requirements.append(item)
    extensions = config.get("extensions", {})
    extension = (
        extensions.get("runtime_access", "none")
        if isinstance(extensions, dict)
        else "none"
    )
    plan = {
        "mode": selected["mode"],
        "network": {
            "hosts": sorted(set(host.lower() for host in selected["network"]["hosts"]))
        },
        "filesystem": filesystem,
        "sources": sources,
        "requirements": requirements,
        "runtime_extension": extension,
    }
    plan["plan_digest"] = _digest(plan)
    return plan


_REMEDY = (
    "prepare the named roots, hosts and mode in a new runtime session, select a native "
    "runtime_access implementation that verifies the actual worker launch, and repeat preflight"
)


def verify_plan(
    plan: JsonObject, transport: str
) -> tuple[JsonObject, extensions.RuntimeAccessObservation | None]:
    """Ask the pinned native implementation for fresh, worker-scoped evidence."""
    validate_plan(plan)
    if (
        plan["mode"] == "inherit"
        and all(source == "legacy" for source in plan["sources"].values())
        and not plan["filesystem"]
        and not plan["requirements"]
        and not plan["network"]["hosts"]
    ):
        return {
            "status": "legacy-inherit",
            "verified": [],
            "unverified": [],
            "remedy": None,
        }, None
    provider = extensions.runtime_access(plan["runtime_extension"])
    # The inert extension uses the telemetry observe signature and is never a permission proof.
    observation = None
    if plan["runtime_extension"] != "none":
        try:
            observation = provider.observe(plan, transport)
        except (
            Exception
        ):  # A failing provider is unverified evidence, never a crash of preflight.
            observation = None
    reason = _observation_problem(plan, transport, observation)
    summary: JsonObject = {
        "status": "unverified" if reason else "verified",
        "reason": reason,
        "required": {
            "hosts": plan["network"]["hosts"],
            "filesystem": plan["requirements"],
        },
        "verified": [] if reason else ["mode", "network", "filesystem"],
        "unverified": ["mode", "network", "filesystem"] if reason else [],
        "remedy": _REMEDY if reason else None,
    }
    if observation is not None:
        summary["evidence"] = asdict(observation)
    return summary, observation


def _observation_problem(
    plan: JsonObject,
    transport: str,
    observation: extensions.RuntimeAccessObservation | None,
) -> str | None:
    if not isinstance(observation, extensions.RuntimeAccessObservation):
        return "native worker access proof is unavailable"
    if (
        observation.plan_digest != plan["plan_digest"]
        or observation.transport != transport
    ):
        return "native worker access proof names a different plan or transport"
    if (
        not observation.environment_id
        or not observation.launch_id
        or observation.source not in ("native-runtime", "runtime-adapter")
        or observation.mechanism not in ("native-apply", "confirmed-inheritance")
    ):
        return "native worker launch identity or application mechanism is unverified"
    try:
        observed = datetime.fromisoformat(observation.observed_at)
        age = (datetime.now(UTC) - observed).total_seconds()
    except (ValueError, TypeError):
        return "native worker proof timestamp is invalid"
    if age < 0 or age > 300:
        return "native worker proof is stale"
    mode = plan["mode"]
    if mode not in observation.supported_modes:
        return f"native runtime does not support requested mode {mode}"
    if mode != "inherit" and observation.effective_mode != mode:
        return "native worker effective mode differs from the requested mode"
    if any(host not in observation.hosts for host in plan["network"]["hosts"]):
        return "native worker network host requirements are unverified"
    if any(
        (item["path"], item["access"]) not in observation.filesystem
        for item in plan["requirements"]
    ):
        return "native worker filesystem requirements are unverified"
    return None


def apply_plan(
    brief: JsonObject,
    transport: str,
    *,
    handoff: bool = False,
    command: tuple[str, ...] | None = None,
) -> JsonObject:
    """Gate handoff and apply the approved plan through the native implementation.

    A new observation comes from the pinned implementation on every send. Neither current
    config nor a previous preflight observation can expand or prove this dispatch's access.
    """
    plan = brief.get("runtime_access")
    if plan is None:
        return {"status": "legacy-inherit"}
    if not isinstance(plan, dict):
        raise AccessError(
            "invalid pinned access plan",
            remedy="create a new dispatch with a valid access plan",
        )
    verification, observation = verify_plan(plan, transport)
    if verification["status"] == "legacy-inherit":
        return verification
    if verification["status"] != "verified" or observation is None:
        raise AccessError(str(verification["reason"]), remedy=_REMEDY)
    provider = extensions.runtime_access(plan["runtime_extension"])
    launch = getattr(provider, "handoff", None) if handoff else None
    if handoff and not callable(launch):
        raise AccessError(
            "native access provider cannot bind access to the actual worker handoff",
            remedy=_REMEDY,
        )
    # A project-supplied provider may fail in any way; the send must fail closed.
    try:
        applied = provider.apply(brief, observation)
    except Exception as exc:
        raise AccessError(
            "native runtime could not apply the approved worker access", remedy=_REMEDY
        ) from exc
    reason = _observation_problem(plan, transport, applied)
    if (
        reason
        or applied is None
        or not applied.applied
        or applied.environment_id != observation.environment_id
        or applied.launch_id != observation.launch_id
    ):
        raise AccessError(
            reason
            or "native access application or matching inheritance was not confirmed",
            remedy=_REMEDY,
        )
    # Set only for a handoff; the gate above refuses a provider without a callable handoff.
    if callable(launch):
        # A project-supplied launch may fail in any way; the handoff must fail closed.
        try:
            receipt = launch(brief, applied, command)
        except Exception as exc:
            raise AccessError("native worker handoff failed", remedy=_REMEDY) from exc
        reason = _observation_problem(plan, transport, receipt)
        if (
            reason
            or receipt is None
            or not receipt.applied
            or not receipt.handed_off
            or receipt.dispatch_id != brief["dispatch_id"]
            or receipt.launch_id != applied.launch_id
            or receipt.environment_id != applied.environment_id
        ):
            raise AccessError(
                reason
                or "native worker handoff receipt does not bind the approved dispatch and launch",
                remedy=_REMEDY,
            )
        applied = receipt
    return {
        "status": "applied",
        "dispatch_id": brief.get("dispatch_id"),
        "plan_digest": plan["plan_digest"],
        "evidence": asdict(applied),
    }
