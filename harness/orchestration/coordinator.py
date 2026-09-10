#!/usr/bin/env python3
"""Runtime-neutral coordinator for the backend-orchestration capability.

The coordinator owns batch, approval, dispatch and completion-report state.  A runtime adapter
is an explicitly selected transport: it receives a dispatch only after this CLI has persisted the
approved immutable brief.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Iterator, Optional


STATE_REL = Path(".harness/orchestration/state")
SENSITIVE_KEY = re.compile(r"(?:api[_-]?key|credential|password|secret|token)", re.IGNORECASE)
ROLE_MODES = {"write", "read-only"}
REPORT_OUTCOMES = {"completed", "blocked", "failed"}
DECISIONS = {"accept", "override-warning", "retry", "block"}
PLAN_FIELDS = (
    "batch_id", "created_at", "ticket", "branch", "worktree", "zone", "definition_of_done",
    "prohibited_changes", "verification_commands", "required_gates", "dependencies",
)
DISPATCH_FIELDS = {
    "dispatch_id", "batch_id", "ticket", "role", "access", "zone", "write_paths", "branch", "worktree",
    "definition_of_done", "prohibited_changes", "verification_commands", "required_gates", "dependencies",
    "resolved_provider_profile", "resolved_model", "coordinator_approval", "state", "created_at",
}
REPORT_FIELDS = {
    "dispatch_id",
    "ticket",
    "role",
    "outcome",
    "output",
    "commit_sha",
    "changed_files",
    "checks_run",
    "risks",
    "blockers",
    "next_coordinator_action",
}


class CoordinatorError(Exception):
    """A request that must fail without advancing coordinator state."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _non_empty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoordinatorError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise CoordinatorError(f"{label} must be a JSON object")
    return value


