"""Deterministic, side-effect-free preparation for a coordinator handoff.

The coordinator calls this module before it writes an immutable brief.  It deliberately performs
no ledger mutation and starts no transport, so an approval always applies to the same resolved
runtime, worktree and snapshot that the resulting dispatch will use.
"""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from contract import ContractError, resolve_runtime_name


class PreflightError(Exception):
    """A dispatch cannot safely be prepared from the supplied project state."""


@dataclass(frozen=True)
class PreparedDispatch:
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
    context_package: dict[str, Any]
    preview_brief: dict[str, Any]
    decision_packet: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PreflightError(f"project_state requires a non-empty {label}")
    return value.strip()


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise PreflightError(f"git {' '.join(args)} failed: {detail or 'unknown error'}")
    return result.stdout.strip()


def _worktree_paths(repo: Path) -> set[Path]:
    output = _git(repo, "worktree", "list", "--porcelain")
    paths: set[Path] = set()
    for line in output.splitlines():
        if line.startswith("worktree "):
            paths.add(Path(line.removeprefix("worktree ")).resolve())
    return paths


def _role_context(role: str, state: Mapping[str, Any], snapshot: str) -> dict[str, Any]:
    """Compact, role-specific context pointer metadata; package contents remain ledger-owned."""
    keys = {
        "architect": ("contracts", "neighbour_tickets", "starting_files"),
        "developer": ("architecture_decision", "affected_symbols", "related_tests", "starting_files"),
        "code-review": ("pinned_diff", "prior_findings", "verification_commands", "starting_files"),
    }.get(role, ("starting_files",))
    included: dict[str, Any] = {"snapshot_sha": snapshot, "role": role}
    for key in keys:
        value = state.get(key)
        if value not in (None, [], ""):
            included[key] = value
    return included


def prepare(ticket: str, role: str, project_state: Mapping[str, Any]) -> PreparedDispatch:
    """Resolve and validate one potential dispatch without creating it.

    ``project_state`` is intentionally plain data so a CLI, adapter, or test can call the same
    deterministic function. Required fields are ``repo``, ``config``, ``branch``, ``worktree``,
    ``zone`` and ``base_sha``; ``candidate_sha`` is required only when the caller pins a candidate.
    """
    ticket = _text(ticket, "ticket")
    role = _text(role, "role")
    repo = Path(_text(project_state.get("repo"), "repo")).resolve()
    if not repo.is_dir():
        raise PreflightError("project_state repo does not exist")
    config = project_state.get("config")
    if not isinstance(config, dict):
        raise PreflightError("project_state config must be an object")
    plans = config.get("assignment_plans")
    if not isinstance(plans, dict) or not isinstance(plans.get(role), dict):
        raise PreflightError(f"project_state has no assignment plan for role {role!r}")
    plan = plans[role]
    try:
        runtime = resolve_runtime_name(plan, project_state.get("runtime"))
    except ContractError as exc:
        raise PreflightError(str(exc)) from exc

    branch = _text(project_state.get("branch"), "branch")
    worktree = Path(_text(project_state.get("worktree"), "worktree")).resolve()
    base_sha = _text(project_state.get("base_sha"), "base_sha")
    candidate = project_state.get("candidate_sha")
    if candidate is not None:
        candidate = _text(candidate, "candidate_sha")
    integration_ref = _text(project_state.get("integration_ref") or "base", "integration_ref")
    if worktree not in _worktree_paths(repo):
        raise PreflightError("worktree is not registered by git worktree")
    if _git(worktree, "rev-parse", "--is-inside-work-tree") != "true":
        raise PreflightError("worktree is not a Git worktree")
    worktree_sha = _git(worktree, "rev-parse", "--verify", "HEAD^{commit}")
    expected_sha = candidate or _text(project_state.get("snapshot_sha") or base_sha, "snapshot_sha")
    if worktree_sha != expected_sha:
        raise PreflightError(
            f"worktree is pinned to {worktree_sha}, expected snapshot {expected_sha}"
        )
    if role in {"architect", "developer"} and _git(worktree, "branch", "--show-current") != branch:
        raise PreflightError("write/planning worktree is not on the resolved issue branch")

    checks = project_state.get("mandatory_checks", [])
    if not isinstance(checks, list) or not all(isinstance(item, str) and item.strip() for item in checks):
        raise PreflightError("project_state mandatory_checks must be a list of commands")
    package = _role_context(role, project_state, expected_sha)
    preview = {
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
        "verification_commands": checks,
    }
    packet = {
        "action": f"create and send {role} dispatch",
        "branch": branch,
        "worktree": str(worktree),
        "runtime": runtime,
        "base_sha": base_sha,
        "snapshot_sha": expected_sha,
        "candidate_sha": candidate,
        "checks": checks,
        "context_package": package,
        "approval_reason": "the immutable brief will bind this exact runtime, worktree and snapshot",
        "options": ["accept", "retry", "block", "full review", "delta-review"],
    }
    return PreparedDispatch(
        ticket=ticket, role=role, integration_ref=integration_ref, base_sha=base_sha,
        candidate_sha=candidate, issue_branch=branch, worktree=str(worktree),
        worktree_sha=worktree_sha, runtime=runtime, mandatory_checks=list(checks),
        context_package=package, preview_brief=preview, decision_packet=packet,
    )
