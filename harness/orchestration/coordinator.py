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
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, replace as _vo_replace
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Iterator, Optional

MODULE_ROOT = Path(__file__).resolve().parent
if str(MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULE_ROOT))

GATE_RUNNER_ROOT = MODULE_ROOT.parent / "gate_runner"
if str(GATE_RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(GATE_RUNNER_ROOT))
CONTEXT_BUILDER_ROOT = MODULE_ROOT.parent / "context_builder"
if str(CONTEXT_BUILDER_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTEXT_BUILDER_ROOT))
from contract import (
    COMMUNICATION_POLICY_FIELDS, ContractError, health_problems, load_role_manifest,
    resolve_allowed_tools, resolve_assignment, resolve_runtime_name, validate_brief_policy,
)
from context_builder import ContextPackageError, build_context_package
from dispatch_preflight import PreflightError, prepare as prepare_dispatch
from gate_runner import concise_evidence, sanitise
from ledger import (
    BatchRecord, CheckpointRecord, ContextPackageRecord, DispatchRecord, DispatchStatusRecord,
    LedgerError, LifecycleLedger, PlanRecord, RiskAssessmentRecord,
)
from coordinator_cli import build_parser
import qa_lane
from runtime_attestation import AttestationError, attest as attest_runtime_worktree


STATE_REL = Path(".harness/orchestration/state")
SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|credential|password|secret|(?:access|auth|refresh|id|bearer)[_-]?token|(?:^|[_-])token(?:$|[_-](?:id|value|secret|key)$))",
    re.IGNORECASE,
)
REPORT_OUTCOMES = {"completed", "blocked", "failed"}
DECISIONS = {"accept", "override-warning", "retry", "block", "fail"}
TERMINAL_BATCH_STATES = {"completed", "failed", "blocked"}
DISPATCH_PURPOSES = {"work", "publish"}
ROLE_TRANSPORTS = {"orca", "in-process"}
DEFAULT_ZONE = "repository"
DEFAULT_PROFILE = "session"
DEFAULT_STALE_AFTER_SECONDS = 900
DEFAULT_COMMUNICATION_POLICY = {
    "agent_to_agent_language": "en",
    "coordinator_report_language": "ru",
}
LIVE_DISPATCH_STATES = {"dispatched", "working"}
REVIEW_SEVERITIES = {"none", "clean", "warning", "blocker"}
FINDING_SEVERITIES = {"info", "warning", "blocker"}
QA_LEASE_FIELDS = {"dispatch_id", "host", "pid", "acquired_at", "expires_at"}
QA_QUEUE_FIELDS = {"dispatch_id", "sequence", "queued_at"}
PLAN_FIELDS = (
    "batch_id", "created_at", "base_commit", "integration_ref", "branch_start_commit", "ticket", "branch",
    "worktree", "zone", "definition_of_done", "prohibited_changes", "developer_verification_commands",
    "verification_commands", "required_gates", "dependencies", "approval_policy", "communication_policy",
    "scope_preflight",
    "harness_runtime_sha256",
)
LEGACY_PLAN_FIELDS = tuple(
    field for field in PLAN_FIELDS
    if field not in {"scope_preflight", "harness_runtime_sha256", "communication_policy"}
)
PRE_APPROVAL_LEGACY_PLAN_FIELDS = tuple(field for field in LEGACY_PLAN_FIELDS if field != "approval_policy")
DISPATCH_FIELDS = {
    "dispatch_id", "batch_id", "ticket", "role", "access", "zone", "write_paths", "branch", "worktree",
    "definition_of_done", "prohibited_changes", "verification_commands", "required_gates", "dependencies",
    "resolved_runtime", "resolved_provider_profile", "resolved_model", "resolved_effort", "resolved_transport", "coordinator_approval", "candidate_commit", "review_base", "review_scope",
    "risk_assessment_id", "purpose", "state", "created_at", "delta_review_of", "delta_review_axis",
    "context_package_id", "context_package_sha256", "context_package_summary", "worker_attestation_required",
    "communication_policy",
    "snapshot_commit",
    "report_staging_path",
    "allowed_tools", "context_budget",
}
DEFAULT_TEST_PATH_PATTERNS = ("tests/**", "**/tests/**", "**/test_*.py", "**/*_test.py")
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
REPORT_OPTIONAL_FIELDS = {"risk_triggers", "review", "report_language"}
RISK_ASSESSMENT_FIELDS = {
    "risk_assessment_id", "batch_id", "candidate_commit", "base_commit", "changed_files", "matched_triggers",
    "developer_triggers", "review_required", "review_scope", "created_at",
}
CONTEXT_PACKAGE_FIELDS = {
    "context_package_id", "batch_id", "base_commit", "candidate_commit", "diff", "starting_files",
    "symbol_graph", "related_tests", "precedent_cards", "file_hashes", "size_bytes", "created_at",
    "role", "inclusion_reason", "estimated_tokens",
}
LEGACY_CONTEXT_PACKAGE_FIELDS = CONTEXT_PACKAGE_FIELDS - {"estimated_tokens"}
CHECKPOINT_NO_CONTEXT_PACKAGE = "not applicable — no context package registered"
CHECKPOINT_INPUT_FIELDS = {
    "dispatch_id", "commit_sha", "changed_files", "remaining_definition_of_done", "passing_checks",
    "risks", "blockers", "context_package_id",
}
CHECKPOINT_FIELDS = CHECKPOINT_INPUT_FIELDS | {"checkpoint_id", "batch_id", "created_at"}
# Fixed runtime-adapter termination vocabulary, not a project policy value -- a rate-limit signal
# always authorizes a continuation automatically, whatever project a batch belongs to.
RATE_LIMIT_TERMINATION_REASONS = {"rate_limit", "rate-limit", "429"}
PLANNED_TRIGGER_KINDS = {"context-limit", "tdd-cycles", "failure-log", "vertical-slice"}
PLANNED_TRIGGER_THRESHOLD_KEY = {
    "context-limit": "context_limit",
    "tdd-cycles": "tdd_cycle_count",
    "failure-log": "failure_log_bytes",
}
DEFAULT_ADAPTIVE_CONTINUATION_POLICY = {
    "context_limit": 150_000,
    "tdd_cycle_count": 3,
    "failure_log_bytes": 20_000,
    "context_warn_ratio": 0.8,
}
DEFAULT_CONTEXT_PACKAGE_POLICY = {
    "max_tokens": 200_000,
    "context_window_tokens": 250_000,
    "reserved_prompt_tokens": 20_000,
    "symbol_graph_depth": 2,
    "max_related_tests": 25,
}
DEFAULT_CONTINUATION_POLICY = {"max_continuations": 2, "max_rate_limit_resumes": 1}
DEFAULT_RETRY_POLICY = {"max_developer_retries": 1}
DEFAULT_PREFLIGHT_POLICY = {
    "require_estimates": True,
    "max_definition_of_done_items": 5,
    "max_dependencies": 3,
    "max_expected_files": 12,
    "max_expected_services": 1,
    "max_expected_changed_lines": 800,
    "max_expected_context_tokens": 80_000,
}
DEFAULT_RATE_LIMIT_RETRY_SECONDS = 60
MAX_CHECK_EVIDENCE_CHARS = 1_600
CONTINUATION_FACTS_FIELDS = {"dispatch_id", "remaining_definition_of_done", "risks", "dependencies"}
TELEMETRY_FIELDS = {
    "dispatch_id", "session_kind", "input_tokens", "output_tokens", "cache_read_tokens",
    "cache_write_tokens", "max_context_tokens", "tool_calls", "tool_output_bytes", "poll_turns",
    "restart_reason", "recorded_at",
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


def _write_exclusive(ledger: LifecycleLedger, path: Path, value: dict[str, Any]) -> None:
    try:
        ledger.write_immutable(path, value)
    except LedgerError as exc:
        raise CoordinatorError(str(exc)) from exc


def _write_text_exclusive(ledger: LifecycleLedger, path: Path, value: str) -> None:
    try:
        ledger.write_artifact(path, value)
    except LedgerError as exc:
        raise CoordinatorError(str(exc)) from exc


def _write_record(ledger: LifecycleLedger, record: Any) -> None:
    try:
        ledger.write_record(record)
    except LedgerError as exc:
        raise CoordinatorError(str(exc)) from exc


def _replace_record(ledger: LifecycleLedger, record: Any) -> None:
    try:
        ledger.replace_record(record)
    except LedgerError as exc:
        raise CoordinatorError(str(exc)) from exc


def _safe_id(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"(?:batch|dispatch|risk|context-package|checkpoint)-[0-9a-f-]+", value) is None:
        raise CoordinatorError(f"{label} is not a valid coordinator ID")
    return value


def _repo(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "repo", ".")).resolve()


def _state_root(args: argparse.Namespace, repo: Path) -> Path:
    supplied = getattr(args, "state_dir", None)
    return (Path(supplied).resolve() if supplied else repo / STATE_REL).resolve()


SCRATCH_REL = Path(".harness") / "scratch"
AGENT_INBOX_REL = SCRATCH_REL / "inbox"


def _agent_inbox(repo: Path) -> Path:
    """The one canonical place a role writes the JSON it is about to hand to the coordinator.

    Without a declared absolute location a role invents one (``~/reports``, ``~/review-reports``,
    the system temp), so the evidence a human later looks for is scattered outside the project.
    """
    return (repo / AGENT_INBOX_REL).resolve()


NON_ENGLISH_BRIEF_PATTERN = re.compile(r"[\u0400-\u04FF\u0500-\u052F]")


def _reject_non_english(values: Any, field: str) -> None:
    """Hold the language contract where it is machine-checkable: brief text handed to a role.

    Agent-to-agent protocol text is English. A completion report addressed to the coordinator is
    Russian by contract and is deliberately not checked here.
    """
    items = values if isinstance(values, (list, tuple)) else [values]
    for item in items:
        if isinstance(item, str) and NON_ENGLISH_BRIEF_PATTERN.search(item):
            raise CoordinatorError(
                f"{field} is handed to a role as agent-to-agent protocol text and must be written "
                "in English; translate it before creating the batch and keep commands, paths, IDs "
                "and quoted evidence verbatim"
            )


def _prepare_agent_inbox(repo: Path) -> Path:
    """Create the staging directory and keep its contents out of version control."""
    inbox = _agent_inbox(repo)
    inbox.mkdir(parents=True, exist_ok=True)
    ignore = inbox.parent / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n!.gitignore\n", encoding="utf-8")
    return inbox


def _worktree_roots(repo: Path) -> set[Path]:
    """Every checkout of this repository: the main one plus each linked worktree."""
    roots = {repo.resolve()}
    try:
        listing = _git(repo, "worktree", "list", "--porcelain")
    except CoordinatorError:
        return roots
    for line in listing.splitlines():
        if line.startswith("worktree "):
            roots.add(Path(line[len("worktree "):].strip()).resolve())
    return roots


def _agent_authored_file(repo: Path, value: str, label: str) -> Path:
    """Resolve a role-authored payload and refuse anything written outside the project.

    A path under the home directory or the system temp is never the project's audit trail; it is a
    guessed location, and accepting it is what lets evidence drift out of the ledger.
    """
    path = Path(value).expanduser().resolve()
    for root in _worktree_roots(repo):
        try:
            path.relative_to(root)
        except ValueError:
            continue
        return path
    raise CoordinatorError(
        f"{label} must be written inside the repository or one of its worktrees, not at {path}; "
        f"use the canonical staging path {_agent_inbox(repo)}"
    )


def _records_root(root: Path) -> Path:
    try:
        return LifecycleLedger(root).records_root()
    except LedgerError as exc:
        raise CoordinatorError(str(exc)) from exc


@contextmanager
def _ledger_lock(ledger: LifecycleLedger) -> Iterator[None]:
    """Exclusive lock through ``LifecycleLedger.lock()``, translating ``LedgerError`` to
    ``CoordinatorError`` for this call site -- the same translation ``_write_exclusive`` and
    ``_replace_record`` already apply on every write.  Centralising the translation here (rather than
    repeating a ``try/except`` at every one of this module's lock sites) removes the risk of a lock
    site forgetting it and leaking an uncaught ``LedgerError`` into the CLI."""
    try:
        with ledger.lock():
            yield
    except LedgerError as exc:
        raise CoordinatorError(str(exc)) from exc


def _project(repo: Path) -> dict[str, Any]:
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


def _default_config(repo: Path) -> dict[str, Any]:
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


def _config(repo: Path) -> dict[str, Any]:
    if not _configured(repo):
        return _default_config(repo)
    value = _read_object(repo / ".harness/orchestration.json", "project orchestration config")
    _reject_sensitive(value, "project orchestration config")
    problems = health_problems(repo / ".harness/orchestration.json", repo / ".harness/orchestration/roles")
    if problems:
        raise CoordinatorError("invalid project orchestration config: " + "; ".join(problems))
    return value


def _adaptive_continuation_policy(config: dict[str, Any]) -> dict[str, Any]:
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


def _context_advisory(config: dict[str, Any], observed: int | None) -> dict[str, Any]:
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


def _numeric_policy(config: dict[str, Any], key: str, defaults: dict[str, int]) -> dict[str, int]:
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
        minimum = 0 if key == "retry_policy" and name == "max_developer_retries" else 1
        if isinstance(value, int) and not isinstance(value, bool) and value >= minimum:
            resolved[name] = value
    return resolved


def _context_package_policy(config: dict[str, Any]) -> dict[str, int]:
    policy = _numeric_policy(config, "context_package_policy", DEFAULT_CONTEXT_PACKAGE_POLICY)
    if policy["reserved_prompt_tokens"] >= policy["context_window_tokens"]:
        raise CoordinatorError("context_package_policy reserved_prompt_tokens must be below context_window_tokens")
    available = policy["context_window_tokens"] - policy["reserved_prompt_tokens"]
    if policy["max_tokens"] > available:
        raise CoordinatorError("context_package_policy max_tokens exceeds available context after prompt headroom")
    return policy


def _continuation_policy(config: dict[str, Any]) -> dict[str, int]:
    return _numeric_policy(config, "continuation_policy", DEFAULT_CONTINUATION_POLICY)


def _retry_policy(config: dict[str, Any]) -> dict[str, int]:
    return _numeric_policy(config, "retry_policy", DEFAULT_RETRY_POLICY)


def _preflight_policy(config: dict[str, Any]) -> dict[str, Any]:
    policy: dict[str, Any] = dict(DEFAULT_PREFLIGHT_POLICY)
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


