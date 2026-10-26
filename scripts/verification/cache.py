"""Worktree cache and stage evidence directory of a verification run (ADR 0023, sections 4 and 5).

A developer or review run keeps its mypy and pytest caches and its previous JUnit report in
``.harness/.sandboxes/cache/<worktree-id>/<environment_fingerprint>/``. A cache of another
worktree or of another fingerprint lies in another directory and is never read. The clean-room QA
run is cold: the gate marks its environment with ``HARNESS_VERIFY_COLD`` and a run with a worktree
cache refuses to start there. No cache is a proof of a pass.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from harness.gate_runner.gate_runner import (
    STAGE_ENV_COLD,
    STAGE_ENV_JUNIT,
    EvidenceBundle,
)
from harness.orchestration.verification_plan import fingerprint_of
from harness.storage import storage_path

COLD_ENV = STAGE_ENV_COLD
JUNIT_ENV = STAGE_ENV_JUNIT
EVIDENCE_FILE = "stage-evidence.json"
# Bound of the Git probe for the checked-out commit.
_GIT_TIMEOUT_SECONDS = 30


class VerificationCacheError(Exception):
    """A cache or an evidence directory cannot be used; the message says why."""


def worktree_id(root: Path) -> str:
    """A stable id of this checkout: its directory name and a digest of its resolved path."""
    resolved = root.resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    return f"{resolved.name}-{digest}"


def checkout_fingerprint(root: Path) -> str:
    """The environment fingerprint of the checkout's own manifests (ADR 0023, section 2).

    It is the digest the VerificationPlan computes from a commit, applied to the files on disk.
    """

    def read(name: str) -> bytes | None:
        path = root / name
        return path.read_bytes() if path.is_file() else None

    return fingerprint_of(read("uv.lock"), read("pyproject.toml"))


def head_commit(root: Path) -> str:
    """The commit the run verifies: the checkout's ``HEAD``."""
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VerificationCacheError(f"cannot read HEAD of {root}: {exc}") from exc
    commit = done.stdout.strip()
    if done.returncode != 0 or not commit:
        raise VerificationCacheError(
            f"cannot read HEAD of {root}: {done.stderr.strip() or 'no commit'}"
        )
    return commit


@dataclass(frozen=True)
class WorktreeCache:
    """The cache directory of one worktree in one environment."""

    directory: Path

    @property
    def pytest_dir(self) -> Path:
        """Where pytest keeps its own cache."""
        return self.directory / "pytest"

    @property
    def mypy_dir(self) -> Path:
        """Where mypy keeps its incremental cache."""
        return self.directory / "mypy"

    @property
    def junit_path(self) -> Path:
        """The JUnit report of the previous pytest run."""
        return self.directory / "junit.xml"


def open_cache(root: Path, env: Mapping[str, str]) -> WorktreeCache:
    """The worktree cache of ``root``, created on demand; refused in a cold environment."""
    if env.get(COLD_ENV):
        raise VerificationCacheError(
            f"{COLD_ENV} is set: a clean-room QA run is cold and must not use a worktree cache"
        )
    directory = storage_path(
        root, "cache", worktree_id(root), checkout_fingerprint(root)
    )
    directory.mkdir(parents=True, exist_ok=True)
    return WorktreeCache(directory)


def write_evidence(directory: Path, bundle: EvidenceBundle) -> Path:
    """Write the evidence document and its streams into ``directory``; return the document path."""
    directory.mkdir(parents=True, exist_ok=True)
    for relative, text in bundle.files.items():
        (directory / relative).write_text(text, encoding="utf-8", newline="\n")
    document = directory / EVIDENCE_FILE
    document.write_text(bundle.document.dumps(), encoding="utf-8", newline="\n")
    return document
