"""Validate the runtime's actual checkout before it may submit a role report.

The coordinator owns the small interface: ``attest(repo, dispatch, worktree)``.  It hides Git
worktree discovery and role-specific pin checks so adapters and report gates share one proof.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from ..errors import HarnessError
from ..json_types import JsonObject

# Local Git plumbing only; a slow disk or a huge repository is the worst expected case.
GIT_TIMEOUT_SECONDS = 60
# Runtime and adapter probes are deliberately much shorter than a worker session.  They only
# inspect executable capability (normally ``--help``); they never hand over a dispatch brief.
PROBE_TIMEOUT_SECONDS = 15
PROBE_OUTPUT_LIMIT = 1200


class AttestationError(HarnessError):
    """The runtime did not start in the immutable dispatch's Git context."""


_UNSUPPORTED_OPTION = re.compile(
    r"(?:unrecognized|unknown|unsupported|invalid) (?:command|argument|option|flag)"
    r"|no such option|not a valid option|not recognized as an internal",
    re.IGNORECASE,
)
_SECRET_ARGUMENT = re.compile(
    r"(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret|token)=",
    re.IGNORECASE,
)
_SECRET_OUTPUT = re.compile(
    r"((?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret|token)\s*[=:]\s*)\S+",
    re.IGNORECASE,
)


def _bounded_output(value: object) -> str:
    """Keep probe diagnostics useful without copying an unbounded adapter log into evidence."""
    text = value if isinstance(value, str) else ""
    return _SECRET_OUTPUT.sub(r"\1<redacted>", text[:PROBE_OUTPUT_LIMIT])


def _command(value: str | Sequence[str], label: str) -> list[str]:
    if isinstance(value, str):
        parts = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        parts = list(value)
    else:
        raise AttestationError(
            f"{label} must be a command string or a list of strings",
            remedy=f"configure {label} as an executable command without secrets",
        )
    if not parts or not all(isinstance(part, str) and part.strip() for part in parts):
        raise AttestationError(
            f"{label} must contain non-empty command strings",
            remedy=f"configure {label} as a non-empty command",
        )
    return [part.strip() for part in parts]


def _safe_command(command: Sequence[str]) -> list[str]:
    return ["<redacted>" if _SECRET_ARGUMENT.search(part) else part for part in command]