def _runtime_snapshot_root(repo: Path) -> Path:
    installed = repo / ".harness/orchestration"
    return installed if installed.is_dir() else MODULE_ROOT


def _harness_runtime_sha256(repo: Path) -> str:
    """Hash the coordinator runtime without volatile state so a batch cannot span an update."""
    root = _runtime_snapshot_root(repo)
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file() and "state" not in item.relative_to(root).parts):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _validate_harness_runtime_snapshot(repo: Path, batch: dict[str, Any]) -> None:
    expected = batch.get("harness_runtime_sha256")
    if expected is None:  # Explicitly supported legacy batch; never rewrite history in place.
        return
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise CoordinatorError("batch has an invalid harness runtime snapshot hash")
    actual = _harness_runtime_sha256(repo)
    if actual != expected:
        raise CoordinatorError(
            "the harness runtime changed after this batch was planned; do not spend a worker on recovery. "
            "Finish with the pinned harness revision or abandon and re-plan the batch."
        )


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


def _fetch_ref_tip(repo: Path, ref: str) -> str:
    """The current commit an integration ref points to on origin, fetched fresh — never a locally
    cached remote-tracking branch, which is exactly the staleness this gate exists to catch."""
    try:
        _git(repo, "fetch", "origin", ref)
    except CoordinatorError as exc:
        raise CoordinatorError(f"could not fetch origin {ref!r}: {exc}") from exc
    return _git(repo, "rev-parse", "--verify", "FETCH_HEAD")


def _required_base_branch(repo: Path) -> str:
    """Unlike `_validate_branch`'s own silent `"master"` default, the base-commit gate has nothing
    safe to fetch when the project config omits `base_branch` — fail loudly instead of pinning
    against a branch the project never named."""
    base = _project(repo).get("base_branch")
    if not _non_empty(base):
        raise CoordinatorError("project config has no usable base_branch for the integration ref fallback")
    return base


def _integration_ref(repo: Path, batch: dict[str, Any]) -> str:
    ref = batch.get("integration_ref")
    if _non_empty(ref):
        return ref
    return _required_base_branch(repo)


def _enforce_base_freshness(repo: Path, root: Path, ledger: LifecycleLedger, batch: dict[str, Any]) -> None:
    """Mandatory re-check, immediately before a review or publish dispatch: the batch's pinned
    integration base must still be the integration ref's current tip. A stale base is cleared only
    by a new developer dispatch (a rebase), never by the coordinator moving this field directly."""
    recorded = batch.get("integration_base_commit")
    if not isinstance(recorded, str) or not recorded:
        raise CoordinatorError("batch has no recorded integration base commit to check freshness against")
    ref = _integration_ref(repo, batch)
    current = _fetch_ref_tip(repo, ref)
    if current == recorded:
        return
    batch["next_action"] = "developer"
    batch["required_next_role"] = "developer"
    batch["retry_candidate_required"] = True
    batch["base_rebase_required"] = True
    _safe_id(batch["batch_id"], "batch")
    _replace_record(ledger, BatchRecord.from_dict(batch))
    raise CoordinatorError(
        f"batch base is stale: origin/{ref} has moved from {recorded} to {current}; "
        "only a new developer rebase dispatch can clear this block"
    )


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


def _test_path_patterns(config: dict[str, Any]) -> list[str]:
    patterns = config.get("test_path_patterns")
    if isinstance(patterns, list) and patterns and all(_non_empty(item) for item in patterns):
        return list(patterns)
    return list(DEFAULT_TEST_PATH_PATTERNS)


def _is_test_path(path: str, patterns: list[str]) -> bool:
    return any(fnmatchcase(path, pattern) for pattern in patterns)


def _role(repo: Path, name: str) -> dict[str, Any]:
    try:
        return load_role_manifest(repo / ".harness/orchestration/roles" / f"{name}.md")
    except ContractError as exc:
        raise CoordinatorError(str(exc)) from exc


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


def _validate_worktree(repo: Path, worktree: str) -> None:
    if not _non_empty(worktree):
        raise CoordinatorError("worktree must be a non-empty string")
    try:
        resolved = Path(worktree).resolve()
    except Exception as exc:
        raise CoordinatorError("worktree is not a valid path") from exc
    
    output = _git(repo, "worktree", "list", "--porcelain")
    paths: set[Path] = set()
    for line in output.splitlines():
        if line.startswith("worktree "):
            paths.add(Path(line.removeprefix("worktree ")).resolve())
            
    if resolved not in paths:
        raise CoordinatorError(f"worktree {worktree!r} is not registered by git worktree")


def _verification_commands(config: dict[str, Any]) -> list[str]:
    commands = config.get("verification_commands")
    return _strings(commands, "verification_commands", allow_empty=True)


def _developer_verification_commands(config: dict[str, Any]) -> list[str]:
    """Focused developer proof, with the historical full-QA list as a safe fallback.

    Older project configs have one verification list.  Keeping that as the fallback preserves their
    existing approval contract, while a project can opt into a narrow developer loop without
    weakening the clean-room QA commands stored separately in ``verification_commands``.
    """
    commands = config.get("developer_verification_commands")
    if commands is None:
        return _verification_commands(config)
    return _strings(commands, "developer_verification_commands", allow_empty=True)


def _worker_attestation_required(config: dict[str, Any]) -> bool:
    value = config.get("worker_attestation_required", False)
    if not isinstance(value, bool):
        raise CoordinatorError("worker_attestation_required must be a boolean")
    return value


def _communication_policy(config: dict[str, Any]) -> dict[str, str]:
    value = config.get("communication_policy", DEFAULT_COMMUNICATION_POLICY)
    if not isinstance(value, dict) or set(value) != COMMUNICATION_POLICY_FIELDS:
        raise CoordinatorError(
            "communication_policy must contain agent_to_agent_language and coordinator_report_language"
        )
    if value != DEFAULT_COMMUNICATION_POLICY:
        raise CoordinatorError(
            "communication_policy must use English for agent communication and Russian for coordinator reports"
        )
    return dict(value)


def _resolve_assignment(
    repo: Path,
    config: dict[str, Any],
    role_name: str,
    zone_name: str,
    runtime_name: str,
    *,
    session_model: object = None,
    session_effort: object = None,
) -> tuple[dict[str, Any], dict[str, Any], str, str, str, str, str]:
    role = _role(repo, role_name)
    if not _configured(repo):
        if zone_name != DEFAULT_ZONE:
            raise CoordinatorError(
                f"without .harness/orchestration.json the only backend zone is {DEFAULT_ZONE!r}"
            )
        if not _non_empty(session_model) or not _non_empty(session_effort):
            raise CoordinatorError(
                "without .harness/orchestration.json the invoking session must supply --model and --effort"
            )
        # No project-owned provider profile exists, so the only honest transport is the invoking
        # session itself; an Orca worker would have no agent to start.
        return (
            role, {"paths": ["**"]}, DEFAULT_PROFILE, session_model.strip(), session_effort.strip(),
            "in-process", runtime_name.strip() if _non_empty(runtime_name) else "session",
        )
    try:
        plan = config.get("assignment_plans", {}).get(role_name)
        resolved_runtime = resolve_runtime_name(plan, runtime_name) if isinstance(plan, dict) else ""
        assignment = resolve_assignment(config, role, role_name, zone_name, resolved_runtime)
    except ContractError as exc:
        raise CoordinatorError(str(exc)) from exc
    return (
        assignment["role"], assignment["zone"], assignment["profile_id"], assignment["model"],
        assignment["effort"], assignment["transport"], resolved_runtime,
    )


