"""qa-gate test_summary.py: a command that cannot start yields an ERROR summary, not a traceback."""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import sys
import types
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "first-party"
    / "pvmalove"
    / "qa-gate"
    / "scripts"
    / "test_summary.py"
)


def _load() -> types.ModuleType:
    loader = importlib.machinery.SourceFileLoader(
        "qa_gate_test_summary_under_test", str(SCRIPT)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


summary = _load()


@pytest.fixture(autouse=True)
def _source_gate_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    # The script would otherwise alias `harness` in sys.modules to an installed `.harness` copy.
    monkeypatch.setattr(
        summary,
        "_GATE_RUNNER",
        importlib.import_module("harness.gate_runner.gate_runner"),
    )


def test_a_missing_command_prints_an_error_summary_and_leaves_no_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = summary.summarize([str(tmp_path / "missing-command")], tmp_path, 10, 10)

    assert code == 127
    out = capsys.readouterr().out
    assert out.startswith(
        "=== TEST SUMMARY ===\nStatus: ERROR (could not start command"
    )
    assert list(tmp_path.iterdir()) == []


def test_a_missing_gate_runner_prints_an_error_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing() -> types.ModuleType:
        raise RuntimeError("shared gate-runner is missing; run harness update")

    monkeypatch.setattr(summary, "_gate_runner", missing)

    code = summary.summarize([sys.executable, "-c", "pass"], tmp_path, 10, 10)

    assert code == 127
    assert "gate-runner is missing" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_a_passing_command_prints_its_pytest_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    command = [sys.executable, "-c", "print('3 passed in 0.10s')"]

    assert summary.summarize(command, tmp_path, 10, 10) == 0
    out = capsys.readouterr().out
    assert "Status: PASS\nPytest: 3 passed\nDuration: 0.10s\n" in out
