"""Plan selection and pre-action verification of coordinator-executed operations."""

import errno
import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast

import pytest

from harness.orchestration import extensions, operation_access, runtime_access
from harness.orchestration.core import git_utils
from harness.orchestration.core.utils import CoordinatorError, JsonObject


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return repo


def _brief(repo: Path, config: JsonObject) -> JsonObject:
    plan = runtime_access.resolve_plan(
        repo, repo, config, "qa", "read-only", operation="qa"
    )
    return {
        "role": "qa",
        "access": "read-only",
        "worktree": str(repo),
        "runtime_access": plan,
        "transition": {"runtime_access_sha256": plan["plan_digest"]},
    }


def test_a_pinned_brief_plan_is_used_even_after_the_live_config_changes(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    pinned = {"access_policy": {"defaults": {"mode": "inherit"}}}
    brief = _brief(repo, pinned)
    live = {"access_policy": {"defaults": {"mode": "unsandboxed"}}}
    plan = operation_access.select_plan(repo, live, "qa", brief=brief)
    assert plan == brief["runtime_access"]
    assert plan["mode"] == "inherit"


def test_a_pinned_plan_that_is_not_bound_to_its_approval_is_refused(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    brief = _brief(repo, {"access_policy": {"defaults": {"mode": "inherit"}}})
    brief["transition"] = {"runtime_access_sha256": "0" * 64}
    with pytest.raises(runtime_access.AccessError, match="not bound"):
        operation_access.select_plan(repo, {}, "qa", brief=brief)


def test_a_historical_brief_without_a_plan_selects_legacy_inherit(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    live = {"access_policy": {"defaults": {"mode": "sandbox"}}}
    plan = operation_access.select_plan(
        repo, live, "publish", brief={"role": "developer", "transition": {}}
    )
    assert plan["mode"] == "inherit"
    assert set(plan["sources"].values()) == {"legacy"}
    assert plan["requirements"] == []


def test_the_live_config_selects_the_operation_override_without_a_brief(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    config = {
        "access_policy": {
            "defaults": {"mode": "inherit"},
            "roles": {"developer": {"mode": "sandbox"}},
            "operations": {"git": {"network": {"hosts": ["github.com"]}}},
        }
    }
    plan = operation_access.select_plan(repo, config, "git", worktree=repo)
    assert plan["mode"] == "inherit"
    assert plan["network"] == {"hosts": ["github.com"]}
    assert plan["sources"]["network"] == "operations.git"
    checkout = next(i for i in plan["requirements"] if i["resource"] == "checkout")
    assert checkout["access"] == "write"


def test_a_project_without_authored_access_keeps_legacy_inherit(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    plan = operation_access.select_plan(repo, {}, "qa")
    assert plan["mode"] == "inherit"
    assert set(plan["sources"].values()) == {"legacy"}


def test_an_unknown_operation_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(
        runtime_access.AccessError, match="unknown coordinator operation"
    ):
        operation_access.select_plan(repo, {}, "deploy")


_INHERIT = {"access_policy": {"defaults": {"mode": "inherit"}}}
_needs_non_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="a privileged process is not denied by file permissions",
)


def _project(tmp_path: Path) -> Path:
    repo = _repo(tmp_path)
    (repo / ".harness").mkdir()
    return repo


def _plan(
    repo: Path, config: JsonObject = _INHERIT, operation: str = "qa"
) -> JsonObject:
    return operation_access.select_plan(repo, config, operation, worktree=repo)


@contextmanager
def _read_only(path: Path) -> Iterator[None]:
    mode = path.stat().st_mode & 0o777
    path.chmod(0o555)
    try:
        yield
    finally:
        path.chmod(mode)


def _bare_remote(tmp_path: Path, repo: Path) -> Path:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", str(remote)], check=True
    )
    return remote


def test_a_legacy_plan_is_not_probed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    evidence = operation_access.verify(
        "qa", operation_access.select_plan(repo, {}, "qa"), repo=repo
    )
    assert evidence["status"] == "legacy-inherit"
    assert evidence["checks"] == []


def test_an_inherited_plan_verifies_every_requirement_and_leaves_no_probe_behind(
    tmp_path: Path,
) -> None:
    repo = _project(tmp_path)
    clean_room = repo / ".harness/runs/qa"
    evidence = operation_access.verify(
        "qa", _plan(repo), repo=repo, checkout=clean_room
    )
    checks = {check["label"]: check for check in evidence["checks"]}
    assert evidence["status"] == "verified"
    assert {
        "mode",
        "shared Git metadata",
        "shared storage",
        "clean-room checkout",
    } <= set(checks)
    assert {check["state"] for check in checks.values()} == {"verified"}
    assert clean_room.is_dir()
    leftovers = [
        path
        for root in (repo / ".git", repo / ".harness", clean_room)
        for path in root.rglob(".access-probe-*")
    ]
    assert leftovers == []


@_needs_non_root
def test_a_confirmed_metadata_write_denial_stops_with_structured_evidence_and_a_remedy(
    tmp_path: Path,
) -> None:
    repo = _project(tmp_path)
    with _read_only(repo / ".git"):
        with pytest.raises(operation_access.OperationAccessError) as stopped:
            operation_access.verify("git", _plan(repo, operation="git"), repo=repo)
    denial = next(
        check
        for check in stopped.value.evidence["checks"]
        if check["requirement"] == "git_common"
    )
    assert stopped.value.evidence["status"] == "denied"
    assert denial["state"] == "denied"
    assert denial["path"] == str(repo / ".git")
    assert str(repo / ".git") in stopped.value.remedy
    assert isinstance(stopped.value, CoordinatorError)


def test_a_missing_requirement_is_unverified_not_assumed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)  # no .harness directory: shared storage cannot be proven
    with pytest.raises(operation_access.OperationAccessError) as stopped:
        operation_access.verify("qa", _plan(repo), repo=repo)
    assert stopped.value.evidence["status"] == "unverified"
    assert "does not exist" in stopped.value.message


def test_an_operation_may_not_exceed_the_pinned_plan(tmp_path: Path) -> None:
    repo = _project(tmp_path)
    plan = runtime_access.resolve_plan(repo, repo, _INHERIT, "qa", "read-only")
    with pytest.raises(operation_access.OperationAccessError) as stopped:
        operation_access.verify("qa", plan, repo=repo)
    assert stopped.value.evidence["status"] == "denied"
    assert "does not allow write on git_common" in stopped.value.message


def test_a_mode_the_coordinator_cannot_prove_is_unsupported(tmp_path: Path) -> None:
    repo = _project(tmp_path)
    config = {
        "access_policy": {
            "defaults": {"mode": "inherit"},
            "operations": {"qa": {"mode": "sandbox"}},
        }
    }
    with pytest.raises(operation_access.OperationAccessError) as stopped:
        operation_access.verify("qa", _plan(repo, config), repo=repo)
    assert stopped.value.evidence["status"] == "unsupported"
    assert "native runtime" in stopped.value.remedy


def test_a_native_runtime_that_proves_the_mode_makes_it_supported(
    tmp_path: Path,
) -> None:
    repo = _project(tmp_path)

    class Native:
        def observe(
            self, plan: JsonObject, transport: str
        ) -> extensions.RuntimeAccessObservation:
            return extensions.RuntimeAccessObservation(
                plan_digest=plan["plan_digest"],
                transport=transport,
                supported_modes=("sandbox",),
                effective_mode="sandbox",
                mechanism="native-apply",
                environment_id="coordinator",
                launch_id="session",
                source="native-runtime",
                observed_at=datetime.now(UTC).isoformat(),
                hosts=(),
                filesystem=tuple(
                    (item["path"], item["access"]) for item in plan["requirements"]
                ),
            )

        def apply(
            self, brief: JsonObject, observation: extensions.RuntimeAccessObservation
        ) -> extensions.RuntimeAccessObservation:
            return replace(observation, applied=True)

    extensions.register("runtime_access", "test-coordinator-native", Native())
    try:
        config = {
            "access_policy": {"defaults": {"mode": "sandbox"}},
            "extensions": {"runtime_access": "test-coordinator-native"},
        }
        evidence = operation_access.verify("qa", _plan(repo, config), repo=repo)
    finally:
        extensions.unregister("runtime_access", "test-coordinator-native")
    mode = next(c for c in evidence["checks"] if c["requirement"] == "mode")
    assert (evidence["status"], mode["source"]) == ("verified", "native-runtime")


def test_a_reachable_remote_is_verified(tmp_path: Path) -> None:
    repo = _project(tmp_path)
    _bare_remote(tmp_path, repo)
    evidence = operation_access.verify(
        "publish", _plan(repo, operation="publish"), repo=repo, remote="origin"
    )
    remote = next(c for c in evidence["checks"] if c["requirement"] == "remote")
    assert remote["state"] == "verified"


def test_an_unreachable_remote_is_unverified_and_never_assumed(tmp_path: Path) -> None:
    repo = _project(tmp_path)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", str(tmp_path / "gone.git")],
        check=True,
    )
    with pytest.raises(operation_access.OperationAccessError) as stopped:
        operation_access.verify(
            "git", _plan(repo, operation="git"), repo=repo, remote="origin"
        )
    assert stopped.value.evidence["status"] == "unverified"
    assert "remote origin" in stopped.value.message


def test_a_remote_host_outside_the_approved_hosts_is_denied_before_any_network_use(
    tmp_path: Path,
) -> None:
    repo = _project(tmp_path)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "git@example.invalid:o/r.git",
        ],
        check=True,
    )
    config = {
        "access_policy": {
            "defaults": {"mode": "inherit", "network": {"hosts": ["github.com"]}}
        }
    }
    with pytest.raises(operation_access.OperationAccessError) as stopped:
        operation_access.verify(
            "publish", _plan(repo, config, "publish"), repo=repo, remote="origin"
        )
    assert stopped.value.evidence["status"] == "denied"
    assert "example.invalid" in stopped.value.remedy


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("git@github.com:o/r.git", "github.com"),
        ("https://user@Example.org:8443/o/r.git", "example.org"),
        ("ssh://git@host.example/o/r.git", "host.example"),
        ("file:///srv/r.git", None),
        ("/srv/r.git", None),
        ("../r.git", None),
        ("C:/r.git", None),
    ],
)
def test_the_remote_host_is_taken_from_the_url_not_from_a_local_path(
    url: str, host: str | None
) -> None:
    assert operation_access._remote_host(url) == host


