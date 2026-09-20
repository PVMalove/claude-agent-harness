#!/usr/bin/env python3
"""Characterisation tests pinning the Orca adapter's helper behaviour before typing it (issue #224)."""

from __future__ import annotations

import argparse
import ast
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock
from collections.abc import Mapping
from pathlib import Path

from harness.errors import HarnessError
from harness.orchestration import orca_adapter as adapter


def _tempdir(case: unittest.TestCase) -> Path:
    directory = Path(tempfile.mkdtemp())
    case.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
    return directory


class DispatchErrorInvariantTests(unittest.TestCase):
    def test_dispatch_error_is_a_harness_error(self) -> None:
        self.assertTrue(issubclass(adapter.DispatchError, HarnessError))
        self.assertTrue(issubclass(adapter.OrcaLaunchRejected, adapter.DispatchError))

    def test_every_raise_site_passes_a_non_empty_remedy(self) -> None:
        tree = ast.parse(Path(adapter.__file__).read_text(encoding="utf-8"))
        sites = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
            and isinstance(node.exc.func, ast.Name) and node.exc.func.id == "DispatchError"
        ]
        self.assertEqual(len(sites), 38)
        for site in sites:
            assert isinstance(site.exc, ast.Call)
            remedies = [keyword.value for keyword in site.exc.keywords if keyword.arg == "remedy"]
            self.assertEqual(len(remedies), 1, f"line {site.lineno} has no remedy")
            value = remedies[0]
            if isinstance(value, ast.Constant):
                self.assertTrue(isinstance(value.value, str) and value.value.strip(), f"line {site.lineno}")

    def test_launch_rejection_remedy_reflects_fallback_safety(self) -> None:
        safe = adapter.OrcaLaunchRejected(True)
        unsafe = adapter.OrcaLaunchRejected(False)
        self.assertTrue(safe.safe_to_fallback)
        self.assertFalse(unsafe.safe_to_fallback)
        self.assertIn("fallback chain", safe.remedy)
        self.assertIn("duplicate worker", unsafe.remedy)


class ReadJsonTests(unittest.TestCase):
    def test_reads_an_object(self) -> None:
        path = _tempdir(self) / "c.json"
        path.write_text('{"a": 1}', encoding="utf-8")
        self.assertEqual(adapter._read_json(path, "cfg"), {"a": 1})

    def test_rejects_missing_file_and_bad_syntax(self) -> None:
        directory = _tempdir(self)
        bad = directory / "bad.json"
        bad.write_text("{", encoding="utf-8")
        for path in (directory / "missing.json", bad):
            with self.assertRaises(adapter.DispatchError) as ctx:
                adapter._read_json(path, "cfg")
            self.assertEqual(ctx.exception.message, "cfg is not valid JSON")
            self.assertEqual(ctx.exception.remedy, f"fix the JSON syntax in {path}")

    def test_rejects_a_non_object(self) -> None:
        path = _tempdir(self) / "list.json"
        path.write_text("[]", encoding="utf-8")
        with self.assertRaises(adapter.DispatchError) as ctx:
            adapter._read_json(path, "cfg")
        self.assertEqual(ctx.exception.message, "cfg must be a JSON object")


class ValueValidatorTests(unittest.TestCase):
    def test_non_empty_string(self) -> None:
        self.assertTrue(adapter._non_empty_string(" x "))
        for value in ("", "  ", None, 3, ["x"]):
            self.assertFalse(adapter._non_empty_string(value))

    def test_model_id_is_stripped_and_validated(self) -> None:
        self.assertEqual(adapter._validate_model_id(" claude-opus-4.1:x "), "claude-opus-4.1:x")
        for value in ("", "has space", "-leading", None, 7):
            with self.assertRaises(adapter.DispatchError) as ctx:
                adapter._validate_model_id(value)
            self.assertEqual(ctx.exception.message, "assignment model must be a CLI model ID or alias without spaces")

    def test_sensitive_keys_are_rejected_at_any_depth(self) -> None:
        adapter._reject_sensitive_keys({"a": [{"input_tokens": 1}, "x"], "n": None}, "cfg")
        with self.assertRaises(adapter.DispatchError) as ctx:
            adapter._reject_sensitive_keys({"a": [{}, {"api_key": "x"}]}, "cfg")
        self.assertEqual(ctx.exception.message, "cfg.a[1] must not contain secret-shaped field 'api_key'")
        with self.assertRaises(adapter.DispatchError) as ctx:
            adapter._reject_sensitive_keys({1: "x"}, "cfg")
        self.assertEqual(ctx.exception.message, "cfg contains a non-string key")


