#!/usr/bin/env python3
"""Explicit, project-configured Orca dispatch boundary for backend orchestration.

This module intentionally owns only the runtime translation. Role policy remains in the
portable Markdown manifests and provider/model selection remains in the project-owned config.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SENSITIVE_KEY = re.compile(r"(?:api[_-]?key|credential|password|secret|token)", re.IGNORECASE)
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")


class DispatchError(Exception):
    """An invalid or unsafe request that must not reach Orca."""


class OrcaLaunchRejected(DispatchError):
    """An Orca launch rejection whose recovery safety is explicit."""

    def __init__(self, safe_to_fallback: bool):
        super().__init__("Orca rejected the dispatch request")
        self.safe_to_fallback = safe_to_fallback


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DispatchError(f"{label} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise DispatchError(f"{label} must be a JSON object")
    return data


def _non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_model_id(value: object) -> str:
    if not _non_empty_string(value) or MODEL_ID.fullmatch(value.strip()) is None:
        raise DispatchError("assignment model must be a CLI model ID or alias without spaces")
    return value.strip()


def _issue_branch_exists(repo: Path, branch: str) -> None:
    for reference in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
        result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", reference])
        if result.returncode == 0:
            return
    raise DispatchError("approved issue branch does not exist locally or on origin")


def _paths_within_zone(paths: list[str], zone_paths: list[str]) -> bool:
    def inside(path: str, boundary: str) -> bool:
        prefix = boundary[:-2] if boundary.endswith("**") else boundary
        return path == boundary or path.startswith(prefix)

    return all(any(inside(path, boundary) for boundary in zone_paths) for path in paths)


def _resolved_commit(repo: Path, value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{7,64}", value.strip()) is None:
        raise DispatchError("candidate_commit must be a hexadecimal commit SHA")
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"{value.strip()}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or result.stdout.strip() != value.strip().lower():
        raise DispatchError("candidate_commit must resolve to its full commit SHA in the target repository")
    return result.stdout.strip()


def _git_output(repo: Path, arguments: list[str]) -> str:
    result = subprocess.run(["git", "-C", str(repo), *arguments], capture_output=True, text=True)
    if result.returncode != 0:
        raise DispatchError("cannot inspect the pinned candidate commit")
    return result.stdout.strip()


def _candidate_files(repo: Path, candidate: str, base: str | None) -> list[str]:
    if base:
        output = _git_output(repo, ["diff", "--name-only", "--no-renames", base, candidate])
    else:
        output = _git_output(repo, ["diff-tree", "--root", "--no-commit-id", "--name-only", "-r", candidate])
    return [line.replace("\\", "/") for line in output.splitlines() if line.strip()]


def _is_ancestor(repo: Path, base: str, candidate: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", base, candidate],
        capture_output=True,
        text=True,
    )
    if result.returncode not in {0, 1}:
        raise DispatchError("cannot verify review_base ancestry")
    return result.returncode == 0


def _reject_sensitive_keys(value: Any, location: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise DispatchError(f"{location} contains a non-string key")
            if SENSITIVE_KEY.search(key):
                raise DispatchError(f"{location} must not contain secret-shaped field {key!r}")
            _reject_sensitive_keys(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_keys(child, f"{location}[{index}]")


def _role_metadata(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DispatchError(f"role manifest {path.name!r} cannot be read") from exc
    match = re.match(r"\A---\r?\n(?P<body>.*?)\r?\n---(?:\r?\n|$)", text, re.DOTALL)
    if not match:
        raise DispatchError(f"role manifest {path.name!r} has no valid frontmatter")

    metadata: dict[str, Any] = {}
    current_list: str | None = None
    for line in match.group("body").splitlines():
        if not line.strip():
            continue
        item = re.fullmatch(r"\s+-\s+(.+?)\s*", line)
        if item:
            if current_list is None:
                raise DispatchError(f"role manifest {path.name!r} has an orphan list item")
            metadata.setdefault(current_list, []).append(item.group(1))
            continue
        field = re.fullmatch(r"([a-z_]+):\s*(.*?)\s*", line)
        if not field:
            raise DispatchError(f"role manifest {path.name!r} has invalid frontmatter")
        key, value = field.groups()
        current_list = key if not value else None
        metadata[key] = [] if not value else value
    return metadata


def _validate_brief(brief: dict[str, Any], repo: Path, config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    _reject_sensitive_keys(brief, "dispatch brief")
    approval = brief.get("coordinator_approval")
    if not isinstance(approval, dict) or not _non_empty_string(approval.get("approved_by")) or not _non_empty_string(
        approval.get("approved_at")
    ):
        raise DispatchError("dispatch brief requires coordinator_approval with approved_by and approved_at")

    for field in (
        "ticket", "role", "zone", "branch", "worktree", "definition_of_done", "prohibited_changes",
        "verification_commands", "required_gates", "dependencies",
    ):
        if field not in brief:
            raise DispatchError(f"dispatch brief is missing {field!r}")
    if not _non_empty_string(brief["ticket"]) or not _non_empty_string(brief["role"]):
        raise DispatchError("dispatch brief ticket and role must be non-empty strings")
    if not _non_empty_string(brief["zone"]) or not _non_empty_string(brief["branch"]):
        raise DispatchError("dispatch brief zone and branch must be non-empty strings")
    if not _non_empty_string(brief["worktree"]):
        raise DispatchError("dispatch brief worktree must be a non-empty string")
    if not isinstance(brief["definition_of_done"], list) or not all(_non_empty_string(item) for item in brief["definition_of_done"]):
        raise DispatchError("dispatch brief definition_of_done must be a non-empty list of strings")
    if not isinstance(brief["prohibited_changes"], list) or not all(
        _non_empty_string(item) for item in brief["prohibited_changes"]
    ):
        raise DispatchError("dispatch brief prohibited_changes must be a non-empty list of strings")
    for field in ("verification_commands", "required_gates", "dependencies"):
        if not isinstance(brief[field], list) or not all(_non_empty_string(item) for item in brief[field]):
            raise DispatchError(f"dispatch brief {field} must be a list of strings")
    if brief["verification_commands"] != config.get("verification_commands"):
        raise DispatchError("dispatch brief verification_commands must exactly match project configuration")

    role_name = brief["role"]
    role = _role_metadata(repo / ".harness" / "orchestration" / "roles" / f"{role_name}.md")
    if role.get("name") != role_name or role.get("mode") not in {"write", "read-only"}:
        raise DispatchError(f"dispatch brief references invalid role {role_name!r}")
    if brief.get("access") != role["mode"]:
        raise DispatchError(f"dispatch brief access must be {role['mode']!r} for role {role_name!r}")

    project = _read_json(repo / ".harness" / "project.json", "project config")
    branch = brief["branch"]
    branch_pattern = project.get("branch_pattern", r"^feature/issue-[0-9]+-.+")
    base_branch = project.get("base_branch", "master")
    try:
        is_issue_branch = isinstance(branch_pattern, str) and re.fullmatch(branch_pattern, branch) is not None
    except re.error as exc:
        raise DispatchError("project config has an invalid branch_pattern") from exc
    if branch.startswith("integration/") or branch == base_branch or not is_issue_branch:
        raise DispatchError("dispatch brief branch must be an issue branch and never a protected or integration branch")

    assignments = config.get("assignment_plans")
    zones = config.get("backend_zones")
    if not isinstance(assignments, dict) or not isinstance(zones, dict):
        raise DispatchError("project orchestration config has no valid assignment plans or backend zones")
    plan = assignments.get(role_name)
    if not isinstance(plan, dict) or plan.get("zone") != brief["zone"]:
        raise DispatchError(f"dispatch brief zone does not match the project assignment for role {role_name!r}")
    runtime_name = brief.get("resolved_runtime", "codex")
    runtime_plans = plan.get("runtimes")
    if not _non_empty_string(runtime_name) or not isinstance(runtime_plans, dict):
        raise DispatchError("dispatch brief runtime does not match a project runtime assignment")
    plan = runtime_plans.get(runtime_name)
    if not isinstance(plan, dict):
        raise DispatchError(f"dispatch brief runtime {runtime_name!r} is not assigned to role {role_name!r}")
    zone = zones.get(brief["zone"])
    if not isinstance(zone, dict) or not isinstance(zone.get("paths"), list) or not zone["paths"]:
        raise DispatchError(f"dispatch brief references invalid zone {brief['zone']!r}")
    assigned_paths = assignments[role_name].get("write_paths", zone["paths"])
    if role["mode"] == "write" and (not isinstance(assigned_paths, list) or not assigned_paths):
        raise DispatchError("write role assignment must declare valid write_paths")
    if role["mode"] == "write" and not _paths_within_zone(assigned_paths, zone["paths"]):
        raise DispatchError("write role assignment paths must remain inside its backend zone")
    if role["mode"] == "write" and not isinstance(brief.get("write_paths"), list):
        raise DispatchError("write dispatch must declare its one allowed zone paths")
    if role["mode"] == "read-only" and brief.get("write_paths"):
        raise DispatchError("read-only role cannot receive write paths")
    if role["mode"] == "write" and brief["write_paths"] != assigned_paths:
        raise DispatchError("write dispatch paths must exactly match its role assignment")
    candidate = brief.get("candidate_commit")
    if role_name in {"code-review", "qa"} and candidate is None:
        raise DispatchError(f"{role_name} dispatch must pin candidate_commit")
    if candidate is not None:
        candidate = _resolved_commit(repo, candidate)
        if candidate != brief["candidate_commit"]:
            raise DispatchError("dispatch brief candidate_commit must be the full resolved commit SHA")
    if role_name == "code-review":
        scope = brief.get("review_scope")
        if not isinstance(scope, list) or not scope or not all(_non_empty_string(item) for item in scope):
            raise DispatchError("code-review dispatch must declare its immutable review_scope")
        base = brief.get("review_base")
        if base is not None:
            base = _resolved_commit(repo, base)
            if not _is_ancestor(repo, base, candidate):
                raise DispatchError("review_base must be an ancestor of candidate_commit")
        if _candidate_files(repo, candidate, base) != scope:
            raise DispatchError("code-review review_scope does not match the pinned candidate diff")
    return role, plan


def _candidate_profiles(
    config: dict[str, Any], plan: dict[str, Any], role: dict[str, Any], preferred: object = None
) -> list[tuple[str, str, str | None]]:
    profiles = config.get("provider_profiles")
    if not isinstance(profiles, dict) or not isinstance(plan.get("profiles"), list):
        raise DispatchError("project orchestration config has no valid provider profiles")
    candidates: list[tuple[str, str, str | None]] = []
    role_model = _validate_model_id(plan.get("model"))
    role_effort = plan.get("effort")
    if not _non_empty_string(role_effort):
        raise DispatchError("assignment plan effort must be a non-empty string")

    def add(profile_id: object) -> None:
        if not _non_empty_string(profile_id) or profile_id not in profiles:
            raise DispatchError("assignment plan references an unknown provider profile")
        if any(candidate[0] == profile_id for candidate in candidates):
            return
        profile = profiles[profile_id]
        if not isinstance(profile, dict) or not _non_empty_string(profile.get("agent")):
            raise DispatchError(f"provider profile {profile_id!r} has no valid agent")
        capabilities = profile.get("capabilities")
        required_capabilities = role.get("required_capabilities")
        if not isinstance(capabilities, list) or not isinstance(required_capabilities, list) or not set(capabilities).intersection(
            required_capabilities
        ):
            raise DispatchError(f"provider profile {profile_id!r} is incompatible with the requested role")
        candidates.append((profile_id, role_model, role_effort))
        fallback = profile.get("fallback")
        if not isinstance(fallback, list):
            raise DispatchError(f"provider profile {profile_id!r} has invalid fallback")
        for fallback_id in fallback:
            add(fallback_id)

    profile_ids = [preferred] if preferred is not None else plan["profiles"]
    for profile_id in profile_ids:
        add(profile_id)
    if not candidates:
        raise DispatchError("assignment plan has no provider profiles")
    return candidates


def _orca_command(orca_bin: str, args: list[str]) -> list[str]:
    executable = Path(orca_bin)
    if executable.suffix.lower() == ".py":
        return [sys.executable, str(executable), *args]
    return [orca_bin, *args]


def _run_orca(orca_bin: str, args: list[str]) -> dict[str, Any]:
    result = subprocess.run(_orca_command(orca_bin, args), capture_output=True, text=True)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        if result.returncode:
            raise DispatchError("Orca returned non-JSON output for a rejected dispatch") from exc
        raise DispatchError("Orca returned non-JSON output") from exc
    if result.returncode:
        error = payload.get("error") if isinstance(payload, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        raise OrcaLaunchRejected(code in {"agent_unavailable", "model_unavailable", "account_unavailable"})
    if not isinstance(payload, dict) or payload.get("ok") is False:
        raise DispatchError("Orca returned an unsuccessful dispatch result")
    return payload


def _active_workers(payload: dict[str, Any]) -> int:
    result = payload.get("result", payload)
    workers = result.get("workers", []) if isinstance(result, dict) else []
    if not isinstance(workers, list):
        raise DispatchError("Orca worker listing is invalid")
    return len(workers)


def _result_id(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    value: Any = payload.get("result", payload)
    for key in keys:
        if isinstance(value, dict):
            value = value.get(key)
        else:
            return None
    return value if _non_empty_string(value) else None


def _write_record(records_dir: Path, record: dict[str, Any]) -> Path:
    records_dir.mkdir(parents=True, exist_ok=True)
    path = records_dir / f"{record['dispatch_id']}.json"
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
    except FileExistsError as exc:
        raise DispatchError("refusing to overwrite an immutable dispatch record") from exc
    return path


def _dispatch_locked(args: argparse.Namespace, repo: Path, records_dir: Path) -> dict[str, Any]:
    config = _read_json(repo / ".harness" / "orchestration.json", "project orchestration config")
    brief = _read_json(Path(args.brief), "dispatch brief")
    _reject_sensitive_keys(config, "project orchestration config")
    role, plan = _validate_brief(brief, repo, config)
    candidates = _candidate_profiles(config, plan, role, brief.get("resolved_provider_profile"))
    primary_profile, primary_model, primary_effort = candidates[0]
    if brief.get("resolved_runtime", "codex") not in config.get("assignment_plans", {}).get(brief["role"], {}).get("runtimes", {}):
        raise DispatchError("dispatch brief runtime does not match the project assignment")
    if brief.get("resolved_provider_profile") is not None and brief["resolved_provider_profile"] != primary_profile:
        raise DispatchError("dispatch brief provider profile does not match the project assignment")
    if brief.get("resolved_model") is not None and brief["resolved_model"] != primary_model:
        raise DispatchError("dispatch brief model does not match the project assignment")
    if brief.get("resolved_effort") is not None and brief["resolved_effort"] != primary_effort:
        raise DispatchError("dispatch brief effort does not match the project assignment")
    budget = config.get("concurrency_budget")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise DispatchError("project orchestration config has an invalid concurrency_budget")

    _issue_branch_exists(repo, brief["branch"])
    active = _active_workers(
        _run_orca(args.orca_bin, ["orchestration", "worker-list", "--run", args.run, "--terminal-state", "active", "--json"])
    )
    if active >= budget:
        raise DispatchError("concurrency_budget is exhausted; no dispatch was created")

    dispatch_id = f"dispatch-{uuid.uuid4()}"
    task = _run_orca(
        args.orca_bin,
        [
            "orchestration", "task-create", "--run", args.run, "--task-title", dispatch_id, "--spec",
            json.dumps({"dispatch_id": dispatch_id, "brief": brief}, ensure_ascii=False), "--json",
        ],
    )
    task_id = _result_id(task, ("task", "id")) or _result_id(task, ("id",))
    if task_id is None:
        raise DispatchError("Orca task creation returned no task ID")

    profiles = config["provider_profiles"]
    base_ref = brief.get("candidate_commit") or brief["branch"]
    last_error: DispatchError | None = None
    for profile_id, model, effort in candidates:
        profile = profiles[profile_id]
        try:
            worker = _run_orca(
                args.orca_bin,
                [
                    "orchestration", "worker-start", "--run", args.run, "--task", task_id, "--worktree", "new-top-level", "--repo",
                    f"path:{repo}", "--base-branch", base_ref, "--name", brief["branch"], "--display-name",
                    brief["worktree"], "--agent", profile["agent"],
                    "--model", model,
                    *( ["--effort", effort] if effort is not None else [] ),
                    "--setup", "run", "--json",
                ],
            )
        except OrcaLaunchRejected as exc:
            if not exc.safe_to_fallback:
                raise DispatchError("Orca launch outcome is uncertain; no fallback dispatch was created") from exc
            last_error = exc
            continue
        record = {
            "dispatch_id": dispatch_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "brief": copy.deepcopy(brief),
            "resolved": {"profile": profile_id, "agent": profile["agent"], "model": model, "effort": effort},
            "role": {"name": brief["role"], "mode": role["mode"], "zone": brief["zone"]},
            "orca": {"task_id": task_id, "worker_id": _result_id(worker, ("worker", "id"))},
            "terminal_outcome": "ready",
        }
        record_path = _write_record(records_dir, record)
        return {
            "dispatch_id": dispatch_id,
            "record": str(record_path),
            "profile": profile_id,
            "model": model,
            "effort": effort,
        }
    raise DispatchError("all project-configured provider profiles rejected the dispatch") from last_error


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    repo = Path(args.repo).resolve()
    records_dir = Path(args.records_dir).resolve() if args.records_dir else repo / ".harness" / "orca-dispatches"
    records_dir.mkdir(parents=True, exist_ok=True)
    lock = records_dir / ".dispatch.lock"
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise DispatchError("another dispatch is being admitted; retry after it settles") from exc
    try:
        return _dispatch_locked(args, repo, records_dir)
    finally:
        lock.rmdir()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Dispatch an approved backend orchestration role through Orca.")
    commands = root.add_subparsers(dest="command", required=True)
    command = commands.add_parser("dispatch")
    command.add_argument("--repo", default=".", help="target project root")
    command.add_argument("--brief", required=True, help="immutable, approved dispatch brief JSON")
    command.add_argument("--run", required=True, help="coordinator-owned Orca Run ID")
    command.add_argument("--records-dir", help="project-owned directory for immutable dispatch records")
    command.add_argument("--orca-bin", default=os.environ.get("ORCA_CLI_COMMAND", "orca"), help="Orca CLI executable")
    command.set_defaults(func=dispatch)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        output = args.func(args)
    except DispatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
