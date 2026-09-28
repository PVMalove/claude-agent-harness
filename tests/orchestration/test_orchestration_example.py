"""`.harness/orchestration.example.json` is a managed backend-orchestration resource: init, adopt
and update install and refresh it, while `.harness/orchestration.json` is seeded from it once and
then belongs to the project."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HARNESS = [sys.executable, str(ROOT / "harness" / "bin" / "harness.py")]
EXAMPLE = ROOT / "harness" / "orchestration.example.json"


def _harness(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*HARNESS, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        check=False,
    )


def _init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    result = _harness(
        "init",
        str(repo),
        "--capability",
        "backend-orchestration",
        "--project-type",
        "software",
        "--stack",
        "python",
        "--base-branch",
        "main",
        "--language",
        "ru",
        "--qa-gate-command",
        "true",
    )
    assert result.returncode == 0, result.stderr


def test_init_installs_the_example_and_seeds_the_project_config_from_it(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "project"
    _init(repo)

    example = repo / ".harness" / "orchestration.example.json"
    config = repo / ".harness" / "orchestration.json"
    assert example.read_bytes() == EXAMPLE.read_bytes()
    assert config.read_bytes() == EXAMPLE.read_bytes()
    lock = json.loads((repo / ".harness" / "harness.lock").read_text(encoding="utf-8"))
    assert ".harness/orchestration.example.json" in lock["files"]
    assert ".harness/orchestration.json" not in lock["files"]


def test_update_refreshes_the_example_but_keeps_the_project_config(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "project"
    _init(repo)
    example = repo / ".harness" / "orchestration.example.json"
    config = repo / ".harness" / "orchestration.json"
    customized = json.loads(config.read_text(encoding="utf-8"))
    customized["concurrency_budget"] = 3
    config.write_text(json.dumps(customized, indent=2) + "\n", encoding="utf-8")
    example.unlink()

    result = _harness("update", str(repo))

    assert result.returncode == 0, result.stderr
    assert example.read_bytes() == EXAMPLE.read_bytes()
    assert json.loads(config.read_text(encoding="utf-8"))["concurrency_budget"] == 3


def test_the_example_is_valid_and_ships_without_repository_specific_commands() -> None:
    from harness.orchestration import contract

    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert (
        contract.health_problems(EXAMPLE, ROOT / "harness" / "orchestration" / "roles")
        == []
    )
    for key in (
        "developer_verification_commands",
        "review_verification_commands",
        "verification_commands",
    ):
        assert data[key] == []
