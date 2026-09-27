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
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from scripts import verify
from scripts.verification import process

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
            self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
            self.assertNotIn("PYTHONPYCACHEPREFIX", env)
            self.assertEqual(env["MYPY_CACHE_DIR"], str(run_tmp / "mypy"))
            resolved = subprocess.run(
                [sys.executable, "-c", "import tempfile; print(tempfile.gettempdir())"],
                env=dict(os.environ, **env),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(os.path.normcase(resolved), os.path.normcase(str(run_tmp)))

    def test_child_git_does_not_discover_the_parent_checkout(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            project = Path(temporary) / "project"
            project.mkdir()
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            run_tmp = project / ".harness" / "tmp" / "tests" / "run"
            nested = run_tmp / "not-a-repo"
            nested.mkdir(parents=True)
            env = verify.isolated_temp_env(dict(os.environ), run_tmp)

            result = subprocess.run(
                ["git", "-C", str(nested), "rev-parse", "--show-toplevel"],
                env=env,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)


class WrittenPathBudgetTest(unittest.TestCase):
    """PYTHONPYCACHEPREFIX mirrors the absolute source path under the run root, so a QA checkout
    nested in the same repository wrote a 260-char .pyc and failed without LongPathsEnabled (#381).
    What verify's Python writes must be bounded by the run root, not by how deep the sources are."""

    # Headroom for the run root's own files (mypy cache, pytest basetemp names), not for sources.
    RUN_ROOT_HEADROOM = 64

    def _written(self, root: Path, before: set[Path]) -> list[Path]:
        return [path for path in root.rglob("*") if path.is_file() and path not in before]

    def test_nothing_written_mirrors_a_deep_checkout(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            base = Path(temporary)
            run_tmp = base / "v"
            run_tmp.mkdir()
            # A checkout deep enough that mirroring it under the run root would blow the headroom.
            checkout = base / ("q" * 40) / ("c" * 40)
            package = checkout / "pkg"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
            before = set(base.rglob("*"))
            env = verify.isolated_temp_env(dict(os.environ, PYTHONPATH=str(checkout)), run_tmp)

            verify.check_syntax([str(package / "__init__.py"), str(package / "mod.py")])
            subprocess.run([sys.executable, "-c", "import pkg.mod"], env=env, check=True)

            written = self._written(base, before)
            self.assertEqual([path for path in written if path.suffix == ".pyc"], [])
            longest = max((len(str(path)) for path in written), default=0)
            self.assertLessEqual(longest, len(str(run_tmp)) + self.RUN_ROOT_HEADROOM)

    def test_syntax_check_reports_the_broken_file(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            broken = Path(temporary) / "broken.py"
            broken.write_text("def f(:\n", encoding="utf-8")
            with (
                mock.patch("sys.stderr") as stderr,
                self.assertRaises(SystemExit) as raised,
            ):
                verify.check_syntax([str(broken)])

            self.assertEqual(raised.exception.code, 1)
            reported = "".join(call.args[0] for call in stderr.write.call_args_list)
            self.assertIn("broken.py", reported)
            self.assertEqual(list(Path(temporary).rglob("*.pyc")), [])


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


class VerifyStageTimingTest(unittest.TestCase):
    def test_successful_stage_reports_its_name_and_duration(self) -> None:
        with (
            mock.patch.object(time, "perf_counter", side_effect=[10.0, 12.5]),
            mock.patch.object(process, "run_ok") as run_ok,
            mock.patch("builtins.print") as printed,
        ):
            verify.run_stage("pytest", ["pytest"])

        run_ok.assert_called_once_with(["pytest"], env=None, stdout=None, cwd=None)
        printed.assert_called_once_with("[verify] pytest: passed in 2.50s")

    def test_failed_stage_reports_its_name_and_duration_before_propagating(self) -> None:
        with (
            mock.patch.object(time, "perf_counter", side_effect=[10.0, 11.0]),
            mock.patch.object(process, "run_ok", side_effect=SystemExit(7)),
            mock.patch("builtins.print") as printed,
            self.assertRaises(SystemExit),
        ):
            verify.run_stage("clean-room", ["clean-room"])

        printed.assert_called_once_with("[verify] clean-room: failed in 1.00s")


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
