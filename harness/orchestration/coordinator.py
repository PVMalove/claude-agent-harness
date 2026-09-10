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
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Iterator, Optional


STATE_REL = Path(".harness/orchestration/state")
SENSITIVE_KEY = re.compile(r"(?:api[_-]?key|credential|password|secret|token)", re.IGNORECASE)
ROLE_MODES = {"write", "read-only"}
REPORT_OUTCOMES = {"completed", "blocked", "failed"}
DECISIONS = {"accept", "override-warning", "retry", "block", "fail"}
DISPATCH_PURPOSES = {"work", "publish"}
REVIEW_SEVERITIES = {"none", "clean", "warning", "blocker"}
FINDING_SEVERITIES = {"info", "warning", "blocker"}
QA_LEASE_FIELDS = {"dispatch_id", "host", "pid", "acquired_at", "expires_at"}
QA_QUEUE_FIELDS = {"dispatch_id", "sequence", "queued_at"}
SENSITIVE_OUTPUT = re.compile(
    r"(?i)\b(api[_-]?key|credential|password|secret|token)\b(\s*(?:[:=]|is)\s*)([^\s]+)"
)
PLAN_FIELDS = (
    "batch_id", "created_at", "base_commit", "ticket", "branch", "worktree", "zone", "definition_of_done",
    "prohibited_changes", "verification_commands", "required_gates", "dependencies",
)
DISPATCH_FIELDS = {
    "dispatch_id", "batch_id", "ticket", "role", "access", "zone", "write_paths", "branch", "worktree",
    "definition_of_done", "prohibited_changes", "verification_commands", "required_gates", "dependencies",
    "resolved_provider_profile", "resolved_model", "coordinator_approval", "candidate_commit", "review_base", "review_scope",
    "risk_assessment_id", "purpose", "state", "created_at",
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
REPORT_OPTIONAL_FIELDS = {"risk_triggers", "review"}
RISK_ASSESSMENT_FIELDS = {
    "risk_assessment_id", "batch_id", "candidate_commit", "base_commit", "changed_files", "matched_triggers",
    "developer_triggers", "review_required", "review_scope", "created_at",
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


def _write_text_exclusive(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
    except FileExistsError as exc:
        if path.read_text(encoding="utf-8") != value:
            raise CoordinatorError(f"refusing to overwrite immutable artifact: {path.name}") from exc


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
    if not isinstance(value, str) or re.fullmatch(r"(?:batch|dispatch|risk)-[0-9a-f-]+", value) is None:
        raise CoordinatorError(f"{label} is not a valid coordinator ID")
    return value


def _repo(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "repo", ".")).resolve()


def _state_root(args: argparse.Namespace, repo: Path) -> Path:
    supplied = getattr(args, "state_dir", None)
    return (Path(supplied).resolve() if supplied else repo / STATE_REL).resolve()


def _qa_state_root(args: argparse.Namespace, repo: Path) -> Path:
    if getattr(args, "state_dir", None):
        raise CoordinatorError("QA lane is repository-scoped and does not support --state-dir")
    return repo / STATE_REL


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


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments], capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise CoordinatorError(f"git command failed: {detail or 'unknown error'}")
    return result.stdout.strip()


def _head_commit(repo: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _candidate_commit(repo: Path, value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{7,64}", value.strip()) is None:
        raise CoordinatorError("candidate_commit must be a hexadecimal commit SHA")
    try:
        return _git(repo, "rev-parse", "--verify", f"{value.strip()}^{{commit}}")
    except CoordinatorError as exc:
        raise CoordinatorError("candidate_commit does not resolve to a commit") from exc


def _commit_changed_files(repo: Path, commit: str) -> list[str]:
    output = _git(repo, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", commit)
    return [line.replace("\\", "/") for line in output.splitlines() if line.strip()]


def _commit_parent(repo: Path, commit: str) -> str | None:
    output = _git(repo, "rev-list", "--parents", "-n", "1", commit).split()
    return output[1] if len(output) > 1 else None


def _git_is_ancestor(repo: Path, base: str, candidate: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", base, candidate],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode not in {0, 1}:
        raise CoordinatorError("cannot verify the candidate diff ancestry")
    return result.returncode == 0


def _changed_files_between(repo: Path, base: str, candidate: str) -> list[str]:
    output = _git(repo, "diff", "--name-only", "--no-renames", base, candidate)
    return [line.replace("\\", "/") for line in output.splitlines() if line.strip()]


def _commit_evidence(repo: Path, base: str | None, commit: str) -> str:
    if base:
        return "\n".join(
            (
                _git(repo, "log", "--format=%B", f"{base}..{commit}"),
                _git(repo, "diff", "--no-ext-diff", "--no-renames", base, commit),
            )
        )
    return _git(repo, "show", "--format=%B", "--no-ext-diff", "--no-renames", commit)


def _risk_triggers(repo: Path) -> list[str]:
    role = _role(repo, "code-review")
    triggers = role.get("risk_triggers")
    if not isinstance(triggers, list) or not all(_non_empty(item) for item in triggers):
        raise CoordinatorError("code-review role has no valid risk triggers")
    return list(triggers)


def _validate_trigger_names(triggers: object, label: str, known: list[str]) -> list[str]:
    values = _strings(triggers, label, allow_empty=True)
    unknown = [trigger for trigger in values if trigger not in known]
    if unknown:
        raise CoordinatorError(f"{label} contains unknown risk triggers: {unknown}")
    return list(dict.fromkeys(values))


def _matching_triggers(text: str, known: list[str]) -> list[str]:
    normalized = text.casefold()
    return [
        trigger
        for trigger in known
        if trigger in normalized
        or any(re.search(pattern, normalized, re.IGNORECASE) for pattern in _trigger_patterns(trigger))
    ]


def _trigger_patterns(trigger: str) -> tuple[str, ...]:
    words = [
        word
        for word in re.split(r"[^a-z0-9]+", trigger.casefold())
        if word and word not in {"change", "changes"}
    ]
    patterns = [rf"\b{re.escape(word)}\b" for word in words]
    for word in words:
        if word.endswith("s") and not word.endswith(("is", "us", "ss")):
            patterns.append(rf"\b{re.escape(word[:-1])}s?\b")
    joined = set(words)
    if "api" in joined:
        patterns.extend((r"\bopenapi\b", r"endpoint", r"public[ _-]+contract"))
    if "migration" in joined:
        patterns.append(r"\bmigrate\b")
    if "message" in joined or "routing" in joined:
        patterns.extend((r"\bmessaging\b", r"\broute\b"))
    if "transaction" in joined:
        patterns.append(r"\batomic\b")
    if "authorization" in joined:
        patterns.extend((r"authori[sz]", r"security", r"permission", r"credential"))
    if "concurrency" in joined:
        patterns.extend((r"concurr", r"parallel", r"lock", r"retry"))
    if "retry" in joined:
        patterns.extend((r"\bdlq\b", r"dead[- ]letter"))
    return tuple(patterns)


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


def _risk_path(root: Path, risk_id: str) -> Path:
    return root / "risk-assessments" / f"{_safe_id(risk_id, 'risk assessment')}.json"


def _plan_path(root: Path, batch_id: str) -> Path:
    return root / "plans" / f"{_safe_id(batch_id, 'batch')}.json"


def _qa_lane_path(root: Path) -> Path:
    return root / "qa-lane" / "lease.json"


def _qa_queue_root(root: Path) -> Path:
    return root / "qa-lane" / "queue"


def _qa_queue_counter_path(root: Path) -> Path:
    return root / "qa-lane" / "sequence.json"


def _qa_artifact_path(root: Path, checksum: str) -> Path:
    return root / "qa-artifacts" / f"{checksum}.log"


def _qa_queue_entries(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    entries: list[tuple[Path, dict[str, Any]]] = []
    for path in _qa_queue_root(root).glob("*.json"):
        entry = _read_object(path, "QA queue entry")
        if set(entry) != QA_QUEUE_FIELDS or not isinstance(entry["sequence"], int) or entry["sequence"] < 1:
            raise CoordinatorError("QA queue entry has an invalid schema")
        _safe_id(entry["dispatch_id"], "QA queue dispatch")
        if not _non_empty(entry["queued_at"]):
            raise CoordinatorError("QA queue entry has an invalid queued_at value")
        entries.append((path, entry))
    return sorted(entries, key=lambda item: item[1]["sequence"])


def _qa_enqueue(root: Path, dispatch_id: str) -> tuple[Path, dict[str, Any]]:
    for path, entry in _qa_queue_entries(root):
        if entry["dispatch_id"] == dispatch_id:
            return path, entry
    counter_path = _qa_queue_counter_path(root)
    counter = _read_object(counter_path, "QA queue sequence") if counter_path.exists() else {"next": 1}
    if set(counter) != {"next"} or not isinstance(counter["next"], int) or counter["next"] < 1:
        raise CoordinatorError("QA queue sequence is invalid")
    entry = {"dispatch_id": dispatch_id, "sequence": counter["next"], "queued_at": _now()}
    path = _qa_queue_root(root) / f"{entry['sequence']:020d}-{dispatch_id}.json"
    _write_exclusive(path, entry)
    _replace(counter_path, {"next": counter["next"] + 1})
    return path, entry


def _qa_lease(root: Path) -> dict[str, Any] | None:
    path = _qa_lane_path(root)
    if not path.exists():
        return None
    lease = _read_object(path, "QA lease")
    if set(lease) != QA_LEASE_FIELDS or not _non_empty(lease.get("host")) or not isinstance(lease.get("pid"), int):
        raise CoordinatorError("QA lease has an invalid schema")
    _safe_id(lease.get("dispatch_id"), "QA lease dispatch")
    for field in ("acquired_at", "expires_at"):
        if not _non_empty(lease.get(field)):
            raise CoordinatorError(f"QA lease has an invalid {field}")
    return lease


def _lease_expired(lease: dict[str, Any]) -> bool:
    try:
        expiry = datetime.fromisoformat(lease["expires_at"])
    except ValueError as exc:
        raise CoordinatorError("QA lease has an unreadable expiry") from exc
    if expiry.tzinfo is None:
        raise CoordinatorError("QA lease expiry must include a timezone")
    return expiry <= datetime.now(timezone.utc)


def _sanitise(text: str) -> str:
    return SENSITIVE_OUTPUT.sub(lambda match: f"{match.group(1)}{match.group(2)}<redacted>", text)


def _concise_evidence(text: str) -> str:
    lines = [line.strip() for line in _sanitise(text).splitlines() if line.strip()]
    if not lines:
        return "no output"
    return lines[0][:240]


def _load_batch(root: Path, batch_id: str) -> dict[str, Any]:
    return _read_object(_batch_path(root, batch_id), "batch record")


def _load_dispatch(root: Path, dispatch_id: str) -> dict[str, Any]:
    return _read_object(_dispatch_path(root, dispatch_id), "dispatch record")


def _load_dispatch_status(root: Path, dispatch_id: str) -> dict[str, Any]:
    return _read_object(_dispatch_status_path(root, dispatch_id), "dispatch status")


def _load_risk(root: Path, risk_id: str) -> dict[str, Any]:
    return _read_object(_risk_path(root, risk_id), "risk assessment")


def _validate_risk(root: Path, batch: dict[str, Any], risk: dict[str, Any]) -> None:
    _reject_sensitive(risk, "risk assessment")
    if set(risk) != RISK_ASSESSMENT_FIELDS:
        raise CoordinatorError("risk assessment schema mismatch")
    if risk["batch_id"] != batch["batch_id"]:
        raise CoordinatorError("risk assessment does not belong to its batch")
    if not isinstance(risk["candidate_commit"], str) or re.fullmatch(r"[0-9a-f]{40}", risk["candidate_commit"]) is None:
        raise CoordinatorError("risk assessment has an invalid candidate commit")
    if risk["base_commit"] is not None and (
        not isinstance(risk["base_commit"], str) or re.fullmatch(r"[0-9a-f]{40}", risk["base_commit"]) is None
    ):
        raise CoordinatorError("risk assessment has an invalid base commit")
    if not isinstance(risk["changed_files"], list) or not all(_non_empty(item) for item in risk["changed_files"]):
        raise CoordinatorError("risk assessment has invalid changed files")
    if not isinstance(risk["matched_triggers"], list) or not all(_non_empty(item) for item in risk["matched_triggers"]):
        raise CoordinatorError("risk assessment has invalid matched triggers")
    if not isinstance(risk["developer_triggers"], list) or not all(_non_empty(item) for item in risk["developer_triggers"]):
        raise CoordinatorError("risk assessment has invalid developer triggers")
    if not isinstance(risk["review_required"], bool) or risk["review_scope"] != risk["changed_files"]:
        raise CoordinatorError("risk assessment review scope is invalid")
    entry = next(
        (item for item in batch.get("risk_assessments", []) if item.get("risk_assessment_id") == risk["risk_assessment_id"]),
        None,
    )
    expected = hashlib.sha256(_canonical(risk).encode("utf-8")).hexdigest()
    if not entry or entry.get("record_sha256") != expected:
        raise CoordinatorError("risk assessment failed immutable record integrity check")


def _risk_for_candidate(root: Path, batch: dict[str, Any], candidate: str) -> dict[str, Any] | None:
    matches = [
        item for item in batch.get("risk_assessments", []) if item.get("candidate_commit") == candidate
    ]
    if not matches:
        return None
    risk = _load_risk(root, matches[-1].get("risk_assessment_id"))
    _validate_risk(root, batch, risk)
    return risk


def _latest_developer_candidate(repo: Path, root: Path, batch: dict[str, Any]) -> str:
    accepted = [
        item for item in batch.get("dispatches", [])
        if item.get("role") == "developer"
        and item.get("state") == "reported"
        and item.get("decision", {}).get("decision") in {"accept", "override-warning"}
    ]
    if not accepted:
        raise CoordinatorError("candidate dispatch requires an accepted developer completion report")
    report = _pending_report(root, batch, accepted[-1])
    try:
        return _candidate_commit(repo, report["commit_sha"])
    except (KeyError, CoordinatorError) as exc:
        raise CoordinatorError("accepted developer report has no resolvable candidate commit") from exc


def _accepted_qa_for_candidate(root: Path, batch: dict[str, Any], candidate: str) -> dict[str, Any]:
    """Return the accepted green QA report pinned to exactly ``candidate``."""
    for entry in reversed(batch.get("dispatches", [])):
        if entry.get("role") != "qa" or entry.get("state") != "reported":
            continue
        if entry.get("decision", {}).get("decision") != "accept":
            continue
        dispatch = _load_dispatch(root, entry.get("dispatch_id"))
        if dispatch.get("candidate_commit") != candidate:
            continue
        report = _pending_report(root, batch, entry)
        if report.get("outcome") == "completed":
            return report
    raise CoordinatorError("publish requires accepted green QA evidence for the candidate commit")


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
    if dispatch.get("purpose") not in DISPATCH_PURPOSES:
        raise CoordinatorError("dispatch record has an invalid purpose")
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
    candidate = dispatch.get("candidate_commit")
    if dispatch["role"] in {"code-review", "qa"} and not isinstance(candidate, str):
        raise CoordinatorError("review and QA dispatches must pin a candidate commit")
    if dispatch["purpose"] == "publish":
        if dispatch["role"] != "developer" or not isinstance(candidate, str):
            raise CoordinatorError("publish dispatches must be pinned developer briefs")
        _accepted_qa_for_candidate(root, batch, candidate)
    if candidate is not None:
        resolved = _candidate_commit(repo, candidate)
        if resolved != candidate:
            raise CoordinatorError("dispatch candidate_commit must be the full resolved commit SHA")
        risk = _risk_for_candidate(root, batch, candidate)
        if risk is None or dispatch.get("risk_assessment_id") != risk["risk_assessment_id"]:
            raise CoordinatorError("dispatch candidate is not linked to its immutable risk assessment")
        if dispatch["role"] == "code-review" and dispatch.get("review_scope") != risk["review_scope"]:
            raise CoordinatorError("review dispatch scope does not match its immutable risk assessment")
        if dispatch["role"] == "code-review" and dispatch.get("review_base") != risk["base_commit"]:
            raise CoordinatorError("review dispatch base does not match its immutable risk assessment")
    elif dispatch.get("review_scope") or dispatch.get("risk_assessment_id"):
        raise CoordinatorError("dispatch contains review metadata without a candidate commit")


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


def assess_risk(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    root = _state_root(args, repo)
    known = _risk_triggers(repo)
    candidate = _candidate_commit(repo, args.candidate_commit)
    changed_files = [item.replace("\\", "/") for item in _strings(args.changed_file, "changed_files")]
    developer_triggers = _validate_trigger_names(args.developer_trigger or [], "developer_triggers", known)
    with _state_lock(root):
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        if batch.get("state") != "awaiting-approval":
            raise CoordinatorError("risk assessment requires a batch awaiting coordinator approval")
        base = batch.get("base_commit")
        if args.base_commit:
            requested_base = _candidate_commit(repo, args.base_commit)
            if requested_base != base:
                raise CoordinatorError("risk assessment base must match the batch-captured base commit")
        if base and not _git_is_ancestor(repo, base, candidate):
            raise CoordinatorError("risk assessment base must be an ancestor of the candidate commit")
        actual_files = _changed_files_between(repo, base, candidate) if base else _commit_changed_files(repo, candidate)
        if actual_files != changed_files:
            raise CoordinatorError("changed_files must exactly match the candidate diff")
        if _latest_developer_candidate(repo, root, batch) != candidate:
            raise CoordinatorError("candidate commit does not match the accepted developer report")
        inherited_triggers = {
            trigger
            for escalation in batch.get("risk_escalations", [])
            if escalation.get("candidate_commit") == candidate
            for trigger in escalation.get("triggers", [])
        }
        for trigger in sorted(inherited_triggers):
            if trigger not in developer_triggers:
                developer_triggers.append(trigger)
        evidence = " ".join(batch["definition_of_done"] + changed_files) + "\n" + _commit_evidence(repo, base, candidate)
        matched = _matching_triggers(evidence, known)
        for trigger in developer_triggers:
            if trigger not in matched:
                matched.append(trigger)
        risk = {
            "risk_assessment_id": f"risk-{uuid.uuid4()}",
            "batch_id": batch["batch_id"],
            "candidate_commit": candidate,
            "base_commit": base,
            "changed_files": changed_files,
            "matched_triggers": matched,
            "developer_triggers": developer_triggers,
            "review_required": bool(matched),
            "review_scope": list(changed_files),
            "created_at": _now(),
        }
        _reject_sensitive(risk, "risk assessment")
        _write_exclusive(_risk_path(root, risk["risk_assessment_id"]), risk)
        batch.setdefault("risk_assessments", []).append(
            {
                "risk_assessment_id": risk["risk_assessment_id"],
                "candidate_commit": candidate,
                "matched_triggers": matched,
                "review_required": risk["review_required"],
                "record_sha256": hashlib.sha256(_canonical(risk).encode("utf-8")).hexdigest(),
            }
        )
        pending_candidate = batch.get("risk_reassessment_candidate")
        pending_triggers = set(batch.get("risk_reassessment_triggers", []))
        if (
            batch.get("risk_reassessment_required")
            and pending_candidate == candidate
            and pending_triggers <= set(matched)
        ):
            batch["risk_reassessment_required"] = False
            batch.pop("risk_reassessment_candidate", None)
            batch.pop("risk_reassessment_triggers", None)
        # Assessment is evidence, not a launch instruction.  It makes the one allowed next
        # handoff visible to the coordinator; a later, separately approved dispatch creates the
        # immutable brief.
        batch["next_action"] = "code-review" if risk["review_required"] else "qa"
        _replace(_batch_path(root, batch["batch_id"]), batch)
    return risk


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
        "base_commit": _head_commit(repo),
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
        "risk_assessments": [],
        "risk_escalations": [],
        "risk_reassessment_required": False,
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


def _pending_report(root: Path, batch: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    report_path = entry.get("report")
    if not isinstance(report_path, str):
        raise CoordinatorError("reported dispatch has no completion report")
    report = _read_object(root / report_path, "completion report")
    expected = entry.get("report_sha256")
    actual = hashlib.sha256(_canonical(report).encode("utf-8")).hexdigest()
    if not isinstance(expected, str) or expected != actual:
        raise CoordinatorError("completion report failed immutable integrity check")
    return report


def _review_severity(review: dict[str, Any]) -> dict[str, str]:
    return {axis: review[axis]["severity"] for axis in ("standards", "spec")}


def decide_batch(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    root = _state_root(args, repo)
    with _state_lock(root):
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        pending = [item for item in batch.get("dispatches", []) if item.get("state") == "reported" and "decision" not in item]
        if len(pending) != 1:
            raise CoordinatorError("batch has no single completion report awaiting a coordinator decision")
        report = _pending_report(root, batch, pending[0])
        dispatch = _load_dispatch(root, pending[0]["dispatch_id"])
        _validate_dispatch(repo, _config(repo), root, batch, dispatch)
        _validate_report(report, dispatch, _role(repo, dispatch["role"]), repo, batch.get("base_commit"))
        if report.get("outcome") != "completed" and args.decision in {"accept", "override-warning"}:
            raise CoordinatorError("a non-completed role report cannot be accepted or warning-overridden")
        if report.get("role") == "code-review":
            severities = _review_severity(report["review"])
            if any(value == "blocker" for value in severities.values()):
                if args.decision != "retry":
                    raise CoordinatorError("a review blocker requires a new developer retry")
            elif any(value == "warning" for value in severities.values()):
                if args.decision == "accept":
                    raise CoordinatorError("a review warning requires override-warning or retry")
                if args.decision == "override-warning" and not _non_empty(args.note):
                    raise CoordinatorError("warning override requires a recorded note")
            elif args.decision == "override-warning":
                raise CoordinatorError("override-warning requires a review warning")
        elif args.decision == "override-warning":
            raise CoordinatorError("only a recorded review warning can be overridden")
        decision = {
            "decision": args.decision,
            "approved_by": _approval(args)["approved_by"],
            "approved_at": _approval(args)["approved_at"],
            "note": args.note.strip() if _non_empty(args.note) else "none",
        }
        pending[0]["decision"] = decision
        batch.setdefault("coordinator_decisions", []).append({"dispatch_id": pending[0]["dispatch_id"], **decision})
        if args.decision == "retry":
            batch["required_next_role"] = "developer"
            batch["retry_candidate_required"] = True
            batch["next_action"] = "developer-retry"
        elif args.decision in {"accept", "override-warning"}:
            if report["role"] == "developer":
                if dispatch.get("purpose") == "publish":
                    batch.pop("next_action", None)
                    batch["state"] = "completed"
                else:
                    batch["next_action"] = "risk-assessment"
            elif report["role"] == "code-review":
                batch["next_action"] = "qa"
            elif report["role"] == "qa":
                batch["next_action"] = "publish"
        if args.decision == "block":
            batch["state"] = "blocked"
        elif args.decision == "fail":
            batch.pop("next_action", None)
            batch["state"] = "failed"
        elif batch.get("state") != "completed":
            batch["state"] = "awaiting-approval"
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
        purpose = args.purpose
        next_action = batch.get("next_action")
        required = {
            None: {("developer", "work")},
            "code-review": {("code-review", "work")},
            "qa": {("qa", "work")},
            "publish": {("developer", "publish")},
            "developer-retry": {("developer", "work")},
        }
        if next_action == "risk-assessment":
            raise CoordinatorError("risk assessment must prepare the next dispatch before another role starts")
        if (role_name, purpose) not in required.get(next_action, set()):
            raise CoordinatorError("dispatch does not match the coordinator-prepared next action")
        role, zone, profile_id, model = _resolve_assignment(repo, config, role_name, batch["zone"])
        candidate = None
        risk = None
        review_scope: list[str] = []
        if args.candidate_commit is not None:
            candidate = _candidate_commit(repo, args.candidate_commit)
        if role_name in {"code-review", "qa"} and candidate is None:
            raise CoordinatorError(f"{role_name} dispatch requires candidate_commit")
        if purpose == "publish":
            if role_name != "developer" or candidate is None:
                raise CoordinatorError("publish requires a developer role and candidate_commit")
            if candidate != _latest_developer_candidate(repo, root, batch):
                raise CoordinatorError("publish must use the latest accepted developer candidate")
            _accepted_qa_for_candidate(root, batch, candidate)
        elif purpose != "work":
            raise CoordinatorError("dispatch purpose is invalid")
        if candidate is not None:
            risk = _risk_for_candidate(root, batch, candidate)
        if role_name in {"code-review", "qa"} and candidate != _latest_developer_candidate(repo, root, batch):
            raise CoordinatorError("review and QA dispatches must use the latest accepted developer candidate")
        if role_name in {"code-review", "qa"} and risk is None:
            raise CoordinatorError("candidate commit has no coordinator risk assessment")
        if role_name == "code-review":
            if not risk["review_required"]:
                raise CoordinatorError("code-review dispatch is not required by the risk assessment")
            review_scope = list(risk["review_scope"])
        if role_name == "qa":
            if batch.get("risk_reassessment_required"):
                raise CoordinatorError("QA is blocked until the candidate is risk-assessed again")
            if risk["review_required"]:
                accepted_review = any(
                    item.get("role") == "code-review"
                    and item.get("state") == "reported"
                    and item.get("decision", {}).get("decision") in {"accept", "override-warning"}
                    and _load_dispatch(root, item["dispatch_id"]).get("candidate_commit") == candidate
                    for item in batch.get("dispatches", [])
                )
                if not accepted_review:
                    raise CoordinatorError("QA requires an accepted composite review for the candidate")
        required_role = batch.get("required_next_role")
        if required_role and role_name != required_role:
            raise CoordinatorError(f"the coordinator requires a new {required_role} dispatch before this role")
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
            "candidate_commit": candidate,
            "review_base": risk["base_commit"] if risk else None,
            "review_scope": review_scope,
            "risk_assessment_id": risk["risk_assessment_id"] if risk else None,
            "purpose": purpose,
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
        if required_role and role_name == required_role:
            batch.pop("required_next_role", None)
        batch["state"] = "active"
        _replace(_batch_path(root, batch["batch_id"]), batch)
    return {"dispatch_id": dispatch_id, "batch_id": batch["batch_id"], "state": "approved", "brief": brief}


def _validate_checkout(checkout: Path, candidate: str, base: str | None, scope: list[str]) -> None:
    if not checkout.is_dir():
        raise CoordinatorError(f"review checkout does not exist: {checkout}")
    try:
        actual = _git(checkout, "rev-parse", "--verify", "HEAD^{commit}")
    except CoordinatorError as exc:
        raise CoordinatorError("review checkout is not a git worktree") from exc
    if actual != candidate:
        raise CoordinatorError("review checkout HEAD does not match the pinned candidate commit")
    status = _git(checkout, "status", "--porcelain", "--ignored", "--untracked-files=all")
    mutable_paths = []
    for line in status.splitlines():
        path = line[3:].split(" -> ", 1)[-1].replace("\\", "/")
        if not path.startswith(".harness/orchestration/state/"):
            mutable_paths.append(path)
    if mutable_paths:
        raise CoordinatorError("review checkout must be clean; mutable files are outside the pinned scope")
    actual_files = _changed_files_between(checkout, base, candidate) if base else _commit_changed_files(checkout, candidate)
    if actual_files != scope:
        raise CoordinatorError("review checkout changed files do not match the immutable review scope")


def send_dispatch(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    root = _state_root(args, repo)
    dispatch_record = _load_dispatch(root, args.dispatch)
    if dispatch_record.get("role") == "qa":
        raise CoordinatorError("QA dispatches must run through the clean-room QA lane, never a runtime adapter")
    if dispatch_record.get("purpose") == "publish":
        raise CoordinatorError("publish-only dispatches must use the verified coordinator publish boundary")
    adapter = Path(args.adapter).resolve()
    if not adapter.is_file():
        raise CoordinatorError(f"runtime adapter does not exist: {adapter}")
    with _state_lock(root):
        dispatch = _load_dispatch(root, args.dispatch)
        batch = _load_batch(root, dispatch["batch_id"])
        _validate_batch_integrity(root, batch)
        config = _config(repo)
        _validate_dispatch(repo, config, root, batch, dispatch)
        if dispatch["role"] == "code-review":
            checkout = Path(args.checkout).resolve() if args.checkout else None
            if checkout is None:
                raise CoordinatorError("code-review dispatch requires an explicit checkout")
            _validate_checkout(checkout, dispatch["candidate_commit"], dispatch["review_base"], dispatch["review_scope"])
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


def publish_dispatch(args: argparse.Namespace) -> dict[str, Any]:
    """Push one QA-accepted candidate through an approved publish-only brief."""
    repo = _repo(args)
    root = _state_root(args, repo)
    remote = args.remote.strip() if _non_empty(args.remote) else ""
    if not remote:
        raise CoordinatorError("publish remote must be a non-empty string")
    if remote not in _git(repo, "remote").splitlines():
        raise CoordinatorError("publish remote is not configured for this repository")
    with _state_lock(root):
        dispatch = _load_dispatch(root, args.dispatch)
        batch = _load_batch(root, dispatch["batch_id"])
        _validate_batch_integrity(root, batch)
        _validate_dispatch(repo, _config(repo), root, batch, dispatch)
        if dispatch["purpose"] != "publish":
            raise CoordinatorError("only a publish-only dispatch may push a candidate")
        status = _load_dispatch_status(root, dispatch["dispatch_id"])
        entry = next((item for item in batch["dispatches"] if item["dispatch_id"] == dispatch["dispatch_id"]), None)
        if not entry or entry.get("state") != "approved" or status.get("state") != "approved":
            raise CoordinatorError("publish requires an approved, unsent publish-only dispatch")
        candidate = dispatch["candidate_commit"]
        _accepted_qa_for_candidate(root, batch, candidate)
        result = subprocess.run(
            ["git", "-C", str(repo), "push", remote, f"{candidate}:refs/heads/{dispatch['branch']}"],
            capture_output=True, text=True, encoding="utf-8",
        )
        if result.returncode != 0:
            detail = _sanitise((result.stderr or result.stdout).strip())
            raise CoordinatorError(f"could not publish the accepted candidate: {detail or 'unknown error'}")
        published = _git(repo, "ls-remote", "--heads", remote, f"refs/heads/{dispatch['branch']}")
        if not published or published.split()[0] != candidate:
            raise CoordinatorError("remote branch does not resolve to the accepted QA candidate")
        entry["state"] = "dispatched"
        _replace(_dispatch_status_path(root, dispatch["dispatch_id"]), {
            "dispatch_id": dispatch["dispatch_id"], "state": "dispatched", "updated_at": _now(),
        })
        _replace(_batch_path(root, batch["batch_id"]), batch)
        changed = _changed_files_between(repo, batch["base_commit"], candidate) if batch.get("base_commit") else _commit_changed_files(repo, candidate)
        report = {
            "dispatch_id": dispatch["dispatch_id"], "ticket": dispatch["ticket"], "role": "developer",
            "outcome": "completed", "output": f"published accepted QA candidate {candidate} to {remote}/{dispatch['branch']}",
            "commit_sha": candidate, "changed_files": changed,
            "checks_run": [
                {"command": command, "result": "pass", "evidence": f"accepted clean-room QA evidence for {candidate}"}
                for command in dispatch["verification_commands"]
            ],
            "risks": "none", "blockers": "none", "next_coordinator_action": "accept publication or inspect remote evidence",
        }
        report_path = _persist_report(root, batch, dispatch, report)
    return {"dispatch_id": dispatch["dispatch_id"], "state": "reported", "report": str(report_path), "candidate_commit": candidate}


def _qa_report(dispatch: dict[str, Any], checks: list[dict[str, str]], artifact: Path, checksum: str) -> dict[str, Any]:
    failed = any(check["result"] == "fail" for check in checks)
    return {
        "dispatch_id": dispatch["dispatch_id"],
        "ticket": dispatch["ticket"],
        "role": "qa",
        "outcome": "failed" if failed else "completed",
        "output": (
            f"QA gate {'failed' if failed else 'passed'}; full sanitised output: "
            f"{artifact.as_posix()} (sha256:{checksum})"
        ),
        "commit_sha": "not applicable — read-only role",
        "changed_files": [],
        "checks_run": checks,
        "risks": "QA gate failed; inspect immutable evidence" if failed else "none",
        "blockers": "new approved developer retry required" if failed else "none",
        "next_coordinator_action": "create a new approved developer retry" if failed else "accept or continue",
    }


def _persist_report(root: Path, batch: dict[str, Any], dispatch: dict[str, Any], report: dict[str, Any]) -> Path:
    report_json = root / "reports" / f"{dispatch['dispatch_id']}.json"
    report_md = root / "reports" / f"{dispatch['dispatch_id']}.md"
    if report_json.exists() or report_md.exists():
        raise CoordinatorError("refusing to overwrite immutable completion report")
    _write_exclusive(report_json, report)
    try:
        _write_text_exclusive(report_md, _report_markdown(report))
    except CoordinatorError as exc:
        raise CoordinatorError("refusing to overwrite immutable Markdown report") from exc
    entry = next((item for item in batch["dispatches"] if item["dispatch_id"] == dispatch["dispatch_id"]), None)
    if entry is None:
        raise CoordinatorError("dispatch is not registered in its batch")
    entry["state"] = "reported"
    entry["report"] = f"reports/{dispatch['dispatch_id']}.json"
    entry["report_sha256"] = hashlib.sha256(_canonical(report).encode("utf-8")).hexdigest()
    batch["state"] = "awaiting-approval"
    _replace(_dispatch_status_path(root, dispatch["dispatch_id"]), {
        "dispatch_id": dispatch["dispatch_id"], "state": "reported", "updated_at": _now(),
    })
    _replace(_batch_path(root, batch["batch_id"]), batch)
    return report_json


def _record_qa_report(root: Path, repo: Path, dispatch: dict[str, Any], report: dict[str, Any]) -> Path:
    batch = _load_batch(root, dispatch["batch_id"])
    _validate_batch_integrity(root, batch)
    _validate_dispatch(repo, _config(repo), root, batch, dispatch)
    status = _load_dispatch_status(root, dispatch["dispatch_id"])
    entry = next((item for item in batch.get("dispatches", []) if item["dispatch_id"] == dispatch["dispatch_id"]), None)
    if not entry or entry.get("state") != "dispatched" or status.get("state") != "working":
        raise CoordinatorError("QA report requires a running QA dispatch")
    _validate_report(report, dispatch, _role(repo, "qa"), repo, batch.get("base_commit"))
    return _persist_report(root, batch, dispatch, report)


def run_qa(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    root = _qa_state_root(args, repo)
    lease_seconds = args.lease_seconds
    if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds < 1:
        raise CoordinatorError("QA lease-seconds must be a positive integer")
    with _state_lock(root):
        dispatch = _load_dispatch(root, args.dispatch)
        batch = _load_batch(root, dispatch["batch_id"])
        _validate_batch_integrity(root, batch)
        _validate_dispatch(repo, _config(repo), root, batch, dispatch)
        if dispatch["role"] != "qa":
            raise CoordinatorError("clean-room QA runner accepts only QA dispatches")
        if not dispatch["verification_commands"]:
            raise CoordinatorError("clean-room QA runner requires configured verification_commands")
        status = _load_dispatch_status(root, dispatch["dispatch_id"])
        entry = next((item for item in batch.get("dispatches", []) if item["dispatch_id"] == dispatch["dispatch_id"]), None)
        if not entry or entry.get("state") != "approved" or status.get("state") != "approved":
            raise CoordinatorError("QA runner requires an approved, unsent dispatch")
        queue_path, queue_entry = _qa_enqueue(root, dispatch["dispatch_id"])
        queue = _qa_queue_entries(root)
        position = next(index for index, (_, item) in enumerate(queue, start=1) if item["dispatch_id"] == dispatch["dispatch_id"])
        lease = _qa_lease(root)
        if lease is not None:
            if _lease_expired(lease):
                raise CoordinatorError("QA lease is stale; a coordinator must clear it explicitly before another gate runs")
            return {"dispatch_id": dispatch["dispatch_id"], "state": "queued", "position": position}
        if position != 1:
            return {"dispatch_id": dispatch["dispatch_id"], "state": "queued", "position": position}
        acquired = _now()
        lease = {
            "dispatch_id": dispatch["dispatch_id"],
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "acquired_at": acquired,
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat(),
        }
        _write_exclusive(_qa_lane_path(root), lease)
        entry["state"] = "dispatched"
        _replace(_dispatch_status_path(root, dispatch["dispatch_id"]), {
            "dispatch_id": dispatch["dispatch_id"], "state": "working", "updated_at": _now(),
        })
        _replace(_batch_path(root, batch["batch_id"]), batch)

    worktree_root = Path(tempfile.mkdtemp(prefix="agent-harness-qa-"))
    checkout = worktree_root / "checkout"
    outputs: list[str] = []
    checks: list[dict[str, str]] = []
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "--detach", str(checkout), dispatch["candidate_commit"]],
            capture_output=True, text=True, encoding="utf-8",
        )
        if result.returncode != 0:
            detail = _sanitise((result.stderr or result.stdout).strip())
            raise CoordinatorError(f"could not create clean QA worktree: {detail or 'unknown error'}")
        if _git(checkout, "rev-parse", "--verify", "HEAD^{commit}") != dispatch["candidate_commit"]:
            raise CoordinatorError("clean QA worktree HEAD does not match the pinned candidate commit")
        if _git(checkout, "status", "--porcelain", "--untracked-files=all"):
            raise CoordinatorError("clean QA worktree contains mutable files")
        for command in dispatch["verification_commands"]:
            result = subprocess.run(command, cwd=checkout, shell=True, capture_output=True, text=True, encoding="utf-8")
            combined = _sanitise((result.stdout or "") + ("\n" if result.stdout and result.stderr else "") + (result.stderr or ""))
            outputs.append(f"$ {_sanitise(command)}\nexit_code={result.returncode}\n{combined}\n")
            checks.append({
                "command": command,
                "result": "pass" if result.returncode == 0 else "fail",
                "evidence": f"exit {result.returncode}; {_concise_evidence(combined)}",
            })
    finally:
        if checkout.exists():
            subprocess.run(["git", "-C", str(repo), "worktree", "remove", "--force", str(checkout)], capture_output=True, text=True)
        shutil.rmtree(worktree_root, ignore_errors=True)

    artifact_text = "\n".join(outputs)
    checksum = hashlib.sha256(artifact_text.encode("utf-8")).hexdigest()
    artifact = _qa_artifact_path(root, checksum)
    try:
        _write_text_exclusive(artifact, artifact_text)
    except CoordinatorError as exc:
        raise CoordinatorError("could not persist immutable QA evidence") from exc
    report = _qa_report(dispatch, checks, artifact, checksum)
    with _state_lock(root):
        report_path = _record_qa_report(root, repo, dispatch, report)
        _qa_queue_entries(root)  # validate before removing the completed request
        queue_path.unlink(missing_ok=True)
        lease_path = _qa_lane_path(root)
        current = _qa_lease(root)
        if current and current["dispatch_id"] == dispatch["dispatch_id"]:
            lease_path.unlink()
    return {
        "dispatch_id": dispatch["dispatch_id"],
        "state": "reported",
        "report": str(report_path),
        "artifact": str(artifact),
        "sha256": checksum,
    }


def qa_status(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    root = _qa_state_root(args, repo)
    with _state_lock(root):
        queue = _qa_queue_entries(root)
        lease = _qa_lease(root)
        return {
            "lease": lease,
            "lease_stale": _lease_expired(lease) if lease else False,
            "queue": [entry for _, entry in queue],
        }


def clear_qa_lease(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    root = _qa_state_root(args, repo)
    with _state_lock(root):
        lease = _qa_lease(root)
        if lease is None:
            raise CoordinatorError("there is no QA lease to clear")
        if not _lease_expired(lease):
            raise CoordinatorError("a live QA lease cannot be force-unlocked")
        expected = {"host": args.expected_host, "pid": args.expected_pid, "expires_at": args.expected_expiry}
        if any(lease[field] != value for field, value in expected.items()):
            raise CoordinatorError("QA lease changed; coordinator must validate the current owner again")
        approval = _approval(args)
        queue = _qa_queue_entries(root)
        recovery = {
            "cleared_dispatch_id": lease["dispatch_id"], "lease": lease, "approval": approval,
            "reason": args.reason.strip(), "cleared_at": _now(),
        }
        _write_exclusive(root / "qa-lane" / "recoveries" / f"{uuid.uuid4()}.json", recovery)
        owner = next(((path, entry) for path, entry in queue if entry["dispatch_id"] == lease["dispatch_id"]), None)
        if owner is not None:
            owner[0].unlink()
        _qa_lane_path(root).unlink()
    return {"state": "cleared", "dispatch_id": lease["dispatch_id"]}


def _validate_review(review: object, dispatch: dict[str, Any]) -> None:
    if not isinstance(review, dict) or set(review) != {"candidate_commit", "scope", "standards", "spec"}:
        raise CoordinatorError("composite review must contain independent standards and spec evidence")
    if review["candidate_commit"] != dispatch["candidate_commit"] or review["scope"] != dispatch["review_scope"]:
        raise CoordinatorError("composite review evidence does not match the approved review brief")
    for axis in ("standards", "spec"):
        evidence = review[axis]
        if not isinstance(evidence, dict) or set(evidence) != {"severity", "findings", "risks", "blockers"}:
            raise CoordinatorError(f"composite review {axis} evidence has an invalid schema")
        if evidence["severity"] not in REVIEW_SEVERITIES:
            raise CoordinatorError(f"composite review {axis} severity is invalid")
        if not isinstance(evidence["findings"], list):
            raise CoordinatorError(f"composite review {axis} findings must be a list")
        highest = "none"
        for finding in evidence["findings"]:
            if not isinstance(finding, dict) or set(finding) != {"severity", "summary", "evidence"}:
                raise CoordinatorError(f"composite review {axis} finding has an invalid schema")
            if finding["severity"] not in FINDING_SEVERITIES or not all(
                _non_empty(finding[field]) for field in ("summary", "evidence")
            ):
                raise CoordinatorError(f"composite review {axis} finding is invalid")
            if finding["severity"] == "blocker":
                highest = "blocker"
            elif finding["severity"] == "warning" and highest == "none":
                highest = "warning"
        if highest == "blocker" and evidence["severity"] != "blocker":
            raise CoordinatorError(f"composite review {axis} hides a blocker finding")
        if highest == "warning" and evidence["severity"] in {"none", "clean"}:
            raise CoordinatorError(f"composite review {axis} hides a warning finding")
        if not _non_empty(evidence["risks"]) or not _non_empty(evidence["blockers"]):
            raise CoordinatorError(f"composite review {axis} must state risks and blockers")


def _validate_report(
    report: dict[str, Any], dispatch: dict[str, Any], role: dict[str, Any], repo: Path | None = None,
    base_commit: str | None = None,
) -> None:
    _reject_sensitive(report, "completion report")
    if not REPORT_FIELDS <= set(report) or set(report) - REPORT_FIELDS - REPORT_OPTIONAL_FIELDS:
        missing = sorted(REPORT_FIELDS - set(report))
        extra = sorted(set(report) - REPORT_FIELDS - REPORT_OPTIONAL_FIELDS)
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
        if repo is not None:
            resolved = _candidate_commit(repo, commit_sha)
            actual_files = _changed_files_between(repo, base_commit, resolved) if base_commit else _commit_changed_files(repo, resolved)
            if actual_files != changed_files:
                raise CoordinatorError("completion report changed_files must exactly match commit_sha")
    if role["mode"] == "read-only" and commit_sha != "not applicable — read-only role":
        raise CoordinatorError("read-only completion reports must not claim a commit SHA")
    if "risk_triggers" in report:
        _strings(report["risk_triggers"], "completion report risk_triggers", allow_empty=True)
    if role.get("name") == "code-review":
        _validate_review(report.get("review"), dispatch)
    elif "review" in report:
        raise CoordinatorError("only the code-review role may submit composite review evidence")


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
        ]
    )
    review = report.get("review")
    if isinstance(review, dict):
        lines.extend(
            [
                f"- Review candidate commit: {review['candidate_commit']}",
                f"- Review scope: {', '.join(review['scope']) or 'none'}",
            ]
        )
        for axis in ("standards", "spec"):
            evidence = review[axis]
            lines.extend(
                [
                    f"- Review {axis.title()} severity: {evidence['severity']}",
                    f"- Review {axis.title()} findings: {len(evidence['findings'])}",
                    f"- Review {axis.title()} risks: {evidence['risks']}",
                    f"- Review {axis.title()} blockers: {evidence['blockers']}",
                ]
            )
            for finding in evidence["findings"]:
                lines.append(f"  - [{finding['severity']}] {finding['summary']}: {finding['evidence']}")
    lines.append("")
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
        if dispatch["role"] == "qa":
            raise CoordinatorError("QA reports must be produced by the clean-room QA runner")
        status = _load_dispatch_status(root, dispatch["dispatch_id"])
        entry = next((item for item in batch.get("dispatches", []) if item["dispatch_id"] == dispatch["dispatch_id"]), None)
        if not entry or entry.get("state") != "dispatched" or status.get("state") != "dispatched":
            raise CoordinatorError("completion report requires a dispatched role")
        role = _role(repo, dispatch["role"])
        _validate_report(report, dispatch, role, repo, batch.get("base_commit"))
        retry_candidate: str | None = None
        was_retry = False
        if role["name"] == "developer" and batch.get("retry_candidate_required"):
            retry_candidate = _candidate_commit(repo, report["commit_sha"])
            was_retry = True
            prior_candidates = {
                item.get("candidate_commit")
                for item in batch.get("risk_assessments", [])
                if isinstance(item.get("candidate_commit"), str)
            }
            if retry_candidate in prior_candidates:
                raise CoordinatorError("a retry must produce a new candidate commit before review or QA")
            batch["retry_candidate_required"] = False
        if "risk_triggers" in report and role["name"] == "developer":
            triggers = _validate_trigger_names(
                report["risk_triggers"], "completion report risk_triggers", _risk_triggers(repo)
            )
            if triggers:
                escalated_candidate = _candidate_commit(repo, report["commit_sha"])
                batch.setdefault("risk_escalations", []).append(
                    {"dispatch_id": dispatch["dispatch_id"], "candidate_commit": escalated_candidate, "triggers": triggers}
                )
                batch["risk_reassessment_required"] = True
                batch["risk_reassessment_candidate"] = escalated_candidate
                batch["risk_reassessment_triggers"] = triggers
            elif was_retry:
                inherited = sorted(
                    {
                        trigger
                        for escalation in batch.get("risk_escalations", [])
                        for trigger in escalation.get("triggers", [])
                    }
                )
                if inherited:
                    batch.setdefault("risk_escalations", []).append(
                        {"dispatch_id": dispatch["dispatch_id"], "candidate_commit": retry_candidate, "triggers": inherited}
                    )
                    batch["risk_reassessment_required"] = True
                    batch["risk_reassessment_candidate"] = retry_candidate
                    batch["risk_reassessment_triggers"] = inherited
                else:
                    batch["risk_reassessment_required"] = False
                    batch.pop("risk_reassessment_candidate", None)
                    batch.pop("risk_reassessment_triggers", None)
            elif not batch.get("risk_reassessment_required"):
                batch["risk_reassessment_required"] = False
                batch.pop("risk_reassessment_candidate", None)
                batch.pop("risk_reassessment_triggers", None)
        report_json = _persist_report(root, batch, dispatch, report)
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

    risk = commands.add_parser("risk")
    risk_commands = risk.add_subparsers(dest="risk_command", required=True)
    assess = risk_commands.add_parser("assess")
    _common(assess)
    assess.add_argument("--batch", required=True)
    assess.add_argument("--candidate-commit", required=True)
    assess.add_argument("--base-commit", help="optional immutable diff base for a multi-commit candidate")
    assess.add_argument("--changed-file", action="append", required=True)
    assess.add_argument("--developer-trigger", action="append")
    assess.set_defaults(handler=assess_risk)

    dispatch = commands.add_parser("dispatch")
    dispatch_commands = dispatch.add_subparsers(dest="dispatch_command", required=True)
    dispatch_create = dispatch_commands.add_parser("create", aliases=["approve"])
    _common(dispatch_create)
    dispatch_create.add_argument("--batch", required=True)
    dispatch_create.add_argument("--role", default="developer")
    dispatch_create.add_argument("--purpose", choices=sorted(DISPATCH_PURPOSES), default="work")
    dispatch_create.add_argument("--candidate-commit")
    dispatch_create.add_argument("--approved-by", required=True)
    dispatch_create.add_argument("--approved-at", required=True)
    dispatch_create.set_defaults(handler=create_dispatch)

    dispatch_send = dispatch_commands.add_parser("send")
    _common(dispatch_send)
    dispatch_send.add_argument("--dispatch", required=True)
    dispatch_send.add_argument("--adapter", required=True)
    dispatch_send.add_argument("--adapter-arg", action="append")
    dispatch_send.add_argument("--checkout")
    dispatch_send.set_defaults(handler=send_dispatch)

    dispatch_publish = dispatch_commands.add_parser("publish")
    _common(dispatch_publish)
    dispatch_publish.add_argument("--dispatch", required=True)
    dispatch_publish.add_argument("--remote", default="origin")
    dispatch_publish.set_defaults(handler=publish_dispatch)

    qa = commands.add_parser("qa")
    qa_commands = qa.add_subparsers(dest="qa_command", required=True)
    qa_run = qa_commands.add_parser("run")
    _common(qa_run)
    qa_run.add_argument("--dispatch", required=True)
    qa_run.add_argument("--lease-seconds", type=int, default=1800)
    qa_run.set_defaults(handler=run_qa)

    qa_status_command = qa_commands.add_parser("status")
    _common(qa_status_command)
    qa_status_command.set_defaults(handler=qa_status)

    qa_clear = qa_commands.add_parser("clear-stale-lease")
    _common(qa_clear)
    qa_clear.add_argument("--approved-by", required=True)
    qa_clear.add_argument("--approved-at", required=True)
    qa_clear.add_argument("--expected-host", required=True)
    qa_clear.add_argument("--expected-pid", type=int, required=True)
    qa_clear.add_argument("--expected-expiry", required=True)
    qa_clear.add_argument("--reason", required=True)
    qa_clear.set_defaults(handler=clear_qa_lease)

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
