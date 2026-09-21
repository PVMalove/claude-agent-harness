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
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeGuard

# `harness/bin/harness`'s package_files() copies this file verbatim into target projects as
# `.harness/orchestration/orca_adapter.py` -- a different directory name than the source tree's
# `harness/`. Alias `harness` to whichever of the two this file actually lives under so
# `from harness...` resolves the same way in both places. See docs/adr/0018.
_HARNESS_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _HARNESS_ROOT.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if _HARNESS_ROOT.name != "harness":
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "harness",
        _HARNESS_ROOT / "__init__.py",
        submodule_search_locations=[str(_HARNESS_ROOT)],
    )
    assert _spec is not None and _spec.loader is not None
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules["harness"] = _pkg
    _spec.loader.exec_module(_pkg)

from harness.errors import INTERNAL_INVARIANT_REMEDY, HarnessError, print_and_exit
from harness.orchestration.contract import (
    ContractError,
    JsonObject,
    validate_brief_policy,
)

SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|credential|password|secret|(?:access|auth|refresh|id|bearer)[_-]?token|(?:^|[_-])token(?:$|[_-](?:id|value|secret|key)$))",
    re.IGNORECASE,
)
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")


class DispatchError(HarnessError):
    """An invalid or unsafe request that must not reach Orca."""


class OrcaLaunchRejected(DispatchError):
    """An Orca launch rejection whose recovery safety is explicit."""

    def __init__(self, safe_to_fallback: bool):
        super().__init__(
            "Orca rejected the dispatch request",
            remedy=(
                "the failing provider profile's fallback chain will be tried next"
                if safe_to_fallback
                else "the launch outcome is uncertain -- check Orca directly before retrying, to avoid a duplicate worker"
            ),
        )
        self.safe_to_fallback = safe_to_fallback


def _read_json(path: Path, label: str) -> JsonObject:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DispatchError(
            f"{label} is not valid JSON", remedy=f"fix the JSON syntax in {path}"
        ) from exc
    if not isinstance(data, dict):
        raise DispatchError(
            f"{label} must be a JSON object", remedy=f"rewrite {path} as a JSON object"
        )
    return data


def _non_empty_string(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and bool(value.strip())


def _validate_model_id(value: object) -> str:
    if not _non_empty_string(value) or MODEL_ID.fullmatch(value.strip()) is None:
        raise DispatchError(
            "assignment model must be a CLI model ID or alias without spaces",
            remedy="set the assignment plan's model to a non-empty CLI model ID/alias with no spaces",
        )
    return value.strip()


def _issue_branch_exists(repo: Path, branch: str) -> None:
    for reference in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", reference],
            check=False,
        )
        if result.returncode == 0:
            return
    raise DispatchError(
        "approved issue branch does not exist locally or on origin",
        remedy=f"push or fetch branch {branch!r} so it exists locally or on origin before dispatching",
    )


def _resolved_commit(repo: Path, value: object) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9a-fA-F]{7,64}", value.strip()) is None
    ):
        raise DispatchError(
            "candidate_commit must be a hexadecimal commit SHA",
            remedy="pass candidate_commit as a 7-64 character hex commit SHA",
        )
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "rev-parse",
            "--verify",
            f"{value.strip()}^{{commit}}",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0 or result.stdout.strip() != value.strip().lower():
        raise DispatchError(
            "candidate_commit must resolve to its full commit SHA in the target repository",
            remedy=f"verify commit {value!r} exists in {repo} and pass its full resolved SHA",
        )
    return result.stdout.strip()