@pytest.mark.parametrize(
    ("detail", "category"),
    [
        (
            "fatal: Unable to create '/r/.git/index.lock': Permission denied",
            git_utils.METADATA_DENIED,
        ),
        (
            "error: could not lock config file /r/.git/config: Read-only file system",
            git_utils.METADATA_DENIED,
        ),
        ("git@github.com: Permission denied (publickey).", git_utils.REMOTE_DENIED),
        ("remote: Permission to o/r.git denied to user.", git_utils.REMOTE_DENIED),
        (
            "remote: error: insufficient permission for adding an object to repository database ./objects",
            git_utils.REMOTE_DENIED,
        ),
        (
            "fatal: Authentication failed for 'https://github.com/o/r.git/'",
            git_utils.REMOTE_DENIED,
        ),
        (
            "fatal: unable to access 'https://x/': Could not resolve host: x",
            git_utils.REMOTE_UNREACHABLE,
        ),
        (
            "fatal: '/gone.git' does not appear to be a git repository",
            git_utils.REMOTE_UNREACHABLE,
        ),
        ("fatal: not a git repository (or any of the parent directories)", None),
        ("error: pathspec 'x' did not match any file(s) known to git", None),
        # Ordinary failures that merely resemble a refusal stay unclassified.
        (
            "fatal: Unable to create '/r/.git/index.lock': File exists.\n\n"
            "Another git process seems to be running in this repository",
            None,
        ),
        (
            "fatal: cannot lock ref 'refs/heads/a/b': 'refs/heads/a' exists; "
            "cannot create 'refs/heads/a/b'",
            None,
        ),
        (
            "error: cannot lock ref 'refs/heads/x': is at abc but expected def",
            None,
        ),
        (
            "error: could not apply 1a2b3c4... fix: handle permission denied on metadata write",
            None,
        ),
        (
            "Rebasing (1/2)\nerror: could not apply 1a2b3c4... fix: access denied\n"
            "hint: Resolve all conflicts manually\n"
            "CONFLICT (content): Merge conflict in a.py",
            None,
        ),
        (
            "error: could not apply 1a2b3c4... handle Permission denied\n"
            "fatal: Unable to create '/r/.git/index.lock': Permission denied",
            git_utils.METADATA_DENIED,
        ),
    ],
)
def test_one_classifier_names_the_git_access_failures(
    detail: str, category: str | None
) -> None:
    assert git_utils.classify_git_failure(detail) == category


