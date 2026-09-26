"""Источник фактов о файлах для полного режима Repo Map.

`repo_map` зависит от протокола `ParserBackend`, а не от конкретного parser bundle: backend сообщает
свою идентичность для ключа кэша и разбирает файлы коммита. `BundleParserBackend` — реализация на
проверенном offline tree-sitter bundle; тесты и другие окружения могут передать свою.
"""

from __future__ import annotations

import base64
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from harness.repo_map import parser_bundle
from harness.repo_map.bundle_worker import FileFacts
from harness.repo_map.git_source import Blob, read_blobs
from harness.repo_map.graph import Diagnostic
from harness.repo_map.policy import RepoMapPolicy

# `degradation_reason` of a `full` map: the contract field is required even when nothing degraded.
BUNDLE_APPLIED_REASON = "parser bundle applied"


@dataclass(frozen=True)
class ParseOutcome:
    """Итог разбора в режиме `full`: записи файлов и факты парсера либо причина деградации."""

    applied: bool
    reason: str
    provenance: dict[str, object]
    records: dict[str, dict[str, object]]
    facts: dict[str, FileFacts]
    diagnostics: list[Diagnostic]
    extension_grammars: dict[str, str]

    @classmethod
    def degraded(cls, reason: str, provenance: dict[str, object]) -> ParseOutcome:
        """Создать итог деградации с причиной и provenance, без фактов."""
        return cls(False, reason, provenance, {}, {}, [], {})


class ParserBackend(Protocol):
    """Парсер полного режима: идентичность для ключа кэша и разбор файлов коммита."""

    def identity(self) -> str:
        """Идентичность парсера: меняется вместе с любым байтом, влияющим на результат разбора."""
        ...

    def parse(self, commit: str, paths: list[str]) -> ParseOutcome:
        """Разобрать разрешённые пути коммита или вернуть итог деградации."""
        ...


def _record(path: str, status: str = "ok") -> dict[str, object]:
    """Запись файла полного режима без сигнатур."""
    return {"path": path, "signatures": [], "parser_status": status}


class BundleParserBackend:
    """`ParserBackend` на проверенном offline parser bundle.

    Bundle ищется один раз на экземпляр: тот же найденный bundle даёт и идентичность для ключа кэша,
    и установку для разбора. Сеть не используется: только локальные файлы, offline-установка через uv
    и ограниченный subprocess worker.
    """

    def __init__(
        self, repo: Path, policy: RepoMapPolicy, python_executable: str = sys.executable
    ) -> None:
        """Запомнить репозиторий, политику и интерпретатор, который запустит worker."""
        self._repo = repo
        self._policy = policy
        self._python = python_executable
        self._located: parser_bundle.LocatedBundle | parser_bundle.DegradationReason | None = None

    def _locate(self) -> parser_bundle.LocatedBundle | parser_bundle.DegradationReason:
        """Найти bundle один раз и запомнить результат."""
        if self._located is None:
            self._located = parser_bundle.locate_bundle(
                repo=self._repo,
                registry_paths=self._policy.parser_bundle_registry_paths,
                python_executable=self._python,
                timeout_seconds=self._policy.parser_bundle_timeout_seconds,
            )
        return self._located

    def identity(self) -> str:
        """Идентичность найденного bundle или причина, по которой его нет."""
        located = self._locate()
        if isinstance(located, str):
            return f"unavailable: {located}"
        return located.identity()

    def _records(
        self, paths: list[str], blobs: dict[str, Blob], extensions: dict[str, str]
    ) -> tuple[dict[str, dict[str, object]], list[Diagnostic], dict[str, str]]:
        """Записи файлов, диагностика слишком больших файлов и запрос worker в base64.

        Бинарные файлы, подмодули и пути, которые нельзя прочитать пакетно, остаются в карте без
        сигнатур, как и файлы языков без грамматики, поэтому набор путей совпадает с уровнем `minimal`.
        """
        records: dict[str, dict[str, object]] = {}
        diagnostics: list[Diagnostic] = []
        request: dict[str, str] = {}
        for path in paths:
            blob = blobs[path]
            if blob.size is not None and blob.size > self._policy.max_file_bytes:
                records[path] = _record(path, "too_large")
                diagnostics.append({"code": "file_too_large", "path": path})
                continue
            records[path] = _record(path)
            content = blob.content
            if content is None or b"\0" in content or Path(path).suffix not in extensions:
                continue
            request[path] = base64.b64encode(content).decode("ascii")
        return records, diagnostics, request

    def parse(self, commit: str, paths: list[str]) -> ParseOutcome:
        """Разобрать поддерживаемые файлы проверенным bundle или деградировать.

        Все языки, включая Python, проходят через tree-sitter worker: встроенного запасного парсера
        нет, поэтому любой сбой даёт причину для уровня `minimal`. Bundle проверяется до чтения
        содержимого файлов, поэтому деградировавший запуск читает только пути.
        """
        policy = self._policy
        bundle = parser_bundle.acquire_bundle(
            repo=self._repo,
            registry_paths=policy.parser_bundle_registry_paths,
            python_executable=self._python,
            timeout_seconds=policy.parser_bundle_timeout_seconds,
            located=self._locate(),
        )
        if isinstance(bundle, str):
            provenance = parser_bundle.build_provenance(
                None, bundle_mode="degraded", bundle_source="none", python_tag=None, platform_tag=None
            )
            return ParseOutcome.degraded(bundle, provenance)
        blobs = read_blobs(
            self._repo,
            commit,
            paths,
            max_file_bytes=policy.max_file_bytes,
            timeout_seconds=policy.timeout_seconds,
        )
        records, diagnostics, request = self._records(paths, blobs, bundle.worker_extensions)
        parse_result = parser_bundle.run_bundle_parser(
            self._python,
            bundle.worker_script,
            bundle.install_dir,
            {"paths": request, "languages": bundle.worker_extensions},
            timeout_seconds=policy.parser_bundle_timeout_seconds,
            max_output_bytes=policy.parser_bundle_max_output_bytes,
            expected_script_sha256=bundle.lock.script_sha256,
            error_log=bundle.error_log,
        )
        provenance = parser_bundle.build_provenance(
            bundle.lock,
            bundle_mode="degraded" if isinstance(parse_result, str) else "applied",
            bundle_source=bundle.bundle_source,
            python_tag=bundle.python_tag,
            platform_tag=bundle.platform_tag,
        )
        if isinstance(parse_result, str):
            return ParseOutcome.degraded(parse_result, provenance)
        facts = {
            path: record for path, record in parse_result["files"].items() if path in request
        }
        return ParseOutcome(
            True,
            BUNDLE_APPLIED_REASON,
            provenance,
            records,
            facts,
            diagnostics,
            bundle.worker_extensions,
        )