def _git_output(repo: Path, arguments: list[str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise DispatchError(
            "cannot inspect the pinned candidate commit",
            remedy=f"inspect the git error above and fix the repository/commit before retrying 'git {' '.join(arguments)}'",
        )
    return result.stdout.strip()


def _candidate_files(repo: Path, candidate: str, base: str | None) -> list[str]:
    if base:
        output = _git_output(
            repo, ["diff", "--name-only", "--no-renames", base, candidate]
        )
    else:
        output = _git_output(
            repo,
            ["diff-tree", "--root", "--no-commit-id", "--name-only", "-r", candidate],
        )
    return [line.replace("\\", "/") for line in output.splitlines() if line.strip()]


def _is_ancestor(repo: Path, base: str, candidate: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", base, candidate],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise DispatchError(
            "cannot verify review_base ancestry",
            remedy=f"inspect the git merge-base error above for base {base!r} and candidate {candidate!r}",
        )
    return result.returncode == 0


def _reject_sensitive_keys(value: object, location: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise DispatchError(
                    f"{location} contains a non-string key",
                    remedy=f"use only string keys in {location}",
                )
            if SENSITIVE_KEY.search(key):
                raise DispatchError(
                    f"{location} must not contain secret-shaped field {key!r}",
                    remedy=f"remove the secret-shaped field {key!r} from {location}; credentials never belong in this config",
                )
            _reject_sensitive_keys(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_keys(child, f"{location}[{index}]")


def _validate_brief(
    brief: Mapping[str, object], repo: Path, config: Mapping[str, object]
) -> tuple[JsonObject, JsonObject]:
    try:
        role, assignment = validate_brief_policy(
            brief,
            _read_json(repo / ".harness" / "project.json", "project config"),
            config,
            repo / ".harness" / "orchestration" / "roles",
            expected_transport="orca",
        )
    except ContractError as exc:
        raise DispatchError(exc.message, remedy=exc.remedy) from exc

    candidate = brief.get("candidate_commit")
    role_name = brief["role"]
    if candidate is None:
        if role_name in {"code-review", "qa"}:
            raise DispatchError(
                f"{role_name} dispatch must pin candidate_commit",
                remedy=f"set candidate_commit before dispatching the {role_name} role",
            )
        return role, assignment["runtime_plan"]
    pinned = _resolved_commit(repo, candidate)
    if pinned != candidate:
        raise DispatchError(
            "dispatch brief candidate_commit must be the full resolved commit SHA",
            remedy=f"set the brief's candidate_commit to its full resolved SHA {pinned}",
        )
    if role_name == "code-review":
        scope = brief.get("review_scope")
        if (
            not isinstance(scope, list)
            or not scope
            or not all(_non_empty_string(item) for item in scope)
        ):
            raise DispatchError(
                "code-review dispatch must declare its immutable review_scope",
                remedy="set review_scope to the non-empty list of changed files",
            )
        base = brief.get("review_base")
        if base is not None:
            base = _resolved_commit(repo, base)
            if not _is_ancestor(repo, base, pinned):
                raise DispatchError(
                    "review_base must be an ancestor of candidate_commit",
                    remedy=f"pass a review_base that is an ancestor of {pinned}",
                )
        if _candidate_files(repo, pinned, base) != scope:
            raise DispatchError(
                "code-review review_scope does not match the pinned candidate diff",
                remedy=f"regenerate review_scope from the actual diff between {base!r} and {pinned}",
            )
    return role, assignment["runtime_plan"]


def _candidate_profiles(
    config: Mapping[str, object],
    plan: Mapping[str, object],
    role: Mapping[str, object],
    preferred: object = None,
) -> list[tuple[str, str, str | None]]:
    profiles = config.get("provider_profiles")
    plan_profiles = plan.get("profiles")
    if not isinstance(profiles, dict) or not isinstance(plan_profiles, list):
        raise DispatchError(
            "project orchestration config has no valid provider profiles",
            remedy="set provider_profiles to an object and the assignment plan's profiles to a list",
        )
    candidates: list[tuple[str, str, str | None]] = []
    role_model = _validate_model_id(plan.get("model"))
    role_effort = plan.get("effort")
    if not _non_empty_string(role_effort):
        raise DispatchError(
            "assignment plan effort must be a non-empty string",
            remedy="set the assignment plan's effort to a non-empty string",
        )

    def add(profile_id: object) -> None:
        if not _non_empty_string(profile_id) or profile_id not in profiles:
            raise DispatchError(
                "assignment plan references an unknown provider profile",
                remedy=f"add {profile_id!r} to provider_profiles, or fix the assignment plan's profile reference",
            )
        if any(candidate[0] == profile_id for candidate in candidates):
            return
        profile = profiles[profile_id]
        if not isinstance(profile, dict) or not _non_empty_string(profile.get("agent")):
            raise DispatchError(
                f"provider profile {profile_id!r} has no valid agent",
                remedy=f"set provider_profiles[{profile_id!r}].agent to a non-empty string",
            )
        capabilities = profile.get("capabilities")
        required_capabilities = role.get("required_capabilities")
        if (
            not isinstance(capabilities, list)
            or not isinstance(required_capabilities, list)
            or not set(capabilities).intersection(required_capabilities)
        ):
            raise DispatchError(
                f"provider profile {profile_id!r} is incompatible with the requested role",
                remedy=f"add one of the role's required_capabilities to provider_profiles[{profile_id!r}].capabilities",
            )
        candidates.append((profile_id, role_model, role_effort))
        fallback = profile.get("fallback")
        if not isinstance(fallback, list):
            raise DispatchError(
                f"provider profile {profile_id!r} has invalid fallback",
                remedy=f"set provider_profiles[{profile_id!r}].fallback to a list",
            )
        for fallback_id in fallback:
            add(fallback_id)

    profile_ids = [preferred] if preferred is not None else plan_profiles
    for profile_id in profile_ids:
        add(profile_id)
    if not candidates:
        raise DispatchError(
            "assignment plan has no provider profiles",
            remedy="add at least one profile ID to the assignment plan's profiles",
        )
    return candidates


def _orca_command(orca_bin: str, args: list[str]) -> list[str]:
    executable = Path(orca_bin)
    if executable.suffix.lower() == ".py":
        return [sys.executable, str(executable), *args]
    return [orca_bin, *args]


def _run_orca(orca_bin: str, args: list[str]) -> JsonObject:
    result = subprocess.run(
        _orca_command(orca_bin, args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        if result.returncode:
            raise DispatchError(
                "Orca returned non-JSON output for a rejected dispatch",
                remedy="inspect the Orca CLI's raw stderr/stdout to see why the command failed",
            ) from exc
        raise DispatchError(
            "Orca returned non-JSON output",
            remedy="check the Orca CLI version and invocation; it should always return JSON",
        ) from exc
    if result.returncode:
        error = payload.get("error") if isinstance(payload, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        raise OrcaLaunchRejected(
            code in {"agent_unavailable", "model_unavailable", "account_unavailable"}
        )
    if not isinstance(payload, dict) or payload.get("ok") is False:
        raise DispatchError(
            "Orca returned an unsuccessful dispatch result",
            remedy="inspect the Orca response payload for the actual failure reason",
        )
    return payload


def _active_workers(payload: Mapping[str, object]) -> int:
    result = payload.get("result", payload)
    workers = result.get("workers", []) if isinstance(result, dict) else []
    if not isinstance(workers, list):
        raise DispatchError(
            "Orca worker listing is invalid",
            remedy="check the Orca CLI version; 'worker-list --json' should return a workers list",
        )
    return len(workers)


def _result_id(payload: Mapping[str, object], keys: tuple[str, ...]) -> str | None:
    value: object = payload.get("result", payload)
    for key in keys:
        if isinstance(value, dict):
            value = value.get(key)
        else:
            return None
    return value if _non_empty_string(value) else None


def _write_record(records_dir: Path, record: Mapping[str, object]) -> Path:
    records_dir.mkdir(parents=True, exist_ok=True)
    path = records_dir / f"{record['dispatch_id']}.json"
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
    except FileExistsError as exc:
        raise DispatchError(
            "refusing to overwrite an immutable dispatch record",
            remedy=f"{path} already exists -- {INTERNAL_INVARIANT_REMEDY}",
        ) from exc
    return path


def _dispatch_locked(
    args: argparse.Namespace, repo: Path, records_dir: Path
) -> dict[str, str | None]:
    config = _read_json(
        repo / ".harness" / "orchestration.json", "project orchestration config"
    )
    brief = _read_json(Path(args.brief), "dispatch brief")
    _reject_sensitive_keys(config, "project orchestration config")
    role, plan = _validate_brief(brief, repo, config)
    candidates = _candidate_profiles(
        config, plan, role, brief.get("resolved_provider_profile")
    )
    primary_profile, primary_model, primary_effort = candidates[0]
    if brief.get("resolved_runtime") not in config.get("assignment_plans", {}).get(
        brief["role"], {}
    ).get("runtimes", {}):
        raise DispatchError(
            "dispatch brief runtime does not match the project assignment",
            remedy=INTERNAL_INVARIANT_REMEDY,
        )
    if (
        brief.get("resolved_provider_profile") is not None
        and brief["resolved_provider_profile"] != primary_profile
    ):
        raise DispatchError(
            "dispatch brief provider profile does not match the project assignment",
            remedy=INTERNAL_INVARIANT_REMEDY,
        )
    if (
        brief.get("resolved_model") is not None
        and brief["resolved_model"] != primary_model
    ):
        raise DispatchError(
            "dispatch brief model does not match the project assignment",
            remedy=INTERNAL_INVARIANT_REMEDY,
        )
    if (
        brief.get("resolved_effort") is not None
        and brief["resolved_effort"] != primary_effort
    ):
        raise DispatchError(
            "dispatch brief effort does not match the project assignment",
            remedy=INTERNAL_INVARIANT_REMEDY,
        )
    budget = config.get("concurrency_budget")
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise DispatchError(
            "project orchestration config has an invalid concurrency_budget",
            remedy="set concurrency_budget to a positive integer in the project orchestration config",
        )

    _issue_branch_exists(repo, brief["branch"])
    active = _active_workers(
        _run_orca(
            args.orca_bin,
            [
                "orchestration",
                "worker-list",
                "--run",
                args.run,
                "--terminal-state",
                "active",
                "--json",
            ],
        )
    )
    if active >= budget:
        raise DispatchError(
            "concurrency_budget is exhausted; no dispatch was created",
            remedy=f"wait for an active worker to finish, or raise concurrency_budget above {budget}",
        )

    dispatch_id = f"dispatch-{uuid.uuid4()}"
    task = _run_orca(
        args.orca_bin,
        [
            "orchestration",
            "task-create",
            "--run",
            args.run,
            "--task-title",
            dispatch_id,
            "--spec",
            json.dumps(
                {"dispatch_id": dispatch_id, "brief": brief}, ensure_ascii=False
            ),
            "--json",
        ],
    )
    task_id = _result_id(task, ("task", "id")) or _result_id(task, ("id",))
    if task_id is None:
        raise DispatchError(
            "Orca task creation returned no task ID",
            remedy="check the Orca CLI version; 'task-create --json' should return a task/id field",
        )

    profiles = config["provider_profiles"]
    base_ref = brief.get("candidate_commit") or brief["branch"]
    last_error: DispatchError | None = None
    for profile_id, model, effort in candidates:
        profile = profiles[profile_id]
        try:
            worker = _run_orca(
                args.orca_bin,
                [
                    "orchestration",
                    "worker-start",
                    "--run",
                    args.run,
                    "--task",
                    task_id,
                    "--worktree",
                    "new-top-level",
                    "--repo",
                    f"path:{repo}",
                    "--base-branch",
                    base_ref,
                    "--name",
                    brief["branch"],
                    "--display-name",
                    brief["worktree"],
                    "--agent",
                    profile["agent"],
                    "--model",
                    model,
                    *(["--effort", effort] if effort is not None else []),
                    "--setup",
                    "run",
                    "--json",
                ],
            )
        except OrcaLaunchRejected as exc:
            if not exc.safe_to_fallback:
                raise DispatchError(
                    "Orca launch outcome is uncertain; no fallback dispatch was created",
                    remedy="check Orca directly (worker-list) before retrying, to avoid a duplicate worker",
                ) from exc
            last_error = exc
            continue
        record = {
            "dispatch_id": dispatch_id,
            "created_at": datetime.now(UTC).isoformat(),
            "brief": copy.deepcopy(brief),
            "resolved": {
                "profile": profile_id,
                "agent": profile["agent"],
                "model": model,
                "effort": effort,
            },
            "role": {
                "name": brief["role"],
                "mode": role["mode"],
                "zone": brief["zone"],
            },
            "orca": {
                "task_id": task_id,
                "worker_id": _result_id(worker, ("worker", "id")),
            },
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
    raise DispatchError(
        "all project-configured provider profiles rejected the dispatch",
        remedy="check Orca/provider availability, or add a working profile to the assignment plan",
    ) from last_error


def dispatch(args: argparse.Namespace) -> dict[str, str | None]:
    repo = Path(args.repo).resolve()
    records_dir = (
        Path(args.records_dir).resolve()
        if args.records_dir
        else repo / ".harness" / "orca-dispatches"
    )
    records_dir.mkdir(parents=True, exist_ok=True)
    lock = records_dir / ".dispatch.lock"
    try:
        lock.mkdir()
    except FileExistsError as exc:
        raise DispatchError(
            "another dispatch is being admitted; retry after it settles",
            remedy=f"wait for the other dispatch to finish, or remove a stale lock at {lock} if none is actually running",
        ) from exc
    try:
        return _dispatch_locked(args, repo, records_dir)
    finally:
        lock.rmdir()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Dispatch an approved backend orchestration role through Orca."
    )
    commands = root.add_subparsers(dest="command", required=True)
    command = commands.add_parser("dispatch")
    command.add_argument("--repo", default=".", help="target project root")
    command.add_argument(
        "--brief", required=True, help="immutable, approved dispatch brief JSON"
    )
    command.add_argument("--run", required=True, help="coordinator-owned Orca Run ID")
    command.add_argument(
        "--records-dir", help="project-owned directory for immutable dispatch records"
    )
    command.add_argument(
        "--orca-bin",
        default=os.environ.get("ORCA_CLI_COMMAND", "orca"),
        help="Orca CLI executable",
    )
    command.set_defaults(func=dispatch)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        output = args.func(args)
    except HarnessError as exc:
        return print_and_exit(exc)
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
