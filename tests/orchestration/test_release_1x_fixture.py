"""The fixture of the last 1.x release: its CLI runs from the Git history, and the state its own
code writes holds a batch in every ``DispatchState`` that ``harness-runtime migrate`` admits."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from .release_1x import RELEASE_1X_COMMIT, STATE_REL, build_1x_state, cli_1x

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the release fixture drives the POSIX CLI of 1.x"
)

ADMITTED = {
    "proposed",
    "approved",
    "blocked",
    "failed",
    "reported",
    "checkpointed",
    "rate_limited",
    "abandoned",
    "cancelled",
}


def test_the_release_is_pinned_by_its_commit() -> None:
    assert len(RELEASE_1X_COMMIT) == 40
    assert all(digit in "0123456789abcdef" for digit in RELEASE_1X_COMMIT)


def test_the_cli_of_the_release_runs_from_its_own_tree(release_1x: Path, tmp_path: Path) -> None:
    result = cli_1x(release_1x, tmp_path, "--help")

    assert result.returncode == 0, result.stderr
    assert "ledger" in result.stdout


def test_the_state_the_release_writes_passes_its_own_ledger_validate(
    release_1x: Path, tmp_path: Path
) -> None:
    repo, ids = build_1x_state(release_1x, tmp_path)

    result = cli_1x(release_1x, repo, "ledger", "validate")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["valid"] is True
    pointer = json.loads((repo / STATE_REL / "ledger.json").read_text(encoding="utf-8"))
    generation = repo / STATE_REL / "generations" / pointer["generation"]
    states = {
        json.loads(path.read_text(encoding="utf-8"))["state"]
        for path in (generation / "dispatch-status").glob("*.json")
    }
    assert states == ADMITTED
    batch = json.loads(
        (generation / "batches" / f"{ids['auto']}.json").read_text(encoding="utf-8")
    )
    assert batch["approval_policy"] == "auto"
    repaired = json.loads(
        (generation / "batches" / f"{ids['repair_781']}.json").read_text(encoding="utf-8")
    )
    assert repaired["risk_reassessment_required"] is True
    assert "risk_reassessment_candidate" not in repaired
