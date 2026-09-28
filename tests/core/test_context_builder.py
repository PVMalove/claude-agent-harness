#!/usr/bin/env python3
"""Focused public-contract tests for the standalone Context Package builder.

The default (offline, bundle-free) test run exercises Repo Map's minimal tier: no tree-sitter
grammar bundle is configured, so Repo Map returns empty `edges` and no `signatures` for every file
(ADR 0008 degradation). Graph widening, symbol-graph depth, and dependency-signature behaviour
that need real edges are therefore covered separately by the `HARNESS_PARSER_BUNDLE_DIR`-gated tests
below, following the pattern in tests/test_repo_map_tree_sitter.py: unset, they skip; set with a
missing bundle, they fail (an unavailable artifact is a failure, not a silent skip).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import patch

import pytest

from harness.context_builder.context_builder import (
    ContextPackageError,
    _dependency_context,
    _fallback_excerpt,
    _redact_symbols,
    build_context_package,
    estimate_tokens,
)
from harness.errors import HarnessError

MODULE_ROOT = Path(__file__).resolve().parents[2] / "harness" / "context_builder"
BUNDLE_ENV = "HARNESS_PARSER_BUNDLE_DIR"


def _run(*args: str, cwd: Path) -> None:
    """Выполнить команду git в указанной рабочей директории."""
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _write(repo: Path, path: str, content: str) -> None:
    """Записать текстовый файл по указанному относительному пути в репозитории."""
    file_path = repo / path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")


def _head(repo: Path) -> str:
    """Получить хэш текущего HEAD-коммита репозитория."""
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class ContextBuilderFixture(unittest.TestCase):
    """Базовая фикстура для создания тестового git-репозитория с пакетом, тестом и ADR."""

    def setUp(self) -> None:
        """Инициализировать временный git-репозиторий и записать тестовые коммиты."""
        self._temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.repo = Path(self._temporary.name) / "repo"
        self.repo.mkdir()
        _run("init", "-q", cwd=self.repo)
        _run("config", "user.email", "test@example.invalid", cwd=self.repo)
        _run("config", "user.name", "Context Builder Test", cwd=self.repo)

        _write(self.repo, "pkg/base.py", "def helper():\n    return 1\n")
        _write(
            self.repo,
            "pkg/dependency.py",
            "from pkg import second_hop\n\n"
            "class Service:\n    pass\n\n"
            "def compose(value: int) -> Service:\n    return Service()\n\n"
            "async def fetch() -> None:\n    return None\n",
        )
        _write(
            self.repo,
            "pkg/second_hop.py",
            "def hidden_detail():\n    return 'not direct'\n",
        )
        _write(
            self.repo,
            "pkg/consumer.py",
            "from pkg import base\n\ndef use():\n    return base.helper()\n",
        )
        _write(
            self.repo,
            "pkg/indirect.py",
            "from pkg import consumer\n\ndef call():\n    return consumer.use()\n",
        )
        _write(
            self.repo,
            "tests/test_base.py",
            "from pkg import base\n\ndef test_helper():\n    assert base.helper() == 1\n",
        )
        _write(
            self.repo,
            "docs/adr/0001-base-module.md",
            "# Base module contract\n\nDescribes the base helper contract used across the pkg package.\n",
        )
        _run("add", ".", cwd=self.repo)
        _run("commit", "-qm", "feat: base module and its adr", cwd=self.repo)
        self.base_commit = _head(self.repo)

        _write(
            self.repo,
            "pkg/base.py",
            "from pkg import dependency\n\n\ndef helper():\n    return dependency.compose(2)\n",
        )
        _run("add", ".", cwd=self.repo)
        _run("commit", "-qm", "fix: change base helper return value", cwd=self.repo)
        self.candidate_commit = _head(self.repo)

    def tearDown(self) -> None:
        """Очистить временную директорию после выполнения теста."""
        self._temporary.cleanup()


class ContextBuilderTests(ContextBuilderFixture):
    """Набор тестов для сборщика контекста при использовании минимального (безбандлового) уровня Repo Map."""

    def test_builds_diff_starting_files_cards_and_hashes(self) -> None:
        """Проверить сборку диффа, начальных файлов, карточек прецедентов и хэшей файлов."""
        package = build_context_package(
            self.repo,
            self.base_commit,
            self.candidate_commit,
            min_starting_files=1,
            max_starting_files=10,
        )

        self.assertEqual(package.base_commit, self.base_commit)
        self.assertEqual(package.candidate_commit, self.candidate_commit)
        self.assertIn("dependency.compose(2)", package.diff)
        self.assertIn("return 1", package.diff)

        paths = {item.path for item in package.starting_files}
        self.assertIn("pkg/base.py", paths)
        base_reason = next(
            item.reason for item in package.starting_files if item.path == "pkg/base.py"
        )
        self.assertIn("changed in diff", base_reason)

        # Repo Map has no grammar bundle configured, so it runs at minimal tier: no edges are
        # returned at all, and `symbol_graph` reflects that honestly instead of inventing a graph.
        self.assertEqual(
            package.symbol_graph["pkg/base.py"],
            {"imports": [], "imported_by": [], "references": [], "referenced_by": []},
        )
        self.assertEqual(package.related_tests, [])

        self.assertTrue(package.precedent_cards)
        self.assertEqual(package.precedent_cards[0].id, "0001-base-module")
        self.assertEqual(package.precedent_cards[0].title, "Base module contract")

        self.assertIn("pkg/base.py", package.file_hashes)
        self.assertGreater(package.size_bytes, 0)

        self.assertEqual(package.schema_version, 2)
        self.assertEqual(package.parser, "path-only")
        assert package.parser_provenance is not None
        self.assertEqual(package.parser_provenance["tier"], "minimal")
        self.assertEqual(package.parser_provenance["parser"], "path-only")
        self.assertIn("degradation_reason", package.parser_provenance)
        self.assertIn("token_estimator_version", package.parser_provenance)
        self.assertIsInstance(package.parser_provenance["parser_provenance"], dict)

    def test_the_default_no_policy_diff_still_includes_every_changed_file(self) -> None:
        """Проверить, что по умолчанию без политики diff включает каждый измененный файл."""
        _write(
            self.repo,
            "pkg/consumer.py",
            "from pkg import base\n\ndef use():\n    return base.helper() + 1\n",
        )
        _run("add", ".", cwd=self.repo)
        _run("commit", "-qm", "fix: also touch consumer", cwd=self.repo)
        candidate = _head(self.repo)

        package = build_context_package(
            self.repo, self.base_commit, candidate, min_starting_files=1
        )

        self.assertIn("dependency.compose(2)", package.diff)
        self.assertIn("base.helper() + 1", package.diff)

    def test_output_is_byte_identical_across_repeated_builds(self) -> None:
        """Проверить детерминированность вывода и побайтовое совпадение при повторной сборке."""
        first = build_context_package(
            self.repo, self.base_commit, self.candidate_commit, min_starting_files=1
        )
        second = build_context_package(
            self.repo, self.base_commit, self.candidate_commit, min_starting_files=1
        )

        self.assertEqual(first.to_json(), second.to_json())

    def test_fails_clearly_instead_of_truncating_when_the_size_limit_is_exceeded(
        self,
    ) -> None:
        """Проверить понятную ошибку вместо усечения при превышении лимита размера пакета."""
        with self.assertRaises(ContextPackageError):
            build_context_package(
                self.repo,
                self.base_commit,
                self.candidate_commit,
                min_starting_files=1,
                max_package_size_bytes=1,
            )

    def test_reports_a_deterministic_token_estimate_and_enforces_it(self) -> None:
        """Проверить детерминированную оценку токенов и соблюдение лимита токенов."""
        package = build_context_package(
            self.repo,
            self.base_commit,
            self.candidate_commit,
            min_starting_files=1,
            max_package_tokens=10_000,
        )

        self.assertGreater(package.estimated_tokens, 0)
        with self.assertRaisesRegex(ContextPackageError, "max_package_tokens"):
            build_context_package(
                self.repo,
                self.base_commit,
                self.candidate_commit,
                min_starting_files=1,
                max_package_tokens=1,
            )

    def _commit_mirror_pair(self, content: str) -> str:
        """Создать коммит с парой зеркальных (байт-в-байт идентичных) файлов."""
        _write(self.repo, "docs/agents/guide.md", content)
        _write(self.repo, "harness/project/docs-agents/guide.md", content)
        _run("add", ".", cwd=self.repo)
        _run("commit", "-qm", "docs: add a mirrored guide", cwd=self.repo)
        return _head(self.repo)

    def test_byte_identical_seed_files_are_counted_once_and_noted_as_a_mirror(
        self,
    ) -> None:
        """Проверить, что побайтово идентичные seed-файлы учитываются один раз и помечаются как зеркала."""
        guide = "".join(
            f"## Раздел {index}\n\nТекст раздела {index}.\n\n" for index in range(400)
        )
        snapshot = self._commit_mirror_pair(guide)
        one_copy = build_context_package(
            self.repo,
            snapshot,
            snapshot,
            min_starting_files=1,
            seed_paths=["docs/agents/guide.md"],
        )

        package = build_context_package(
            self.repo,
            snapshot,
            snapshot,
            min_starting_files=2,
            seed_paths=["docs/agents/guide.md", "harness/project/docs-agents/guide.md"],
        )

        reasons = {item.path: item.reason for item in package.starting_files}
        self.assertEqual(
            set(reasons),
            {"docs/agents/guide.md", "harness/project/docs-agents/guide.md"},
        )
        self.assertNotIn("mirror", reasons["docs/agents/guide.md"])
        self.assertIn(
            "byte-identical mirror of docs/agents/guide.md",
            reasons["harness/project/docs-agents/guide.md"],
        )
        # Only the second path's symbol-graph entry is added; its content is not charged again.
        self.assertLess(
            package.estimated_tokens - one_copy.estimated_tokens,
            estimate_tokens(guide) // 10,
        )

    def test_byte_identical_changed_files_are_counted_once(self) -> None:
        """Проверить, что побайтово идентичные измененные файлы тарифицируются один раз."""
        before = self._commit_mirror_pair("# Guide\n\nOld text.\n")
        guide = "# Guide\n\n" + "".join(f"Строка {index}.\n" for index in range(400))
        after = self._commit_mirror_pair(guide)

        package = build_context_package(self.repo, before, after, min_starting_files=2)

        reasons = {item.path: item.reason for item in package.starting_files}
        self.assertIn(
            "byte-identical mirror of docs/agents/guide.md",
            reasons["harness/project/docs-agents/guide.md"],
        )
        # The diff carries both hunks, but the full mirrored content is charged only once.
        self.assertLess(
            package.estimated_tokens,
            estimate_tokens(package.diff) + estimate_tokens(guide) * 1.5,
        )

    def _commit_large_guide(self) -> tuple[str, str]:
        """Создать коммит с большим markdown-руководством для тестов разбивки на разделы."""
        guide = (
            "# Guide\n\nIntro.\n\n"
            "## Первый раздел\n\n" + "Текст первого раздела.\n" * 300 + "\n"
            "```md\n## not a heading inside a fence\n```\n\n"
            "### Подраздел\n\n" + "Текст подраздела.\n" * 300 + "\n"
            "## Second\n\n" + "Second text.\n" * 300
        )
        _write(self.repo, "docs/guide.md", guide)
        _run("add", ".", cwd=self.repo)
        _run("commit", "-qm", "docs: add a large guide", cwd=self.repo)
        return _head(self.repo), guide

    def test_a_large_markdown_starting_file_is_seeded_as_a_section_index(
        self,
    ) -> None:
        """Проверить, что большой markdown-файл включается в виде индекса разделов."""
        snapshot, guide = self._commit_large_guide()
        lines = guide.split("\n")

        package = build_context_package(
            self.repo,
            snapshot,
            snapshot,
            min_starting_files=1,
            seed_paths=["docs/guide.md"],
            section_index_min_tokens=1_000,
        )

        (starting,) = package.starting_files
        self.assertIn("section index", starting.reason)
        self.assertEqual(
            [(section.heading, section.level) for section in starting.sections],
            [("Guide", 1), ("Первый раздел", 2), ("Подраздел", 3), ("Second", 2)],
        )
        first, sub, second = starting.sections[1:]
        self.assertEqual(lines[first.start_line - 1], "## Первый раздел")
        self.assertEqual(first.end_line, sub.start_line - 1)
        self.assertEqual(lines[second.start_line - 1], "## Second")
        self.assertEqual(second.end_line, len(lines))
        self.assertIn("docs/guide.md", package.file_hashes)
        self.assertLess(package.estimated_tokens, estimate_tokens(guide) // 10)

    def test_a_markdown_file_below_the_threshold_keeps_its_full_content(
        self,
    ) -> None:
        """Проверить, что markdown-файл меньше порогового размера сохраняет полное содержимое."""
        snapshot, guide = self._commit_large_guide()

        package = build_context_package(
            self.repo,
            snapshot,
            snapshot,
            min_starting_files=1,
            seed_paths=["docs/guide.md"],
            section_index_min_tokens=estimate_tokens(guide) + 1,
        )

        (starting,) = package.starting_files
        self.assertEqual(starting.sections, [])
        self.assertGreaterEqual(package.estimated_tokens, estimate_tokens(guide))

    def test_fails_clearly_instead_of_silently_returning_fewer_than_the_minimum_starting_files(
        self,
    ) -> None:
        """Проверить ошибку при невозможности набрать минимальное число начальных файлов."""
        # At minimal tier the import graph is empty, so widening below the minimum can never
        # succeed from a single changed file -- it fails clearly instead of under-delivering.
        with self.assertRaises(ContextPackageError):
            build_context_package(
                self.repo, self.base_commit, self.candidate_commit, min_starting_files=2
            )

    def test_added_file_content_is_not_double_counted_against_its_diff(self) -> None:
        """Проверить, что содержимое добавленного файла не учитывается дважды по отношению к его диффу."""
        _write(
            self.repo,
            "pkg/generated.py",
            "\n".join(f"VALUE_{index} = {index}" for index in range(400)),
        )
        _run("add", ".", cwd=self.repo)
        _run("commit", "-qm", "feat: add a large generated file", cwd=self.repo)
        candidate_with_addition = _head(self.repo)

        package = build_context_package(
            self.repo,
            self.candidate_commit,
            candidate_with_addition,
            min_starting_files=1,
        )

        self.assertIn(
            "pkg/generated.py", {item.path for item in package.starting_files}
        )
        # A brand-new file's unified diff already contains 100% of its content as `+` lines.
        # Re-reading and re-counting that same content for the token estimate would roughly
        # double it for this dominant file; assert it stays close to the diff-only estimate
        # instead of drifting toward double.
        diff_only_estimate = estimate_tokens(package.diff)
        self.assertLess(package.estimated_tokens, diff_only_estimate * 1.5)

    def test_every_raise_path_is_a_harness_error_with_a_message_and_a_remedy(
        self,
    ) -> None:
        """Проверить, что все ветки исключений возбуждают HarnessError с понятным сообщением и решением."""
        repo, base, candidate = self.repo, self.base_commit, self.candidate_commit
        cases: tuple[tuple[str, str, Callable[[], object]], ...] = (
            (
                "git diff",
                "git diff",
                lambda: build_context_package(repo, base, "0" * 40),
            ),
            (
                "direct import-graph neighbours",
                "lower min_starting_files below 2",
                lambda: build_context_package(
                    repo, base, candidate, min_starting_files=2
                ),
            ),
            (
                "must be >=1",
                "got min=3, max=2",
                lambda: build_context_package(
                    repo, base, candidate, min_starting_files=3, max_starting_files=2
                ),
            ),
            (
                "static starting file",
                f"seed_paths that exist at {candidate}",
                lambda: build_context_package(
                    repo,
                    candidate,
                    candidate,
                    min_starting_files=999,
                    max_starting_files=1000,
                ),
            ),
            (
                "max_package_size_bytes=1",
                "raise max_package_size_bytes above",
                lambda: build_context_package(
                    repo,
                    base,
                    candidate,
                    min_starting_files=1,
                    max_package_size_bytes=1,
                ),
            ),
            (
                "max_package_tokens=1",
                "max_package_tokens above",
                lambda: build_context_package(
                    repo, base, candidate, min_starting_files=1, max_package_tokens=1
                ),
            ),
        )
        for expected_message, expected_remedy, build in cases:
            with self.subTest(expected_message=expected_message):
                with self.assertRaises(ContextPackageError) as raised:
                    build()
                self.assertIsInstance(raised.exception, HarnessError)
                self.assertIn(expected_message, raised.exception.message)
                self.assertIn(expected_remedy, raised.exception.remedy)

    def test_makes_no_model_call_and_stays_pure_python_over_git_plumbing(self) -> None:
        """Проверить отсутствие вызовов моделей и сетевых библиотек в модуле сборщика контекста."""
        module_source = (MODULE_ROOT / "context_builder.py").read_text(encoding="utf-8")
        for banned in ("anthropic", "openai", "requests.post", "http://", "https://"):
            self.assertNotIn(banned, module_source)

    def test_a_policy_denied_changed_file_never_reaches_the_package(self) -> None:
        """Проверить, что запрещенный политикой deny_paths измененный файл не попадает в пакет контекста."""
        (self.repo / ".harness").mkdir(exist_ok=True)
        (self.repo / ".harness" / "orchestration.json").write_text(
            json.dumps({"repo_map_policy": {"deny_paths": ["pkg/base.py"]}}),
            encoding="utf-8",
        )

        package = build_context_package(
            self.repo,
            self.base_commit,
            self.candidate_commit,
            min_starting_files=1,
        )

        paths = {item.path for item in package.starting_files}
        self.assertNotIn("pkg/base.py", paths)
        self.assertNotIn("pkg/base.py", package.symbol_graph)
        self.assertNotIn("pkg/base.py", package.file_hashes)

    def test_a_policy_denied_changed_files_patch_is_absent_from_the_diff(self) -> None:
        """Проверить, что патч запрещенного политикой deny_paths файла отсутствует в итоговом диффе пакета."""
        (self.repo / ".harness").mkdir(exist_ok=True)
        (self.repo / ".harness" / "orchestration.json").write_text(
            json.dumps({"repo_map_policy": {"deny_paths": ["pkg/base.py"]}}),
            encoding="utf-8",
        )

        package = build_context_package(
            self.repo, self.base_commit, self.candidate_commit, min_starting_files=1
        )

        self.assertEqual(package.diff, "")
        self.assertNotIn("dependency.compose(2)", package.diff)

    def test_a_redact_paths_changed_file_is_also_absent_from_the_diff(self) -> None:
        """Проверить, что измененный файл из redact_paths исключается из диффа и хэшей пакета."""
        (self.repo / ".harness").mkdir(exist_ok=True)
        (self.repo / ".harness" / "orchestration.json").write_text(
            json.dumps({"repo_map_policy": {"redact_paths": ["pkg/base.py"]}}),
            encoding="utf-8",
        )

        package = build_context_package(
            self.repo, self.base_commit, self.candidate_commit, min_starting_files=1
        )

        self.assertEqual(package.diff, "")
        self.assertNotIn("pkg/base.py", package.file_hashes)

    def test_allow_paths_restricts_the_diff_to_the_allowed_slice(self) -> None:
        """Проверить, что фильтр allow_paths ограничивает дифф только разрешенным набором путей."""
        _write(
            self.repo,
            "pkg/consumer.py",
            "from pkg import base\n\ndef use():\n    return base.helper() + 1\n",
        )
        _run("add", ".", cwd=self.repo)
        _run("commit", "-qm", "fix: also touch consumer", cwd=self.repo)
        candidate = _head(self.repo)

        (self.repo / ".harness").mkdir(exist_ok=True)
        (self.repo / ".harness" / "orchestration.json").write_text(
            json.dumps({"repo_map_policy": {"allow_paths": ["pkg/base.py"]}}),
            encoding="utf-8",
        )

        package = build_context_package(
            self.repo, self.base_commit, candidate, min_starting_files=1
        )

        self.assertIn("dependency.compose(2)", package.diff)
        self.assertNotIn("base.helper() + 1", package.diff)
        self.assertNotIn("pkg/consumer.py", package.file_hashes)

    def test_redact_symbols_scrubs_matching_identifiers_from_diff_and_hashed_content(
        self,
    ) -> None:
        """Проверить удаление скрываемых символов из диффа и хэшируемого содержимого файлов пакета."""
        (self.repo / ".harness").mkdir(exist_ok=True)
        (self.repo / ".harness" / "orchestration.json").write_text(
            json.dumps({"repo_map_policy": {"redact_symbols": ["helper"]}}),
            encoding="utf-8",
        )

        package = build_context_package(
            self.repo, self.base_commit, self.candidate_commit, min_starting_files=1
        )

        self.assertNotIn("helper", package.diff)
        self.assertIn("[REDACTED-SYMBOL]", package.diff)

        raw_base_py = subprocess.run(
            ["git", "show", f"{self.candidate_commit}:pkg/base.py"],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        expected_hash = hashlib.sha256(
            _redact_symbols(raw_base_py, ("helper",)).encode("utf-8")
        ).hexdigest()
        self.assertEqual(package.file_hashes["pkg/base.py"], expected_hash)

    def test_redact_symbols_defaults_to_a_no_op(self) -> None:
        """Проверить, что redact_symbols по умолчанию без изменений возвращает переданный текст."""
        self.assertEqual(
            _redact_symbols("def helper(): pass", ()), "def helper(): pass"
        )
        self.assertEqual(_redact_symbols("", ("helper",)), "")


def _patch_repo_map_call(
    fake: subprocess.CompletedProcess[str],
) -> AbstractContextManager[object]:
    """Подменить вызов процесса Repo Map, не затрагивая остальные вызовы git через subprocess.run."""
    real_run = subprocess.run

    def _dispatch(
        args: list[str],
        *,
        capture_output: bool = True,
        text: bool = True,
        encoding: str = "utf-8",
        errors: str = "replace",
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """Диспетчер вызовов subprocess для подмены процесса repo_map."""
        if any(str(item).endswith("repo_map.py") for item in args):
            return fake
        return real_run(
            args,
            capture_output=capture_output,
            text=text,
            encoding=encoding,
            errors=errors,
            check=check,
        )

    return patch(
        "harness.context_builder.context_builder.subprocess.run", side_effect=_dispatch
    )


class RepoMapContractTests(ContextBuilderFixture):
    """Набор тестов для контракта взаимодействия и обработки ошибок Repo Map."""

    def test_invalid_json_on_stdout_raises_a_contract_error_with_a_remedy(self) -> None:
        """Проверить ошибку контракта с рекомендацией при некорректном JSON в выводе Repo Map."""
        fake = subprocess.CompletedProcess(
            args=["repo_map"], returncode=0, stdout="not json", stderr=""
        )
        with _patch_repo_map_call(fake):
            with self.assertRaises(ContextPackageError) as raised:
                build_context_package(
                    self.repo,
                    self.base_commit,
                    self.candidate_commit,
                    min_starting_files=1,
                )
        self.assertIn("invalid JSON", raised.exception.message)
        self.assertTrue(raised.exception.remedy)

    def test_a_missing_required_field_raises_a_contract_error_with_a_remedy(
        self,
    ) -> None:
        """Проверить ошибку контракта при отсутствии обязательного поля в ответе Repo Map."""
        incomplete = json.dumps({"schema_version": 1, "commit": "x" * 40})
        fake = subprocess.CompletedProcess(
            args=["repo_map"], returncode=0, stdout=incomplete, stderr=""
        )
        with _patch_repo_map_call(fake):
            with self.assertRaises(ContextPackageError) as raised:
                build_context_package(
                    self.repo,
                    self.base_commit,
                    self.candidate_commit,
                    min_starting_files=1,
                )
        self.assertIn("contract violation", raised.exception.message)
        self.assertIn("repo_map.schema.json", raised.exception.remedy)

    def test_an_unsupported_schema_version_raises_a_contract_error_with_a_remedy(
        self,
    ) -> None:
        """Проверить ошибку контракта при неподдерживаемой версии схемы Repo Map."""
        payload = json.dumps(
            {
                "schema_version": 2,
                "commit": "x" * 40,
                "tier": "minimal",
                "parser": "path-only",
                "degradation_reason": "n/a",
                "token_estimator_version": "n/a",
                "parser_provenance": {},
                "files": [],
                "edges": [],
                "diagnostics": [],
                "estimated_tokens": 0,
            }
        )
        fake = subprocess.CompletedProcess(
            args=["repo_map"], returncode=0, stdout=payload, stderr=""
        )
        with _patch_repo_map_call(fake):
            with self.assertRaises(ContextPackageError) as raised:
                build_context_package(
                    self.repo,
                    self.base_commit,
                    self.candidate_commit,
                    min_starting_files=1,
                )
        self.assertIn("schema_version", raised.exception.message)
        self.assertTrue(raised.exception.remedy)

    def test_invalid_tier_and_edge_are_rejected_at_the_repo_map_boundary(self) -> None:
        """Проверить отклонение некорректных значений tier и связей edges на границе Repo Map."""
        valid = {
            "schema_version": 1,
            "commit": self.candidate_commit,
            "tier": "minimal",
            "parser": "path-only",
            "degradation_reason": "policy requested minimal tier",
            "token_estimator_version": "1",
            "parser_provenance": {
                "policy_mode": "portable",
                "policy_sha256": None,
                "max_file_bytes": 1,
                "max_files": 1,
                "timeout_seconds": 1,
                "max_tokens": 4000,
            },
            "files": [{"path": "pkg/base.py"}],
            "edges": [],
            "diagnostics": [],
            "estimated_tokens": 1,
        }
        for change in ({"tier": "unknown"}, {"edges": [{"source": 7}]}):
            with self.subTest(change=change):
                fake = subprocess.CompletedProcess(
                    args=["repo_map"],
                    returncode=0,
                    stdout=json.dumps({**valid, **change}),
                    stderr="",
                )
                with _patch_repo_map_call(fake):
                    with self.assertRaises(ContextPackageError) as raised:
                        build_context_package(
                            self.repo,
                            self.base_commit,
                            self.candidate_commit,
                            min_starting_files=1,
                        )
                self.assertIn("contract violation", raised.exception.message)

    def test_a_nonzero_exit_parses_the_error_remedy_stderr_contract(self) -> None:
        """Проверить разбор ошибок и рекомендаций из stderr при ненулевом коде завершения Repo Map."""
        fake = subprocess.CompletedProcess(
            args=["repo_map"],
            returncode=2,
            stdout="",
            stderr="ERROR: something went wrong\nREMEDY: do the specific fix\n",
        )
        with _patch_repo_map_call(fake):
            with self.assertRaises(ContextPackageError) as raised:
                build_context_package(
                    self.repo,
                    self.base_commit,
                    self.candidate_commit,
                    min_starting_files=1,
                )
        self.assertIn("something went wrong", raised.exception.message)
        self.assertIn("do the specific fix", raised.exception.remedy)

    def test_a_nonzero_exit_with_unrecognised_stderr_still_raises_with_a_remedy(
        self,
    ) -> None:
        """Проверить генерацию ошибки с рекомендацией при нераспознанном выводе stderr аварийно завершенного процесса."""
        fake = subprocess.CompletedProcess(
            args=["repo_map"],
            returncode=1,
            stdout="",
            stderr="totally unexpected crash\n",
        )
        with _patch_repo_map_call(fake):
            with self.assertRaises(ContextPackageError) as raised:
                build_context_package(
                    self.repo,
                    self.base_commit,
                    self.candidate_commit,
                    min_starting_files=1,
                )
        self.assertIn("totally unexpected crash", raised.exception.message)
        self.assertTrue(raised.exception.remedy)


class PolicySliceGuardTests(ContextBuilderFixture):
    """Набор тестов для защиты от чтения файлов git в обход среза политики Repo Map."""

    def test_an_edge_target_outside_files_never_triggers_a_raw_read_of_it(self) -> None:
        """Проверить, что целевой файл ребра за пределами разрешенных файлов никогда не читается напрямую из git."""
        payload = json.dumps(
            {
                "schema_version": 1,
                "commit": self.candidate_commit,
                "tier": "minimal",
                "parser": "path-only",
                "degradation_reason": "n/a",
                "token_estimator_version": "n/a",
                "parser_provenance": {
                    "policy_mode": "portable",
                    "policy_sha256": None,
                    "max_file_bytes": 1,
                    "max_files": 1,
                    "timeout_seconds": 1,
                    "max_tokens": 4000,
                },
                "files": [{"path": "pkg/base.py", "signatures": []}],
                "edges": [
                    {
                        "source": "pkg/base.py",
                        "target": "pkg/dependency.py",
                        "kind": "import",
                        "confidence": "high",
                    }
                ],
                "diagnostics": [],
                "estimated_tokens": 0,
            }
        )
        fake = subprocess.CompletedProcess(
            args=["repo_map"], returncode=0, stdout=payload, stderr=""
        )
        with _patch_repo_map_call(fake):
            package = build_context_package(
                self.repo, self.base_commit, self.candidate_commit, min_starting_files=1
            )

        serialized = package.to_json()
        # `pkg/dependency.py`'s own source (never in `files`) must not leak anywhere in the
        # package, regardless of which field a future change might route a raw read through.
        self.assertNotIn("class Service", serialized)
        self.assertNotIn("async def fetch", serialized)
        self.assertNotIn("pkg/dependency.py", package.file_hashes)


class DependencyContextUnitTests(unittest.TestCase):
    """Набор модульных тестов для выбора контекста зависимостей (сигнатуры Repo Map или первые 30 строк)."""

    def test_uses_repo_map_signatures_when_present(self) -> None:
        """Проверить использование сигнатур Repo Map для контекста зависимости при их наличии."""
        context = _dependency_context(
            import_graph={"seed.py": {"dep.py"}},
            seeds=["seed.py"],
            files={"dep.py": "def f():\n    pass\n"},
            signatures_by_path={"dep.py": ["def f()"]},
        )
        self.assertEqual(context, {"dep.py": ["def f()"]})

    def test_falls_back_to_the_first_thirty_lines_without_signatures(self) -> None:
        """Проверить откат к первым 30 строкам файла при отсутствии сигнатур Repo Map."""
        text = "\n".join(f"line {index}" for index in range(35))
        context = _dependency_context(
            import_graph={"seed.py": {"dep.txt"}},
            seeds=["seed.py"],
            files={"dep.txt": text},
            signatures_by_path={},
        )
        self.assertEqual(context, {"dep.txt": [f"line {index}" for index in range(30)]})

    def test_fallback_excerpt_is_bounded_at_thirty_lines(self) -> None:
        """Проверить, что резервная выдержка контекста ограничена максимум 30 строками."""
        text = "\n".join(str(index) for index in range(40))
        self.assertEqual(_fallback_excerpt(text), [str(index) for index in range(30)])


class GuardHotPathTests(unittest.TestCase):
    """Набор тестов для проверки отсутствия сетевых вызовов, venv и uv run на критическом пути."""

    def test_repo_map_is_invoked_with_sys_executable_and_no_uv(self) -> None:
        """Проверить, что Repo Map вызывается через sys.executable без фреймворка uv."""
        calls: list[list[str]] = []
        real_run = subprocess.run

        def _spy(
            args: list[str],
            *,
            capture_output: bool = True,
            text: bool = True,
            encoding: str = "utf-8",
            errors: str = "replace",
            check: bool = False,
        ) -> subprocess.CompletedProcess[str]:
            """Шпионская функция для перехвата и логирования вызовов subprocess.run."""
            if any(str(item).endswith("repo_map.py") for item in args):
                calls.append([str(item) for item in args])
            return real_run(
                args,
                capture_output=capture_output,
                text=text,
                encoding=encoding,
                errors=errors,
                check=check,
            )

        fixture = ContextBuilderFixture()
        fixture.setUp()
        try:
            with patch(
                "harness.context_builder.context_builder.subprocess.run",
                side_effect=_spy,
            ):
                build_context_package(
                    fixture.repo,
                    fixture.base_commit,
                    fixture.candidate_commit,
                    min_starting_files=1,
                )
        finally:
            fixture.tearDown()

        self.assertEqual(len(calls), 1)
        invoked = calls[0]
        self.assertEqual(invoked[0], sys.executable)
        # -B: no .pyc bytecode caches written into the target repository.
        self.assertIn("-B", invoked)
        self.assertNotIn("uv", invoked)
        for token in invoked:
            self.assertNotIn("uv run", token)

    def test_a_real_invocation_writes_no_bytecode_cache_under_the_harness_package(
        self,
    ) -> None:
        """Проверить, что реальный запуск не создает кэш байткода __pycache__ в пакете harness."""
        harness_root = MODULE_ROOT.parent  # .../harness
        before = {path for path in harness_root.rglob("__pycache__")}
        fixture = ContextBuilderFixture()
        fixture.setUp()
        try:
            build_context_package(
                fixture.repo,
                fixture.base_commit,
                fixture.candidate_commit,
                min_starting_files=1,
            )
        finally:
            fixture.tearDown()
        after = {path for path in harness_root.rglob("__pycache__")}
        self.assertEqual(after - before, set())

    def test_hot_path_source_has_no_network_venv_requirements_or_uv_run(self) -> None:
        """Проверить отсутствие упоминаний сети, venv, requirements.txt и uv run в исходном коде сборщика."""
        module_source = (MODULE_ROOT / "context_builder.py").read_text(encoding="utf-8")
        for banned in (
            "uv run",
            ".venv",
            "requirements.txt",
            "urlopen",
            "requests.get",
            "socket.",
        ):
            self.assertNotIn(banned, module_source)


# --- Repo Map full (bundle) tier: graph widening, symbol-graph depth, and signature-based
# dependency context need real edges, which only exist with a verified tree-sitter grammar bundle
# (ADR 0008). Follows the skip/fail pattern of tests/test_repo_map_tree_sitter.py.


@pytest.fixture
def bundle_dir() -> Path:
    """Фикстура для получения пути к каталогу с бандлом парсеров из окружения."""
    configured = os.environ.get(BUNDLE_ENV)
    if not configured:
        pytest.skip(f"{BUNDLE_ENV} is not set; the bundle CI job runs these tests")
    path = Path(configured)
    if not (path / "parser_bundle.lock.json").is_file():
        pytest.fail(f"{BUNDLE_ENV}={configured} has no parser_bundle.lock.json")
    return path


def _bundle_repo(tmp_path: Path, bundle: Path) -> tuple[Path, str, str]:
    """Создать тестовый git-репозиторий с конфигурацией полного уровня (full tier) Repo Map."""
    repo = tmp_path / "project"
    repo.mkdir()
    _run("init", "-q", cwd=repo)
    _run("config", "user.email", "test@example.invalid", cwd=repo)
    _run("config", "user.name", "Test", cwd=repo)

    (repo / ".harness").mkdir()
    (repo / ".harness" / "orchestration.json").write_text(
        json.dumps(
            {
                "repo_map_policy": {
                    "tier": "full",
                    "parser_bundle_registry_paths": [str(bundle)],
                    "parser_bundle_timeout_seconds": 120,
                }
            }
        ),
        encoding="utf-8",
    )

    _write(repo, "pkg/__init__.py", "")
    _write(repo, "pkg/base_util.py", "def util() -> int:\n    return 1\n")
    _write(
        repo,
        "pkg/helper.py",
        "from pkg import base_util\n\n\ndef render(value: int) -> str:\n    return str(base_util.util())\n",
    )
    _write(
        repo,
        "pkg/consumer.py",
        "from pkg import helper\n\n\ndef use() -> str:\n    return helper.render(1)\n",
    )
    _write(
        repo,
        "pkg/caller.py",
        "from pkg import consumer\n\n\ndef trigger() -> str:\n    return consumer.use()\n",
    )
    _write(
        repo,
        "tests/test_consumer.py",
        "from pkg import consumer\n\n\ndef test_use() -> None:\n    assert consumer.use() == '1'\n",
    )
    _write(
        repo,
        "docs/adr/0001-consumer.md",
        "# Consumer contract\n\nDescribes the consumer module used across pkg.\n",
    )
    _run("add", ".", cwd=repo)
    _run("commit", "-qm", "feat: base package", cwd=repo)
    base_commit = _head(repo)

    _write(
        repo,
        "pkg/consumer.py",
        "from pkg import helper\n\n\ndef use() -> str:\n    return helper.render(2)\n",
    )
    _run("add", ".", cwd=repo)
    _run("commit", "-qm", "fix: change consumer value", cwd=repo)
    candidate_commit = _head(repo)
    return repo, base_commit, candidate_commit


def test_full_tier_symbol_graph_depth_bounds_indirect_dependents(
    tmp_path: Path, bundle_dir: Path
) -> None:
    """Проверить ограничение глубины графа символов для косвенных зависимостей на уровне full."""
    repo, base, candidate = _bundle_repo(tmp_path, bundle_dir)

    shallow = build_context_package(
        repo, base, candidate, min_starting_files=1, symbol_graph_depth=1
    )
    deep = build_context_package(
        repo, base, candidate, min_starting_files=1, symbol_graph_depth=2
    )

    assert "pkg/base_util.py" not in shallow.symbol_graph
    assert "pkg/base_util.py" in deep.symbol_graph


def test_full_tier_expands_below_minimum_starting_files_via_the_import_graph(
    tmp_path: Path, bundle_dir: Path
) -> None:
    """Проверить расширение списка начальных файлов через граф импортов при нехватке до минимума."""
    repo, base, candidate = _bundle_repo(tmp_path, bundle_dir)

    package = build_context_package(
        repo, base, candidate, min_starting_files=2, max_starting_files=10
    )

    paths = {item.path for item in package.starting_files}
    assert len(paths) >= 2
    assert "pkg/consumer.py" in paths


def test_full_tier_uses_repo_map_signatures_for_a_direct_dependency(
    tmp_path: Path, bundle_dir: Path
) -> None:
    """Проверить извлечение сигнатур Repo Map для прямой зависимости на уровне full."""
    repo, base, candidate = _bundle_repo(tmp_path, bundle_dir)

    package = build_context_package(repo, base, candidate, min_starting_files=1)

    assert package.symbol_graph["pkg/helper.py"]["context"] == [
        "def render(value: int) -> str"
    ]
    assert package.related_tests == ["tests/test_consumer.py"]
    assert package.parser == "bundle"
    assert package.parser_provenance is not None
    assert package.parser_provenance["tier"] == "full"


def test_full_tier_fails_clearly_instead_of_silently_including_too_many_related_tests(
    tmp_path: Path, bundle_dir: Path
) -> None:
    """Проверить ошибку при превышении максимально допустимого количества связанных тестов."""
    repo, base, candidate = _bundle_repo(tmp_path, bundle_dir)

    with pytest.raises(ContextPackageError, match="max_related_tests"):
        build_context_package(
            repo, base, candidate, min_starting_files=1, max_related_tests=0
        )


if __name__ == "__main__":
    unittest.main()