class OrcaPayloadTests(unittest.TestCase):
    def test_result_id_follows_the_key_path(self) -> None:
        payload = {"result": {"task": {"id": "t1"}}}
        self.assertEqual(adapter._result_id(payload, ("task", "id")), "t1")
        self.assertEqual(adapter._result_id({"id": "bare"}, ("id",)), "bare")

    def test_result_id_is_none_for_missing_blank_or_non_object_paths(self) -> None:
        self.assertIsNone(adapter._result_id({"result": {"task": {}}}, ("task", "id")))
        self.assertIsNone(adapter._result_id({"result": {"task": "flat"}}, ("task", "id")))
        self.assertIsNone(adapter._result_id({"result": {"task": {"id": "  "}}}, ("task", "id")))
        self.assertIsNone(adapter._result_id({"result": {"task": {"id": 3}}}, ("task", "id")))

    def test_active_workers_counts_the_list(self) -> None:
        self.assertEqual(adapter._active_workers({"result": {"workers": [{}, {}]}}), 2)
        self.assertEqual(adapter._active_workers({"workers": [{}]}), 1)
        self.assertEqual(adapter._active_workers({"result": {}}), 0)
        self.assertEqual(adapter._active_workers({"result": "opaque"}), 0)

    def test_active_workers_rejects_a_non_list(self) -> None:
        with self.assertRaises(adapter.DispatchError) as ctx:
            adapter._active_workers({"result": {"workers": "many"}})
        self.assertEqual(ctx.exception.message, "Orca worker listing is invalid")


class RunOrcaTests(unittest.TestCase):
    def _fake(self, body: str) -> str:
        path = _tempdir(self) / "fake-orca.py"
        path.write_text(f"import sys\n{body}\n", encoding="utf-8")
        return str(path)

    def test_returns_the_success_payload(self) -> None:
        fake = self._fake('print(\'{"ok": true, "result": {"x": 1}}\')')
        self.assertEqual(adapter._run_orca(fake, ["a"]), {"ok": True, "result": {"x": 1}})

    def test_non_json_output_is_rejected(self) -> None:
        clean = self._fake('print("not json")')
        failing = self._fake('print("not json"); sys.exit(1)')
        with self.assertRaises(adapter.DispatchError) as ctx:
            adapter._run_orca(clean, [])
        self.assertEqual(ctx.exception.message, "Orca returned non-JSON output")
        with self.assertRaises(adapter.DispatchError) as ctx:
            adapter._run_orca(failing, [])
        self.assertEqual(ctx.exception.message, "Orca returned non-JSON output for a rejected dispatch")

    def test_unsuccessful_payloads_are_rejected(self) -> None:
        for body in ('print("[]")', 'print(\'{"ok": false}\')'):
            with self.assertRaises(adapter.DispatchError) as ctx:
                adapter._run_orca(self._fake(body), [])
            self.assertEqual(ctx.exception.message, "Orca returned an unsuccessful dispatch result")

    def test_nonzero_exit_classifies_fallback_safety_by_error_code(self) -> None:
        cases = {
            "agent_unavailable": True, "model_unavailable": True, "account_unavailable": True, "boom": False,
        }
        for code, safe in cases.items():
            fake = self._fake(f'print(\'{{"error": {{"code": "{code}"}}}}\'); sys.exit(1)')
            with self.assertRaises(adapter.OrcaLaunchRejected) as ctx:
                adapter._run_orca(fake, [])
            self.assertEqual(ctx.exception.safe_to_fallback, safe, code)
        with self.assertRaises(adapter.OrcaLaunchRejected) as ctx:
            adapter._run_orca(self._fake('print("[]"); sys.exit(1)'), [])
        self.assertFalse(ctx.exception.safe_to_fallback)

    def test_python_scripts_run_through_the_current_interpreter(self) -> None:
        self.assertEqual(adapter._orca_command("x.PY", ["a"])[1:], ["x.PY", "a"])
        self.assertEqual(adapter._orca_command("orca", ["a"]), ["orca", "a"])


