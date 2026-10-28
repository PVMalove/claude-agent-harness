"""The data behind the Update Harness screen: drift report and per-file diff."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from harness.console import data

CLI = Path(__file__).resolve().parents[2] / "harness" / "bin" / "harness.py"
RULES = ".claude/rules/token-efficiency.md"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            sys.executable,
            str(CLI),
            "init",
            str(tmp_path),
            "--capability",
            "engineering-suite",
            "--language",
            "ru",
            "--pr-base-branch",
            "main",
            "--branch-pattern",
            "^feature/.+",
            "--qa-gate-command",
            "true",
        ],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    return tmp_path


def test_drift_report_returns_the_diff_json(project: Path) -> None:
    payload = data.drift_report(project)
    assert payload is not None and payload["state"] == "clean"
    assert payload["seed_differing"] == []
    assert data.drift_state(project) == "clean"


def test_drift_report_says_missing_for_a_project_without_a_harness(
    tmp_path: Path,
) -> None:
    payload = data.drift_report(tmp_path)
    assert payload is not None and payload["state"] == "missing"
    assert data.drift_state(tmp_path) == "missing"


@pytest.mark.parametrize(
    "outcome",
    [OSError("no python"), subprocess.TimeoutExpired("harness", 30), "not json"],
)
def test_drift_report_is_none_without_a_readable_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: object
) -> None:
    def fake(*_args: object, **_kwargs: object) -> object:
        if isinstance(outcome, BaseException):
            raise outcome
        return subprocess.CompletedProcess([], 0, str(outcome), "")

    monkeypatch.setattr(subprocess, "run", fake)
    assert data.drift_report(tmp_path) is None
    assert data.drift_state(tmp_path) == "неизвестно"


def test_update_diff_text_shows_a_seed_file_against_its_template(project: Path) -> None:
    path = project / RULES
    path.write_text(path.read_text(encoding="utf-8") + "\nLOCAL\n", encoding="utf-8")
    text = data.update_diff_text(project, RULES)
    assert text.startswith(f"--- проект/{RULES}\n+++ после обновления/{RULES}")
    assert "-LOCAL" in text


def test_a_file_that_matches_what_the_update_writes_has_an_empty_diff(
    project: Path,
) -> None:
    assert data.update_diff_text(project, RULES) == ""
    assert data.update_diff_text(project, "no/such/file.md") == ""


def test_a_binary_file_has_no_diff(project: Path) -> None:
    path = project / RULES
    path.write_bytes(b"\xff\xfe\x00binary")
    assert "diff недоступен" in data.update_diff_text(project, RULES)
