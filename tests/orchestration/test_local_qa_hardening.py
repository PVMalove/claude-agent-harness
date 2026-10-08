"""Local QA hardening: a candidate that does not contain its target is reported as such."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.workflow import integration, local_qa, pr_refresh


def _git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(f"{name}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def history(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """``main`` moved past the commit ``feature`` started from."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    base = _commit(repo, "base.txt")
    _git(repo, "checkout", "-q", "-b", "feature")
    feature = _commit(repo, "feature.txt")
    _git(repo, "checkout", "-q", "main")
    moved = _commit(repo, "moved.txt")
    return repo, {"base": base, "feature": feature, "moved": moved}


def _observe(
    monkeypatch: pytest.MonkeyPatch, repo: Path, pair: JsonObject
) -> JsonObject:
    record: JsonObject = {
        "identity": {"remote": "origin", "branch": "feature", "integration_ref": "main"}
    }
    monkeypatch.setattr(integration, "_check_history_unchanged", lambda *_: None)
    monkeypatch.setattr(pr_refresh, "current_pair", lambda *_: pair)
    tips = {"feature": pair["candidate_sha"], "main": pair["target_sha"]}
    monkeypatch.setattr(
        local_qa, "_remote_branch_tip", lambda _repo, _remote, ref: tips[ref]
    )
    return local_qa._observe_pair(repo, repo, record, pair)


def test_a_candidate_without_its_target_is_not_based_on_it(
    monkeypatch: pytest.MonkeyPatch, history: tuple[Path, dict[str, str]]
) -> None:
    repo, commits = history
    pair: JsonObject = {
        "candidate_sha": commits["feature"],
        "target_sha": commits["moved"],
    }

    with pytest.raises(CoordinatorError) as refused:
        _observe(monkeypatch, repo, pair)

    assert refused.value.message == "the candidate is not based on its target"
    assert refused.value.remedy == "refresh the issue branch before local QA"


def test_a_candidate_on_its_target_is_observed(
    monkeypatch: pytest.MonkeyPatch, history: tuple[Path, dict[str, str]]
) -> None:
    repo, commits = history
    pair: JsonObject = {
        "candidate_sha": commits["feature"],
        "target_sha": commits["base"],
    }

    assert _observe(monkeypatch, repo, pair) == {
        "remote_branch_sha": commits["feature"],
        "integration_tip": commits["base"],
    }
