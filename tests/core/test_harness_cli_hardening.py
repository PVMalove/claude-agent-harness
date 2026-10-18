"""The packager CLI fails with a message instead of a traceback or silent data loss when git does
not answer or a JSON file it reads is malformed."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

from harness.bin import harness as harness_cli
from harness.health.project_files import INTEGRATIONS_REL


def _record(repo: Path, identifier: str = "hooks") -> None:
    (repo / "config.json").write_text("{}\n", encoding="utf-8")
    harness_cli.record_integration(
        repo,
        identifier=identifier,
        kind="hook",
        runtimes=["claude"],
        config=Path("config.json"),
        secret_refs=[],
        verify="open a session",
    )


@pytest.mark.parametrize(
    "content",
    ["{not json", "[]", '{"integrations": {"id": "x"}}'],
    ids=["invalid-json", "not-an-object", "integrations-not-a-list"],
)
def test_an_unreadable_integrations_inventory_is_never_rewritten(
    tmp_path: Path, content: str, capsys: pytest.CaptureFixture[str]
) -> None:
    inventory = tmp_path / INTEGRATIONS_REL
    inventory.parent.mkdir(parents=True)
    inventory.write_text(content, encoding="utf-8")
    with pytest.raises(SystemExit):
        _record(tmp_path)
    assert inventory.read_text(encoding="utf-8") == content
    assert "fix or remove it" in capsys.readouterr().err


def test_recording_an_integration_keeps_the_project_entries(tmp_path: Path) -> None:
    inventory = tmp_path / INTEGRATIONS_REL
    inventory.parent.mkdir(parents=True)
    own = {"id": "own-mcp", "kind": "mcp"}
    stale = {"id": "hooks", "kind": "hook"}
    inventory.write_text(
        json.dumps({"schema": 1, "integrations": [own, "note", stale]}),
        encoding="utf-8",
    )
    _record(tmp_path)
    entries = json.loads(inventory.read_text(encoding="utf-8"))["integrations"]
    assert entries[:2] == [own, "note"]
    assert [entry["id"] for entry in entries[2:]] == ["hooks"]
    assert entries[2]["verify"] == "open a session"


def test_lock_project_skills_rejects_an_overlay_lock_that_is_not_an_object(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / "harness.lock").write_text('{"files": {}}', "utf-8")
    overlays = tmp_path / ".harness" / "overlays"
    overlays.mkdir()
    (overlays / "vendor.lock").write_text("[]", encoding="utf-8")
    with pytest.raises(SystemExit):
        harness_cli.cmd_lock_project_skills(argparse.Namespace(repo=str(tmp_path)))
    assert "expected an object with a skills list" in capsys.readouterr().err


def test_a_missing_git_fails_with_a_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def missing(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(SystemExit):
        harness_cli.ensure_git_repo(tmp_path)
    assert "cannot run git" in capsys.readouterr().err
    assert harness_cli.source_revision() == "uncommitted"


def test_git_probes_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts: list[object] = []

    def hang(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        timeouts.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(subprocess, "run", hang)
    assert harness_cli.source_revision() == "uncommitted"
    assert timeouts == [harness_cli.GIT_TIMEOUT_SECONDS]


def test_foundation_only_projects_get_no_capability_extras(tmp_path: Path) -> None:
    args = argparse.Namespace()
    assert (
        harness_cli.seed_capability_extras(tmp_path, args, ["project-foundation"]) == []
    )
    assert not any(tmp_path.iterdir())


def test_preparation_command_validation() -> None:
    assert harness_cli._preparation_command("pytest -v") == ["pytest", "-v"]
    with pytest.raises(
        argparse.ArgumentTypeError, match="must be non-empty argv without {test_id}"
    ):
        harness_cli._preparation_command("pytest '{unclosed")
    with pytest.raises(
        argparse.ArgumentTypeError, match="must be non-empty argv without {test_id}"
    ):
        harness_cli._preparation_command("")
    with pytest.raises(
        argparse.ArgumentTypeError, match="must be non-empty argv without {test_id}"
    ):
        harness_cli._preparation_command("pytest {test_id}")
    with pytest.raises(
        argparse.ArgumentTypeError, match="must be non-empty argv without {test_id}"
    ):
        harness_cli._preparation_command("pytest \x00")


def test_seed_test_fields_build_and_database() -> None:
    args = argparse.Namespace(
        test_runner_command=["pytest", "{test_id}"],
        test_build_command=["make", "build"],
        test_artifact_path=["build/**"],
        test_database_engine="postgresql",
        test_database_test_id=["tests/db/**"],
        test_database_command=["postgres"],
        test_database_ready_command=["pg_isready"],
    )
    raw = harness_cli._seed_test_fields(args)
    fields = json.loads("{" + raw.rstrip(",") + "}")
    assert fields["test_runner"]["build"]["command"] == ["make", "build"]
    assert fields["test_runner"]["build"]["artifact_paths"] == ["build/**"]
    assert fields["test_runner"]["database"]["engine"] == "postgresql"
    assert fields["test_runner"]["database"]["test_ids"] == ["tests/db/**"]
    assert fields["test_runner"]["database"]["command"] == ["postgres"]
    assert fields["test_runner"]["database"]["ready_command"] == ["pg_isready"]

    # Without build and database
    plain_args = argparse.Namespace(
        test_runner_command=["pytest", "{test_id}"],
        test_build_command=None,
        test_database_engine=None,
    )
    plain_raw = harness_cli._seed_test_fields(plain_args)
    assert "build" not in plain_raw
    assert "database" not in plain_raw


def test_validate_test_options() -> None:
    parser = harness_cli.parser()
    valid_args = argparse.Namespace(
        test_runner_command=["pytest", "{test_id}"],
        test_build_command=["make", "build"],
        test_artifact_path=["build/**"],
        test_database_engine="postgresql",
        test_database_test_id=["tests/db/**"],
        test_database_command=["postgres"],
        test_database_ready_command=["pg_isready"],
    )
    harness_cli._validate_test_options(valid_args, parser)

    # Missing test_runner_command with build command
    no_runner = argparse.Namespace(
        test_runner_command=None,
        test_build_command=["make"],
        test_artifact_path=["build/**"],
        test_database_engine=None,
        test_database_test_id=None,
        test_database_command=None,
        test_database_ready_command=None,
    )
    with pytest.raises(SystemExit):
        harness_cli._validate_test_options(no_runner, parser)

    # Incomplete group
    incomplete = argparse.Namespace(
        test_runner_command=["pytest", "{test_id}"],
        test_build_command=["make"],
        test_artifact_path=None,
        test_database_engine=None,
        test_database_test_id=None,
        test_database_command=None,
        test_database_ready_command=None,
    )
    with pytest.raises(SystemExit):
        harness_cli._validate_test_options(incomplete, parser)

    # Invalid test_runner problem (absolute path in artifact_paths)
    invalid = argparse.Namespace(
        test_runner_command=["pytest", "{test_id}"],
        test_build_command=["make"],
        test_artifact_path=["/absolute/**"],
        test_database_engine=None,
        test_database_test_id=None,
        test_database_command=None,
        test_database_ready_command=None,
    )
    with pytest.raises(SystemExit):
        harness_cli._validate_test_options(invalid, parser)


def test_main_help(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["harness", "--help"])
    with pytest.raises(SystemExit) as exc:
        harness_cli.main()
    assert exc.value.code == 0
    assert "harness" in capsys.readouterr().out

