#!/usr/bin/env python3
"""Tests for scripts/check_private_terms.py (issue #438): staged changes, the commits of a range, the
branch name and body files are checked against a maintainer-only term list, and the output names
only a location and a term number. Every term in this file is synthetic."""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_private_terms.py"

# Term numbers are physical line numbers: line 1 is a comment, so the terms are #2..#5.
TERMS = (
    "# synthetic terms for tests only\n"
    "Zorblax\n"
    "КвазиПлюх\n"
    "ёлкин-хост.example.invalid\n"
    "QX-7741\n"
)
ENV = {"HARNESS_PRIVATE_TERMS": TERMS}
SKIPPED = (
    "private-terms: skipped: no term list (set HARNESS_PRIVATE_TERMS or create "
    ".private-terms.txt in the main checkout; see docs/agents/releases.md)\n"
)
_GIT = (
    "git",
    "-c",
    "user.name=Test",
    "-c",
    "user.email=test@example.invalid",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "init.defaultBranch=main",
)


def _load() -> types.ModuleType:
    loader = importlib.machinery.SourceFileLoader(
        "check_private_terms_under_test", str(SCRIPT)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


check = _load()


def _run(
    argv: list[str], environ: dict[str, str] | None = None
) -> tuple[int, str, str]:
    environ = ENV if environ is None else environ
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = check.main(argv, environ=environ)
    _assert_no_leak(out.getvalue() + err.getvalue(), environ)
    return code, out.getvalue(), err.getvalue()


def _assert_no_leak(output: str, environ: dict[str, str]) -> None:
    """Fail when the output contains any term of the run, compared the way the check compares."""
    lists = (TERMS, environ.get("HARNESS_PRIVATE_TERMS", ""))
    terms = {line.strip() for text in lists for line in text.splitlines()}
    folded = check.fold(output)
    for number, term in enumerate(sorted(t for t in terms if t and t[0] != "#"), 1):
        if check.fold(term) in folded:
            # Never put the term itself into a failure message: it would reach the CI log.
            raise AssertionError(f"the output contains term {number} of the test lists")


def _matches(stderr: str) -> list[str]:
    return [line.strip() for line in stderr.splitlines() if line.startswith("  ")]


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        [*_GIT, "-C", str(cwd), *args], capture_output=True, check=True
    )
    return result.stdout.decode("utf-8").strip()


def _repo(root: Path, name: str = "repo") -> Path:
    repo = root / name
    repo.mkdir()
    _git(repo, "init", "-q")
    return repo


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


