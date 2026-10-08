"""Access of the operations the coordinator executes itself: QA, Git and publish.

A worker's plan is proven by the native runtime that launches it. These operations run in the
coordinator process, so the plan is selected here and checked against that process before the
operation changes anything.
"""

from __future__ import annotations

import errno
import os
import re
import subprocess
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .core import git_utils
from .core.constants import ACCESS_OPERATIONS
from .core.utils import CoordinatorError, JsonObject
from .runtime_access import (
    AccessError,
    resolve_plan,
    validate_binding,
    validate_plan,
    verify_plan,
)

REMOTE_PROBE_SECONDS = 30


class OperationAccessError(AccessError, CoordinatorError):
    """An operation was stopped before it changed anything; ``evidence`` says why, structured."""

    def __init__(self, message: str, *, remedy: str, evidence: JsonObject) -> None:
        super().__init__(message, remedy=remedy)
        self.evidence = evidence


def select_plan(
    repo: Path,
    config: Mapping[str, object],
    operation: str,
    *,
    brief: Mapping[str, object] | None = None,
    worktree: Path | None = None,
) -> JsonObject:
    """The plan the operation runs under.

    An operation with a dispatch uses the plan pinned in its approved brief, never the live
    config: later edits cannot widen it. A brief without a plan is historical and keeps
    ``legacy-inherit``. An operation without a dispatch resolves the live config's defaults and its
    own override, without any role override.
    """
    if operation not in ACCESS_OPERATIONS:
        raise AccessError(
            f"unknown coordinator operation {operation!r}",
            remedy=f"select one of: {', '.join(ACCESS_OPERATIONS)}",
        )
    if brief is None:
        return resolve_plan(
            repo,
            worktree or repo,
            config,
            None,
            "write" if operation == "git" else "read-only",
            operation=operation,
        )
    if "runtime_access" not in brief:
        return resolve_plan(repo, repo, {}, None, "read-only")
    validate_binding(brief)
    plan = brief["runtime_access"]
    assert isinstance(plan, dict)
    return plan


@dataclass(frozen=True)
class _Need:
    """One thing the operation does to the environment, tied to the plan requirement allowing it."""

    resource: str
    access: str
    label: str
    root: Path  # the plan requirement that must allow it
    path: Path  # where the probe runs; inside ``root``
    create: bool = False


def is_legacy_inherit(plan: Mapping[str, object]) -> bool:
    """A plan that came from a project or brief without authored access: nothing to verify."""
    sources = plan["sources"]
    assert isinstance(sources, dict)
    return all(source == "legacy" for source in sources.values())


def _requirement_root(plan: JsonObject, resource: str) -> Path:
    return Path(
        next(i["path"] for i in plan["requirements"] if i["resource"] == resource)
    )


def _needs(operation: str, plan: JsonObject, checkout: Path | None) -> list[_Need]:
    git_common = _requirement_root(plan, "git_common")
    storage = _requirement_root(plan, "shared_storage")
    needs = [
        _Need("git_common", "write", "shared Git metadata", git_common, git_common),
        _Need("shared_storage", "write", "shared storage", storage, storage),
    ]
    if checkout is not None and operation == "qa":
        needs.append(
            _Need(
                "shared_storage",
                "write",
                "clean-room checkout",
                storage,
                checkout,
                True,
            )
        )
    elif checkout is not None:
        # The plan's requirement paths are resolved; a symlinked or relative worktree must compare equal.
        resolved = checkout.resolve()
        needs.append(_Need("checkout", "write", "checkout", resolved, resolved))
    for item in plan["filesystem"]:
        if item["resource"] == "cache":
            path = Path(item["path"])
            needs.append(
                _Need(
                    "cache",
                    item["access"],
                    "cache",
                    path,
                    path,
                    item["access"] == "write",
                )
            )
    return needs


def _allowed(plan: JsonObject, need: _Need) -> bool:
    return any(
        item["resource"] == need.resource
        and Path(item["path"]) == need.root
        and (item["access"] == "write" or need.access == "read")
        for item in plan["requirements"]
    )


