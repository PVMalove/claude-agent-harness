"""The project keeps the `uv` cache inside the project unless the user sets one."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    shutil.which("uv") is None, reason="uv is not installed"
)


def _uv_cache_dir(environment: dict[str, str]) -> str:
    """The cache directory `uv` resolves from the project root, without running a sync."""
    env = {key: value for key, value in os.environ.items() if key != "UV_CACHE_DIR"}
    env.update(environment)
    result = subprocess.run(
        ["uv", "cache", "dir"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_the_uv_cache_defaults_to_a_directory_inside_the_project() -> None:
    assert _uv_cache_dir({}) == ".harness/.sandboxes/cache/uv"


def test_a_user_uv_cache_dir_is_not_overridden() -> None:
    assert _uv_cache_dir({"UV_CACHE_DIR": "/custom/uv"}) == "/custom/uv"