def _moment(value: object, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise CoordinatorError(f"{label} is not a readable timestamp") from exc
    if parsed.tzinfo is None:
        raise CoordinatorError(f"{label} must include a timezone")
    return parsed


def _lease_expired(lease: dict[str, Any]) -> bool:
    return _moment(lease["expires_at"], "QA lease expiry") <= datetime.now(timezone.utc)


def _silent_seconds(status: dict[str, Any]) -> int:
    """Seconds since a dispatch last proved it was alive.  This generalizes the QA lane's
    lease-expiry check to every dispatch, whatever transport is carrying it."""
    last = status.get("heartbeat_at") or status.get("updated_at")
    elapsed = datetime.now(timezone.utc) - _moment(last, "dispatch heartbeat")
    return max(0, int(elapsed.total_seconds()))


def _sanitise(text: str) -> str:
    return sanitise(text)


def _concise_evidence(text: str) -> str:
    return concise_evidence(text)


def _load_batch(root: Path, batch_id: str) -> dict[str, Any]:
    return _read_object(_records_root(root) / BatchRecord.directory / f"{_safe_id(batch_id, 'batch')}.json", "batch record")


def _load_dispatch(root: Path, dispatch_id: str) -> dict[str, Any]:
    return _read_object(_records_root(root) / DispatchRecord.directory / f"{_safe_id(dispatch_id, 'dispatch')}.json", "dispatch record")


def _load_dispatch_status(root: Path, dispatch_id: str) -> dict[str, Any]:
    return _read_object(_records_root(root) / DispatchStatusRecord.directory / f"{_safe_id(dispatch_id, 'dispatch')}.json", "dispatch status")


def _load_risk(root: Path, risk_id: str) -> dict[str, Any]:
    return _read_object(_records_root(root) / RiskAssessmentRecord.directory / f"{_safe_id(risk_id, 'risk assessment')}.json", "risk assessment")


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


def _load_checkpoint(root: Path, checkpoint_id: str) -> dict[str, Any]:
    return _read_object(_records_root(root) / CheckpointRecord.directory / f"{_safe_id(checkpoint_id, 'checkpoint')}.json", "checkpoint")


def _latest_checkpoint_for_dispatch(root: Path, batch: dict[str, Any], dispatch_id: str) -> dict[str, Any]:
    entries = [item for item in batch.get("checkpoints", []) if item.get("dispatch_id") == dispatch_id]
    if not entries:
        raise CoordinatorError("dispatch has no recorded checkpoint to resume from")
    checkpoint = _load_checkpoint(root, entries[-1]["checkpoint_id"])
    expected = hashlib.sha256(_canonical(checkpoint).encode("utf-8")).hexdigest()
    if entries[-1].get("record_sha256") != expected:
        raise CoordinatorError("checkpoint failed immutable record integrity check")
    return checkpoint


def _load_context_package(root: Path, package_id: str) -> dict[str, Any]:
    return _read_object(_records_root(root) / ContextPackageRecord.directory / f"{_safe_id(package_id, 'context package')}.json", "context package")


def _validate_context_package(root: Path, batch: dict[str, Any], package: dict[str, Any]) -> None:
    _reject_sensitive(package, "context package")
    if set(package) != CONTEXT_PACKAGE_FIELDS and set(package) != LEGACY_CONTEXT_PACKAGE_FIELDS:
        raise CoordinatorError("context package schema mismatch")
    if package["batch_id"] != batch["batch_id"]:
        raise CoordinatorError("context package does not belong to its batch")
    entry = next(
        (
            item for item in batch.get("context_packages", [])
            if item.get("context_package_id") == package["context_package_id"]
        ),
        None,
    )
    expected = hashlib.sha256(_canonical(package).encode("utf-8")).hexdigest()
    if not entry or entry.get("record_sha256") != expected:
        raise CoordinatorError("context package failed immutable record integrity check")


def _context_package_summary(package: dict[str, Any]) -> dict[str, Any]:
    """Compact portable handoff data for a new role session.

    The full immutable package remains in the ledger exactly once.  The brief carries enough
    bounded navigation to start work without rediscovering files or copying the full diff into
    every model prompt; the pinned commits let a role obtain a precise diff when it truly needs it.
    """
    return {
        "base_commit": package["base_commit"],
        "candidate_commit": package["candidate_commit"],
        "starting_files": package["starting_files"],
        "related_tests": package["related_tests"],
        "precedent_cards": package["precedent_cards"],
        "estimated_tokens": package.get("estimated_tokens"),
    }


def _reusable_context_package(
    root: Path, batch: dict[str, Any], base_commit: str, candidate_commit: str,
) -> dict[str, Any] | None:
    """Return the current batch's shared package for exactly the same pinned diff.

    A package is immutable and role-neutral. Architect and developer therefore share the base
    snapshot, and a resumed worker keeps its brief's exact package ID instead of rebuilding or
    re-reading discovery. Review gets a new package only once the candidate actually changes.
    """
    for entry in reversed(batch.get("context_packages", [])):
        if entry.get("base_commit") != base_commit or entry.get("candidate_commit") != candidate_commit:
            continue
        package = _load_context_package(root, entry.get("context_package_id"))
        _validate_context_package(root, batch, package)
        if package.get("role") == "shared":
            return package
    return None


def _latest_context_package(root: Path, batch: dict[str, Any]) -> dict[str, Any] | None:
    entries = batch.get("context_packages", [])
    if not entries:
        return None
    package = _load_context_package(root, entries[-1]["context_package_id"])
    _validate_context_package(root, batch, package)
    return package


def _context_package_freshness(repo: Path, root: Path, batch: dict[str, Any]) -> dict[str, Any] | None:
    """Shadow-mode evidence only: records whether the batch's latest registered Context Package
    still matches current repository state (its base and the latest accepted developer candidate).
    Never blocks dispatch creation -- roles are not yet restricted to the package."""
    package = _latest_context_package(root, batch)
    if package is None:
        return None
    current_base = batch.get("integration_base_commit") or batch.get("base_commit")
    try:
        current_candidate = _latest_developer_candidate(repo, root, batch)
    except CoordinatorError:
        current_candidate = None
    fresh = package["base_commit"] == current_base and (
        current_candidate is None or package["candidate_commit"] == current_candidate
    )
    return {
        "context_package_id": package["context_package_id"],
        "status": "fresh" if fresh else "stale",
        "checked_at": _now(),
        "registered_base_commit": package["base_commit"],
        "current_base_commit": current_base,
        "registered_candidate_commit": package["candidate_commit"],
        "current_candidate_commit": current_candidate,
    }


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


def _accepted_architect(batch: dict[str, Any]) -> bool:
    return any(
        item.get("role") == "architect"
        and item.get("state") == "reported"
        and isinstance(item.get("decision"), dict)
        and item["decision"].get("decision") in {"accept", "override-warning"}
        for item in batch.get("dispatches", [])
    )


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


def _batch_for_ticket_branch(
    root: Path, ticket: str, branch: str, candidate: str, requested_batch: object = None,
) -> dict[str, Any]:
    """Find the batch whose accepted QA proof is pinned to this candidate.

    A coordinator can retain abandoned planning attempts for the same ticket and issue branch.
    Those records are audit evidence, not competing QA proof, so a current SHA selects the batch
    rather than making PR preparation depend on deleting its history.
    """
    batches_dir = _records_root(root) / "batches"
    if not batches_dir.is_dir():
        raise CoordinatorError("no orchestration batches exist for the ticket branch")
    matches = []
    for path in sorted(batches_dir.glob("*.json")):
        batch = _read_object(path, "batch record")
        if batch.get("ticket") == ticket and batch.get("branch") == branch:
            _validate_batch_integrity(root, batch)
            matches.append(batch)
    if not matches:
        raise CoordinatorError("no orchestration batch matches the ticket and issue branch")
    if requested_batch is not None:
        batch_id = _safe_id(requested_batch, "batch")
        selected = next((batch for batch in matches if batch.get("batch_id") == batch_id), None)
        if selected is None:
            raise CoordinatorError("requested batch does not match the ticket and issue branch")
        return selected
    candidates = []
    for batch in matches:
        try:
            _accepted_qa_for_candidate(root, batch, candidate)
        except CoordinatorError:
            continue
        candidates.append(batch)
    if len(candidates) != 1:
        if not candidates:
            raise CoordinatorError("no batch has accepted green QA evidence for the candidate commit")
        raise CoordinatorError("multiple batches have accepted green QA evidence for the candidate commit; pass --batch")
    return candidates[0]


def qa_evidence(args: argparse.Namespace) -> dict[str, Any]:
    return qa_lane.qa_evidence(args, sys.modules[__name__])


def ledger_status(args: argparse.Namespace) -> dict[str, Any]:
    """Report the selected lifecycle-ledger generation without changing it."""
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        try:
            return ledger.status()
        except LedgerError as exc:
            raise CoordinatorError(str(exc)) from exc


def migrate_ledger(args: argparse.Namespace) -> dict[str, Any]:
    """Explicitly validate legacy state and atomically select its versioned replacement."""
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        try:
            return ledger.migrate()
        except LedgerError as exc:
            raise CoordinatorError(str(exc)) from exc


def reset_ledger(args: argparse.Namespace) -> dict[str, Any]:
    """Select an empty generation only after an explicit confirmation and no active batch."""
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        try:
            return ledger.reset(args.confirm)
        except LedgerError as exc:
            raise CoordinatorError(str(exc)) from exc


def clean_ledger(args: argparse.Namespace) -> dict[str, Any]:
    """Safely remove orphaned dispatch evidence from the ledger state."""
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        try:
            return ledger.clean()
        except LedgerError as exc:
            raise CoordinatorError(str(exc)) from exc


def _validate_batch_integrity(root: Path, batch: dict[str, Any]) -> None:
    plan = _read_object(
        _records_root(root) / "plans" / f"{_safe_id(batch.get('batch_id'), 'batch')}.json", "immutable batch plan",
    )
    for field in ("approval_policy", "communication_policy"):
        if (field in batch) != (field in plan):
            raise CoordinatorError("batch record is incomplete")
    for fields in (PLAN_FIELDS, LEGACY_PLAN_FIELDS):
        if all(field in batch for field in fields) and all(field in plan for field in fields):
            if {field: batch[field] for field in fields} == {field: plan[field] for field in fields}:
                return
            raise CoordinatorError("batch record does not match its immutable plan")
    # Batch records created before approval_policy was added are still immutable and safe to
    # continue: the transition code resolves the project default when the field is absent. Accept
    # this historical shape only when both records omit the field; a one-sided omission indicates
    # corruption or an incomplete manual migration and must remain blocked.
    if (
        all(field in batch for field in PRE_APPROVAL_LEGACY_PLAN_FIELDS)
        and all(field in plan for field in PRE_APPROVAL_LEGACY_PLAN_FIELDS)
        and "approval_policy" not in batch
        and "approval_policy" not in plan
        and {
            field: batch[field] for field in PRE_APPROVAL_LEGACY_PLAN_FIELDS
        } == {
            field: plan[field] for field in PRE_APPROVAL_LEGACY_PLAN_FIELDS
        }
    ):
        return
    raise CoordinatorError("batch record is incomplete")


def _validate_dispatch(repo: Path, config: dict[str, Any], root: Path, batch: dict[str, Any], dispatch: dict[str, Any]) -> None:
    _validate_harness_runtime_snapshot(repo, batch)
    _reject_sensitive(dispatch, "dispatch record")
    # Briefs are immutable. A record created before worker attestation was introduced keeps its
    # historical shape and is treated as an explicit legacy opt-out instead of being rewritten.
    pre_summary_fields = DISPATCH_FIELDS - {"context_package_summary"}
    legacy_fields = DISPATCH_FIELDS - {
        "context_package_summary", "worker_attestation_required", "snapshot_commit", "communication_policy",
    }
    # A brief written before the canonical reporting path existed keeps its historical shape, the
    # same way every earlier field addition is treated here.
    accepted = {
        frozenset(fields) for fields in (DISPATCH_FIELDS, pre_summary_fields, legacy_fields)
    }
    accepted |= {fields - {"report_staging_path"} for fields in set(accepted)}
    # The role tool policy and context budget were added together, so a brief holds both or neither.
    accepted |= {fields - {"allowed_tools", "context_budget"} for fields in set(accepted)}
    if frozenset(dispatch) not in accepted:
        raise CoordinatorError("dispatch record schema mismatch")
    if dispatch.get("state") != "approved":
        raise CoordinatorError("dispatch record is not an approved immutable brief")
    if dispatch.get("batch_id") != batch.get("batch_id"):
        raise CoordinatorError("dispatch record does not belong to its batch")
    expected_communication_policy = batch.get("communication_policy", DEFAULT_COMMUNICATION_POLICY)
    if dispatch.get("communication_policy", expected_communication_policy) != expected_communication_policy:
        raise CoordinatorError("dispatch communication policy does not match its batch")
    if dispatch.get("purpose") not in DISPATCH_PURPOSES:
        raise CoordinatorError("dispatch record has an invalid purpose")
    package_id = dispatch.get("context_package_id")
    package_sha = dispatch.get("context_package_sha256")
    if dispatch["role"] in {"architect", "developer", "code-review"}:
        if not isinstance(package_id, str) or not isinstance(package_sha, str):
            raise CoordinatorError("architect, developer and code-review briefs require a Context Package reference")
        package = _load_context_package(root, package_id)
        _validate_context_package(root, batch, package)
        if package.get("role") not in {"shared", dispatch["role"]}:
            raise CoordinatorError("dispatch Context Package is neither shared nor assigned to this role")
        if hashlib.sha256(_canonical(package).encode("utf-8")).hexdigest() != package_sha:
            raise CoordinatorError("dispatch Context Package hash does not match its immutable package")
        summary = dispatch.get("context_package_summary")
        if summary is not None and summary != _context_package_summary(package):
            raise CoordinatorError("dispatch Context Package summary does not match its immutable package")
    elif package_id is not None or package_sha is not None or dispatch.get("context_package_summary") is not None:
        raise CoordinatorError("only architect, developer and code-review briefs may reference a Context Package")
    entry = next((item for item in batch.get("dispatches", []) if item.get("dispatch_id") == dispatch.get("dispatch_id")), None)
    if not entry or entry.get("brief_sha256") != hashlib.sha256(_canonical(dispatch).encode("utf-8")).hexdigest():
        raise CoordinatorError("dispatch record failed immutable brief integrity check")
    for field in ("ticket", "branch", "worktree", "zone", "definition_of_done", "prohibited_changes", "required_gates", "dependencies"):
        if dispatch[field] != batch[field]:
            raise CoordinatorError(f"dispatch record {field} does not match its batch")
    expected_commands = (
        batch["developer_verification_commands"]
        if dispatch["role"] == "developer" and dispatch["purpose"] == "work"
        else batch["verification_commands"]
    )
    if dispatch["verification_commands"] != expected_commands:
        raise CoordinatorError("dispatch record verification_commands do not match its batch and role")
    if _configured(repo):
        try:
            validate_brief_policy(
                dispatch,
                _project(repo),
                config,
                repo / ".harness/orchestration/roles",
            )
        except ContractError as exc:
            raise CoordinatorError(str(exc)) from exc
    else:
        _validate_branch(repo, dispatch["branch"])
        # In zero-config mode the brief itself is the only record of the session-supplied runtime,
        # so it is replayed here; brief_sha256 above already protects it from being edited.
        role, zone, profile_id, model, effort, transport, resolved_runtime = _resolve_assignment(
            repo, config, dispatch["role"], batch["zone"], dispatch["resolved_runtime"],
            session_model=dispatch["resolved_model"], session_effort=dispatch["resolved_effort"],
        )
        if (
            dispatch["access"] != role["mode"]
            or dispatch["resolved_provider_profile"] != profile_id
            or dispatch["resolved_model"] != model
            or dispatch["resolved_effort"] != effort
            or dispatch["resolved_transport"] != transport
            or dispatch["resolved_runtime"] != resolved_runtime
        ):
            raise CoordinatorError("dispatch record does not match the role assignment")
        expected_paths = zone["paths"] if role["mode"] == "write" else []
        if dispatch["write_paths"] != expected_paths:
            raise CoordinatorError("dispatch record write paths do not match the role boundary")
    if "allowed_tools" in dispatch:
        if dispatch["allowed_tools"] != resolve_allowed_tools(config, dispatch["role"], dispatch["access"]):
            raise CoordinatorError("dispatch record allowed_tools do not match the project tool policy")
        if dispatch["context_budget"] != _adaptive_continuation_policy(config)["context_limit"]:
            raise CoordinatorError("dispatch record context_budget does not match the project context limit")
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
    for path in sorted((_records_root(root) / "batches").glob("batch-*.json")):
        other = _read_object(path, "batch record")
        if other.get("batch_id") == batch.get("batch_id") or other.get("state") not in {"active", "awaiting-approval"}:
            continue
        active += 1
        if other.get("zone") == batch.get("zone"):
            raise CoordinatorError("another active batch already owns this backend zone")
    if active >= budget:
        raise CoordinatorError("concurrency_budget is exhausted")


def _human_approval_gate(config: dict[str, Any]) -> str:
    gate = config.get("human_approval_gate", "trusted")
    if gate not in {"trusted", "tty"}:
        raise CoordinatorError("human_approval_gate must be trusted or tty")
    return gate


def _confirm_on_terminal(approved_by: str) -> None:
    """Take the approval from the controlling terminal instead of from the calling session.

    `--approved-by` and `--approved-at` are only claims: a coordinator session holding a shell can
    type them itself, which is exactly how a gate gets advanced without the human ever seeing the
    decision packet. A line read from the real terminal cannot be produced by a non-interactive
    tool call, so under this gate the approval is the operator's or it does not happen.
    """
    prompt = f"Type 'approve' to record this decision as {approved_by}: "
    try:
        if os.name == "nt":
            stream = open("CONIN$", "r", encoding="utf-8")  # noqa: SIM115 - closed below
            sink = open("CONOUT$", "w", encoding="utf-8")  # noqa: SIM115 - closed below
        else:
            stream = open("/dev/tty", "r", encoding="utf-8")  # noqa: SIM115 - closed below
            sink = open("/dev/tty", "w", encoding="utf-8")  # noqa: SIM115 - closed below
    except OSError as exc:
        raise CoordinatorError(
            "human_approval_gate is 'tty': this decision must be confirmed by a human on the "
            "terminal, and this session has none. Show the decision packet and have the operator "
            "run the same command in their own terminal."
        ) from exc
    try:
        sink.write(prompt)
        sink.flush()
        answer = stream.readline().strip().lower()
    finally:
        stream.close()
        sink.close()
    if answer != "approve":
        raise CoordinatorError("human approval was not confirmed on the terminal")


def _approval(args: argparse.Namespace) -> dict[str, str]:
    approved_by = getattr(args, "approved_by", None)
    approved_at = getattr(args, "approved_at", None)
    if not _non_empty(approved_by) or not _non_empty(approved_at):
        raise CoordinatorError("explicit coordinator approval requires approved-by and approved-at")
    result = {"approved_by": approved_by.strip(), "approved_at": approved_at.strip()}
    _reject_sensitive(result, "coordinator approval")
    try:
        config = _config(_repo(args))
    except CoordinatorError:
        config = {}
    if _human_approval_gate(config) == "tty":
        _confirm_on_terminal(result["approved_by"])
    return result


def _approval_policy(config: dict[str, Any]) -> str:
    policy = config.get("approval_policy", "manual_all")
    if policy not in {"manual_all", "milestone", "low_risk"}:
        raise CoordinatorError("approval_policy must be manual_all, milestone or low_risk")
    return policy


def _dispatch_approval(
    args: argparse.Namespace, batch: dict[str, Any], config: dict[str, Any], role: str,
    purpose: str, risk: dict[str, Any] | None,
) -> dict[str, str]:
    """Apply a project-approved continuation only outside the preserved risk milestones."""
    if _non_empty(getattr(args, "approved_by", None)) or _non_empty(getattr(args, "approved_at", None)):
        return _approval(args)
    policy = batch.get("approval_policy", _approval_policy(config))
    risk_triggered = bool(risk and risk.get("matched_triggers"))
    milestone = purpose == "publish" or role == "qa" or risk_triggered or batch.get("risk_reassessment_required")
    if policy == "manual_all" or milestone:
        raise CoordinatorError("this transition requires --approved-by and --approved-at under its approval policy")
    if policy == "low_risk":
        zones = config.get("low_risk_zones", [])
        if batch["zone"] not in zones:
            raise CoordinatorError("low_risk continuation requires the batch zone in low_risk_zones")
    return {"approved_by": f"policy:{policy}", "approved_at": _now()}


def preflight_dispatch(args: argparse.Namespace) -> dict[str, Any]:
    """Render one deterministic dispatch proposal without writing a brief or starting a worker."""
    repo = _repo(args)
    root = _state_root(args, repo)
    config = _config(repo)
    if not _configured(repo):
        raise CoordinatorError("dispatch preflight requires a project-owned .harness/orchestration.json")
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        candidate = _candidate_commit(repo, args.candidate_commit) if args.candidate_commit else None
        if candidate is None:
            try:
                candidate = _latest_developer_candidate(repo, root, batch)
            except CoordinatorError:
                candidate = None
        snapshot = candidate or batch["base_commit"]
        package = _latest_context_package(root, batch)
        package_pointer: dict[str, Any] = {}
        if package is not None:
            package_pointer = {
                "package_id": package["context_package_id"],
                "sha256": hashlib.sha256(_canonical(package).encode("utf-8")).hexdigest(),
                "freshness": _context_package_freshness(repo, root, batch),
            }
        checks = (
            batch["developer_verification_commands"]
            if args.role == "developer" else batch["verification_commands"]
        )
        state = {
            "repo": str(repo), "config": config, "branch": batch["branch"], "worktree": batch["worktree"],
            "zone": batch["zone"], "base_sha": batch["base_commit"], "candidate_sha": candidate,
            "snapshot_sha": snapshot, "integration_ref": _integration_ref(repo, batch), "runtime": args.runtime,
            "mandatory_checks": checks, "starting_files": package_pointer,
            "architecture_decision": batch.get("architecture_decision"),
            "affected_symbols": batch.get("affected_symbols", []),
            "related_tests": package.get("related_tests", []) if package else [],
            "pinned_diff": package.get("diff", "") if package else "",
            "prior_findings": batch.get("prior_findings", []),
        }
    try:
        prepared = prepare_dispatch(batch["ticket"], args.role, state)
    except PreflightError as exc:
        raise CoordinatorError(str(exc)) from exc
    return prepared.to_dict()


def decision_packet(args: argparse.Namespace) -> dict[str, Any]:
    """Return concise approval evidence, with immutable report and diff paths kept in the ledger."""
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        entry = None
        if args.dispatch:
            entry = next((item for item in batch.get("dispatches", []) if item.get("dispatch_id") == args.dispatch), None)
            if entry is None:
                raise CoordinatorError("decision packet dispatch does not belong to this batch")
        else:
            pending = [item for item in batch.get("dispatches", []) if item.get("state") == "reported" and "decision" not in item]
            entry = pending[0] if len(pending) == 1 else None
        if entry is None:
            return {
                "batch_id": batch["batch_id"], "ticket": batch["ticket"], "action": "approve next dispatch",
                "branch": batch["branch"], "worktree": batch["worktree"], "base_sha": batch["base_commit"],
                "snapshot_sha": batch["base_commit"], "candidate_sha": None, "changed_files": [], "checks": [], "risks": "not assessed yet",
                "blockers": "none", "report": None, "diff": None,
                "worker_attestation_required": _worker_attestation_required(_config(repo)),
                "approval_reason": "the next immutable dispatch has not been created",
                "options": ["accept", "block", "full review"],
            }
        dispatch = _load_dispatch(root, entry["dispatch_id"])
        report = _pending_report(root, batch, entry) if entry.get("state") == "reported" else None
        candidate = dispatch.get("candidate_commit")
        changed = report.get("changed_files", []) if report else dispatch.get("review_scope", [])
        risk = _risk_for_candidate(root, batch, candidate) if isinstance(candidate, str) else None
        return {
            "batch_id": batch["batch_id"], "ticket": batch["ticket"],
            "action": "decide completion report" if report else "approve and send dispatch",
            "dispatch_id": dispatch["dispatch_id"], "role": dispatch["role"], "runtime": dispatch["resolved_runtime"],
            "branch": dispatch["branch"], "worktree": dispatch["worktree"], "base_sha": batch["base_commit"],
            "snapshot_sha": dispatch.get("snapshot_commit", batch["base_commit"]), "candidate_sha": candidate, "changed_files": changed,
            "scope": dispatch["write_paths"] or dispatch.get("review_scope", []),
            "worker_attestation_required": dispatch.get("worker_attestation_required", False),
            "summary": report.get("output") if report else "immutable brief prepared",
            "checks": report.get("checks_run", []) if report else [
                {"command": command, "result": "pending"} for command in dispatch["verification_commands"]
            ],
            "risks": report.get("risks") if report else (risk.get("matched_triggers") if risk else "not assessed yet"),
            "blockers": report.get("blockers") if report else "none",
            "report": str(_records_root(root) / entry["report"]) if report else None,
            "diff": f"git diff {batch['base_commit']}..{candidate}" if candidate else None,
            "approval_reason": "a human decision is required before the ledger may advance this gate",
            "options": ["accept", "retry", "block", "full review", "delta-review"],
        }


def assess_risk(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    root = _state_root(args, repo)
    known = _risk_triggers(repo)
    candidate = _candidate_commit(repo, args.candidate_commit)
    changed_files = [item.replace("\\", "/") for item in _strings(args.changed_file, "changed_files")]
    developer_triggers = _validate_trigger_names(args.developer_trigger or [], "developer_triggers", known)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
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
        _safe_id(risk["risk_assessment_id"], "risk assessment")
        _write_record(ledger, RiskAssessmentRecord.from_dict(risk))
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
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))
    return risk


