"""Behavior tests for the fail-closed test-author preflight."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import cast

import pytest

ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT = ROOT / "harness" / "project" / "hooks" / "test-author-preflight.sh"
GUARD = ROOT / "harness" / "project" / "hooks" / "test-author-guard.py"


def _project(tmp_path: Path, *, test_paths: object = ["tests/**"]) -> Path:
    """Create a trusted project with a controllable isolation probe."""
    project = tmp_path / "project"
    (project / ".harness").mkdir(parents=True)
    hooks = project / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "src").mkdir()
    config = {
        "language": "en",
        "base_branch": "main",
        "branch_pattern": "^feature/.+",
        "qa_gate_commands": [],
        "test_paths": test_paths,
        "test_runner": {"command": ["python3", "-m", "pytest", "{test_id}"]},
    }
    (project / ".harness" / "project.json").write_text(
        json.dumps(config), encoding="utf-8"
    )
    for source, target in (
        (GUARD, hooks / "test-author-guard.py"),
        (PREFLIGHT, hooks / "test-author-preflight.sh"),
    ):
        target.write_bytes(source.read_bytes())
        target.chmod(target.stat().st_mode | stat.S_IXUSR)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    return project


def _runner(project: Path, answer: str) -> None:
    """Install a runner stub with the probe answer used by the preflight seam."""
    path = project / ".claude" / "hooks" / "run-test.sh"
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{answer}'\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run(project: Path) -> subprocess.CompletedProcess[str]:
    """Run preflight and keep its machine-readable output bounded."""
    return subprocess.run(
        [str(project / ".claude" / "hooks" / "test-author-preflight.sh")],
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=dict(os.environ, CLAUDE_PROJECT_DIR=str(project)),
        check=False,
    )


def _answer(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    """Assert the preflight process contract and parse its only output line."""
    assert result.returncode == 0
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    return cast(dict[str, object], json.loads(result.stdout))


def test_preflight_activates_only_after_probe_and_canary_pass(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _runner(project, '{"status":"ok"}')

    answer = _answer(_run(project))

    assert answer == {"status": "ok"}


def test_missing_test_paths_disables_test_author(tmp_path: Path) -> None:
    project = _project(tmp_path, test_paths=None)
    data = json.loads((project / ".harness" / "project.json").read_text())
    data.pop("test_paths")
    (project / ".harness" / "project.json").write_text(json.dumps(data))
    _runner(project, '{"status":"ok"}')

    assert _answer(_run(project)) == {
        "status": "disabled",
        "reason": "test_paths_missing",
    }


def test_unavailable_isolation_disables_test_author_without_detail_leak(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    _runner(
        project,
        '{"status":"unavailable","reason":"isolation_unavailable","detail":"bwrap_missing"}',
    )

    answer = _answer(_run(project))

    assert answer == {"status": "disabled", "reason": "isolation_unavailable"}


def test_runner_configuration_is_required_for_activation(tmp_path: Path) -> None:
    project = _project(tmp_path)
    data = json.loads((project / ".harness" / "project.json").read_text())
    data.pop("test_runner")
    (project / ".harness" / "project.json").write_text(json.dumps(data))
    _runner(project, '{"status":"ok"}')

    assert _answer(_run(project)) == {
        "status": "disabled",
        "reason": "test_runner_missing",
    }


@pytest.mark.parametrize("bad_answer", ["not-json", '{"status":"disabled"}'])
def test_unverifiable_guard_or_probe_fails_closed(
    tmp_path: Path, bad_answer: str
) -> None:
    project = _project(tmp_path)
    _runner(project, '{"status":"ok"}')
    if bad_answer == "not-json":
        (project / ".claude" / "hooks" / "test-author-guard.py").write_text(
            "print('not-json')\n", encoding="utf-8"
        )
    else:
        (project / ".claude" / "hooks" / "run-test.sh").write_text(
            "#!/bin/sh\nprintf '%s\\n' '{\"status\":\"disabled\"}'\n",
            encoding="utf-8",
        )
        (project / ".claude" / "hooks" / "run-test.sh").chmod(
            (project / ".claude" / "hooks" / "run-test.sh").stat().st_mode
            | stat.S_IXUSR
        )

    answer = _answer(_run(project))

    assert answer == {"status": "disabled", "reason": "guard_hook_not_firing" if bad_answer == "not-json" else "isolation_unavailable"}