def _probe(need: _Need) -> tuple[str, str]:
    """Really exercise the access the operation needs: ``(state, reason)``.

    A write creates and removes one uniquely named file; a refusal by the environment is a
    confirmed denial, anything else leaves the requirement unverified.
    """
    path = need.path
    if not path.is_dir():
        if not need.create:
            return "unverified", f"{path} does not exist"
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return _refusal(exc)
    if need.access == "read":
        return (
            ("verified", "readable")
            if os.access(path, os.R_OK | os.X_OK)
            else ("denied", "read access is refused")
        )
    probe = path / f".access-probe-{uuid.uuid4().hex}"
    try:
        os.close(os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        probe.unlink()
    except OSError as exc:
        return _refusal(exc)
    return "verified", "writable"


def _refusal(exc: OSError) -> tuple[str, str]:
    refused = exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS)
    return ("denied" if refused else "unverified"), os.strerror(exc.errno or 0) or str(
        exc
    )


def _check(
    plan: JsonObject, need: _Need, operation: str, *, dispatch_bound: bool
) -> JsonObject:
    entry: JsonObject = {
        "requirement": need.resource,
        "label": need.label,
        "access": need.access,
        "path": str(need.path),
    }
    if not _allowed(plan, need):
        entry.update(
            state="denied",
            source="plan",
            reason=f"the approved access plan does not allow {need.access} on {need.resource}",
            remedy=(
                f"allow {need.access} on {need.resource} for the {operation} operation in "
                "access_policy"
                + (
                    "; its dispatch pinned the plan, so a newly proposed and approved dispatch is needed"
                    if dispatch_bound
                    else ""
                )
            ),
        )
        return entry
    state, reason = _probe(need)
    entry.update(state=state, source="probe", reason=reason)
    if state == "denied":
        entry["remedy"] = (
            f"grant the coordinator process {need.access} access to {need.path} "
            f"({need.label}) in its sandbox or filesystem permissions"
        )
    elif state == "unverified":
        entry["remedy"] = (
            f"make {need.path} ({need.label}) exist and be reachable for the coordinator process"
        )
    return entry


def _mode_check(operation: str, plan: JsonObject) -> JsonObject:
    mode = plan["mode"]
    entry: JsonObject = {
        "requirement": "mode",
        "label": "mode",
        "access": mode,
        "path": "",
    }
    if mode == "inherit":
        entry.update(
            state="verified",
            source="inherit",
            reason="the coordinator runs in its own inherited environment",
        )
        return entry
    summary, _ = verify_plan(plan, "in-process")
    if summary["status"] == "verified":
        entry.update(
            state="verified", source="native-runtime", reason="confirmed natively"
        )
        return entry
    entry.update(
        state="unsupported",
        source="native-runtime",
        reason=f"mode {mode} is not provable for the coordinator: {summary.get('reason')}",
        remedy=(
            f"run the coordinator in a native runtime whose runtime_access extension confirms mode "
            f"{mode}, or set mode to inherit for the {operation} operation in access_policy"
        ),
    )
    return entry


def _remote_host(url: str) -> str | None:
    """The network host of a remote URL, or ``None`` for a local path or ``file://`` URL."""
    scheme = re.match(
        r"^([a-z][a-z0-9+.-]*)://(?:[^@/]*@)?(\[[^\]]+\]|[^/:]+)", url, re.I
    )
    if scheme:
        return None if scheme.group(1).lower() == "file" else scheme.group(2).lower()
    scp = re.match(r"^(?:[^@/\s]+@)?([^:/\s]{2,}):(?!//)", url)
    return scp.group(1).lower() if scp else None