class WriteRecordTests(unittest.TestCase):
    def test_writes_a_sorted_record_and_refuses_to_overwrite(self) -> None:
        directory = _tempdir(self) / "nested"
        path = adapter._write_record(directory, {"dispatch_id": "d1", "b": 1, "a": "é"})
        self.assertEqual(path, directory / "d1.json")
        self.assertEqual(path.read_text(encoding="utf-8"), '{\n  "a": "é",\n  "b": 1,\n  "dispatch_id": "d1"\n}\n')
        with self.assertRaises(adapter.DispatchError) as ctx:
            adapter._write_record(directory, {"dispatch_id": "d1"})
        self.assertEqual(ctx.exception.message, "refusing to overwrite an immutable dispatch record")


class CandidateProfileTests(unittest.TestCase):
    ROLE = {"required_capabilities": ["write"]}

    def _config(self) -> dict[str, object]:
        return {
            "provider_profiles": {
                "a": {"agent": "claude", "capabilities": ["write"], "fallback": ["b"]},
                "b": {"agent": "codex", "capabilities": ["write", "read"], "fallback": ["a"]},
                "ro": {"agent": "x", "capabilities": ["read"], "fallback": []},
                "nofb": {"agent": "x", "capabilities": ["write"], "fallback": "b"},
                "noagent": {"agent": "", "capabilities": ["write"], "fallback": []},
            }
        }

    def _candidates(self, plan: Mapping[str, object], preferred: object = None) -> list[tuple[str, str, str | None]]:
        return adapter._candidate_profiles(self._config(), plan, self.ROLE, preferred)

    def test_fallback_chain_is_flattened_without_duplicates(self) -> None:
        plan = {"model": " m1 ", "effort": "high", "profiles": ["a"]}
        self.assertEqual(self._candidates(plan), [("a", "m1", "high"), ("b", "m1", "high")])

    def test_preferred_profile_overrides_the_plan_order(self) -> None:
        plan = {"model": "m1", "effort": "low", "profiles": ["a"]}
        self.assertEqual(self._candidates(plan, "b")[0][0], "b")

    def test_invalid_plans_are_rejected(self) -> None:
        cases: dict[str, dict[str, object]] = {
            "profiles is not a list": {"model": "m", "effort": "x", "profiles": "a"},
            "blank effort": {"model": "m", "effort": " ", "profiles": ["a"]},
            "unknown profile": {"model": "m", "effort": "x", "profiles": ["zzz"]},
            "no agent": {"model": "m", "effort": "x", "profiles": ["noagent"]},
            "incompatible": {"model": "m", "effort": "x", "profiles": ["ro"]},
            "bad fallback": {"model": "m", "effort": "x", "profiles": ["nofb"]},
            "empty profiles": {"model": "m", "effort": "x", "profiles": []},
        }
        for name, plan in cases.items():
            with self.subTest(name), self.assertRaises(adapter.DispatchError):
                self._candidates(plan)

    def test_config_without_a_profile_object_is_rejected(self) -> None:
        with self.assertRaises(adapter.DispatchError) as ctx:
            adapter._candidate_profiles({}, {"profiles": ["a"]}, self.ROLE)
        self.assertEqual(ctx.exception.message, "project orchestration config has no valid provider profiles")


class GitHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = _tempdir(self)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "t@example.invalid")
        self._git("config", "user.name", "T")
        (self.repo / "a.txt").write_text("1", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-qm", "first")
        self.first = self._git("rev-parse", "HEAD")
        (self.repo / "b.txt").write_text("2", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-qm", "second")
        self.second = self._git("rev-parse", "HEAD")

    def _git(self, *arguments: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.repo), *arguments], check=True, capture_output=True, text=True
        ).stdout.strip()

    def test_issue_branch_lookup(self) -> None:
        adapter._issue_branch_exists(self.repo, "main")
        with self.assertRaises(adapter.DispatchError) as ctx:
            adapter._issue_branch_exists(self.repo, "nope")
        self.assertEqual(ctx.exception.message, "approved issue branch does not exist locally or on origin")

    def test_resolved_commit_requires_the_full_sha(self) -> None:
        self.assertEqual(adapter._resolved_commit(self.repo, self.second.upper()), self.second)
        for value in ("zzzzzzz", 5, None, self.second[:8], "0" * 40):
            with self.assertRaises(adapter.DispatchError):
                adapter._resolved_commit(self.repo, value)

    def test_candidate_files_with_and_without_a_base(self) -> None:
        self.assertEqual(adapter._candidate_files(self.repo, self.second, self.first), ["b.txt"])
        self.assertEqual(adapter._candidate_files(self.repo, self.first, None), ["a.txt"])
        with self.assertRaises(adapter.DispatchError) as ctx:
            adapter._candidate_files(self.repo, "0" * 40, None)
        self.assertEqual(ctx.exception.message, "cannot inspect the pinned candidate commit")

    def test_ancestry(self) -> None:
        self.assertTrue(adapter._is_ancestor(self.repo, self.first, self.second))
        self.assertFalse(adapter._is_ancestor(self.repo, self.second, self.first))
        with self.assertRaises(adapter.DispatchError) as ctx:
            adapter._is_ancestor(self.repo, "0" * 40, self.second)
        self.assertEqual(ctx.exception.message, "cannot verify review_base ancestry")


class DispatchLockTests(unittest.TestCase):
    def _args(self, records: Path) -> argparse.Namespace:
        return argparse.Namespace(repo=str(records), records_dir=str(records))

    def test_an_existing_lock_blocks_the_dispatch(self) -> None:
        records = _tempdir(self)
        (records / ".dispatch.lock").mkdir()
        with self.assertRaises(adapter.DispatchError) as ctx:
            adapter.dispatch(self._args(records))
        self.assertEqual(ctx.exception.message, "another dispatch is being admitted; retry after it settles")

    def test_the_lock_is_released_after_a_failed_dispatch(self) -> None:
        records = _tempdir(self)
        args = self._args(records)
        args.brief = str(records / "missing.json")
        with self.assertRaises(adapter.DispatchError):
            adapter.dispatch(args)
        self.assertFalse((records / ".dispatch.lock").exists())


class ParserTests(unittest.TestCase):
    def test_dispatch_arguments(self) -> None:
        args = adapter.parser().parse_args(["dispatch", "--brief", "b.json", "--run", "r1"])
        self.assertEqual((args.repo, args.brief, args.run, args.records_dir), (".", "b.json", "r1", None))
        self.assertIs(args.func, adapter.dispatch)

    def test_main_prints_an_error_and_exits_two_on_a_harness_error(self) -> None:
        records = _tempdir(self)
        (records / ".dispatch.lock").mkdir()
        with unittest.mock.patch(
            "sys.argv", ["orca_adapter", "dispatch", "--repo", str(records), "--brief", "b", "--run", "r",
                         "--records-dir", str(records)],
        ), unittest.mock.patch("sys.stderr") as stderr:
            self.assertEqual(adapter.main(), 2)
        self.assertIn("REMEDY:", "".join(call.args[0] for call in stderr.write.call_args_list))


if __name__ == "__main__":
    unittest.main()
