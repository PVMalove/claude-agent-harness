"""A batch started on a branch that already holds commits of an earlier batch measures its own
changes from ``branch_start_commit`` (issue #686)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness.orchestration.core.git_utils import _changed_files_between
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.workflow import rebase, reports
from harness.orchestration.workflow.fix_forward import FixForwardHistory

DEVELOPER = {"mode": "write", "name": "developer"}


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def _commit(repo: Path, path: str) -> str:
    (repo / path).write_text(f"{path}\n", encoding="utf-8")
    _git(repo, "add", path)
    _git(
        repo,
        "-c",
        "user.name=Harness Test",
        "-c",
        "user.email=harness-test@example.invalid",
        "commit",
        "-q",
        "-m",
        f"add {path}",
    )
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def published_branch(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """An issue branch that an earlier, finished batch already published.

    ``base`` is the integration base, ``old1`` and ``old2`` the commits of the earlier batch (the
    batch under test starts at ``old2``) and ``new`` the one commit of the batch under test.
    ``side`` sits on a side branch off ``base`` and ``unrelated`` names no commit at all.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    commits = {"base": _commit(repo, "base.txt")}
    commits["old1"] = _commit(repo, "old1.py")
    commits["old2"] = _commit(repo, "old2.py")
    commits["new"] = _commit(repo, "new.py")
    _git(repo, "switch", "-q", "-c", "side", commits["base"])
    commits["side"] = _commit(repo, "side.py")
    _git(repo, "switch", "-q", "-")
    commits["unrelated"] = "0" * 40
    return repo, commits


def _batch(commits: dict[str, str], **fields: object) -> JsonObject:
    return {
        "base_commit": commits["base"],
        "integration_base_commit": commits["base"],
        "branch_start_commit": commits["old2"],
        "dispatches": [],
        **fields,
    }


def _developer(commits: dict[str, str], **fields: object) -> JsonObject:
    return {
        "dispatch_id": "dispatch-1",
        "role": "developer",
        "purpose": "work",
        "transition": {"next_action": "developer"},
        "snapshot_commit": commits["old2"],
        "write_paths": ["new.py"],
        "definition_of_done": ["Ship the parser"],
        "verification_commands": ["make test"],
        **fields,
    }


@pytest.mark.parametrize(
    ("batch_fields", "expected"),
    [
        ({}, "old2"),
        ({"branch_start_commit": "base"}, "base"),
        # An integration base that moved past the branch start is not an ancestor of it.
        ({"integration_base_commit": "new"}, "new"),
        # A resolver batch starts on a candidate that is not a descendant of the new target.
        ({"integration_base_commit": "new", "branch_start_commit": "side"}, "new"),
        # The progress a superseding batch continues on its branch is its own.
        ({"supersedes": {"batch_id": "batch-0"}}, "base"),
        ({"branch_start_commit": None}, "base"),
        ({"branch_start_commit": ""}, "base"),
        ({"branch_start_commit": "unrelated"}, "base"),
        ({"branch_start_commit": None, "integration_base_commit": None}, "base"),
    ],
)
def test_batch_base_is_the_branch_start_only_when_it_follows_the_integration_base(
    published_branch: tuple[Path, dict[str, str]],
    batch_fields: dict[str, object],
    expected: str,
) -> None:
    repo, commits = published_branch
    resolved = {
        key: commits[value] if isinstance(value, str) and value in commits else value
        for key, value in batch_fields.items()
    }

    assert rebase.batch_base(repo, _batch(commits, **resolved)) == commits[expected]


@pytest.mark.parametrize("role", ["developer", "publish"])
def test_report_base_of_a_rerun_batch_is_its_branch_start(
    published_branch: tuple[Path, dict[str, str]], role: str
) -> None:
    repo, commits = published_branch

    base = rebase.report_base(
        repo, repo, _batch(commits), _developer(commits, role=role)
    )

    assert base == commits["old2"]


def test_a_checkpoint_of_a_rerun_batch_is_measured_from_its_branch_start(
    published_branch: tuple[Path, dict[str, str]],
) -> None:
    repo, commits = published_branch

    base = reports._checkpoint_base(
        repo, repo, _batch(commits), _developer(commits), commits["new"]
    )

    assert base == commits["old2"]


def test_a_rebase_target_takes_priority_over_the_branch_start(
    published_branch: tuple[Path, dict[str, str]],
) -> None:
    repo, commits = published_branch
    batch = _batch(
        commits, base_rebase_required=True, rebase_target_commit=commits["old1"]
    )

    base = reports._checkpoint_base(
        repo, repo, batch, _developer(commits), commits["new"]
    )

    assert base == commits["old1"]


def test_a_developer_report_lists_only_the_files_of_its_own_batch(
    published_branch: tuple[Path, dict[str, str]],
) -> None:
    repo, commits = published_branch
    batch, dispatch = _batch(commits), _developer(commits)
    report: JsonObject = {
        "commit_sha": commits["new"],
        "changed_files": ["new.py"],
    }

    FixForwardHistory(repo, repo, batch).validate_report(report, dispatch, DEVELOPER)

    stale = {**report, "changed_files": ["new.py", "old1.py", "old2.py"]}
    with pytest.raises(CoordinatorError, match="changed_files must exactly match"):
        FixForwardHistory(repo, repo, batch).validate_report(stale, dispatch, DEVELOPER)


def test_a_commit_map_covers_only_the_commits_of_its_own_batch(
    published_branch: tuple[Path, dict[str, str]],
) -> None:
    repo, commits = published_branch
    batch = _batch(commits)
    dispatch = _developer(commits, commit_plan=[{"id": "step-1"}])
    report: JsonObject = {
        "commit_sha": commits["new"],
        "changed_files": ["new.py"],
        "commit_map": [{"commit_sha": commits["new"], "plan_entry_id": "step-1"}],
    }

    FixForwardHistory(repo, repo, batch).validate_report(report, dispatch, DEVELOPER)


def test_a_rerun_developer_report_has_no_scope_warnings(
    published_branch: tuple[Path, dict[str, str]],
) -> None:
    repo, commits = published_branch
    batch, dispatch = _batch(commits), _developer(commits)
    base = rebase.report_base(repo, repo, batch, dispatch)
    assert base is not None
    report: JsonObject = {
        "role": "developer",
        "changed_files": _changed_files_between(repo, base, commits["new"]),
    }

    assert reports.report_scope_warnings(report, dispatch) == []
