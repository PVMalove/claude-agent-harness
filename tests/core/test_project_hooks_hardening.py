"""Hardening of the standalone project hooks: Python 3.9 import safety, fail-closed git calls in
direct_commits.py, pushes that may update every branch, and bounded git in qa-gate-state.py."""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

HOOKS = Path(__file__).resolve().parents[2] / "harness" / "project" / "hooks"
ISSUE_BRANCH = "feature/issue-1-demo"


def _load(filename: str, name: str) -> ModuleType:
    """Import a hook the way the installed copy imports itself (siblings on sys.path)."""
    sys.path.insert(0, str(HOOKS))
    bytecode = sys.dont_write_bytecode
    try:
        spec = importlib.util.spec_from_file_location(name, HOOKS / filename)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        # dataclasses resolve postponed annotations through sys.modules, as for `__main__`.
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = bytecode
        sys.path.remove(str(HOOKS))


@pytest.fixture(scope="module")
def direct_commits() -> ModuleType:
    return _load("direct_commits.py", "direct_commits_hardening")


@pytest.fixture(scope="module")
def qa_gate_state() -> ModuleType:
    return _load("qa-gate-state.py", "qa_gate_state_hardening")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )


def _repo(tmp_path: Path, branch: str) -> Path:
    """A repository with one commit on `branch`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "checkout", "-q", "-b", branch)
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
    )
    return repo


@pytest.fixture
def no_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PATH without any git executable."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


@pytest.fixture
def slow_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """PATH whose git never answers within the test's timeout."""
    if sys.platform == "win32":
        pytest.skip("a POSIX shell script stands in for git")
    bin_dir = tmp_path / "slow-bin"
    bin_dir.mkdir()
    script = bin_dir / "git"
    script.write_text("#!/bin/sh\nexec sleep 5\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    yield


@pytest.mark.parametrize(
    "hook", sorted(path.name for path in HOOKS.glob("*.py")), ids=str
)
def test_every_python_hook_imports_on_python_3_9(hook: str) -> None:
    """The wrappers accept any Python 3.9+: `X | None` in a signature must stay unevaluated."""
    source = (HOOKS / hook).read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 9))
    imports = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "__future__"
    ]
    assert any(alias.name == "annotations" for node in imports for alias in node.names)


def test_call_is_immutable(direct_commits: ModuleType, tmp_path: Path) -> None:
    found = direct_commits.calls("git -C sub commit -m x", tmp_path)
    assert found is not None and len(found) == 1
    call = found[0]
    assert call.location == (("-C", "sub"),)
    assert call.args == ("-m", "x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        call.cwd = None


def test_a_wrapper_that_changes_directory_leaves_the_call_unresolved(
    direct_commits: ModuleType, tmp_path: Path
) -> None:
    found = direct_commits.calls("env -C /elsewhere git commit -m x", tmp_path)
    assert found is not None and [call.cwd for call in found] == [None]


@pytest.mark.usefixtures("no_git")
def test_a_commit_is_blocked_when_git_cannot_start(
    direct_commits: ModuleType, tmp_path: Path
) -> None:
    reason = direct_commits.block_reason("git commit -m x", tmp_path, tmp_path)
    assert reason == direct_commits.GIT_NO_ANSWER


@pytest.mark.usefixtures("slow_git")
def test_a_commit_is_blocked_when_git_does_not_answer(
    direct_commits: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(direct_commits, "GIT_TIMEOUT_SECONDS", 0.5)
    started = time.monotonic()
    reason = direct_commits.block_reason("git commit -m x", tmp_path, tmp_path)
    assert reason == direct_commits.GIT_NO_ANSWER
    assert time.monotonic() - started < 4


@pytest.mark.usefixtures("no_git")
def test_the_grill_docs_exception_needs_an_answer_from_git(
    direct_commits: ModuleType, tmp_path: Path
) -> None:
    command = "git commit -m docs"
    found = direct_commits.calls(command, tmp_path)
    assert found is not None
    assert (
        direct_commits._grill_docs_commit(command, found[0], "integration/new") is False
    )


@pytest.mark.parametrize(
    "command",
    [
        "git push --all origin",
        "git push origin --branches",
        "git push --mirror origin",
        "git push origin 'refs/heads/*:refs/heads/*'",
        "git push origin '+refs/heads/*'",
    ],
)
def test_a_push_that_may_update_every_branch_is_blocked(
    direct_commits: ModuleType, tmp_path: Path, command: str
) -> None:
    repo = _repo(tmp_path, ISSUE_BRANCH)
    reason = direct_commits.block_reason(command, repo, repo)
    assert reason.startswith("Zero Direct Commits: ")


@pytest.mark.parametrize(
    "command",
    [
        f"git push origin {ISSUE_BRANCH}",
        "git push -u origin HEAD",
        f"git push origin HEAD:{ISSUE_BRANCH}",
        "git commit -m change",
    ],
)
def test_an_issue_branch_still_commits_and_pushes(
    direct_commits: ModuleType, tmp_path: Path, command: str
) -> None:
    repo = _repo(tmp_path, ISSUE_BRANCH)
    assert direct_commits.block_reason(command, repo, repo) == ""


@pytest.mark.parametrize(
    "command", ["git commit -m change", "git push origin HEAD:main"]
)
def test_a_protected_target_is_still_blocked(
    direct_commits: ModuleType, tmp_path: Path, command: str
) -> None:
    repo = _repo(tmp_path, "main")
    assert "запрещён" in direct_commits.block_reason(command, repo, repo)


def test_parsed_is_immutable() -> None:
    pr_commands = _load("pr_commands.py", "pr_commands_hardening")
    parsed = pr_commands.parse("git status; echo $(git log)")
    assert ["git", "status"] in parsed.commands
    with pytest.raises(dataclasses.FrozenInstanceError):
        parsed.opaque = []


@pytest.mark.usefixtures("slow_git")
def test_qa_gate_git_is_bounded(
    qa_gate_state: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A git that does not answer ends the hook with an error (exit 2), never a hang."""
    monkeypatch.setattr(qa_gate_state, "GIT_TIMEOUT_SECONDS", 0.5)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        qa_gate_state.git(tmp_path, "rev-parse", "HEAD")
    assert time.monotonic() - started < 4