class TermListTests(unittest.TestCase):
    def test_a_non_empty_variable_wins_over_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            (repo / ".private-terms.txt").write_text("Zorblax\n", encoding="utf-8")
            body = Path(tmp) / "body.md"
            body.write_text("Zorblax and QX-7741\n", encoding="utf-8")

            code, _, err = _run(
                ["--repo", str(repo), "--body-file", str(body)],
                {"HARNESS_PRIVATE_TERMS": "\n\nQX-7741\n"},
            )

        self.assertEqual(code, 1)
        self.assertIn("line numbers in HARNESS_PRIVATE_TERMS:", err)
        self.assertEqual(_matches(err), [f"{body}:1: term #3"])

    def test_the_file_of_the_main_checkout_serves_a_linked_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            _commit(repo, "base")
            (repo / ".private-terms.txt").write_text(TERMS, encoding="utf-8")
            worktree = Path(tmp) / "wt"
            _git(repo, "worktree", "add", "-q", str(worktree))
            body = Path(tmp) / "body.md"
            body.write_text("QX-7741\n", encoding="utf-8")

            code, _, err = _run(["--repo", str(worktree), "--body-file", str(body)], {})

        self.assertEqual(code, 1)
        self.assertIn("line numbers in .private-terms.txt:", err)
        self.assertEqual(_matches(err), [f"{body}:1: term #5"])

    def test_a_byte_order_mark_is_not_part_of_the_first_term(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            (repo / ".private-terms.txt").write_bytes("﻿Zorblax\n".encode())
            body = Path(tmp) / "body.md"
            body.write_text("a Zorblax b\n", encoding="utf-8")

            code, _, err = _run(["--repo", str(repo), "--body-file", str(body)], {})

        self.assertEqual(code, 1)
        self.assertEqual(_matches(err), [f"{body}:1: term #1"])

    def test_an_invalid_utf8_file_is_an_error_that_prints_no_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            (repo / ".private-terms.txt").write_bytes(b"Zorblax\n\xff\xfeQX-7741\n")
            body = Path(tmp) / "body.md"
            body.write_text("Zorblax\n", encoding="utf-8")

            code, out, err = _run(["--repo", str(repo), "--body-file", str(body)], {})

        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(err, "private-terms: .private-terms.txt is not valid UTF-8\n")


class StagedTests(unittest.TestCase):
    def test_an_added_line_reports_its_path_and_new_line_number(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            (repo / "notes.md").write_text("one\ntwo\n", encoding="utf-8")
            _commit(repo, "base")
            (repo / "notes.md").write_text(
                "one\ntwo\nsee Zorblax here\n", encoding="utf-8"
            )
            _git(repo, "add", "notes.md")

            code, out, err = _run(["--repo", str(repo), "--staged"])

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(
            err,
            "private-terms: 1 match(es); term numbers are line numbers in HARNESS_PRIVATE_TERMS:\n"
            "  notes.md:3: term #2\n"
            "private-terms: remove or generalize the matched text before publishing.\n",
        )

    def test_a_removed_line_is_not_a_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            (repo / "notes.md").write_text("one\nZorblax\n", encoding="utf-8")
            _commit(repo, "base")
            (repo / "notes.md").write_text("one\n", encoding="utf-8")
            _git(repo, "add", "notes.md")

            code, out, err = _run(["--repo", str(repo), "--staged"])

        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            out, "private-terms: no matches (HARNESS_PRIVATE_TERMS, 4 terms)\n"
        )

    def test_the_name_of_an_empty_new_file_is_checked_and_not_printed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            _commit(repo, "base")
            (repo / "Zorblax-notes.txt").write_text("", encoding="utf-8")
            _git(repo, "add", "Zorblax-notes.txt")

            code, out, err = _run(["--repo", str(repo), "--staged"])

        self.assertEqual(code, 1)
        self.assertEqual(_matches(err), ["staged file #1 (name): term #2"])
        self.assertNotIn("Zorblax", out + err)

    def test_the_content_of_a_binary_file_is_not_checked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            _commit(repo, "base")
            (repo / "blob.bin").write_bytes(b"\x00Zorblax\x00")
            _git(repo, "add", "blob.bin")

            code, _, err = _run(["--repo", str(repo), "--staged"])

        self.assertEqual((code, err), (0, ""))


class CommitTests(unittest.TestCase):
    def test_a_message_line_reports_the_commit_and_the_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            base = _commit(repo, "base")
            head = _commit(repo, "subject\n\nsee QX-7741")

            code, _, err = _run(["--repo", str(repo), "--commits", f"{base}..HEAD"])

        self.assertEqual(code, 1)
        self.assertEqual(_matches(err), [f"commit {head[:12]}:3: term #5"])

    def test_a_term_added_and_removed_inside_the_range_is_still_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            base = _commit(repo, "base")
            (repo / "notes.md").write_text("Zorblax\n", encoding="utf-8")
            added = _commit(repo, "add notes")
            (repo / "notes.md").write_text("generic\n", encoding="utf-8")
            _commit(repo, "generalize notes")

            code, _, err = _run(["--repo", str(repo), "--commits", f"{base}..HEAD"])

        self.assertEqual(code, 1)
        self.assertEqual(_matches(err), [f"commit {added[:12]} notes.md:1: term #2"])

    def test_by_default_only_commits_missing_from_remotes_are_checked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            _commit(repo, "published Zorblax")
            _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
            head = _commit(repo, "fix QX-7741")

            code, _, err = _run(["--repo", str(repo), "--commits"])

        self.assertEqual(code, 1)
        self.assertEqual(_matches(err), [f"commit {head[:12]}:1: term #5"])

    def test_an_unborn_head_has_no_commits_to_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))

            code, _, err = _run(["--repo", str(repo), "--commits"])

        self.assertEqual((code, err), (0, ""))

    def test_an_invalid_range_is_a_git_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            _commit(repo, "base")

            code, out, err = _run(
                ["--repo", str(repo), "--commits", "no-such-rev..HEAD"]
            )

        self.assertEqual((code, out), (2, ""))
        self.assertRegex(err, r"^private-terms: git log failed \(exit \d+\)\n$")


class BranchTests(unittest.TestCase):
    def test_the_current_branch_is_reported_without_its_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            _commit(repo, "base")
            _git(repo, "checkout", "-q", "-b", "feature/Zorblax-fix")

            code, out, err = _run(["--repo", str(repo), "--branch"])

        self.assertEqual(code, 1)
        self.assertEqual(_matches(err), ["branch:1: term #2"])
        self.assertNotIn("Zorblax", out + err)

    def test_an_explicit_branch_name_is_checked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))

            code, _, err = _run(["--repo", str(repo), "--branch", "topic/QX-7741"])

        self.assertEqual(code, 1)
        self.assertEqual(_matches(err), ["branch:1: term #5"])

    def test_a_detached_head_has_no_branch_to_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            _commit(repo, "base")
            _git(repo, "checkout", "-q", "--detach")

            code, _, err = _run(["--repo", str(repo), "--branch"])

        self.assertEqual((code, err), (0, ""))


