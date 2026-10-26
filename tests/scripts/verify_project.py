"""A throwaway project that ``scripts/verify.py`` verifies in the process-seam tests: a Git
repository with the manifests of an environment and a small pytest suite."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from harness.gate_runner import stage_evidence
from scripts import verify
from scripts.verification import cache

GOOD_TEST = "def test_good():\n    assert True\n"
BAD_TEST = (
    "import pytest\n"
    "\n"
    "def test_bad():\n"
    "    assert 0\n"
    "\n"
    '@pytest.mark.parametrize("value", ["a/b", 2])\n'
    "def test_param(value):\n"
    "    assert value == 2\n"
    "\n"
    "class TestGroup:\n"
    "    def test_member(self):\n"
    "        assert False\n"
)


def make_repo(path: Path, tests: Mapping[str, str] | None = None) -> Path:
    """Create a committed repository whose ``tests/`` hold the given files (name to text)."""
    path.mkdir(parents=True)
    git = [
        "git",
        "-C",
        str(path),
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@example.invalid",
    ]
    subprocess.run([*git, "init", "-q"], check=True)
    (path / "uv.lock").write_text("lock-1\n", encoding="utf-8")
    (path / "pyproject.toml").write_text(
        "[tool.x]\nvalue = 1\n\n[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
        encoding="utf-8",
    )
    (path / "tests").mkdir()
    (path / "tests" / "__init__.py").write_text("", encoding="utf-8")
    for name, text in (tests or {}).items():
        (path / "tests" / name).write_text(text, encoding="utf-8")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "test: project"], check=True)
    return path


# Each fake stage writes to both streams and records the mypy cache directory it was given.
_FAKE_STAGE = (
    "import os, sys; print('{name}-out'); print('{name}-err', file=sys.stderr); "
    "print('MYPY_CACHE_DIR=' + os.environ.get('MYPY_CACHE_DIR', ''))"
)


def fake_stage(name: str, exit_code: int = 0) -> list[str]:
    """A small command in place of a slow stage."""
    code = _FAKE_STAGE.format(name=name) + f"; sys.exit({exit_code})"
    return [sys.executable, "-c", code]


def install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    tests: Mapping[str, str] | None = None,
) -> Path:
    """Make ``scripts/verify.py`` verify a throwaway project: real pytest, fake mypy and clean-room."""
    root = make_repo(tmp_path / "project", tests or {"test_a.py": GOOD_TEST})
    for name in (cache.COLD_ENV, cache.JUNIT_ENV):
        monkeypatch.delenv(name, raising=False)
    outer = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    run_tmp = tmp_path / "run"

    def prepare() -> tuple[Path, dict[str, str]]:
        # Every run removes its root at the end, so each run starts a fresh one.
        run_tmp.mkdir(exist_ok=True)
        return run_tmp, verify.isolated_temp_env(outer, run_tmp)

    monkeypatch.setattr(verify, "ROOT", root)
    monkeypatch.setattr(verify, "_static_checks", lambda env: None)
    monkeypatch.setattr(verify, "_prepare_run_root", prepare)
    monkeypatch.setattr(verify, "mypy_command", lambda: fake_stage("mypy"))
    monkeypatch.setattr(verify, "clean_room_command", lambda: fake_stage("clean-room"))
    monkeypatch.setattr(os, "cpu_count", lambda: 1)
    return root


def run_verify(*argv: str) -> int:
    """Run ``verify.main`` and return its exit code."""
    try:
        verify.main(list(argv))
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else 1
    return 0


def read_document(directory: Path) -> stage_evidence.StageEvidenceDocument:
    """The stage-evidence document written into an evidence directory."""
    return stage_evidence.parse(
        (directory / cache.EVIDENCE_FILE).read_text(encoding="utf-8")
    )


def stream(directory: Path, stage: int, kind: str = "stdout") -> str:
    """The text of one kept stream of a recorded stage."""
    recorded = read_document(directory).stages[stage]
    ref = recorded.stdout if kind == "stdout" else recorded.stderr
    assert ref is not None
    return (directory / ref.path).read_text(encoding="utf-8")
