"""Keeps two textual pins equal: harness/console/pin.py (what `uv run --with` requests, ADR 0025)
and pyproject.toml's [dependency-groups].dev (what `uv sync` installs for local TUI tests)."""

from __future__ import annotations

import tomllib
from pathlib import Path

from harness.console.pin import TEXTUAL_PIN

ROOT = Path(__file__).resolve().parents[1]


def test_dev_dependency_group_pins_the_same_textual_version_as_console_pin() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev_group = pyproject["dependency-groups"]["dev"]
    pinned = [entry for entry in dev_group if entry.startswith("textual==")]

    assert pinned == [f"textual=={TEXTUAL_PIN}"]