def _persist_context_package(
    repo: Path,
    root: Path,
    ledger: LifecycleLedger,
    batch: dict[str, Any],
    *,
    role: str,
    snapshot: str,
    inclusion_reason: str,
    min_starting_files: int = 1,
    max_starting_files: int = 10,
    max_package_size_bytes: int | None = None,
    max_package_tokens: int | None = None,
    symbol_graph_depth: int | None = None,
    max_related_tests: int | None = None,
) -> dict[str, Any]:
    """Build once and register a reusable Context Package for one pinned diff."""
    package_base = batch.get("integration_base_commit") or batch["base_commit"]
    reusable = _reusable_context_package(root, batch, package_base, snapshot)
    if reusable is not None:
        return reusable
    if role != "shared":
        raise CoordinatorError("automatic Context Packages must be shared; role focus belongs in the immutable brief")
    policy = _context_package_policy(_config(repo))
    # `max_package_tokens` is documented as an optional *stricter* ceiling (see coordinator_cli.py
    # --max-package-tokens help text). Silently honouring a caller-supplied value above the
    # project's configured budget is exactly how a review context package was pushed to ~130k
    # tokens against an 80k policy without any recorded decision; fail loudly instead.
    if max_package_tokens is not None and max_package_tokens > policy["max_tokens"]:
        raise CoordinatorError(
            f"--max-package-tokens={max_package_tokens} exceeds the configured "
            f"context_package_policy.max_tokens={policy['max_tokens']}; raise context_package_policy.max_tokens "
            "in project orchestration config instead of overriding it ad hoc per dispatch"
        )
    token_limit = max_package_tokens if max_package_tokens is not None else policy["max_tokens"]
    depth = symbol_graph_depth if symbol_graph_depth is not None else policy["symbol_graph_depth"]
    related_tests_cap = max_related_tests if max_related_tests is not None else policy["max_related_tests"]
    seed_files = [
        "AGENTS.md", "README.md", ".harness/orchestration/roles/_common.md",
        ".harness/orchestration/contract.py",
    ]
    try:
        built = build_context_package(
            repo, package_base, snapshot,
            symbol_graph_depth=depth, min_starting_files=min_starting_files,
            max_starting_files=max_starting_files, max_package_size_bytes=max_package_size_bytes,
            max_package_tokens=token_limit, max_related_tests=related_tests_cap,
            seed_paths=seed_files,
        )
    except ContextPackageError as exc:
        raise CoordinatorError(str(exc)) from exc
    package = {
        "context_package_id": f"context-package-{uuid.uuid4()}", "batch_id": batch["batch_id"],
        "base_commit": built.base_commit, "candidate_commit": built.candidate_commit, "diff": built.diff,
        "starting_files": [asdict(item) for item in built.starting_files], "symbol_graph": built.symbol_graph,
        "related_tests": built.related_tests, "precedent_cards": [asdict(item) for item in built.precedent_cards],
        "file_hashes": built.file_hashes, "size_bytes": built.size_bytes, "created_at": _now(),
        "estimated_tokens": built.estimated_tokens, "role": "shared", "inclusion_reason": inclusion_reason,
    }
    _reject_sensitive(package, "context package")
    _safe_id(package["context_package_id"], "context package")
    _write_record(ledger, ContextPackageRecord.from_dict(package))
    batch.setdefault("context_packages", []).append({
        "context_package_id": package["context_package_id"], "base_commit": package["base_commit"],
        "candidate_commit": package["candidate_commit"], "role": "shared",
        "record_sha256": hashlib.sha256(_canonical(package).encode("utf-8")).hexdigest(),
    })
    return package


def register_context_package(args: argparse.Namespace) -> dict[str, Any]:
    """Build one Context Package for the batch's accepted developer candidate and register it as a
    new immutable, versioned, hashed ledger record -- the same ownership pattern as a risk
    assessment. Read-only over the repository; writes no coordinator or batch state beyond the
    package itself and its pointer entry."""
    repo = _repo(args)
    root = _state_root(args, repo)
    candidate = _candidate_commit(repo, args.candidate_commit)
    requested_role = getattr(args, "role", "shared")
    if requested_role != "shared":
        raise CoordinatorError("Context Packages are batch-shared; role-specific focus stays in the dispatch brief")
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        if batch.get("state") != "awaiting-approval":
            raise CoordinatorError("a context package requires a batch awaiting coordinator approval")
        base = batch.get("base_commit")
        if args.base_commit:
            requested_base = _candidate_commit(repo, args.base_commit)
            if requested_base != base:
                raise CoordinatorError("context package base must match the batch-captured base commit")
        if candidate != _latest_developer_candidate(repo, root, batch):
            raise CoordinatorError("candidate commit does not match the accepted developer report")
        package = _persist_context_package(
            repo, root, ledger, batch, role="shared", snapshot=candidate,
            inclusion_reason=getattr(args, "inclusion_reason", "manual immutable context registration"),
            min_starting_files=args.min_starting_files, max_starting_files=args.max_starting_files,
            max_package_size_bytes=args.max_package_size_bytes,
            max_package_tokens=getattr(args, "max_package_tokens", None),
            symbol_graph_depth=getattr(args, "symbol_graph_depth", None),
            max_related_tests=getattr(args, "max_related_tests", None),
        )
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))
    return package


def _scope_values(args: argparse.Namespace, name: str) -> list[str]:
    value = getattr(args, name, None)
    if value is None:
        return []
    return _strings(value, name, allow_empty=True)


def _expected_positive(args: argparse.Namespace, name: str) -> int | None:
    value = getattr(args, name, None)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CoordinatorError(f"{name.replace('_', '-')} must be a positive integer when provided")
    return value