class BodyFileTests(unittest.TestCase):
    def test_a_body_line_reports_the_file_and_the_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("1\n2\n3\n4\nпро КвазиПлюх\n", encoding="utf-8")

            code, _, err = _run(["--body-file", str(body)])

        self.assertEqual(code, 1)
        self.assertEqual(_matches(err), [f"{body}:5: term #3"])

    def test_a_missing_body_file_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "missing.md"

            code, out, err = _run(["--body-file", str(body)])

        self.assertEqual((code, out), (2, ""))
        self.assertEqual(err, f"private-terms: cannot read body file {body}\n")

    def test_sources_of_one_run_are_reported_together(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "title.txt", Path(tmp) / "body.md"
            first.write_text("Zorblax\n", encoding="utf-8")
            second.write_text("ok\nQX-7741 and Zorblax\n", encoding="utf-8")

            code, _, err = _run(["--body-file", str(first), "--body-file", str(second)])

        self.assertEqual(code, 1)
        self.assertIn("private-terms: 3 match(es);", err)
        self.assertEqual(
            _matches(err),
            [f"{first}:1: term #2", f"{second}:2: term #2", f"{second}:2: term #5"],
        )


class FoldingTests(unittest.TestCase):
    def test_latin_and_cyrillic_match_regardless_of_case_and_form(self) -> None:
        lines = [
            "ZORBLAX",
            "zorblaxClient",
            "my_zorblax_db",
            "кВАЗИплюх",
            "КВАЗИПЛЮХ",
            "ЕЛКИН-ХОСТ.example.invalid",  # е written for ё
            "ёлкин-хост.example.invalid",  # decomposed ё
            "ＱＸ-７７４１",  # fullwidth forms
            "qx-774 and Zorbla",  # prefixes of terms are not terms
        ]
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("\n".join(lines) + "\n", encoding="utf-8")

            code, _, err = _run(["--body-file", str(body)])

        self.assertEqual(code, 1)
        self.assertEqual(
            _matches(err),
            [
                f"{body}:1: term #2",
                f"{body}:2: term #2",
                f"{body}:3: term #2",
                f"{body}:4: term #3",
                f"{body}:5: term #3",
                f"{body}:6: term #4",
                f"{body}:7: term #4",
                f"{body}:8: term #5",
            ],
        )

    def test_terms_are_folded_like_the_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("zorblax and квазиплюх\n", encoding="utf-8")

            code, _, err = _run(
                ["--body-file", str(body)],
                {"HARNESS_PRIVATE_TERMS": "ZoRbLaX\nКВАЗИПЛЮХ\n"},
            )

        self.assertEqual(code, 1)
        self.assertEqual(_matches(err), [f"{body}:1: term #1", f"{body}:1: term #2"])

    def test_a_lowercase_branch_name_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))

            code, _, err = _run(
                ["--repo", str(repo), "--branch", "feature/zorblax-fix"]
            )

        self.assertEqual(code, 1)
        self.assertEqual(_matches(err), ["branch:1: term #2"])


class SkipTests(unittest.TestCase):
    def test_without_a_list_the_check_skips_with_a_notice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            body = Path(tmp) / "body.md"
            body.write_text("Zorblax\n", encoding="utf-8")

            code, out, err = _run(["--repo", str(repo), "--body-file", str(body)], {})

        self.assertEqual((code, out, err), (0, SKIPPED, ""))

    def test_the_notice_is_a_workflow_annotation_in_github_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("Zorblax\n", encoding="utf-8")

            code, out, _ = _run(
                ["--repo", tmp, "--body-file", str(body)], {"GITHUB_ACTIONS": "true"}
            )

        self.assertEqual((code, out), (0, "::notice title=private-terms::" + SKIPPED))

    def test_the_check_skips_before_running_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = _run(
                ["--repo", tmp, "--staged", "--commits", "--branch"], {}
            )

        self.assertEqual((code, out, err), (0, SKIPPED, ""))

    def test_an_empty_variable_falls_through_to_the_file(self) -> None:
        # GitHub substitutes an empty string for a secret that does not exist.
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            (repo / ".private-terms.txt").write_text("Zorblax\n", encoding="utf-8")
            body = Path(tmp) / "body.md"
            body.write_text("Zorblax\n", encoding="utf-8")

            code, _, err = _run(
                ["--repo", str(repo), "--body-file", str(body)],
                {"HARNESS_PRIVATE_TERMS": ""},
            )

        self.assertEqual(code, 1)
        self.assertIn("line numbers in .private-terms.txt:", err)

    def test_a_list_of_comments_only_is_no_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            (repo / ".private-terms.txt").write_text("# Zorblax\n\n", encoding="utf-8")
            body = Path(tmp) / "body.md"
            body.write_text("# Zorblax\n", encoding="utf-8")

            from_file = _run(["--repo", str(repo), "--body-file", str(body)], {})
            from_variable = _run(
                ["--body-file", str(body)], {"HARNESS_PRIVATE_TERMS": "# Zorblax\n"}
            )

        self.assertEqual(from_file, (0, SKIPPED, ""))
        self.assertEqual(from_variable, (0, SKIPPED, ""))

    def test_an_explicit_environment_ignores_the_process_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("Zorblax\n", encoding="utf-8")

            with mock.patch.dict(os.environ, ENV):
                code, out, _ = _run(["--repo", tmp, "--body-file", str(body)], {})

        self.assertEqual((code, out), (0, SKIPPED))