def probe_command(
    command: str | Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout: int = PROBE_TIMEOUT_SECONDS,
    label: str = "runtime preflight",
) -> JsonObject:
    """Run one bounded capability probe and return evidence instead of starting a worker.

    A non-zero ``--help`` result without a recognised unsupported-option diagnostic remains
    ``unknown``.  That distinction prevents a broken adapter from being silently treated as a
    supported one, while leaving the human a useful limitation to investigate.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        raise AttestationError(
            "runtime probe timeout must be a positive integer",
            remedy="configure a positive bounded timeout for the read-only runtime probe",
        )
    parts = _command(command, "runtime probe command")
    safe = _safe_command(parts)
    evidence: JsonObject = {
        "operation": label,
        "command": safe,
        "launch_performed": False,
        "worker_started": False,
        "timeout_seconds": timeout,
    }
    try:
        result = subprocess.run(
            parts,
            cwd=Path(cwd).resolve() if cwd is not None else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            **evidence,
            "status": "timeout",
            "error": f"{label} timed out after {timeout}s",
            "stdout": _bounded_output(exc.stdout),
            "stderr": _bounded_output(exc.stderr),
        }
    except OSError as exc:
        return {
            **evidence,
            "status": "unavailable",
            "error": _bounded_output(str(exc)),
        }
    stdout = _bounded_output(result.stdout)
    stderr = _bounded_output(result.stderr)
    combined = f"{stdout}\n{stderr}"
    if _UNSUPPORTED_OPTION.search(combined):
        status = "unsupported"
    elif result.returncode == 0:
        status = "supported"
    else:
        status = "unknown"
    return {
        **evidence,
        "status": status,
        "exit_code": result.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }


def probe_adapter(
    adapter: str | Sequence[str] | None,
    adapter_args: Sequence[str] | None = None,
    *,
    cwd: str | Path | None = None,
    timeout: int = PROBE_TIMEOUT_SECONDS,
) -> JsonObject:
    """Probe a project adapter's dispatch CLI without passing a repository or a brief.

    The production delivery command appends ``dispatch ... --repo --brief``.  This probe only
    asks for help, so unsupported flags are diagnosed before a worker can be handed an immutable
    brief.  ``None`` is an explicit limitation, not a successful capability result.
    """
    if adapter is None:
        return {
            "operation": "adapter-help",
            "status": "not-configured",
            "launch_performed": False,
            "worker_started": False,
            "limitation": "no project-owned adapter was configured for a preflight probe",
        }
    base = _command(adapter, "runtime adapter")
    arguments = list(adapter_args or [])
    if not all(isinstance(argument, str) and argument.strip() for argument in arguments):
        raise AttestationError(
            "runtime adapter arguments must be non-empty strings",
            remedy="remove blank adapter arguments and keep launch flags out of the preflight",
        )
    forbidden = [
        argument
        for argument in arguments
        if argument in {"dispatch", "--dispatch", "--repo", "--brief"}
        or argument.startswith(("--dispatch=", "--repo=", "--brief="))
    ]
    if forbidden:
        return {
            "operation": "adapter-help",
            "status": "invalid-configuration",
            "command": _safe_command(base),
            "launch_performed": False,
            "worker_started": False,
            "unsupported_arguments": forbidden,
            "limitation": "adapter arguments would override the coordinator-owned dispatch, repo or brief",
        }
    if len(base) == 1 and base[0].lower().endswith(".py"):
        base = [sys.executable, *base]
    return probe_command(
        [*base, "dispatch", *arguments, "--help"],
        cwd=cwd,
        timeout=timeout,
        label="adapter-help",
    )


def validate_runtime_evidence(
    dispatch: Mapping[str, object], observed: Mapping[str, object] | None
) -> JsonObject:
    """Compare explicit runtime observations with an immutable brief, fail-closed.

    A runtime/model-list observation is not a worker self-report.  Only the coordinator's
    ``reported_model`` shape (or an explicit ``source`` naming a worker self-report) can satisfy
    the model identity requirement; this function never fills a missing model from a probe.
    """
    source = observed.get("source") if isinstance(observed, Mapping) else None
    if source is None and isinstance(observed, Mapping) and "reported_model" in observed:
        source = "model-self-report"
    model = (
        observed.get("reported_model")
        if isinstance(observed, Mapping)
        else None
    )
    if model is None and isinstance(observed, Mapping):
        model = observed.get("model")
    expected_model = dispatch.get("resolved_model") or dispatch.get("model")
    expected_worktree = dispatch.get("worker_worktree") or dispatch.get("worktree")
    actual_worktree = (
        observed.get("worktree")
        if isinstance(observed, Mapping)
        else None
    )
    if actual_worktree is None and isinstance(observed, Mapping):
        actual_worktree = observed.get("cwd")
    expected_effort = dispatch.get("resolved_effort") or dispatch.get("effort")
    actual_effort = None
    if isinstance(observed, Mapping):
        actual_effort = observed.get("reasoningEffort") or observed.get(
            "reasoning_effort"
        ) or observed.get("effort")
    reasons: list[str] = []
    fields: JsonObject = {
        "source": source,
        "model": model,
        "expected_model": expected_model,
        "worktree": actual_worktree,
        "expected_worktree": expected_worktree,
    }
    if expected_effort is not None:
        fields.update({"effort": actual_effort, "expected_effort": expected_effort})
    if source not in {"model-self-report", "worker-self-report", "dispatch-self-report"}:
        reasons.append("runtime observation is not the mandatory worker model self-report")
    if not isinstance(model, str) or not model.strip():
        reasons.append("model self-report is missing")
    elif isinstance(expected_model, str) and model != expected_model:
        reasons.append("reported model does not match the immutable brief")
    if isinstance(expected_worktree, str):
        if not isinstance(actual_worktree, str) or not actual_worktree.strip():
            reasons.append("runtime worktree evidence is missing")
        else:
            try:
                worktree_match = Path(actual_worktree).resolve() == Path(
                    expected_worktree
                ).resolve()
            except OSError:
                worktree_match = actual_worktree == expected_worktree
            fields["worktree_match"] = worktree_match
            if not worktree_match:
                reasons.append("reported runtime worktree does not match the immutable brief")
    if expected_effort is not None:
        if actual_effort is None:
            reasons.append("runtime reasoning effort evidence is missing")
        elif actual_effort != expected_effort:
            reasons.append("reported reasoning effort does not match the immutable brief")
    if isinstance(observed, Mapping) and observed.get("status") in {
        "unsupported",
        "not-configured",
        "invalid-configuration",
    }:
        reasons.append(f"runtime attestation is {observed['status']}")
    if not reasons:
        status = "verified"
    elif any("not the mandatory" in reason for reason in reasons):
        status = "unsupported"
    elif any("missing" in reason for reason in reasons):
        status = "missing"
    else:
        status = "mismatch"
    return {
        "status": status,
        "match": status == "verified",
        "source": source,
        "model_self_report_required": True,
        "fields": fields,
        "reasons": reasons,
    }


# Stable, descriptive aliases for callers that use the terms from the operator playbook.
probe_runtime = probe_command
verify_runtime_evidence = validate_runtime_evidence


WORKER_ROLES_REQUIRING_ISSUE_WORKTREE = frozenset(
    {
        "architect",
        "developer",
        "conflict-resolver",
        "database-migrations",
        "messaging-integration",
        "code-review",
    }
)
# Roles that write in the issue worktree itself; other roles run in the checkout they select.
ISSUE_WORKTREE_WRITER_ROLES = frozenset({"architect", "developer", "conflict-resolver"})


def requires_attestation(dispatch: Mapping[str, object]) -> bool:
    """Writer/review startup proof is mandatory; projects may also require it for other roles."""
    return (
        dispatch.get("role") in WORKER_ROLES_REQUIRING_ISSUE_WORKTREE
        or dispatch.get("worker_attestation_required") is True
    )


def _git_command(path: Path, *arguments: str) -> list[str]:
    # The coordinator may run under a different Windows sandbox account. Trust only the
    # already selected repository or registered worktree for this one Git invocation.
    trusted = path.resolve()
    return [
        "git",
        "-c",
        f"safe.directory={trusted}",
        "-C",
        str(trusted),
        *arguments,
    ]


def _run_git(path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run one bounded Git command; a hung or missing git is an attestation failure."""
    command = f"git {' '.join(arguments)}"
    try:
        return subprocess.run(
            _git_command(path, *arguments),
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise AttestationError(
            f"{command} did not complete: {exc}",
            remedy=f"make git available and responsive for {path.resolve()}, then retry '{command}'",
        ) from exc


def _git(path: Path, *arguments: str) -> str:
    result = _run_git(path, *arguments)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise AttestationError(
            detail or f"git {' '.join(arguments)} failed",
            remedy=f"inspect the git error above and fix the worktree/repository state before retrying 'git {' '.join(arguments)}'",
        )
    return result.stdout.strip()


def _registered_worktrees(repo: Path) -> set[Path]:
    worktrees: set[Path] = set()
    for line in _git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            worktrees.add(Path(line.removeprefix("worktree ")).resolve())
    return worktrees


def attest(repo: Path, dispatch: Mapping[str, object], worktree: str) -> dict[str, str]:
    """Return immutable startup evidence or reject a wrong CWD before role work is accepted."""
    checkout = Path(worktree).resolve()
    if (
        dispatch.get("role") in WORKER_ROLES_REQUIRING_ISSUE_WORKTREE
        and checkout == repo.resolve()
    ):
        raise AttestationError(
            "worker checkout must be the registered issue worktree, not the coordinator main checkout",
            remedy="start the worker in the batch's registered issue worktree and report its canonical top-level path",
        )
    expected_path = dispatch.get("worker_worktree")
    if expected_path is None and dispatch.get("role") in {
        "architect",
        "developer",
        "conflict-resolver",
    }:
        expected_path = dispatch.get("worktree")
    if isinstance(expected_path, str) and checkout != Path(expected_path).resolve():
        raise AttestationError(
            "runtime worktree path does not match the expected worker checkout",
            remedy=f"start the worker in {Path(expected_path).resolve()} and report that canonical top-level path",
        )
    if not checkout.is_dir():
        raise AttestationError(
            "reported worktree does not exist",
            remedy=f"report the real worktree path instead of {checkout}",
        )
    if checkout not in _registered_worktrees(repo):
        raise AttestationError(
            "reported worktree is not registered by the dispatch repository",
            remedy=f"run 'git worktree add' for {checkout} or report the correct registered worktree",
        )
    try:
        top_level = Path(_git(checkout, "rev-parse", "--show-toplevel")).resolve()
    except AttestationError as exc:
        raise AttestationError(
            "reported worktree is not a Git worktree",
            remedy=f"report a directory that is a real Git worktree, not {checkout}",
        ) from exc
    if top_level != checkout:
        raise AttestationError(
            "reported worktree must be its Git top-level, not a subdirectory",
            remedy=f"report the Git top-level {top_level} instead of the subdirectory {checkout}",
        )
    head = _git(checkout, "rev-parse", "--verify", "HEAD^{commit}")
    snapshot = dispatch.get("snapshot_commit")
    if isinstance(snapshot, str) and snapshot and head != snapshot:
        raise AttestationError(
            f"runtime worktree HEAD ({head}) does not match the immutable startup snapshot ({snapshot}); "
            "do not rewrite the existing commit history to satisfy this check",
            remedy="create a new dispatch with the current candidate pinned as snapshot_commit, or use a worktree already at the immutable snapshot",
        )
    role = dispatch.get("role")
    branch = _git(checkout, "branch", "--show-current")
    if role in {"architect", "developer", "conflict-resolver"}:
        expected_branch = dispatch.get("branch")
        if not isinstance(expected_branch, str):
            raise AttestationError(
                "runtime worktree branch does not match the immutable issue branch",
                remedy=f"pin a string issue branch in the dispatch (got {expected_branch!r}) before dispatching this role",
            )
        if branch != expected_branch:
            raise AttestationError(
                "runtime worktree branch does not match the immutable issue branch",
                remedy=f"checkout branch {expected_branch!r} in {checkout} before dispatching this role",
            )
        ancestor = _run_git(repo, "merge-base", "--is-ancestor", head, expected_branch)
        if ancestor.returncode:
            raise AttestationError(
                "runtime worktree HEAD is not reachable from the immutable issue branch",
                remedy=f"rebase or merge {expected_branch!r} so commit {head} is reachable from it, or dispatch from a worktree that is",
            )
    elif role in {"verification", "code-review"}:
        candidate = dispatch.get("candidate_commit")
        if head != candidate:
            raise AttestationError(
                f"{role} runtime worktree HEAD does not match the pinned candidate commit",
                remedy=f"checkout commit {candidate} in {checkout} before dispatching the {role} role",
            )
    return {"worktree": str(checkout), "branch": branch, "head_commit": head}