def _scope_preflight(
    config: dict[str, Any], ticket: str, zone: str, definition_of_done: list[str], dependencies: list[str],
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Reject an oversized ticket before a batch, worktree dispatch, or model session exists."""
    policy = _preflight_policy(config)
    expected_files = sorted(set(_scope_values(args, "expected_file")))
    expected_services = sorted(set(_scope_values(args, "expected_service")))
    expected_changed_lines = _expected_positive(args, "expected_changed_lines")
    supplied_context_tokens = _expected_positive(args, "expected_context_tokens")
    real_dependencies = [item for item in dependencies if item != "none"]
    missing: list[str] = []
    if policy["require_estimates"]:
        if not expected_files:
            missing.append("--expected-file")
        if not expected_services:
            missing.append("--expected-service")
        if expected_changed_lines is None:
            missing.append("--expected-changed-lines")
    if missing:
        raise CoordinatorError(
            "batch preflight requires " + ", ".join(missing)
            + "; split the ticket or declare a bounded expected scope before any model dispatch"
        )
    # A deterministic conservative admission estimate.  It prevents a tiny-looking line count
    # spread across many files from escaping the same context budget.  A caller can supply a
    # stricter observed estimate, but cannot lower this floor.
    derived_context_tokens = (
        (expected_changed_lines or 0) * 20 + len(expected_files) * 2_000
    )
    expected_context_tokens = max(supplied_context_tokens or 0, derived_context_tokens)
    problems: list[str] = []
    checks = {
        "definition_of_done_items": (len(definition_of_done), policy["max_definition_of_done_items"]),
        "dependencies": (len(real_dependencies), policy["max_dependencies"]),
        "expected_files": (len(expected_files), policy["max_expected_files"]),
        "expected_services": (len(expected_services), policy["max_expected_services"]),
        "expected_changed_lines": (expected_changed_lines or 0, policy["max_expected_changed_lines"]),
        "expected_context_tokens": (expected_context_tokens, policy["max_expected_context_tokens"]),
    }
    for label, (actual, limit) in checks.items():
        if actual > limit:
            problems.append(f"{label}={actual} exceeds {limit}")
    if problems:
        raise CoordinatorError(
            "batch preflight rejected this ticket: " + "; ".join(problems)
            + ". Split it with /to-tickets before creating a batch."
        )
    return {
        "ticket": ticket,
        "zone": zone,
        "policy": policy,
        "definition_of_done_items": len(definition_of_done),
        "dependencies": real_dependencies,
        "expected_files": expected_files,
        "expected_services": expected_services,
        "expected_changed_lines": expected_changed_lines or 0,
        "expected_context_tokens": expected_context_tokens,
        "status": "pass",
        "checked_at": _now(),
    }


def preflight_batch(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    config = _config(repo)
    ticket = getattr(args, "ticket", None)
    zone = getattr(args, "zone", None)
    if not _non_empty(ticket) or not _non_empty(zone):
        raise CoordinatorError("ticket and zone must be non-empty strings")
    definition_of_done = _strings(getattr(args, "definition_of_done", None), "definition_of_done")
    dependencies = _strings(getattr(args, "dependency", None) or ["none"], "dependencies")
    return _scope_preflight(config, ticket.strip(), zone.strip(), definition_of_done, dependencies, args)


def create_batch(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    config = _config(repo)
    ticket = getattr(args, "ticket", None)
    branch = getattr(args, "branch", None)
    worktree = getattr(args, "worktree", None)
    zone = getattr(args, "zone", None)
    integration_ref = getattr(args, "integration_ref", None)
    dod = _strings(getattr(args, "definition_of_done", None), "definition_of_done")
    prohibited = _strings(getattr(args, "prohibited_change", None), "prohibited_changes")
    dependencies = _strings(getattr(args, "dependency", None) or ["none"], "dependencies")
    if not _non_empty(ticket) or not _non_empty(zone):
        raise CoordinatorError("ticket and zone must be non-empty strings")
    _validate_branch(repo, branch)
    _validate_worktree(repo, worktree)
    if not isinstance(config.get("backend_zones"), dict) or zone not in config["backend_zones"]:
        raise CoordinatorError(f"unknown backend zone {zone!r}")
    # Nullable for epic-less tasks: falls back to the project's base_branch, the same field
    # `_validate_branch` falls back to, rather than inventing a second convention.
    fetch_ref = integration_ref.strip() if _non_empty(integration_ref) else _required_base_branch(repo)
    pinned_base = _fetch_ref_tip(repo, fetch_ref)
    _reject_non_english(dod, "definition_of_done")
    _reject_non_english(prohibited, "prohibited_changes")
    scope_preflight = _scope_preflight(config, ticket.strip(), zone.strip(), dod, dependencies, args)
    record = {
        "batch_id": f"batch-{uuid.uuid4()}",
        "created_at": _now(),
        "base_commit": pinned_base,
        "integration_ref": integration_ref.strip() if _non_empty(integration_ref) else None,
        "integration_base_commit": pinned_base,
        "branch_start_commit": _head_commit(repo),
        "state": "planned",
        "ticket": ticket.strip(),
        "branch": branch.strip(),
        "worktree": worktree.strip(),
        "zone": zone.strip(),
        "definition_of_done": dod,
        "prohibited_changes": prohibited,
        "developer_verification_commands": _developer_verification_commands(config),
        "verification_commands": _verification_commands(config),
        "required_gates": _strings(getattr(args, "required_gate", None) or ["none"], "required_gates"),
        "dependencies": dependencies,
        "approval_policy": _approval_policy(config),
        "communication_policy": _communication_policy(config),
        "scope_preflight": scope_preflight,
        "harness_runtime_sha256": _harness_runtime_sha256(repo),
        "dispatches": [],
        "risk_assessments": [],
        "risk_escalations": [],
        "risk_reassessment_required": False,
    }
    _reject_sensitive(record, "batch")
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        try:
            ledger.ensure()
        except LedgerError as exc:
            raise CoordinatorError(str(exc)) from exc
        _safe_id(record["batch_id"], "batch")
        _write_record(ledger, PlanRecord.from_dict({field: record[field] for field in PLAN_FIELDS}))
        _write_record(ledger, BatchRecord.from_dict(record))
    return record


def approve_batch(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        record = _load_batch(root, args.batch)
        _validate_batch_integrity(root, record)
        if record.get("state") != "planned":
            raise CoordinatorError("only a planned batch can receive its planning approval")
        updated = _vo_replace(
            BatchRecord.from_dict(record), coordinator_approval=_approval(args), state="awaiting-approval",
        )
        _safe_id(updated.batch_id, "batch")
        _replace_record(ledger, updated)
        record = updated.to_dict()
    return record


def _pending_report(root: Path, batch: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    report_path = entry.get("report")
    if not isinstance(report_path, str):
        raise CoordinatorError("reported dispatch has no completion report")
    report = _read_object(_records_root(root) / report_path, "completion report")
    expected = entry.get("report_sha256")
    actual = hashlib.sha256(_canonical(report).encode("utf-8")).hexdigest()
    if not isinstance(expected, str) or expected != actual:
        raise CoordinatorError("completion report failed immutable integrity check")
    return report


def _developer_retry_count(batch: dict[str, Any]) -> int:
    """Count approved retry decisions, not ordinary initial developer dispatches."""
    return sum(
        1
        for decision in batch.get("coordinator_decisions", [])
        if decision.get("decision") == "retry" and decision.get("next_role") == "developer"
    )


def _continuation_counts(batch: dict[str, Any], dispatch_id: str) -> tuple[int, int]:
    decisions = [
        decision for decision in batch.get("coordinator_decisions", [])
        if decision.get("dispatch_id") == dispatch_id and decision.get("decision") in {"continue", "continue-automatic"}
    ]
    automatic = sum(1 for decision in decisions if decision.get("decision") == "continue-automatic")
    return len(decisions), automatic


def _review_severity(review: dict[str, Any]) -> dict[str, str]:
    return {axis: review[axis]["severity"] for axis in ("standards", "spec")}


def decide_batch(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
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
        if args.decision == "retry":
            retry_policy = _retry_policy(_config(repo))
            if _developer_retry_count(batch) >= retry_policy["max_developer_retries"]:
                raise CoordinatorError(
                    "developer retry budget is exhausted for this batch; split, block, or re-plan instead of starting another worker"
                )
        decision = {
            "decision": args.decision,
            "approved_by": _approval(args)["approved_by"],
            "approved_at": _approval(args)["approved_at"],
            "note": args.note.strip() if _non_empty(args.note) else "none",
        }
        pending[0]["decision"] = decision
        decision_entry = {"dispatch_id": pending[0]["dispatch_id"], **decision}
        if args.decision == "retry":
            decision_entry["next_role"] = "developer"
        batch.setdefault("coordinator_decisions", []).append(decision_entry)
        if args.decision == "retry":
            batch["required_next_role"] = "developer"
            batch["retry_candidate_required"] = True
            batch["next_action"] = "developer-retry"
        elif args.decision in {"accept", "override-warning"}:
            if report["role"] == "developer":
                if batch.get("base_rebase_required"):
                    ref = _integration_ref(repo, batch)
                    batch["integration_base_commit"] = _fetch_ref_tip(repo, ref)
                    batch["base_rebase_required"] = False
                if dispatch.get("purpose") == "publish":
                    batch.pop("next_action", None)
                    batch["state"] = "completed"
                else:
                    batch["next_action"] = "risk-assessment"
            elif report["role"] == "architect":
                batch["next_action"] = "developer"
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
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))
    return batch


def _settled(entry: dict[str, Any]) -> bool:
    """A dispatch is settled once nothing further can happen to it.

    That is either a report the coordinator has decided on, or an abandonment — both are terminal.
    Anything else, including a dispatch blocked on a model mismatch, is still open.
    """
    if entry.get("state") in {"abandoned", "cancelled"}:
        return True
    return entry.get("state") == "reported" and isinstance(entry.get("decision"), dict)


def list_batches(args: argparse.Namespace) -> dict[str, Any]:
    """Inventory of every batch the coordinator holds, with what is still open in each.

    Without this there is no way to find the leftovers of an earlier attempt short of reading the
    state directory by hand, which is exactly how hand-edited state starts.
    """
    repo = _repo(args)
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        batches = []
        for path in sorted((_records_root(root) / "batches").glob("batch-*.json")):
            batch = _read_object(path, "batch record")
            dispatches = batch.get("dispatches", [])
            open_dispatches = [item["dispatch_id"] for item in dispatches if not _settled(item)]
            state = batch.get("state")
            if args.ticket and batch.get("ticket") != args.ticket:
                continue
            if args.state and state != args.state:
                continue
            if args.open and state in TERMINAL_BATCH_STATES:
                continue
            batches.append({
                "batch_id": batch.get("batch_id"),
                "ticket": batch.get("ticket"),
                "branch": batch.get("branch"),
                "zone": batch.get("zone"),
                "state": state,
                "created_at": batch.get("created_at"),
                "terminal": state in TERMINAL_BATCH_STATES,
                "dispatches": len(dispatches),
                "open_dispatches": open_dispatches,
                "next_action": batch.get("next_action"),
            })
    return {"batches": batches}


def abandon_batch(args: argparse.Namespace) -> dict[str, Any]:
    """Close a batch that can no longer reach a decision, with a recorded reason.

    A dispatch whose worker died before confirming its model can never report, and a batch with no
    pending report can never be decided — so an interrupted attempt would otherwise stay open for
    good, and the only way out was editing the state files by hand. This is that way out, kept inside
    the audit trail: nothing is deleted, the open dispatches are named, and the reason is stored
    beside the approval.
    """
    repo = _repo(args)
    root = _state_root(args, repo)
    approval = _approval(args)
    reason = args.reason.strip() if _non_empty(args.reason) else ""
    if not reason:
        raise CoordinatorError("abandoning a batch requires a recorded reason")
    _reject_sensitive({"reason": reason}, "abandon reason")
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        open_dispatches = [item["dispatch_id"] for item in batch.get("dispatches", []) if not _settled(item)]
        if batch.get("state") in TERMINAL_BATCH_STATES and not open_dispatches:
            raise CoordinatorError(f"batch is already {batch['state']} and has nothing open to close")
        # A terminal batch that still carries an open dispatch is a repair case: its state was moved
        # without closing what it held, and that dispatch would otherwise be surfaced as live for
        # ever. Closing the remainder is exactly this command's job.
        moment = _now()
        for entry in batch.get("dispatches", []):
            if _settled(entry):
                continue
            entry["state"] = "abandoned"
            status_path = _records_root(root) / DispatchStatusRecord.directory / f"{_safe_id(entry['dispatch_id'], 'dispatch')}.json"
            if status_path.exists():
                status = _load_dispatch_status(root, entry["dispatch_id"])
                status.update({"state": "abandoned", "updated_at": moment})
                _replace_record(ledger, DispatchStatusRecord.from_dict(status))
        batch["state"] = "failed"
        batch.pop("next_action", None)
        batch.pop("required_next_role", None)
        batch["abandoned"] = {
            "approved_by": approval["approved_by"],
            "approved_at": approval["approved_at"],
            "abandoned_at": moment,
            "reason": reason,
            "open_dispatches": open_dispatches,
        }
        batch.setdefault("coordinator_decisions", []).append({
            "decision": "abandon",
            "approved_by": approval["approved_by"],
            "approved_at": approval["approved_at"],
            "note": reason,
        })
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))
    return {
        "batch_id": batch["batch_id"],
        "ticket": batch["ticket"],
        "state": "failed",
        "abandoned_dispatches": open_dispatches,
    }


def cancel_dispatch(args: argparse.Namespace) -> dict[str, Any]:
    """Cancel one approved brief before it reaches any runtime.

    This is deliberately narrower than batch abandonment: the immutable brief remains available
    for audit, but a discovered assignment/transport mistake can be corrected without failing the
    otherwise valid batch or consuming a worker slot.
    """
    repo = _repo(args)
    root = _state_root(args, repo)
    approval = _approval(args)
    reason = args.reason.strip() if _non_empty(args.reason) else ""
    if not reason:
        raise CoordinatorError("cancelling a dispatch requires a recorded reason")
    _reject_sensitive({"reason": reason}, "dispatch cancellation reason")
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        dispatch = _load_dispatch(root, args.dispatch)
        batch = _load_batch(root, dispatch["batch_id"])
        _validate_batch_integrity(root, batch)
        entry = next((item for item in batch.get("dispatches", []) if item.get("dispatch_id") == dispatch["dispatch_id"]), None)
        if not entry or entry.get("brief_sha256") != hashlib.sha256(_canonical(dispatch).encode("utf-8")).hexdigest():
            raise CoordinatorError("dispatch record failed immutable brief integrity check")
        if entry.get("state") != "approved":
            raise CoordinatorError("only an approved, unsent dispatch may be cancelled")
        status = _load_dispatch_status(root, dispatch["dispatch_id"])
        if status.get("state") != "approved":
            raise CoordinatorError("only an approved, unsent dispatch may be cancelled")
        moment = _now()
        entry["state"] = "cancelled"
        entry["cancellation"] = {**approval, "cancelled_at": moment, "reason": reason}
        batch["state"] = "awaiting-approval"
        batch.setdefault("coordinator_decisions", []).append({
            "dispatch_id": dispatch["dispatch_id"], "decision": "cancel", **approval, "note": reason,
        })
        _safe_id(dispatch["dispatch_id"], "dispatch")
        _replace_record(ledger, DispatchStatusRecord.from_dict({
            "dispatch_id": dispatch["dispatch_id"], "state": "cancelled", "updated_at": moment,
            "cancellation": entry["cancellation"],
        }))
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))
    return {"dispatch_id": dispatch["dispatch_id"], "batch_id": batch["batch_id"], "state": "cancelled"}


def _prior_review_entry(batch: dict[str, Any], dispatch_id: str) -> dict[str, Any]:
    entry = next(
        (item for item in batch.get("dispatches", []) if item.get("dispatch_id") == dispatch_id), None,
    )
    if entry is None or entry.get("role") != "code-review":
        raise CoordinatorError("delta-review-of must reference a code-review dispatch in this batch")
    if entry.get("state") != "reported" or entry.get("decision", {}).get("decision") != "retry":
        raise CoordinatorError("delta-review-of must reference a retried code-review dispatch")
    return entry


def _delta_review_eligibility(
    repo: Path,
    config: dict[str, Any],
    known: list[str],
    prior_dispatch: dict[str, Any],
    prior_report: dict[str, Any],
    candidate: str,
) -> str:
    """Whether ``candidate`` may be delta-reviewed against ``prior_dispatch``'s review.

    Eligible only for a test-only fix after the prior Spec axis raised a Warning or Blocker. The
    Standards evidence is inherited only when it was Clean; Spec is always re-evaluated against the
    new candidate. Any production or risk-triggering diff falls back to a full independent review.
    """
    severities = _review_severity(prior_report["review"])
    if severities.get("standards") != "clean" or severities.get("spec") not in {"warning", "blocker"}:
        raise CoordinatorError(
            "delta-review requires prior Standards=Clean and prior Spec=Warning or Blocker"
        )
    prior_candidate = prior_dispatch.get("candidate_commit")
    if (
        not isinstance(prior_candidate, str)
        or prior_candidate == candidate
        or not _git_is_ancestor(repo, prior_candidate, candidate)
    ):
        raise CoordinatorError("delta-review requires a new candidate descended from the prior reviewed candidate")
    delta_files = _changed_files_between(repo, prior_candidate, candidate)
    if not delta_files:
        raise CoordinatorError("delta-review requires a non-empty fix diff since the prior reviewed candidate")
    patterns = _test_path_patterns(config)
    non_test = sorted(path for path in delta_files if not _is_test_path(path, patterns))
    if non_test:
        raise CoordinatorError(
            "delta-review is rejected because the fix diff touches non-test file(s): " + ", ".join(non_test)
        )
    evidence = _commit_evidence(repo, prior_candidate, candidate)
    matched = _matching_triggers(evidence, known)
    if matched:
        raise CoordinatorError(
            "delta-review is rejected because the fix diff matches risk trigger(s): " + ", ".join(sorted(matched))
        )
    return "spec"


def create_dispatch(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    root = _state_root(args, repo)
    config = _config(repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        batch = _load_batch(root, args.batch)
        _validate_batch_integrity(root, batch)
        if batch.get("state") != "awaiting-approval":
            raise CoordinatorError("a dispatch requires a batch awaiting explicit approval")
        pending_report = any(item.get("state") == "reported" and "decision" not in item for item in batch.get("dispatches", []))
        if pending_report:
            raise CoordinatorError("the previous completion report requires an explicit coordinator decision")
        _check_batch_conflicts(root, config, batch)
        # Existing packages are recorded as audit evidence. A fresh role-specific package is
        # registered below before the immutable brief is written, so a stale snapshot cannot be
        # silently reused by a new role.
        context_package_freshness = _context_package_freshness(repo, root, batch)
        if context_package_freshness is not None:
            batch.setdefault("context_package_freshness_checks", []).append(context_package_freshness)
        role_name = args.role
        purpose = args.purpose
        next_action = batch.get("next_action")
        required = {
            None: {("architect", "work"), ("developer", "work")},
            "developer": {("developer", "work")},
            "code-review": {("code-review", "work")},
            # A low-risk candidate goes straight to QA, but the fixed pipeline may still review it.
            "qa": {("qa", "work"), ("code-review", "work")},
            "publish": {("developer", "publish")},
            "developer-retry": {("developer", "work")},
        }
        if next_action == "risk-assessment":
            raise CoordinatorError("risk assessment must prepare the next dispatch before another role starts")
        if (role_name, purpose) not in required.get(next_action, set()):
            raise CoordinatorError("dispatch does not match the coordinator-prepared next action")
        # The architect step cannot be skipped, whatever the entry point: no coding dispatch exists
        # for a batch whose architect report has not been accepted.
        if role_name == "developer" and purpose == "work" and not _accepted_architect(batch):
            raise CoordinatorError(
                "a developer dispatch requires an accepted architect report for the same batch"
            )
        role, zone, profile_id, model, effort, transport, resolved_runtime = _resolve_assignment(
            repo, config, role_name, batch["zone"], args.runtime,
            session_model=getattr(args, "model", None), session_effort=getattr(args, "effort", None),
        )
        candidate = None
        risk = None
        review_scope: list[str] = []
        delta_review_of: str | None = None
        delta_review_axis: str | None = None
        requested_delta_review_of = getattr(args, "delta_review_of", None)
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
        is_review_work = role_name == "code-review" and purpose == "work"
        if is_review_work or purpose == "publish":
            _enforce_base_freshness(repo, root, ledger, batch)
        if candidate is not None:
            risk = _risk_for_candidate(root, batch, candidate)
        if role_name in {"code-review", "qa"} and candidate != _latest_developer_candidate(repo, root, batch):
            raise CoordinatorError("review and QA dispatches must use the latest accepted developer candidate")
        if role_name in {"code-review", "qa"} and risk is None:
            raise CoordinatorError("candidate commit has no coordinator risk assessment")
        if role_name == "code-review":
            # The risk assessment decides when review is *mandatory*, never when it is permitted:
            # the fixed pipeline reviews every candidate, high-risk or not.
            review_scope = list(risk["review_scope"])
            if requested_delta_review_of is not None:
                prior_entry = _prior_review_entry(batch, requested_delta_review_of)
                prior_dispatch = _load_dispatch(root, requested_delta_review_of)
                if prior_dispatch.get("batch_id") != batch["batch_id"]:
                    raise CoordinatorError("delta-review-of must reference a dispatch in this batch")
                prior_report = _pending_report(root, batch, prior_entry)
                delta_review_axis = _delta_review_eligibility(
                    repo, config, _risk_triggers(repo), prior_dispatch, prior_report, candidate,
                )
                delta_review_of = requested_delta_review_of
        elif requested_delta_review_of is not None:
            raise CoordinatorError("--delta-review-of is only valid for a code-review dispatch")
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
        approval = _dispatch_approval(args, batch, config, role_name, purpose, risk)
        context_package = None
        if role_name in {"architect", "developer", "code-review"}:
            snapshot = candidate
            if snapshot is None:
                try:
                    snapshot = _latest_developer_candidate(repo, root, batch)
                except CoordinatorError:
                    snapshot = batch["base_commit"]
            context_package = _persist_context_package(
                repo, root, ledger, batch, role="shared", snapshot=snapshot,
                inclusion_reason=(
                    f"automatic shared package for {role_name} at pinned snapshot {snapshot}; "
                    "included before immutable brief creation"
                ),
            )
            context_package_freshness = _context_package_freshness(repo, root, batch)
            if context_package_freshness is None or context_package_freshness["status"] != "fresh":
                raise CoordinatorError("newly registered Context Package is stale; refresh before dispatch")
        dispatch_id = f"dispatch-{uuid.uuid4()}"
        dispatch_commands = (
            batch["developer_verification_commands"]
            if role_name == "developer" and purpose == "work"
            else batch["verification_commands"]
        )
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
            "verification_commands": dispatch_commands,
            "required_gates": batch["required_gates"],
            "dependencies": batch["dependencies"],
            "resolved_runtime": resolved_runtime,
            "resolved_provider_profile": profile_id,
            "resolved_model": model,
            "resolved_effort": effort,
            "resolved_transport": transport,
            "allowed_tools": resolve_allowed_tools(config, role_name, role["mode"]),
            "context_budget": _adaptive_continuation_policy(config)["context_limit"],
            "coordinator_approval": approval,
            "candidate_commit": candidate,
            "review_base": risk["base_commit"] if risk else None,
            "review_scope": review_scope,
            "risk_assessment_id": risk["risk_assessment_id"] if risk else None,
            "purpose": purpose,
            "delta_review_of": delta_review_of,
            "delta_review_axis": delta_review_axis,
            "context_package_id": context_package["context_package_id"] if context_package else None,
            "context_package_sha256": (
                hashlib.sha256(_canonical(context_package).encode("utf-8")).hexdigest()
                if context_package else None
            ),
            "context_package_summary": _context_package_summary(context_package) if context_package else None,
            "worker_attestation_required": _worker_attestation_required(config),
            "communication_policy": batch.get("communication_policy", _communication_policy(config)),
            "snapshot_commit": candidate or batch["base_commit"],
            # Absolute, so a role never resolves a relative reporting path against a guessed
            # current directory and never invents a home-directory folder of its own.
            "report_staging_path": str(_agent_inbox(repo) / f"{dispatch_id}.json"),
        }
        _reject_sensitive(brief, "dispatch brief")
        # The immutable dispatch file is itself the approved brief.  Keeping the brief at the
        # top level lets any runtime-neutral adapter consume exactly the reviewed contract.
        dispatch = dict(brief)
        dispatch["state"] = "approved"
        dispatch["created_at"] = _now()
        _safe_id(dispatch_id, "dispatch")
        _write_record(ledger, DispatchRecord.from_dict(dispatch))
        _write_record(ledger, DispatchStatusRecord.from_dict(
            {"dispatch_id": dispatch_id, "state": "approved", "updated_at": _now()}
        ))
        batch["dispatches"].append({
            "dispatch_id": dispatch_id,
            "role": role_name,
            "state": "approved",
            "brief_sha256": hashlib.sha256(_canonical(dispatch).encode("utf-8")).hexdigest(),
        })
        if required_role and role_name == required_role:
            batch.pop("required_next_role", None)
        batch["state"] = "active"
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))
    _prepare_agent_inbox(repo)
    return {
        "dispatch_id": dispatch_id, "batch_id": batch["batch_id"], "state": "approved", "brief": brief,
        "report_staging_path": brief["report_staging_path"],
        "context_package_freshness": context_package_freshness,
    }


def _validate_checkout(checkout: Path, candidate: str, base: str | None, scope: list[str]) -> None:
    if not checkout.is_dir():
        raise CoordinatorError(f"review checkout does not exist: {checkout}")
    try:
        actual = _git(checkout, "rev-parse", "--verify", "HEAD^{commit}")
    except CoordinatorError as exc:
        raise CoordinatorError("review checkout is not a git worktree") from exc
    if actual != candidate:
        raise CoordinatorError("review checkout HEAD does not match the pinned candidate commit")
    # Ignored virtual environments and tool caches do not alter the pinned candidate.  Treating
    # them as a dirty review checkout sends the coordinator into needless recovery/review loops.
    status = _git(checkout, "status", "--porcelain", "--untracked-files=normal")
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
    transport = dispatch_record.get("resolved_transport")
    if transport not in ROLE_TRANSPORTS:
        raise CoordinatorError("dispatch record has an invalid transport")
    adapter: Path | None = None
    if transport == "orca":
        if not args.adapter:
            raise CoordinatorError("an orca-transport dispatch requires an explicit runtime adapter")
        adapter = Path(args.adapter).resolve()
        if not adapter.is_file():
            raise CoordinatorError(f"runtime adapter does not exist: {adapter}")
    elif args.adapter or args.adapter_arg:
        raise CoordinatorError("an in-process dispatch runs inside this session and takes no runtime adapter")
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        dispatch = _load_dispatch(root, args.dispatch)
        batch = _load_batch(root, dispatch["batch_id"])
        _validate_batch_integrity(root, batch)
        config = _config(repo)
        _validate_dispatch(repo, config, root, batch, dispatch)
        if dispatch["role"] == "code-review":
            checkout = Path(args.checkout).resolve() if args.checkout else None
            if checkout is None:
                raise CoordinatorError(
                    "code-review dispatch requires an explicit checkout: pass "
                    "--checkout <path-to-a-worktree-pinned-at-candidate_commit> "
                    f"(candidate_commit={dispatch['candidate_commit']}); "
                    "other roles omit --checkout entirely"
                )
            _validate_checkout(checkout, dispatch["candidate_commit"], dispatch["review_base"], dispatch["review_scope"])
        status = _load_dispatch_status(root, dispatch["dispatch_id"])
        if status.get("state") != "approved":
            raise CoordinatorError("only an approved dispatch may be sent to a runtime adapter")
        entry = next((item for item in batch.get("dispatches", []) if item["dispatch_id"] == dispatch["dispatch_id"]), None)
        if not entry or entry.get("state") != "approved":
            raise CoordinatorError("dispatch was already sent or is not registered in its batch")
        brief_path = _records_root(root) / DispatchRecord.directory / f"{_safe_id(dispatch['dispatch_id'], 'dispatch')}.json"
        if adapter is not None:
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
            # Windows consoles default to a legacy ANSI codepage: without an explicit encoding a
            # UTF-8 adapter message is mojibaked before it ever reaches the coordinator error.
            result = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()
                raise CoordinatorError(f"runtime adapter rejected dispatch: {detail}")
        for entry in batch["dispatches"]:
            if entry["dispatch_id"] == dispatch["dispatch_id"]:
                entry["state"] = "dispatched"
                break
        else:
            raise CoordinatorError("dispatch is not registered in its batch")
        sent_at = _now()
        _safe_id(dispatch["dispatch_id"], "dispatch")
        _replace_record(ledger, DispatchStatusRecord.from_dict({
            "dispatch_id": dispatch["dispatch_id"], "state": "dispatched", "updated_at": sent_at,
            "heartbeat_at": sent_at,
        }))
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))
    return {
        "dispatch_id": dispatch["dispatch_id"],
        "state": "dispatched",
        "transport": transport,
        "brief": str(brief_path),
        "expected_model": dispatch["resolved_model"],
        "report_staging_path": dispatch.get("report_staging_path") or str(_agent_inbox(repo) / f"{dispatch['dispatch_id']}.json"),
        "next_role_action": "dispatch self-report",
    }


def _live_status(root: Path, dispatch_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    dispatch = _load_dispatch(root, dispatch_id)
    status = _load_dispatch_status(root, dispatch_id)
    if status.get("state") not in LIVE_DISPATCH_STATES:
        raise CoordinatorError("only a dispatched role can report liveness")
    return dispatch, status


def self_report_dispatch(args: argparse.Namespace) -> dict[str, Any]:
    """Compare the model a dispatched role is actually running against its immutable brief.

    A mismatch blocks the dispatch here, at first contact, instead of letting a misconfigured
    worker spend the coordinator's budget producing nothing."""
    repo = _repo(args)
    root = _state_root(args, repo)
    reported = args.model.strip() if _non_empty(args.model) else ""
    if not reported:
        raise CoordinatorError("a model self-report must name the actually active model")
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        dispatch, status = _live_status(root, args.dispatch)
        expected = dispatch["resolved_model"]
        model_matched = reported == expected
        attestation: dict[str, Any] | None = None
        worktree_matched = True
        if dispatch.get("worker_attestation_required", False):
            supplied_worktree = getattr(args, "worktree", None)
            if not _non_empty(supplied_worktree):
                worktree_matched = False
                attestation = {"match": False, "error": "runtime worktree attestation is required"}
            else:
                try:
                    attestation = {"match": True, **attest_runtime_worktree(repo, dispatch, supplied_worktree)}
                except AttestationError as exc:
                    worktree_matched = False
                    attestation = {"match": False, "error": str(exc)}
        matched = model_matched and worktree_matched
        moment = _now()
        status["state"] = "working" if matched else "blocked"
        status["updated_at"] = moment
        status["heartbeat_at"] = moment
        status["model_self_report"] = {
            "reported_model": reported,
            "expected_model": expected,
            "match": model_matched,
            "reported_at": moment,
        }
        if attestation is not None:
            status["worktree_attestation"] = {**attestation, "reported_at": moment}
        _safe_id(dispatch["dispatch_id"], "dispatch")
        _replace_record(ledger, DispatchStatusRecord.from_dict(status))
        if not matched:
            batch = _load_batch(root, dispatch["batch_id"])
            for entry in batch.get("dispatches", []):
                if entry["dispatch_id"] == dispatch["dispatch_id"]:
                    entry["state"] = "blocked"
            batch["state"] = "blocked"
            _safe_id(batch["batch_id"], "batch")
            _replace_record(ledger, BatchRecord.from_dict(batch))
    if not matched:
        mismatch = []
        if not model_matched:
            mismatch.append(f"running {reported!r} but approved brief resolved {expected!r}")
        if not worktree_matched:
            mismatch.append(str(attestation["error"]))
        raise CoordinatorError(
            "dispatch " + "; ".join(mismatch) + "; the dispatch is blocked and needs a new coordinator decision"
        )
    return {"dispatch_id": dispatch["dispatch_id"], "state": "working", "model": reported, "worktree": attestation.get("worktree") if attestation else None}


def heartbeat_dispatch(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    root = _state_root(args, repo)
    note = args.note.strip() if _non_empty(args.note) else "none"
    _reject_sensitive({"note": note}, "dispatch heartbeat")
    context_tokens = args.context_tokens
    context_source = args.context_source
    if (context_tokens is None) != (context_source is None):
        raise CoordinatorError("--context-tokens and --context-source must be given together")
    if context_tokens is not None and (isinstance(context_tokens, bool) or not isinstance(context_tokens, int) or context_tokens < 0):
        raise CoordinatorError("--context-tokens must be a non-negative integer")
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        dispatch, status = _live_status(root, args.dispatch)
        moment = _now()
        status["updated_at"] = moment
        status["heartbeat_at"] = moment
        status["heartbeat_note"] = _sanitise(note)[:240]
        if context_tokens is not None:
            status["context_tokens"] = context_tokens
            status["context_source"] = context_source
        _safe_id(dispatch["dispatch_id"], "dispatch")
        _replace_record(ledger, DispatchStatusRecord.from_dict(status))
    result = {"dispatch_id": dispatch["dispatch_id"], "state": status["state"], "heartbeat_at": moment}
    if context_tokens is not None:
        result["context_tokens"] = context_tokens
        result["context_source"] = context_source
    return result


def rate_limited_dispatch(args: argparse.Namespace) -> dict[str, Any]:
    """Record a provider 429 without model-side polling or a lost checkpoint."""
    repo = _repo(args)
    root = _state_root(args, repo)
    retry_after = args.retry_after_seconds
    if isinstance(retry_after, bool) or not isinstance(retry_after, int) or retry_after < 1:
        raise CoordinatorError("retry-after-seconds must be a positive integer")
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        dispatch = _load_dispatch(root, args.dispatch)
        batch = _load_batch(root, dispatch["batch_id"])
        _validate_batch_integrity(root, batch)
        _latest_checkpoint_for_dispatch(root, batch, dispatch["dispatch_id"])
        status = _load_dispatch_status(root, dispatch["dispatch_id"])
        entry = next((item for item in batch["dispatches"] if item["dispatch_id"] == dispatch["dispatch_id"]), None)
        if not entry or entry.get("state") != "checkpointed" or status.get("state") != "checkpointed":
            raise CoordinatorError("rate_limited requires a checkpointed write dispatch")
        retry_not_before = (datetime.now(timezone.utc) + timedelta(seconds=retry_after)).isoformat()
        status.update({
            "state": "rate_limited", "updated_at": _now(), "retry_not_before": retry_not_before,
            "last_event": "rate_limited",
        })
        entry["state"] = "rate_limited"
        batch.setdefault("liveness_events", []).append({
            "dispatch_id": dispatch["dispatch_id"], "event": "rate_limited", "recorded_at": _now(),
            "retry_not_before": retry_not_before,
        })
        _safe_id(dispatch["dispatch_id"], "dispatch")
        _replace_record(ledger, DispatchStatusRecord.from_dict(status))
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))
    return {"dispatch_id": dispatch["dispatch_id"], "event": "rate_limited", "retry_not_before": retry_not_before}


