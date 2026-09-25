"""Storage root selection at repository and nested-directory boundaries."""

from __future__ import annotations

import subprocess
from pathlib import Path

from harness.storage import storage_root


def test_nested_directory_does_not_inherit_parent_repository_cache(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    nested = tmp_path / "nested"
    nested.mkdir()

    assert storage_root(tmp_path) == tmp_path / ".harness"
    assert storage_root(nested) == nested / ".harness"