def _reject_sensitive(value: Any, location: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise CoordinatorError(f"{location} contains a non-string key")
            if SENSITIVE_KEY.search(key):
                raise CoordinatorError(f"{location} contains secret-shaped field {key!r}")
            _reject_sensitive(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive(child, f"{location}[{index}]")


def _strings(value: object, label: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value) or not all(_non_empty(item) for item in value):
        raise CoordinatorError(f"{label} must be a list of non-empty strings")
    return list(value)


def _canonical(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _write_exclusive(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(_canonical(value))
    except FileExistsError as exc:
        raise CoordinatorError(f"refusing to overwrite immutable record: {path.name}") from exc


def _replace(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(_canonical(value), encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _safe_id(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"(?:batch|dispatch)-[0-9a-f-]+", value) is None:
        raise CoordinatorError(f"{label} is not a valid coordinator ID")
    return value


def _repo(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "repo", ".")).resolve()


def _state_root(args: argparse.Namespace, repo: Path) -> Path:
    supplied = getattr(args, "state_dir", None)
    return (Path(supplied).resolve() if supplied else repo / STATE_REL).resolve()


@contextmanager
def _state_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".coordinator.lock"
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise CoordinatorError("another coordinator operation is in progress") from exc
    try:
        yield
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass


def _project(repo: Path) -> dict[str, Any]:
    return _read_object(repo / ".harness/project.json", "project config")


def _config(repo: Path) -> dict[str, Any]:
    value = _read_object(repo / ".harness/orchestration.json", "project orchestration config")
    _reject_sensitive(value, "project orchestration config")
    return value


def _role(repo: Path, name: str) -> dict[str, Any]:
    path = repo / ".harness/orchestration/roles" / f"{name}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CoordinatorError(f"role manifest {name!r} cannot be read") from exc
    match = re.match(r"\A---\r?\n(?P<body>.*?)\r?\n---(?:\r?\n|$)", text, re.DOTALL)
    if not match:
        raise CoordinatorError(f"role manifest {name!r} has no valid frontmatter")
    metadata: dict[str, Any] = {}
    current: Optional[str] = None
    for line in match.group("body").splitlines():
        if not line.strip():
            continue
        item = re.fullmatch(r"\s+-\s+(.+?)\s*", line)
        if item:
            if current is None:
                raise CoordinatorError(f"role manifest {name!r} has an orphan list item")
            metadata.setdefault(current, []).append(item.group(1))
            continue
        field = re.fullmatch(r"([a-z_]+):\s*(.*?)\s*", line)
        if not field:
            raise CoordinatorError(f"role manifest {name!r} has invalid frontmatter")
        key, value = field.groups()
        current = key if not value else None
        metadata[key] = [] if not value else value
    if metadata.get("name") != name or metadata.get("mode") not in ROLE_MODES:
        raise CoordinatorError(f"role manifest {name!r} has invalid name or mode")
    if not isinstance(metadata.get("required_capabilities"), list):
        raise CoordinatorError(f"role manifest {name!r} has no required capabilities")
    return metadata


def _validate_branch(repo: Path, branch: str) -> None:
    if not _non_empty(branch):
        raise CoordinatorError("branch must be a non-empty string")
    project = _project(repo)
    pattern = project.get("branch_pattern", r"^feature/issue-[0-9]+-.+")
    base = project.get("base_branch", "master")
    if not isinstance(pattern, str):
        raise CoordinatorError("project config has an invalid branch_pattern")
    try:
        matches = re.fullmatch(pattern, branch) is not None
    except re.error as exc:
        raise CoordinatorError("project config has an invalid branch_pattern") from exc
    if branch.startswith("integration/") or branch == base or not matches:
        raise CoordinatorError("branch must be an issue branch, never a protected or integration branch")


def _verification_commands(config: dict[str, Any]) -> list[str]:
    commands = config.get("verification_commands")
    return _strings(commands, "verification_commands", allow_empty=True)


def _resolve_assignment(
    repo: Path, config: dict[str, Any], role_name: str, zone_name: str
) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    role = _role(repo, role_name)
    assignments = config.get("assignment_plans")
    zones = config.get("backend_zones")
    profiles = config.get("provider_profiles")
    if not isinstance(assignments, dict) or not isinstance(zones, dict) or not isinstance(profiles, dict):
        raise CoordinatorError("project orchestration config has invalid assignments, zones or profiles")
    plan = assignments.get(role_name)
    if not isinstance(plan, dict) or plan.get("zone") != zone_name:
        raise CoordinatorError(f"role {role_name!r} is not assigned to zone {zone_name!r}")
    zone = zones.get(zone_name)
    paths = zone.get("paths") if isinstance(zone, dict) else None
    if not isinstance(paths, list) or not paths or not all(_non_empty(item) for item in paths):
        raise CoordinatorError(f"backend zone {zone_name!r} is invalid")
    profile_ids = plan.get("profiles")
    if not isinstance(profile_ids, list) or not profile_ids or not _non_empty(profile_ids[0]):
        raise CoordinatorError(f"role {role_name!r} has no provider profile")
    profile_id = profile_ids[0]
    profile = profiles.get(profile_id)
    required = set(role.get("required_capabilities", []))
    capabilities = profile.get("capabilities") if isinstance(profile, dict) else None
    if not isinstance(profile, dict) or not _non_empty(profile.get("default_model")):
        raise CoordinatorError(f"provider profile {profile_id!r} is invalid")
    if not isinstance(capabilities, list) or not required.intersection(capabilities):
        raise CoordinatorError(f"provider profile {profile_id!r} is incompatible with role {role_name!r}")
    return role, zone, profile_id, profile["default_model"]


def _batch_path(root: Path, batch_id: str) -> Path:
    return root / "batches" / f"{_safe_id(batch_id, 'batch')}.json"


def _dispatch_path(root: Path, dispatch_id: str) -> Path:
    return root / "dispatches" / f"{_safe_id(dispatch_id, 'dispatch')}.json"


def _dispatch_status_path(root: Path, dispatch_id: str) -> Path:
    return root / "dispatch-status" / f"{_safe_id(dispatch_id, 'dispatch')}.json"


def _plan_path(root: Path, batch_id: str) -> Path:
    return root / "plans" / f"{_safe_id(batch_id, 'batch')}.json"


def _load_batch(root: Path, batch_id: str) -> dict[str, Any]:
    return _read_object(_batch_path(root, batch_id), "batch record")


def _load_dispatch(root: Path, dispatch_id: str) -> dict[str, Any]:
    return _read_object(_dispatch_path(root, dispatch_id), "dispatch record")


def _load_dispatch_status(root: Path, dispatch_id: str) -> dict[str, Any]:
    return _read_object(_dispatch_status_path(root, dispatch_id), "dispatch status")


def _validate_batch_integrity(root: Path, batch: dict[str, Any]) -> None:
    plan = _read_object(_plan_path(root, batch.get("batch_id")), "immutable batch plan")
    if any(field not in batch for field in PLAN_FIELDS) or any(field not in plan for field in PLAN_FIELDS):
        raise CoordinatorError("batch record is incomplete")
    if {field: batch[field] for field in PLAN_FIELDS} != {field: plan[field] for field in PLAN_FIELDS}:
        raise CoordinatorError("batch record does not match its immutable plan")


def _validate_dispatch(repo: Path, config: dict[str, Any], root: Path, batch: dict[str, Any], dispatch: dict[str, Any]) -> None:
    _reject_sensitive(dispatch, "dispatch record")
    if set(dispatch) != DISPATCH_FIELDS:
        raise CoordinatorError("dispatch record schema mismatch")
    if dispatch.get("state") != "approved":
        raise CoordinatorError("dispatch record is not an approved immutable brief")
    if dispatch.get("batch_id") != batch.get("batch_id"):
        raise CoordinatorError("dispatch record does not belong to its batch")
    entry = next((item for item in batch.get("dispatches", []) if item.get("dispatch_id") == dispatch.get("dispatch_id")), None)
    if not entry or entry.get("brief_sha256") != hashlib.sha256(_canonical(dispatch).encode("utf-8")).hexdigest():
        raise CoordinatorError("dispatch record failed immutable brief integrity check")
    for field in ("ticket", "branch", "worktree", "zone", "definition_of_done", "prohibited_changes", "verification_commands", "required_gates", "dependencies"):
        if dispatch[field] != batch[field]:
            raise CoordinatorError(f"dispatch record {field} does not match its batch")
    _validate_branch(repo, dispatch["branch"])
    role, zone, profile_id, model = _resolve_assignment(repo, config, dispatch["role"], batch["zone"])
    if dispatch["access"] != role["mode"] or dispatch["resolved_provider_profile"] != profile_id or dispatch["resolved_model"] != model:
        raise CoordinatorError("dispatch record does not match the role assignment")
    expected_paths = zone["paths"] if role["mode"] == "write" else []
    if dispatch["write_paths"] != expected_paths:
        raise CoordinatorError("dispatch record write paths do not match the role boundary")
    approval = dispatch.get("coordinator_approval")
    if not isinstance(approval, dict) or set(approval) != {"approved_by", "approved_at"} or not all(_non_empty(value) for value in approval.values()):
        raise CoordinatorError("dispatch record has invalid coordinator approval")


def _check_batch_conflicts(root: Path, config: dict[str, Any], batch: dict[str, Any]) -> None:
    budget = config.get("concurrency_budget")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise CoordinatorError("project orchestration config has an invalid concurrency_budget")
    active = 0
    for path in sorted((root / "batches").glob("batch-*.json")):
        other = _read_object(path, "batch record")
        if other.get("batch_id") == batch.get("batch_id") or other.get("state") not in {"active", "awaiting-approval"}:
            continue
        active += 1
        if other.get("zone") == batch.get("zone"):
            raise CoordinatorError("another active batch already owns this backend zone")
    if active >= budget:
        raise CoordinatorError("concurrency_budget is exhausted")


def _approval(args: argparse.Namespace) -> dict[str, str]:
    approved_by = getattr(args, "approved_by", None)
    approved_at = getattr(args, "approved_at", None)
    if not _non_empty(approved_by) or not _non_empty(approved_at):
        raise CoordinatorError("explicit coordinator approval requires approved-by and approved-at")
    result = {"approved_by": approved_by.strip(), "approved_at": approved_at.strip()}
    _reject_sensitive(result, "coordinator approval")
    return result


def create_batch(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    config = _config(repo)
    ticket = getattr(args, "ticket", None)
    branch = getattr(args, "branch", None)
    worktree = getattr(args, "worktree", None)
    zone = getattr(args, "zone", None)
    dod = _strings(getattr(args, "definition_of_done", None), "definition_of_done")
    prohibited = _strings(getattr(args, "prohibited_change", None), "prohibited_changes")
    if not _non_empty(ticket) or not _non_empty(worktree) or not _non_empty(zone):
        raise CoordinatorError("ticket, worktree and zone must be non-empty strings")
    _validate_branch(repo, branch)
    if not isinstance(config.get("backend_zones"), dict) or zone not in config["backend_zones"]:
        raise CoordinatorError(f"unknown backend zone {zone!r}")
    record = {
        "batch_id": f"batch-{uuid.uuid4()}",
        "created_at": _now(),
        "state": "planned",
        "ticket": ticket.strip(),
        "branch": branch.strip(),
        "worktree": worktree.strip(),
        "zone": zone.strip(),
        "definition_of_done": dod,
        "prohibited_changes": prohibited,
        "verification_commands": _verification_commands(config),
        "required_gates": _strings(getattr(args, "required_gate", None) or ["none"], "required_gates"),
        "dependencies": _strings(getattr(args, "dependency", None) or ["none"], "dependencies"),
        "dispatches": [],
    }
    _reject_sensitive(record, "batch")
    root = _state_root(args, repo)
    with _state_lock(root):
        _write_exclusive(_plan_path(root, record["batch_id"]), {field: record[field] for field in PLAN_FIELDS})
        _write_exclusive(_batch_path(root, record["batch_id"]), record)
    return record


def approve_batch(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    root = _state_root(args, repo)
    with _state_lock(root):
        record = _load_batch(root, args.batch)
        _validate_batch_integrity(root, record)
        if record.get("state") != "planned":
            raise CoordinatorError("only a planned batch can receive its planning approval")
        record["coordinator_approval"] = _approval(args)
        record["state"] = "awaiting-approval"
        _replace(_batch_path(root, record["batch_id"]), record)
    return record


def decide_batch(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    root = _state_root(args, repo)
    with _state_lock(root):
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        pending = [item for item in batch.get("dispatches", []) if item.get("state") == "reported" and "decision" not in item]
        if len(pending) != 1:
            raise CoordinatorError("batch has no single completion report awaiting a coordinator decision")
        decision = {
            "decision": args.decision,
            "approved_by": _approval(args)["approved_by"],
            "approved_at": _approval(args)["approved_at"],
            "note": args.note.strip() if _non_empty(args.note) else "none",
        }
        pending[0]["decision"] = decision
        batch.setdefault("coordinator_decisions", []).append({"dispatch_id": pending[0]["dispatch_id"], **decision})
        batch["state"] = "blocked" if args.decision == "block" else "awaiting-approval"
        _replace(_batch_path(root, batch["batch_id"]), batch)
    return batch


def create_dispatch(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    root = _state_root(args, repo)
    config = _config(repo)
    with _state_lock(root):
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        if batch.get("state") != "awaiting-approval":
            raise CoordinatorError("a dispatch requires a batch awaiting explicit approval")
        pending_report = any(item.get("state") == "reported" and "decision" not in item for item in batch.get("dispatches", []))
        if pending_report:
            raise CoordinatorError("the previous completion report requires an explicit coordinator decision")
        _check_batch_conflicts(root, config, batch)
        role_name = args.role
        role, zone, profile_id, model = _resolve_assignment(repo, config, role_name, batch["zone"])
        approval = _approval(args)
        dispatch_id = f"dispatch-{uuid.uuid4()}"
        brief: dict[str, Any] = {
            "dispatch_id": dispatch_id,
            "batch_id": batch["batch_id"],
            "ticket": batch["ticket"],
            "role": role_name,
            "access": role["mode"],
            "zone": batch["zone"],
            "write_paths": zone["paths"] if role["mode"] == "write" else [],
            "branch": batch["branch"],
            "worktree": batch["worktree"],
            "definition_of_done": batch["definition_of_done"],
            "prohibited_changes": batch["prohibited_changes"],
            "verification_commands": batch["verification_commands"],
            "required_gates": batch["required_gates"],
            "dependencies": batch["dependencies"],
            "resolved_provider_profile": profile_id,
            "resolved_model": model,
            "coordinator_approval": approval,
        }
        _reject_sensitive(brief, "dispatch brief")
        # The immutable dispatch file is itself the approved brief.  Keeping the brief at the
        # top level lets any runtime-neutral adapter consume exactly the reviewed contract.
        dispatch = dict(brief)
        dispatch["state"] = "approved"
        dispatch["created_at"] = _now()
        _write_exclusive(_dispatch_path(root, dispatch_id), dispatch)
        _write_exclusive(_dispatch_status_path(root, dispatch_id), {"dispatch_id": dispatch_id, "state": "approved", "updated_at": _now()})
        batch["dispatches"].append({
            "dispatch_id": dispatch_id,
            "role": role_name,
            "state": "approved",
            "brief_sha256": hashlib.sha256(_canonical(dispatch).encode("utf-8")).hexdigest(),
        })
        batch["state"] = "active"
        _replace(_batch_path(root, batch["batch_id"]), batch)
    return {"dispatch_id": dispatch_id, "batch_id": batch["batch_id"], "state": "approved", "brief": brief}


def send_dispatch(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    root = _state_root(args, repo)
    adapter = Path(args.adapter).resolve()
    if not adapter.is_file():
        raise CoordinatorError(f"runtime adapter does not exist: {adapter}")
    with _state_lock(root):
        dispatch = _load_dispatch(root, args.dispatch)
        batch = _load_batch(root, dispatch["batch_id"])
        _validate_batch_integrity(root, batch)
        config = _config(repo)
        _validate_dispatch(repo, config, root, batch, dispatch)
        status = _load_dispatch_status(root, dispatch["dispatch_id"])
        if status.get("state") != "approved":
            raise CoordinatorError("only an approved dispatch may be sent to a runtime adapter")
        entry = next((item for item in batch.get("dispatches", []) if item["dispatch_id"] == dispatch["dispatch_id"]), None)
        if not entry or entry.get("state") != "approved":
            raise CoordinatorError("dispatch was already sent or is not registered in its batch")
        brief_path = _dispatch_path(root, dispatch["dispatch_id"])
        command = [str(adapter)] if adapter.suffix.lower() != ".py" else [sys.executable, str(adapter)]
        adapter_args = args.adapter_arg or []
        if any(
            argument in {"dispatch", "--repo", "--brief"}
            or argument.startswith("--repo=")
            or argument.startswith("--brief=")
            for argument in adapter_args
        ):
            raise CoordinatorError("adapter arguments cannot override dispatch, repo or brief")
        command.append("dispatch")
        command.extend(adapter_args)
        command.extend(["--repo", str(repo), "--brief", str(brief_path)])
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise CoordinatorError(f"runtime adapter rejected dispatch: {detail}")
        for entry in batch["dispatches"]:
            if entry["dispatch_id"] == dispatch["dispatch_id"]:
                entry["state"] = "dispatched"
                break
        else:
            raise CoordinatorError("dispatch is not registered in its batch")
        _replace(_dispatch_status_path(root, dispatch["dispatch_id"]), {"dispatch_id": dispatch["dispatch_id"], "state": "dispatched", "updated_at": _now()})
        _replace(_batch_path(root, batch["batch_id"]), batch)
    return {"dispatch_id": dispatch["dispatch_id"], "state": "dispatched"}


def _validate_report(report: dict[str, Any], dispatch: dict[str, Any], role: dict[str, Any]) -> None:
    _reject_sensitive(report, "completion report")
    if set(report) != REPORT_FIELDS:
        missing = sorted(REPORT_FIELDS - set(report))
        extra = sorted(set(report) - REPORT_FIELDS)
        raise CoordinatorError(f"completion report schema mismatch (missing={missing}, extra={extra})")
    brief = dispatch
    for field in ("dispatch_id", "ticket", "role"):
        if report[field] != brief[field]:
            raise CoordinatorError(f"completion report {field} does not match the approved dispatch")
    if report["outcome"] not in REPORT_OUTCOMES:
        raise CoordinatorError("completion report outcome is invalid")
    for field in ("output", "risks", "blockers", "next_coordinator_action"):
        if not _non_empty(report[field]):
            raise CoordinatorError(f"completion report {field} must be a non-empty string")
    changed_files = _strings(report["changed_files"], "completion report changed_files", allow_empty=True)
    checks = report["checks_run"]
    if not isinstance(checks, list) or not checks:
        raise CoordinatorError("completion report checks_run must be a non-empty list")
    for check in checks:
        if not isinstance(check, dict) or set(check) != {"command", "result", "evidence"}:
            raise CoordinatorError("completion report checks_run has an invalid entry")
        if not all(_non_empty(check[field]) for field in ("command", "result", "evidence")):
            raise CoordinatorError("completion report checks_run entries must contain text evidence")
    if [check["command"] for check in checks] != dispatch["verification_commands"]:
        raise CoordinatorError("completion report checks_run must exactly match approved verification commands")
    commit_sha = report["commit_sha"]
    if role["mode"] == "write" and (
        not isinstance(commit_sha, str)
        or re.fullmatch(r"[0-9a-fA-F]{7,64}", commit_sha) is None
        or not changed_files
    ):
        raise CoordinatorError("write-role completion reports require commit_sha and changed_files")
    if role["mode"] == "read-only" and changed_files:
        raise CoordinatorError("read-only completion reports cannot claim changed files")
    if role["mode"] == "write":
        paths = dispatch["write_paths"]
        for changed_file in changed_files:
            normalized = changed_file.replace("\\", "/")
            if normalized.startswith("/") or ".." in Path(normalized).parts or not any(
                fnmatchcase(normalized, pattern) for pattern in paths
            ):
                raise CoordinatorError("completion report changed_files must remain inside the approved zone")
    if role["mode"] == "read-only" and commit_sha != "not applicable — read-only role":
        raise CoordinatorError("read-only completion reports must not claim a commit SHA")


def _report_markdown(report: dict[str, Any]) -> str:
    lines = [f"# Completion report: {report['dispatch_id']}", ""]
    lines.extend(
        [
            f"- Outcome: {report['outcome']}",
            f"- Ticket: {report['ticket']}",
            f"- Role: {report['role']}",
            f"- Output: {report['output']}",
            f"- Commit SHA: {report['commit_sha']}",
            f"- Changed files: {', '.join(report['changed_files']) or 'none'}",
            "- Checks run:",
        ]
    )
    for check in report["checks_run"]:
        lines.append(f"  - `{check['command']}` — {check['result']}, {check['evidence']}")
    lines.extend(
        [
            f"- Risks: {report['risks']}",
            f"- Blockers: {report['blockers']}",
            f"- Next coordinator action: {report['next_coordinator_action']}",
            "",
        ]
    )
    return "\n".join(lines)


def submit_report(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    try:
        report = _read_object(Path(args.file).resolve(), "completion report")
    except CoordinatorError:
        raise
    root = _state_root(args, repo)
    with _state_lock(root):
        dispatch = _load_dispatch(root, report.get("dispatch_id"))
        batch = _load_batch(root, dispatch["batch_id"])
        _validate_batch_integrity(root, batch)
        config = _config(repo)
        _validate_dispatch(repo, config, root, batch, dispatch)
        status = _load_dispatch_status(root, dispatch["dispatch_id"])
        entry = next((item for item in batch.get("dispatches", []) if item["dispatch_id"] == dispatch["dispatch_id"]), None)
        if not entry or entry.get("state") != "dispatched" or status.get("state") != "dispatched":
            raise CoordinatorError("completion report requires a dispatched role")
        role = _role(repo, dispatch["role"])
        _validate_report(report, dispatch, role)
        report_json = root / "reports" / f"{dispatch['dispatch_id']}.json"
        report_md = root / "reports" / f"{dispatch['dispatch_id']}.md"
        if report_json.exists() or report_md.exists():
            raise CoordinatorError("refusing to overwrite immutable completion report")
        _write_exclusive(report_json, report)
        report_md.parent.mkdir(parents=True, exist_ok=True)
        try:
            with report_md.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(_report_markdown(report))
        except FileExistsError as exc:
            raise CoordinatorError("refusing to overwrite immutable Markdown report") from exc
        for entry in batch["dispatches"]:
            if entry["dispatch_id"] == dispatch["dispatch_id"]:
                entry["state"] = "reported"
                entry["report"] = f"reports/{dispatch['dispatch_id']}.json"
                break
        else:
            raise CoordinatorError("dispatch is not registered in its batch")
        batch["state"] = "awaiting-approval"
        _replace(_dispatch_status_path(root, dispatch["dispatch_id"]), {"dispatch_id": dispatch["dispatch_id"], "state": "reported", "updated_at": _now()})
        _replace(_batch_path(root, batch["batch_id"]), batch)
    return {"dispatch_id": dispatch["dispatch_id"], "state": "reported", "report": str(report_json)}


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default=argparse.SUPPRESS, help="target project root")
    parser.add_argument("--state-dir", default=argparse.SUPPRESS, help="coordinator state directory")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Coordinate approved backend role dispatches.")
    root.add_argument("--repo", default=".", help="target project root")
    root.add_argument("--state-dir", help="coordinator state directory")
    commands = root.add_subparsers(dest="command", required=True)

    batch = commands.add_parser("batch")
    batch_commands = batch.add_subparsers(dest="batch_command", required=True)
    create = batch_commands.add_parser("create", aliases=["plan"])
    _common(create)
    create.add_argument("--ticket", required=True)
    create.add_argument("--branch", required=True)
    create.add_argument("--worktree", required=True)
    create.add_argument("--zone", required=True)
    create.add_argument("--definition-of-done", action="append", required=True)
    create.add_argument("--prohibited-change", action="append", required=True)
    create.add_argument("--required-gate", action="append")
    create.add_argument("--dependency", action="append")
    create.set_defaults(handler=create_batch)

    approve = batch_commands.add_parser("approve")
    _common(approve)
    approve.add_argument("--batch", required=True)
    approve.add_argument("--approved-by", required=True)
    approve.add_argument("--approved-at", required=True)
    approve.set_defaults(handler=approve_batch)

    decide = batch_commands.add_parser("decide")
    _common(decide)
    decide.add_argument("--batch", required=True)
    decide.add_argument("--decision", choices=sorted(DECISIONS), required=True)
    decide.add_argument("--approved-by", required=True)
    decide.add_argument("--approved-at", required=True)
    decide.add_argument("--note", default="none")
    decide.set_defaults(handler=decide_batch)

    dispatch = commands.add_parser("dispatch")
    dispatch_commands = dispatch.add_subparsers(dest="dispatch_command", required=True)
    dispatch_create = dispatch_commands.add_parser("create", aliases=["approve"])
    _common(dispatch_create)
    dispatch_create.add_argument("--batch", required=True)
    dispatch_create.add_argument("--role", default="developer")
    dispatch_create.add_argument("--approved-by", required=True)
    dispatch_create.add_argument("--approved-at", required=True)
    dispatch_create.set_defaults(handler=create_dispatch)

    dispatch_send = dispatch_commands.add_parser("send")
    _common(dispatch_send)
    dispatch_send.add_argument("--dispatch", required=True)
    dispatch_send.add_argument("--adapter", required=True)
    dispatch_send.add_argument("--adapter-arg", action="append")
    dispatch_send.set_defaults(handler=send_dispatch)

    report = commands.add_parser("report")
    report_commands = report.add_subparsers(dest="report_command", required=True)
    report_submit = report_commands.add_parser("submit", aliases=["record"])
    _common(report_submit)
    report_submit.add_argument("--file", required=True)
    report_submit.set_defaults(handler=submit_report)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        output = args.handler(args)
    except CoordinatorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