def wait_dispatch(args: argparse.Namespace) -> dict[str, Any]:
    """Wait locally for a significant event; heartbeat updates never reach the coordinator chat."""
    repo = _repo(args)
    root = _state_root(args, repo)
    timeout, interval, threshold = args.timeout, args.poll_interval, args.stale_after
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in (timeout, interval, threshold)):
        raise CoordinatorError("timeout, poll-interval and stale-after must be positive integers")
    deadline = time.monotonic() + timeout
    ledger = LifecycleLedger(root)
    while True:
        with _ledger_lock(ledger):
            dispatch = _load_dispatch(root, args.dispatch)
            status = _load_dispatch_status(root, dispatch["dispatch_id"])
            state = status.get("state")
            if state == "reported":
                return {"dispatch_id": dispatch["dispatch_id"], "event": "reported"}
            if state == "rate_limited":
                return {"dispatch_id": dispatch["dispatch_id"], "event": "rate_limited", "retry_not_before": status.get("retry_not_before")}
            if state == "blocked" and status.get("model_self_report", {}).get("match") is False:
                return {"dispatch_id": dispatch["dispatch_id"], "event": "model_mismatch"}
            if state == "blocked" and status.get("worktree_attestation", {}).get("match") is False:
                return {"dispatch_id": dispatch["dispatch_id"], "event": "worktree_mismatch"}
            if state in {"failed", "abandoned", "cancelled"}:
                return {"dispatch_id": dispatch["dispatch_id"], "event": "failed", "state": state}
            if state in LIVE_DISPATCH_STATES and _silent_seconds(status) >= threshold:
                return {"dispatch_id": dispatch["dispatch_id"], "event": "stale", "silent_seconds": _silent_seconds(status)}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"dispatch_id": args.dispatch, "event": "timeout"}
        time.sleep(min(interval, max(1, int(remaining))))


