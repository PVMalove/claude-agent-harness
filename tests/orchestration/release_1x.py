"""The CLI of the last 1.x release, taken from the Git history of this repository.

ADR 0020, section 5 needs two proofs that a state of installation 1.x survives the cutover and the
way back: the CLI of the last 1.x release and the CLI of this tree give the same answers about one
state, and the last 1.x release accepts the generation that ``rollback --to-legacy`` writes. Both
run the real code of that release. ``git archive`` extracts its ``harness/`` package into a
temporary directory; nothing is installed, and no network is used. The commit is pinned, not the
tag: a tag can move, and CI fetches the whole history (``fetch-depth: 0``) but no tags.

The CLI runs as ``python -I <tree>/harness/orchestration/coordinator.py``: isolated mode keeps this
tree off ``sys.path``, and the coordinator puts its own tree there.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
#: The commit of tag ``v1.5.1``, the last 1.x release; an ancestor of every 2.0.0 commit.
RELEASE_1X_COMMIT = "3e4dee3352bd687bb1a1c6b8967c3fa4bcbba215"
STATE_REL = Path(".harness/orchestration/state")
#: The driver that writes a 1.x state with the code of the release (see ``legacy_1x_state``).
DRIVER = Path(__file__).with_name("legacy_1x_state.py")
#: The tickets of the batches the driver creates through the coordinator of the release.
TICKETS = {"auto": "#901", "send": "#902", "work": "#903", "reported": "#904"}


def _unavailable(message: str) -> None:
    """A missing release fails CI (which builds the runtime and sets the variable) and skips a
    local run on a shallow clone."""
    if os.environ.get("HARNESS_REQUIRE_RUNTIME") == "1":
        pytest.fail(message)
    pytest.skip(message)


def extract_release(target: Path) -> Path:
    """Extract ``harness/`` of the release into ``target`` and return ``target``."""
    found = subprocess.run(
        ["git", "-C", str(ROOT), "cat-file", "-e", f"{RELEASE_1X_COMMIT}^{{commit}}"],
        capture_output=True,
        check=False,
    )
    if found.returncode != 0:
        _unavailable(
            f"the commit {RELEASE_1X_COMMIT} of the last 1.x release is not in this clone; "
            "fetch the whole history (fetch-depth: 0)"
        )
    archive = subprocess.run(
        ["git", "-C", str(ROOT), "archive", "--format=tar", RELEASE_1X_COMMIT, "harness"],
        capture_output=True,
        check=True,
    )
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(target, filter="data")
    return target


@pytest.fixture(scope="session")
def release_1x(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """The source tree of the last 1.x release; its CLI is ``<tree>/harness/orchestration``."""
    yield extract_release(tmp_path_factory.mktemp("release-1x"))


def _isolated_environ() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}


def cli_1x(tree: Path, repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """``coordinator.py --repo <repo> <args>`` of the release."""
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(tree / "harness" / "orchestration" / "coordinator.py"),
            "--repo",
            str(repo),
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_isolated_environ(),
    )


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repo, capture_output=True, text=True, check=True)


def repository_1x(tree: Path, tmp: Path) -> Path:
    """A git repository with an origin, the role manifests of the release and one worktree per
    batch the driver creates through the coordinator."""
    origin = tmp / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    repo = tmp / "work"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    (repo / ".harness").mkdir()
    (repo / ".harness" / "project.json").write_text(
        json.dumps({"qa_gate_commands": ["true"]}), encoding="utf-8"
    )
    roles = repo / ".harness" / "orchestration" / "roles"
    roles.mkdir(parents=True)
    for manifest in (tree / "harness" / "orchestration" / "roles").glob("*.md"):
        (roles / manifest.name).write_bytes(manifest.read_bytes())
    (repo / ".gitignore").write_text(
        ".harness/orchestration/state/\n.harness/orchestration/state-snapshots/\n"
        ".harness/orchestration.json\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    _git(repo, "branch", "-M", "master")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "origin", "master")
    for ticket in TICKETS.values():
        number = ticket.removeprefix("#")
        _git(
            repo,
            "worktree",
            "add",
            "-b",
            f"feature/issue-{number}-golden",
            str(tmp / f"worktree-{number}"),
            "master",
        )
    return repo


def build_1x_state(tree: Path, tmp: Path) -> tuple[Path, dict[str, str]]:
    """A repository whose state the release wrote (see ``legacy_1x_state``), and the ids of its
    batches and dispatches by name."""
    repo = repository_1x(tree, tmp)
    result = subprocess.run(
        [sys.executable, "-I", str(DRIVER), str(tree), str(repo), str(tmp)],
        capture_output=True,
        text=True,
        check=False,
        env=_isolated_environ(),
    )
    assert result.returncode == 0, result.stderr[-4000:]
    ids: dict[str, str] = json.loads(result.stdout)
    return repo, ids
