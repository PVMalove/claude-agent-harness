"""One harness version: pyproject.toml's [project].version follows harness/VERSION (what
`harness update` reports and records), so the two numbers never drift apart again."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_pyproject_version_matches_harness_version() -> None:
    """Проверить, что версия в pyproject.toml совпадает с версией в harness/VERSION."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert (
        pyproject["project"]["version"]
        == (ROOT / "harness" / "VERSION").read_text(encoding="utf-8").strip()
    )