def record_telemetry(args: argparse.Namespace) -> dict[str, Any]:
    """Attach source-observed worker/coordinator metrics to the batch audit trail.

    Missing provider fields stay ``null`` rather than becoming estimated zeroes. This command is
    intentionally data-only: it cannot alter role state, scheduling, approvals or model routing.
    """
    repo = _repo(args)
    root = _state_root(args, repo)
    payload = _read_object(Path(args.file), "telemetry payload")
    if set(payload) != TELEMETRY_FIELDS:
        raise CoordinatorError("telemetry payload schema mismatch")
    _reject_sensitive(payload, "telemetry payload")
    if payload["session_kind"] not in {"worker", "coordinator"}:
        raise CoordinatorError("telemetry session_kind must be worker or coordinator")
    if not _non_empty(payload["restart_reason"]) or not _non_empty(payload["recorded_at"]):
        raise CoordinatorError("telemetry restart_reason and recorded_at must be non-empty strings")
    _moment(payload["recorded_at"], "telemetry recorded_at")
    for field in TELEMETRY_FIELDS - {"dispatch_id", "session_kind", "restart_reason", "recorded_at"}:
        value = payload[field]
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise CoordinatorError(f"telemetry {field} must be a non-negative integer or null")
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        dispatch = _load_dispatch(root, payload["dispatch_id"])
        batch = _load_batch(root, dispatch["batch_id"])
        _validate_batch_integrity(root, batch)
        record = dict(payload)
        record["telemetry_id"] = f"telemetry-{uuid.uuid4()}"
        record["record_sha256"] = hashlib.sha256(_canonical(record).encode("utf-8")).hexdigest()
        batch.setdefault("telemetry", []).append(record)
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))
    advisory = _context_advisory(_config(repo), payload["max_context_tokens"])
    return {"telemetry_id": record["telemetry_id"], "dispatch_id": payload["dispatch_id"], "context_advisory": advisory}


def _validate_checkpoint(
    checkpoint: dict[str, Any], dispatch: dict[str, Any], role: dict[str, Any], repo: Path,
    base_commit: str | None, root: Path, batch: dict[str, Any],
) -> None:
    _reject_sensitive(checkpoint, "checkpoint")
    if set(checkpoint) != CHECKPOINT_INPUT_FIELDS:
        raise CoordinatorError("checkpoint schema mismatch")
    if checkpoint["dispatch_id"] != dispatch["dispatch_id"]:
        raise CoordinatorError("checkpoint dispatch_id does not match the dispatched role")
    if role["mode"] != "write":
        raise CoordinatorError(
            "a checkpoint is only valid for a write role; a read-only role cannot span multiple worker sessions"
        )
    for field in ("risks", "blockers"):
        if not _non_empty(checkpoint[field]):
            raise CoordinatorError(f"checkpoint {field} must be a non-empty string")
    commit_sha = checkpoint["commit_sha"]
    if not isinstance(commit_sha, str) or re.fullmatch(r"[0-9a-fA-F]{7,64}", commit_sha) is None:
        raise CoordinatorError("checkpoint requires a commit_sha")
    changed_files = _strings(checkpoint["changed_files"], "checkpoint changed_files", allow_empty=True)
    paths = dispatch["write_paths"]
    for changed_file in changed_files:
        normalized = changed_file.replace("\\", "/")
        if normalized.startswith("/") or ".." in Path(normalized).parts or not any(
            fnmatchcase(normalized, pattern) for pattern in paths
        ):
            raise CoordinatorError("checkpoint changed_files must remain inside the approved zone")
    resolved = _candidate_commit(repo, commit_sha)
    actual_files = _changed_files_between(repo, base_commit, resolved) if base_commit else _commit_changed_files(repo, resolved)
    if actual_files != changed_files:
        raise CoordinatorError("checkpoint changed_files must exactly match commit_sha")
    remaining = _strings(
        checkpoint["remaining_definition_of_done"], "checkpoint remaining_definition_of_done", allow_empty=True
    )
    if not set(remaining) <= set(dispatch["definition_of_done"]):
        raise CoordinatorError("checkpoint remaining_definition_of_done must be drawn from the dispatch definition_of_done")
    passing_checks = checkpoint["passing_checks"]
    if not isinstance(passing_checks, list):
        raise CoordinatorError("checkpoint passing_checks must be a list")
    for check in passing_checks:
        if not isinstance(check, dict) or set(check) != {"command", "result", "evidence"}:
            raise CoordinatorError("checkpoint passing_checks has an invalid entry")
        if not all(_non_empty(check[field]) for field in ("command", "result", "evidence")):
            raise CoordinatorError("checkpoint passing_checks entries must contain text evidence")
        if check["command"] not in dispatch["verification_commands"]:
            raise CoordinatorError("checkpoint passing_checks must reference an approved verification command")
    package = _latest_context_package(root, batch)
    if package is None:
        if checkpoint["context_package_id"] != CHECKPOINT_NO_CONTEXT_PACKAGE:
            raise CoordinatorError(
                "checkpoint context_package_id must be the no-package sentinel; "
                "the batch has no registered context package"
            )
    elif checkpoint["context_package_id"] != package["context_package_id"]:
        raise CoordinatorError("checkpoint context_package_id must reference the batch's latest registered context package")


def checkpoint_dispatch(args: argparse.Namespace) -> dict[str, Any]:
    """Record a non-terminal checkpoint for an in-flight write-role dispatch so its worker session
    can end here and a fresh session can resume the same dispatch later.

    A checkpoint is deliberately not a completion report: it never touches the outcome enum or the
    reporting path, and it is rejected outright for a read-only role, which may never span more
    than one worker session."""
    repo = _repo(args)
    root = _state_root(args, repo)
    checkpoint = _read_object(_agent_authored_file(repo, args.file, "a checkpoint"), "checkpoint")
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        dispatch, status = _live_status(root, checkpoint.get("dispatch_id"))
        batch = _load_batch(root, dispatch["batch_id"])
        _validate_batch_integrity(root, batch)
        config = _config(repo)
        _validate_dispatch(repo, config, root, batch, dispatch)
        role = _role(repo, dispatch["role"])
        entry = next((item for item in batch.get("dispatches", []) if item["dispatch_id"] == dispatch["dispatch_id"]), None)
        if not entry or entry.get("state") != "dispatched":
            raise CoordinatorError("a checkpoint requires a dispatched role")
        self_report = status.get("model_self_report")
        if not isinstance(self_report, dict) or self_report.get("match") is not True:
            raise CoordinatorError("a dispatched role must confirm its active model before checkpointing")
        _validate_checkpoint(checkpoint, dispatch, role, repo, batch.get("base_commit"), root, batch)
        record = {
            "checkpoint_id": f"checkpoint-{uuid.uuid4()}",
            "batch_id": batch["batch_id"],
            "created_at": _now(),
            **checkpoint,
        }
        _safe_id(record["checkpoint_id"], "checkpoint")
        _write_record(ledger, CheckpointRecord.from_dict(record))
        entry["state"] = "checkpointed"
        # Batch-level pointer, the same ownership pattern as risk_assessments/context_packages;
        # dispatch_id is carried alongside since a checkpoint is dispatch-scoped, not batch-scoped.
        batch.setdefault("checkpoints", []).append({
            "checkpoint_id": record["checkpoint_id"],
            "dispatch_id": dispatch["dispatch_id"],
            "record_sha256": hashlib.sha256(_canonical(record).encode("utf-8")).hexdigest(),
        })
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))
        moment = _now()
        status.update({"state": "checkpointed", "updated_at": moment})
        _safe_id(dispatch["dispatch_id"], "dispatch")
        _replace_record(ledger, DispatchStatusRecord.from_dict(status))
    return {"dispatch_id": dispatch["dispatch_id"], "state": "checkpointed", "checkpoint_id": record["checkpoint_id"]}


def _authorize_rate_limit_continuation(termination_reason: str) -> dict[str, Any]:
    return {
        "decision": "continue-automatic",
        "approved_by": "runtime-adapter",
        "approved_at": _now(),
        "note": f"termination_reason={termination_reason}",
    }


def _authorize_planned_continuation(
    config: dict[str, Any], dispatch: dict[str, Any], checkpoint: dict[str, Any], args: argparse.Namespace,
) -> dict[str, Any]:
    """A planned trigger (context limit, N TDD cycles, a large failure log, or a completed
    vertical slice) is a safe-default-toward-approval path: it always requires the same explicit
    coordinator decision an accept/retry/block/fail already does, reusing that record type rather
    than inventing a new one."""
    trigger = args.trigger.strip() if _non_empty(args.trigger) else ""
    if trigger not in PLANNED_TRIGGER_KINDS:
        raise CoordinatorError(
            "a continuation without a recognized rate-limit termination reason is a planned trigger and "
            f"requires --trigger to be one of {sorted(PLANNED_TRIGGER_KINDS)}"
        )
    threshold_key = PLANNED_TRIGGER_THRESHOLD_KEY.get(trigger)
    if threshold_key is not None:
        threshold = _adaptive_continuation_policy(config)[threshold_key]
        measured = args.measured_value
        if isinstance(measured, bool) or not isinstance(measured, int) or measured < threshold:
            raise CoordinatorError(
                f"planned trigger {trigger!r} requires --measured-value at least the configured "
                f"threshold ({threshold})"
            )
    _check_continuation_facts_unchanged(dispatch, checkpoint, args)
    approval = _approval(args)
    return {
        "decision": "continue",
        "approved_by": approval["approved_by"],
        "approved_at": approval["approved_at"],
        "note": args.note.strip() if _non_empty(args.note) else f"planned trigger: {trigger}",
    }