@_needs_non_root
def test_a_git_command_refused_by_the_environment_raises_the_access_subclass(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    with _read_only(repo / ".git"):
        with pytest.raises(git_utils.GitAccessError) as refused:
            git_utils._git(repo, "config", "user.name", "someone")
    assert refused.value.category == git_utils.METADATA_DENIED
    assert "write access to the repository's Git metadata" in refused.value.remedy
    assert refused.value.message.startswith("git command failed:")


def test_an_ordinary_git_failure_stays_a_plain_coordinator_error(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(CoordinatorError) as failed:
        git_utils._git(repo, "rev-parse", "--verify", "no-such-ref")
    assert not isinstance(failed.value, git_utils.GitAccessError)


def test_a_stale_index_lock_stays_a_plain_coordinator_error(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".git" / "index.lock").write_text("", encoding="utf-8")
    with pytest.raises(CoordinatorError) as failed:
        git_utils._git(repo, "add", "-A")
    assert not isinstance(failed.value, git_utils.GitAccessError)
    assert "index.lock" in failed.value.message


class _RunFn(Protocol):
    def __call__(
        self, command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]: ...


@pytest.mark.parametrize("step", ["get-url", "ls-remote"])
@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired(["git"], 30),
        FileNotFoundError(2, "No such file or directory", "git"),
    ],
)
def test_a_remote_lookup_that_hangs_or_cannot_start_is_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str, failure: Exception
) -> None:
    repo = _project(tmp_path)
    _bare_remote(tmp_path, repo)
    plan = _plan(repo, operation="git")
    real_run = cast(_RunFn, subprocess.run)
    timeouts: list[object] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        timeouts.append(kwargs.get("timeout"))
        if step in command:
            raise failure
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    entry = operation_access._remote_check("git", plan, repo, "origin")

    assert entry["state"] == "unverified"
    assert entry["source"] == "probe"
    assert str(entry["remedy"]).strip()
    assert all(isinstance(value, int) and value > 0 for value in timeouts)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            PermissionError(errno.EACCES, "Permission denied"),
            ("denied", os.strerror(errno.EACCES)),
        ),
        (OSError(errno.ENOENT, "gone"), ("unverified", os.strerror(errno.ENOENT))),
        (
            OSError("probe failed without an errno"),
            ("unverified", "probe failed without an errno"),
        ),
    ],
)
def test_a_probe_refusal_names_its_actual_cause(
    error: OSError, expected: tuple[str, str]
) -> None:
    assert operation_access._refusal(error) == expected


def test_a_non_object_pinned_plan_is_an_internal_invariant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from harness.errors import INTERNAL_INVARIANT_REMEDY

    monkeypatch.setattr(operation_access, "validate_binding", lambda brief: None)
    with pytest.raises(runtime_access.AccessError) as caught:
        operation_access.select_plan(
            tmp_path, {}, "qa", brief={"runtime_access": "not an object"}
        )
    assert caught.value.remedy == INTERNAL_INVARIANT_REMEDY

    with pytest.raises(runtime_access.AccessError) as caught:
        operation_access.is_legacy_inherit({"sources": "legacy"})
    assert caught.value.remedy == INTERNAL_INVARIANT_REMEDY