def _remote_check(
    operation: str, plan: JsonObject, repo: Path, remote: str
) -> JsonObject:
    entry: JsonObject = {
        "requirement": "remote",
        "label": f"remote {remote}",
        "access": "read",
        "path": remote,
    }
    located = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", "--", remote],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if located.returncode:
        entry.update(
            state="unverified",
            source="probe",
            reason=f"remote {remote!r} is not configured",
            remedy=f"configure the remote {remote!r} with 'git remote add' and repeat the command",
        )
        return entry
    host = _remote_host(located.stdout.strip())
    restricted = plan["mode"] == "sandbox" or bool(plan["network"]["hosts"])
    if host is not None and restricted and host not in plan["network"]["hosts"]:
        entry.update(
            state="denied",
            source="plan",
            reason=f"host {host} is not in the approved network hosts",
            remedy=f"add {host} to access_policy network.hosts for the {operation} operation",
        )
        return entry
    try:
        probed = subprocess.run(
            ["git", "-C", str(repo), "ls-remote", "--heads", "--", remote, "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=REMOTE_PROBE_SECONDS,
            env=git_utils.git_environment(),
        )
    except subprocess.TimeoutExpired:
        entry.update(
            state="unverified",
            source="probe",
            reason=f"remote did not answer within {REMOTE_PROBE_SECONDS} seconds",
            remedy=f"restore connectivity to remote {remote!r} and repeat the command",
        )
        return entry
    if probed.returncode == 0:
        entry.update(state="verified", source="probe", reason="reachable")
        return entry
    detail = (probed.stderr or probed.stdout).strip()
    category = git_utils.classify_git_failure(detail)
    entry.update(
        state="denied" if category == git_utils.REMOTE_DENIED else "unverified",
        source="probe",
        reason=detail.splitlines()[-1] if detail else "ls-remote failed",
        remedy=(
            f"grant the credentials this process uses access to remote {remote!r}"
            if category == git_utils.REMOTE_DENIED
            else f"restore connectivity to remote {remote!r} and repeat the command"
        ),
    )
    return entry


def verify(
    operation: str,
    plan: JsonObject,
    *,
    repo: Path,
    checkout: Path | None = None,
    remote: str | None = None,
    dispatch_bound: bool = False,
) -> JsonObject:
    """Check the plan against the coordinator process before the operation changes anything.

    Returns structured evidence. A mode the coordinator cannot prove, a denial the environment
    confirms and a requirement that cannot be verified each raise ``OperationAccessError`` with
    that evidence and a concrete remedy. ``checkout`` is the directory in which the operation
    creates or changes a checkout; ``remote`` the Git remote it reads or pushes.
    """
    if operation not in ACCESS_OPERATIONS:
        raise AccessError(
            f"unknown coordinator operation {operation!r}",
            remedy=f"select one of: {', '.join(ACCESS_OPERATIONS)}",
        )
    validate_plan(plan)
    evidence: JsonObject = {
        "operation": operation,
        "plan_digest": plan["plan_digest"],
        "mode": plan["mode"],
        "sources": plan["sources"],
    }
    if is_legacy_inherit(plan):
        return {**evidence, "status": "legacy-inherit", "checks": []}
    checks = [_mode_check(operation, plan)]
    checks += [
        _check(plan, need, operation, dispatch_bound=dispatch_bound)
        for need in _needs(operation, plan, checkout)
    ]
    if remote is not None and operation in ("git", "publish"):
        checks.append(_remote_check(operation, plan, repo, remote))
    failed = [check for check in checks if check["state"] != "verified"]
    states = {check["state"] for check in failed}
    status = next(
        (name for name in ("unsupported", "denied", "unverified") if name in states),
        "verified",
    )
    evidence = {**evidence, "status": status, "checks": checks}
    if not failed:
        return evidence
    raise OperationAccessError(
        f"{operation} access is {status}: "
        + "; ".join(
            f"{check['label']} ({check['access']} {check['path']}): {check['reason']}"
            for check in failed
        ),
        remedy="; ".join(dict.fromkeys(str(check["remedy"]) for check in failed))
        + "; then repeat the command. No lifecycle state was changed",
        evidence=evidence,
    )


def require(
    repo: Path,
    config: Mapping[str, object],
    operation: str,
    *,
    brief: Mapping[str, object] | None = None,
    worktree: Path | None = None,
    checkout: Path | None = None,
    remote: str | None = None,
) -> JsonObject:
    """Select the operation's plan and verify it: the one call a coordinator operation makes."""
    plan = select_plan(repo, config, operation, brief=brief, worktree=worktree)
    return verify(
        operation,
        plan,
        repo=repo,
        checkout=checkout,
        remote=remote,
        dispatch_bound=brief is not None,
    )