def _check_continuation_facts_unchanged(
    dispatch: dict[str, Any], checkpoint: dict[str, Any], args: argparse.Namespace,
) -> None:
    """Reject a continuation whose recorded facts show scope, Definition of Done, risks, or
    dependencies changed since the checkpoint (Issue #140's continuation-authorization contract).
    The coordinator must restate those facts explicitly -- a mismatch means real drift, not
    something the coordinator can wave through, so it must close the dispatch and open a new one
    through ordinary approval instead. Blockers are deliberately not compared here: unlike the
    other four, their wording can legitimately evolve session to session without the underlying
    scope, DoD, risks or dependencies having changed at all."""
    if not _non_empty(getattr(args, "file", None)):
        raise CoordinatorError(
            "a planned-trigger continuation requires --file restating the current remaining "
            "Definition of Done, risks and dependencies"
        )
    facts = _read_object(Path(args.file).resolve(), "continuation facts")
    _reject_sensitive(facts, "continuation facts")
    if set(facts) != CONTINUATION_FACTS_FIELDS:
        raise CoordinatorError("continuation facts schema mismatch")
    if facts["dispatch_id"] != dispatch["dispatch_id"]:
        raise CoordinatorError("continuation facts dispatch_id does not match the dispatched role")
    unchanged = (
        facts["remaining_definition_of_done"] == checkpoint["remaining_definition_of_done"]
        and facts["risks"] == checkpoint["risks"]
        and facts["dependencies"] == dispatch["dependencies"]
    )
    if not unchanged:
        raise CoordinatorError(
            "continuation facts differ from the checkpointed scope, Definition of Done, risks, blockers "
            "or dependencies; close this dispatch and open a new one through ordinary approval instead "
            "of resuming it"
        )


def resume_dispatch(args: argparse.Namespace) -> dict[str, Any]:
    """Start a new worker session for a checkpointed dispatch, under the same dispatch ID.

    The resumed session is put through the exact same liveness contract as a first session: its
    prior model self-report is discarded, so `dispatch self-report` and `dispatch heartbeat` are
    both mandatory again before any further checkpoint or completion report.

    Continuation authorization (Issue #140): a runtime adapter reporting a recognized rate-limit
    termination reason authorizes the new session automatically, without a new human/coordinator
    decision. Anything else -- no reason, or one this coordinator does not recognize -- is treated
    as a planned trigger and requires the existing coordinator-decision approval, with its recorded
    facts checked against the checkpoint for drift."""
    repo = _repo(args)
    root = _state_root(args, repo)
    termination_reason = args.termination_reason.strip().lower() if _non_empty(args.termination_reason) else ""
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        dispatch = _load_dispatch(root, args.dispatch)
        batch = _load_batch(root, dispatch["batch_id"])
        _validate_batch_integrity(root, batch)
        config = _config(repo)
        _validate_dispatch(repo, config, root, batch, dispatch)
        status = _load_dispatch_status(root, dispatch["dispatch_id"])
        entry = next((item for item in batch.get("dispatches", []) if item["dispatch_id"] == dispatch["dispatch_id"]), None)
        resumable_rate_limit = entry and entry.get("state") == "rate_limited" and status.get("state") == "rate_limited"
        if not entry or (entry.get("state") != "checkpointed" and not resumable_rate_limit) or (
            status.get("state") != "checkpointed" and not resumable_rate_limit
        ):
            raise CoordinatorError("only a checkpointed or rate-limited dispatch may start a new worker session")
        continuation_policy = _continuation_policy(config)
        continuation_count, rate_limit_count = _continuation_counts(batch, dispatch["dispatch_id"])
        if continuation_count >= continuation_policy["max_continuations"]:
            raise CoordinatorError(
                "continuation budget is exhausted for this dispatch; submit a final report or block for a new scoped batch"
            )
        if termination_reason in RATE_LIMIT_TERMINATION_REASONS:
            if rate_limit_count >= continuation_policy["max_rate_limit_resumes"]:
                raise CoordinatorError(
                    "automatic rate-limit resume budget is exhausted; require a newly scoped batch instead of looping"
                )
            retry_not_before = status.get("retry_not_before")
            if isinstance(retry_not_before, str) and _moment(retry_not_before, "retry_not_before") > datetime.now(timezone.utc):
                raise CoordinatorError("rate-limit retry window has not elapsed")
            authorization = _authorize_rate_limit_continuation(termination_reason)
        else:
            checkpoint = _latest_checkpoint_for_dispatch(root, batch, dispatch["dispatch_id"])
            authorization = _authorize_planned_continuation(config, dispatch, checkpoint, args)
        entry["state"] = "dispatched"
        batch.setdefault("coordinator_decisions", []).append({
            "dispatch_id": dispatch["dispatch_id"], **authorization,
        })
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))
        moment = _now()
        _safe_id(dispatch["dispatch_id"], "dispatch")
        _replace_record(ledger, DispatchStatusRecord.from_dict({
            "dispatch_id": dispatch["dispatch_id"], "state": "dispatched", "updated_at": moment,
            "heartbeat_at": moment, "last_event": "resumed",
        }))
    return {
        "dispatch_id": dispatch["dispatch_id"], "state": "dispatched",
        "authorization": authorization["decision"],
        "next_role_action": "dispatch self-report",
    }


def dispatch_status(args: argparse.Namespace) -> dict[str, Any]:
    """Liveness view the coordinator session polls; a stale entry is a blocker to surface, never a
    reason for the coordinator to change state on its own."""
    repo = _repo(args)
    root = _state_root(args, repo)
    threshold = args.stale_after
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
        raise CoordinatorError("stale-after must be a positive number of seconds")
    config = _config(repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        entries: list[dict[str, Any]] = []
        for path in sorted((_records_root(root) / DispatchStatusRecord.directory).glob("dispatch-*.json")):
            status = _read_object(path, "dispatch status")
            if args.dispatch and status.get("dispatch_id") != args.dispatch:
                continue
            dispatch = _load_dispatch(root, status.get("dispatch_id"))
            if args.batch and dispatch.get("batch_id") != args.batch:
                continue
            batch = _load_batch(root, dispatch["batch_id"])
            telemetry = [
                record for record in batch.get("telemetry", [])
                if record.get("dispatch_id") == dispatch["dispatch_id"]
            ]
            latest_telemetry = max(telemetry, key=lambda record: record["recorded_at"], default=None)
            live = status.get("state") in LIVE_DISPATCH_STATES
            silent = _silent_seconds(status) if live else 0
            entries.append({
                "dispatch_id": dispatch["dispatch_id"],
                "batch_id": dispatch["batch_id"],
                # The coordinator session needs to see whose work a leftover dispatch belongs to
                # before it proposes a new batch for the same ticket.
                "ticket": dispatch["ticket"],
                "role": dispatch["role"],
                "state": status.get("state"),
                "transport": dispatch.get("resolved_transport"),
                "resolved_model": dispatch["resolved_model"],
                "model_self_report": status.get("model_self_report"),
                "heartbeat_at": status.get("heartbeat_at") or status.get("updated_at"),
                "silent_seconds": silent,
                "stale": live and silent >= threshold,
                "telemetry": latest_telemetry,
                "context_advisory": _context_advisory(
                    config, latest_telemetry["max_context_tokens"] if latest_telemetry else None,
                ),
            })
    return {
        "stale_after_seconds": threshold,
        "stale": [entry["dispatch_id"] for entry in entries if entry["stale"]],
        "dispatches": entries,
    }


def publish_dispatch(args: argparse.Namespace) -> dict[str, Any]:
    """Push one QA-accepted candidate through an approved publish-only brief."""
    repo = _repo(args)
    root = _state_root(args, repo)
    remote = args.remote.strip() if _non_empty(args.remote) else ""
    if not remote:
        raise CoordinatorError("publish remote must be a non-empty string")
    if remote not in _git(repo, "remote").splitlines():
        raise CoordinatorError("publish remote is not configured for this repository")
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
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
        _safe_id(dispatch["dispatch_id"], "dispatch")
        _replace_record(ledger, DispatchStatusRecord.from_dict({
            "dispatch_id": dispatch["dispatch_id"], "state": "dispatched", "updated_at": _now(),
        }))
        _safe_id(batch["batch_id"], "batch")
        _replace_record(ledger, BatchRecord.from_dict(batch))
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
            "report_language": "ru",
        }
        report_path = _persist_report(ledger, root, batch, dispatch, report)
    return {"dispatch_id": dispatch["dispatch_id"], "state": "reported", "report": str(report_path), "candidate_commit": candidate}


def _persist_report(
    ledger: LifecycleLedger, root: Path, batch: dict[str, Any], dispatch: dict[str, Any], report: dict[str, Any],
) -> Path:
    """Persist a role report and advance its batch atomically under the coordinator lock."""
    report_json = _records_root(root) / "reports" / f"{dispatch['dispatch_id']}.json"
    report_md = _records_root(root) / "reports" / f"{dispatch['dispatch_id']}.md"
    if report_json.exists() or report_md.exists():
        raise CoordinatorError("refusing to overwrite immutable completion report")
    _write_exclusive(ledger, report_json, report)
    try:
        _write_text_exclusive(ledger, report_md, _report_markdown(report))
    except CoordinatorError as exc:
        raise CoordinatorError("refusing to overwrite immutable Markdown report") from exc
    entry = next((item for item in batch["dispatches"] if item["dispatch_id"] == dispatch["dispatch_id"]), None)
    if entry is None:
        raise CoordinatorError("dispatch is not registered in its batch")
    entry["state"] = "reported"
    entry["report"] = f"reports/{dispatch['dispatch_id']}.json"
    entry["report_sha256"] = hashlib.sha256(_canonical(report).encode("utf-8")).hexdigest()
    batch["state"] = "awaiting-approval"
    closed = _load_dispatch_status(root, dispatch["dispatch_id"])
    closed.update({"dispatch_id": dispatch["dispatch_id"], "state": "reported", "updated_at": _now()})
    _safe_id(dispatch["dispatch_id"], "dispatch")
    _replace_record(ledger, DispatchStatusRecord.from_dict(closed))
    _safe_id(batch["batch_id"], "batch")
    _replace_record(ledger, BatchRecord.from_dict(batch))
    return report_json


def run_qa(args: argparse.Namespace) -> dict[str, Any]:
    """Run the repository-scoped QA lane without coupling it to CLI wiring."""
    return qa_lane.run(args, sys.modules[__name__])


def qa_status(args: argparse.Namespace) -> dict[str, Any]:
    return qa_lane.status(args, sys.modules[__name__])


def clear_qa_lease(args: argparse.Namespace) -> dict[str, Any]:
    return qa_lane.clear_stale_lease(args, sys.modules[__name__])


def _validate_review(review: object, dispatch: dict[str, Any]) -> None:
    if not isinstance(review, dict) or set(review) != {"candidate_commit", "scope", "standards", "spec"}:
        raise CoordinatorError("composite review must contain independent standards and spec evidence")
    if review["candidate_commit"] != dispatch["candidate_commit"] or review["scope"] != dispatch["review_scope"]:
        raise CoordinatorError("composite review evidence does not match the approved review brief")
    delta_review_of = dispatch.get("delta_review_of")
    delta_review_axis = dispatch.get("delta_review_axis")
    for axis in ("standards", "spec"):
        evidence = review[axis]
        inherited = delta_review_of is not None and axis != delta_review_axis
        expected_keys = (
            {"severity", "findings", "risks", "blockers", "inherited_from"}
            if inherited
            else {"severity", "findings", "risks", "blockers"}
        )
        if not isinstance(evidence, dict) or set(evidence) != expected_keys:
            raise CoordinatorError(f"composite review {axis} evidence has an invalid schema")
        if inherited:
            if evidence["severity"] != "clean" or evidence["findings"] != []:
                raise CoordinatorError(
                    f"composite review {axis} must inherit the prior Clean verdict without re-analysis"
                )
            if evidence["inherited_from"] != delta_review_of:
                raise CoordinatorError(f"composite review {axis} must reference the prior review as evidence")
            if not _non_empty(evidence["risks"]) or not _non_empty(evidence["blockers"]):
                raise CoordinatorError(f"composite review {axis} must state risks and blockers")
            continue
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
    report_language = report.get("report_language")
    if report_language is not None and report_language != "ru":
        raise CoordinatorError("completion report report_language must be ru")
    if "communication_policy" in dispatch and report_language != "ru":
        raise CoordinatorError("new completion reports must set report_language to ru")
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
        if len(check["evidence"]) > MAX_CHECK_EVIDENCE_CHARS:
            raise CoordinatorError(
                "completion report check evidence exceeds the bounded summary limit; store the full log as an artifact and report its path"
            )
    commands_run = [check["command"] for check in checks]
    if commands_run != dispatch["verification_commands"]:
        raise CoordinatorError(
            f"completion report checks_run must exactly match approved verification commands "
            f"(expected {dispatch['verification_commands']}, got {commands_run})"
        )
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
            if "inherited_from" in evidence:
                lines.append(f"- Review {axis.title()} inherited from: {evidence['inherited_from']}")
            for finding in evidence["findings"]:
                lines.append(f"  - [{finding['severity']}] {finding['summary']}: {finding['evidence']}")
    lines.append("")
    return "\n".join(lines)


def submit_report(args: argparse.Namespace) -> dict[str, Any]:
    repo = _repo(args)
    try:
        report = _read_object(_agent_authored_file(repo, args.file, "a completion report"), "completion report")
    except CoordinatorError:
        raise
    root = _state_root(args, repo)
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        dispatch = _load_dispatch(root, report.get("dispatch_id"))
        batch = _load_batch(root, dispatch["batch_id"])
        _validate_batch_integrity(root, batch)
        config = _config(repo)
        _validate_dispatch(repo, config, root, batch, dispatch)
        if dispatch["role"] == "qa":
            raise CoordinatorError("QA reports must be produced by the clean-room QA runner")
        status = _load_dispatch_status(root, dispatch["dispatch_id"])
        entry = next((item for item in batch.get("dispatches", []) if item["dispatch_id"] == dispatch["dispatch_id"]), None)
        if not entry or entry.get("state") != "dispatched" or status.get("state") not in LIVE_DISPATCH_STATES:
            raise CoordinatorError("completion report requires a dispatched role")
        self_report = status.get("model_self_report")
        if not isinstance(self_report, dict) or self_report.get("match") is not True:
            raise CoordinatorError("a dispatched role must confirm its active model before reporting")
        if dispatch.get("worker_attestation_required", False) and status.get("worktree_attestation", {}).get("match") is not True:
            raise CoordinatorError("a dispatched role must attest its canonical Git worktree before reporting")
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
        report_json = _persist_report(ledger, root, batch, dispatch, report)
    return {"dispatch_id": dispatch["dispatch_id"], "state": "reported", "report": str(report_json)}


def parser() -> argparse.ArgumentParser:
    return build_parser(sys.modules[__name__], sys.modules[__name__])


def main() -> int:
    # Git Bash on Windows can inherit a legacy Windows code page while displaying UTF-8.  Emit
    # UTF-8 independently of that inherited setting so JSON evidence is never merely *shown* as
    # corrupted and mistaken for a damaged state record.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
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