class NoLeakTests(unittest.TestCase):
    def test_the_local_term_file_is_ignored_by_git(self) -> None:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "-q", ".private-terms.txt"],
            check=False,
        )

        self.assertEqual(result.returncode, 0)

    def test_a_staged_path_with_a_term_is_replaced_by_its_ordinal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            _commit(repo, "base")
            (repo / "a.md").write_text("ok\n", encoding="utf-8")
            (repo / "Проект").mkdir()
            (repo / "Проект" / "КвазиПлюх.md").write_text("Zorblax\n", encoding="utf-8")
            _git(repo, "add", "-A")

            code, _, err = _run(["--repo", str(repo), "--staged"])

        self.assertEqual(code, 1)
        self.assertEqual(
            _matches(err),
            ["staged file #2 (name): term #3", "staged file #2:1: term #2"],
        )

    def test_a_quoted_path_is_never_printed(self) -> None:
        patch = (
            'diff --git "a/x\\tnotes.md" "b/x\\tnotes.md"\n'
            "new file mode 100644\n"
            "index 0000000..1111111\n"
            "--- /dev/null\n"
            '+++ "b/x\\tnotes.md"\n'
            "@@ -0,0 +1 @@\n"
            "+Zorblax\n"
        )
        terms = check.load_terms(None, ENV)

        findings = check._patch_findings(patch, terms, "", "staged file")

        self.assertEqual(findings, [("staged file #1:1", 2)])

    def test_a_commit_hash_with_a_term_is_replaced_by_its_position(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _repo(Path(tmp))
            base = _commit(repo, "base")
            head = _commit(repo, "Zorblax")
            environ = {"HARNESS_PRIVATE_TERMS": f"{head[2:9]}\nZorblax\n"}

            code, _, err = _run(
                ["--repo", str(repo), "--commits", f"{base}..HEAD"], environ
            )

        self.assertEqual(code, 1)
        self.assertEqual(_matches(err), ["commit #1:1: term #2"])

    def test_a_body_path_with_a_term_is_replaced_by_its_ordinal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plain, named = Path(tmp) / "title.txt", Path(tmp) / "Zorblax-body.md"
            plain.write_text("ok\n", encoding="utf-8")
            named.write_text("QX-7741\n", encoding="utf-8")
            missing = Path(tmp) / "Zorblax-missing.md"

            code, _, err = _run(["--body-file", str(plain), "--body-file", str(named)])
            error = _run(["--body-file", str(plain), "--body-file", str(missing)])

        self.assertEqual(code, 1)
        self.assertEqual(_matches(err), ["body file #2:1: term #5"])
        self.assertEqual(
            error, (2, "", "private-terms: cannot read body file body file #2\n")
        )

    def test_an_internal_error_prints_only_its_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.md"
            body.write_text("ok\n", encoding="utf-8")
            failure = RuntimeError("Zorblax")

            with mock.patch.object(check, "_text_findings", side_effect=failure):
                result = _run(["--body-file", str(body)])

        self.assertEqual(
            result, (2, "", "private-terms: internal error (RuntimeError)\n")
        )

    def test_a_legacy_console_encoding_escapes_instead_of_failing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "Проект" / "body.md"
            body.parent.mkdir()
            body.write_text("Zorblax\n", encoding="utf-8")
            env = dict(os.environ, PYTHONIOENCODING="cp1252", **ENV)

            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--body-file", str(body)],
                capture_output=True,
                env=env,
                check=False,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn(b"\\u041f\\u0440", result.stderr)
        self.assertIn(b": term #2", result.stderr)
        _assert_no_leak(result.stderr.decode("cp1252"), ENV)


class CommandLineTests(unittest.TestCase):
    def test_a_run_without_a_source_is_a_usage_error(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                check.main([], environ=ENV)

        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
