"""The mark hook counts a QA pass only for a run of the whole QA command whose exit status the
Bash call reports: a narrower command, a masked failure or another program's -c does not count."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

HOOKS = Path(__file__).resolve().parents[2] / "harness" / "project" / "hooks"
QA = "python3 -m unittest -q"
COMPOUND = "cd backend && make test"


@pytest.fixture(scope="module")
def hook() -> ModuleType:
    """qa-gate-state.py, imported as the installed hook imports itself (pr_commands beside it)."""
    sys.path.insert(0, str(HOOKS))
    bytecode = sys.dont_write_bytecode
    try:
        spec = importlib.util.spec_from_file_location(
            "qa_gate_state", HOOKS / "qa-gate-state.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.dont_write_bytecode = bytecode
        sys.path.remove(str(HOOKS))


@pytest.mark.parametrize(
    ("command", "qa_command", "counts"),
    [
        (QA, QA, True),
        (f"{QA} 2>&1", QA, True),
        (f"cd sub && {QA}", QA, True),
        (f"{QA} && echo done", QA, True),
        (f"python3 t.py -- bash -lc '{QA}'", QA, True),
        (f'python3 t.py -- bash -lc "{QA}"', QA, True),
        (
            f"python t.py -- powershell -NoProfile -NonInteractive -Command '{QA}'",
            QA,
            True,
        ),
        (f"python t.py -- pwsh -command '{QA}'", QA, True),
        (COMPOUND, COMPOUND, True),
        (f"python3 t.py -- bash -lc '{COMPOUND}'", COMPOUND, True),
        # A narrower or longer command only contains the QA command's text.
        (f"{QA} test_greeting", QA, False),
        (f"python3 t.py -- bash -lc '{QA} test_greeting'", QA, False),
        (f"echo '{QA}'", QA, False),
        ("make test", COMPOUND, False),
        ("cd backend; make test", COMPOUND, False),
        # The Bash call's exit status no longer reports the QA command's.
        (f"{QA} || true", QA, False),
        (f"{QA}; true", QA, False),
        (f"{QA} | tail -n 5", QA, False),
        (f"{QA} &", QA, False),
        (f"true || {QA}", QA, False),
        (f"python3 t.py -- bash -lc '{QA}' || true", QA, False),
        # Only a shell runs a command string.
        (f"mytool -c '{QA}'", QA, False),
        # Outside the strict lexer's bash subset nothing counts.
        (f"FOO=1 {QA}", QA, False),
        ('python3 t.py -- bash -lc "$(jq -r .x f)"', QA, False),
    ],
)
def test_only_a_run_of_the_whole_qa_command_counts(
    hook: ModuleType, command: str, qa_command: str, counts: bool
) -> None:
    """Проверить, что маркер QA засчитывает только прогон всей QA-команды, чей код возврата
    сообщает вызов Bash."""
    assert hook.runs_qa_command(command, qa_command) is counts
