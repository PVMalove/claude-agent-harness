"""Hardening tests for checkpoints and continuation authorization in ``reports``."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from harness.orchestration.core.constants import CHECKPOINT_NO_CONTEXT_PACKAGE
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.workflow import reports


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
def moved_base(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A repository whose integration base moved past the batch base before the ticket's work.

    ``base`` is the batch base, ``upstream`` the moved integration base, ``ticket`` the work on
    top of it, ``target`` a newer integration tip and ``rebased`` the work rebased onto it.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    commits = {"base": _commit(repo, "base.txt")}
    commits["upstream"] = _commit(repo, "upstream.txt")
    commits["ticket"] = _commit(repo, "ticket.py")
    _git(repo, "checkout", "-q", "-b", "target", commits["upstream"])
    commits["target"] = _commit(repo, "newer.txt")
    commits["rebased"] = _commit(repo, "ticket.py")
    return repo, commits


def _batch(commits: dict[str, str]) -> JsonObject:
    return {
        "base_commit": commits["base"],
        "integration_base_commit": commits["upstream"],
        "dispatches": [],
    }


def _developer(commits: dict[str, str], **fields: object) -> JsonObject:
    return {
        "dispatch_id": "dispatch-1",
        "role": "developer",
        "snapshot_commit": commits["ticket"],
        "write_paths": ["ticket.py"],
        "definition_of_done": ["Ship the parser"],
        "verification_commands": ["make test"],
        **fields,
    }


@pytest.mark.parametrize(
    ("target", "checkpointed", "expected"),
    [
        (None, "ticket", "upstream"),
        ("target", "ticket", "upstream"),
        ("target", "rebased", "target"),
    ],
)
def test_checkpoint_is_measured_from_the_base_of_its_report(
    moved_base: tuple[Path, dict[str, str]],
    target: str | None,
    checkpointed: str,
    expected: str,
) -> None:
    repo, commits = moved_base
    fields = {} if target is None else {"rebase_target_commit": commits[target]}

    base = reports._checkpoint_base(
        repo,
        repo,
        _batch(commits),
        _developer(commits, **fields),
        commits[checkpointed],
    )

    assert base == commits[expected]


def test_checkpoint_after_the_integration_base_moved_lists_only_the_ticket_files(
    moved_base: tuple[Path, dict[str, str]],
) -> None:
    repo, commits = moved_base
    batch, dispatch = _batch(commits), _developer(commits)
    checkpoint: JsonObject = {
        "dispatch_id": "dispatch-1",
        "commit_sha": commits["ticket"],
        "changed_files": ["ticket.py"],
        "remaining_definition_of_done": ["Ship the parser"],
        "passing_checks": [],
        "risks": "none",
        "blockers": "none",
        "context_package_id": CHECKPOINT_NO_CONTEXT_PACKAGE,
    }

    reports._validate_checkpoint(
        checkpoint,
        dispatch,
        {"mode": "write", "name": "developer"},
        repo,
        reports._checkpoint_base(repo, repo, batch, dispatch, commits["ticket"]),
        repo,
        batch,
    )


DISPATCH = {"dispatch_id": "dispatch-1", "dependencies": ["none"]}
CHECKPOINT = {
    "remaining_definition_of_done": ["Ship the parser"],
    "risks": "none",
    "blockers": "waiting for review",
}


def _facts(tmp_path: Path, **changes: object) -> argparse.Namespace:
    facts = {
        "dispatch_id": "dispatch-1",
        "remaining_definition_of_done": ["Ship the parser"],
        "risks": "none",
        "dependencies": ["none"],
        **changes,
    }
    path = tmp_path / "facts.json"
    path.write_text(json.dumps(facts), encoding="utf-8")
    return argparse.Namespace(file=str(path))


def test_unchanged_facts_continue_whatever_the_checkpoint_blockers_say(
    tmp_path: Path,
) -> None:
    reports._check_continuation_facts_unchanged(DISPATCH, CHECKPOINT, _facts(tmp_path))


@pytest.mark.parametrize(
    "changes",
    [
        {"remaining_definition_of_done": ["Ship the parser", "Ship the docs"]},
        {"risks": "the parser may drop input"},
        {"dependencies": ["issue-1"]},
    ],
)
def test_drifted_facts_name_only_the_compared_facts(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    with pytest.raises(CoordinatorError) as caught:
        reports._check_continuation_facts_unchanged(
            DISPATCH, CHECKPOINT, _facts(tmp_path, **changes)
        )

    assert "blockers" not in caught.value.message
    assert "ordinary approval" in caught.value.remedy
