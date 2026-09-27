"""harness.console.catalog: the console's command catalog is plain stdlib data. These tests run
without textual and prove each entry's CLI argv dispatches to the CLI function it names, so the
console only ever calls existing CLI logic."""

from __future__ import annotations

import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from harness.console.catalog import (
    CONFIRMATION_REASONS,
    HARNESS_COMMANDS,
    CatalogEntry,
    Reversibility,
    process_argv,
)
from harness.console.launcher import BIN_HARNESS_PATH

ROOT = Path(__file__).resolve().parents[1]
REPO = Path("/work/project")
ENTRIES = {entry.key: entry for entry in HARNESS_COMMANDS}


def test_catalog_covers_every_harness_and_ledger_command() -> None:
    assert {
        "init",
        "update",
        "diff",
        "adopt",
        "registry",
        "lock-project-skills",
        "list",
        "health",
        "cleanup",
        "repo-map",
        "parser-bundle",
        "verify",
        "ledger-migrate",
        "ledger-clean",
        "ledger-reset",
    } <= ENTRIES.keys()
    assert len(ENTRIES) == len(HARNESS_COMMANDS)


def test_irreversible_commands_carry_their_confirmation_class() -> None:
    irreversible = {
        entry.key: entry.reversibility for entry in HARNESS_COMMANDS if entry.needs_confirmation
    }
    assert irreversible == {
        "init": Reversibility.OVERWRITES_MANAGED_FILES,
        "update": Reversibility.OVERWRITES_MANAGED_FILES,
        "adopt": Reversibility.OVERWRITES_MANAGED_FILES,
        "health-online-fix": Reversibility.EXTERNAL_CHANGE,
        "cleanup-apply": Reversibility.DELETES_LOCAL_DATA,
        "cleanup-hard-apply": Reversibility.DELETES_LOCAL_DATA,
        "ledger-clean": Reversibility.DELETES_LOCAL_DATA,
        "ledger-reset": Reversibility.DELETES_LOCAL_DATA,
    }
    assert set(CONFIRMATION_REASONS) == set(Reversibility) - {Reversibility.REVERSIBLE}


def test_typed_confirmations_match_the_cli_safeguard_words() -> None:
    reset = ENTRIES["ledger-reset"]
    assert reset.typed_confirmation == "RESET"
    assert reset.argv[-2:] == ("--confirm", "RESET")
    hard = ENTRIES["cleanup-hard-apply"]
    assert hard.typed_confirmation == "HARD"
    assert hard.argv[-2:] == ("--confirm", "HARD")


@pytest.mark.parametrize(
    "entry", [e for e in HARNESS_COMMANDS if e.argv[0] == "harness"], ids=lambda e: e.key
)
def test_harness_cli_argv_dispatches_to_the_named_function(entry: CatalogEntry) -> None:
    cli = runpy.run_path(str(BIN_HARNESS_PATH))
    args = cli["parser"]().parse_args(entry.cli_argv(REPO)[1:])
    module, name = entry.function.split(":")
    assert module == "harness/bin/harness.py"
    assert args.func.__name__ == name
    assert args.repo == str(REPO)


@pytest.mark.parametrize(
    "entry", [e for e in HARNESS_COMMANDS if e.key.startswith("ledger-")], ids=lambda e: e.key
)
def test_coordinator_argv_dispatches_to_the_named_handler(entry: CatalogEntry) -> None:
    from harness.orchestration import coordinator

    argv = entry.cli_argv(REPO)
    assert argv[:2] == ["python", ".harness/orchestration/coordinator.py"]
    args = coordinator.parser().parse_args(argv[2:])
    module, name = entry.function.split(":")
    assert module == "harness.orchestration.coordinator"
    assert args.handler is getattr(coordinator, name)
    assert args.repo == str(REPO)


@pytest.mark.parametrize(
    "key, source", [("repo-map", "harness/repo_map/repo_map.py"),
                    ("parser-bundle", "scripts/build_parser_bundle.py"),
                    ("verify", "scripts/verify.py")],
)
def test_script_entries_name_an_existing_main(key: str, source: str) -> None:
    entry = ENTRIES[key]
    assert entry.function == f"{source}:main"
    assert entry.argv[1] in (source, f".harness/{source.removeprefix('harness/')}")
    assert "\ndef main(" in (ROOT / source).read_text(encoding="utf-8")


def test_inputs_are_asked_and_filled_into_the_cli_equivalent() -> None:
    entry = ENTRIES["parser-bundle"]
    assert entry.inputs == ("wheelhouse",)
    assert "<wheelhouse>" in entry.cli_line(REPO)
    assert entry.cli_argv(REPO, {"wheelhouse": "wheels"})[3] == "wheels"


def test_process_argv_runs_the_cli_equivalent_under_this_interpreter() -> None:
    assert process_argv(["harness", "diff", "r"]) == [sys.executable, str(BIN_HARNESS_PATH), "diff", "r"]
    assert process_argv(["python", "scripts/verify.py"]) == [sys.executable, "scripts/verify.py"]


def test_process_argv_prefers_the_repository_dev_environment(tmp_path: Path) -> None:
    """The console runs in a one-off `uv run --with textual` environment without pytest/mypy, so
    a `python` command such as verify runs under the repository's `.harness/.venv` when present."""
    assert process_argv(["python", "x.py"], tmp_path) == [sys.executable, "x.py"]
    venv_python = tmp_path / ".harness" / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")

    assert process_argv(["python", "x.py"], tmp_path) == [str(venv_python), "x.py"]
    assert process_argv(["harness", "diff", "r"], tmp_path)[0] == sys.executable


def test_catalog_imports_without_textual() -> None:
    code = "import sys; sys.modules['textual'] = None; import harness.console.catalog"
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
