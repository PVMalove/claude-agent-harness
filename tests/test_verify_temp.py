#!/usr/bin/env python3
"""Tests for the per-run temp isolation in scripts/verify.py and the clean-room path budget
(issue #305): a shared %TEMP%\\pytest-of-<USERNAME> owned by another account fails every run with
WinError 5, and a long temp root pushes the clean-room tree past MAX_PATH."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from scripts import verify

CLEAN_ROOM = Path(__file__).resolve().parents[1] / "scripts" / "test_clean_room.py"


def _load_clean_room() -> types.ModuleType:
    loader = importlib.machinery.SourceFileLoader(
        "test_clean_room_under_test", str(CLEAN_ROOM)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


clean_room = _load_clean_room()


class IsolatedTempEnvTest(unittest.TestCase):
    def test_child_processes_resolve_temp_to_the_run_root(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            run_tmp = Path(temporary)
            env = verify.isolated_temp_env({"KEEP": "1", "TMP": "elsewhere"}, run_tmp)

            self.assertEqual(env["KEEP"], "1")
            for name in ("TMP", "TEMP", "TMPDIR"):
                self.assertEqual(env[name], str(run_tmp))
            resolved = subprocess.run(
                [sys.executable, "-c", "import tempfile; print(tempfile.gettempdir())"],
                env=dict(os.environ, **env),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(os.path.normcase(resolved), os.path.normcase(str(run_tmp)))


class RemoveTreeTest(unittest.TestCase):
    def test_removes_read_only_files_like_git_objects(self) -> None:
        root = Path(tempfile.mkdtemp())
        nested = root / "repo" / ".git" / "objects" / "ab"
        nested.mkdir(parents=True)
        blob = nested / "cdef"
        blob.write_bytes(b"x")
        os.chmod(blob, stat.S_IREAD)

        verify.remove_tree(root)

        self.assertFalse(root.exists())


class CleanRoomPathBudgetTest(unittest.TestCase):
    def test_short_root_passes(self) -> None:
        with mock.patch.object(clean_room, "_long_paths_enabled", return_value=False):
            clean_room._check_path_budget(Path("C:/t/cr.abcdefgh"))

    def test_long_root_without_long_paths_fails_with_actionable_message(self) -> None:
        root = Path("C:/" + "d" * 120)
        with (
            mock.patch.object(clean_room.os, "name", "nt"),
            mock.patch.object(clean_room, "_long_paths_enabled", return_value=False),
            self.assertRaises(SystemExit) as raised,
        ):
            clean_room._check_path_budget(root)
        message = str(raised.exception.code)
        self.assertIn("MAX_PATH", message)
        self.assertIn("TMP", message)

    def test_long_root_is_fine_when_long_paths_are_enabled(self) -> None:
        root = Path("C:/" + "d" * 120)
        with (
            mock.patch.object(clean_room.os, "name", "nt"),
            mock.patch.object(clean_room, "_long_paths_enabled", return_value=True),
        ):
            clean_room._check_path_budget(root)


if __name__ == "__main__":
    unittest.main()
