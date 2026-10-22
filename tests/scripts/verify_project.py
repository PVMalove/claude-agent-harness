"""A throwaway project that ``scripts/verify.py`` verifies in the process-seam tests: a Git
repository with the manifests of an environment and a small pytest suite."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

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
