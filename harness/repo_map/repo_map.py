#!/usr/bin/env python3
"""Детерминированная offline-карта репозитория (Repo Map) для закреплённого коммита Git."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import posixpath
import subprocess
import sys
import sysconfig
import tempfile
from collections import deque
from dataclasses import asdict, dataclass
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path
from typing import Literal, TypedDict, cast

# `harness/bin/harness` copies this file verbatim into target projects as
# `.harness/repo_map/repo_map.py`. Alias `harness` to whichever of the two this
# file actually lives under so this standalone CLI has the same imports in both
# source and installed layouts. See docs/adr/0018.
_HARNESS_ROOT: Path = Path(__file__).resolve().parents[1]
_REPO_ROOT: Path = _HARNESS_ROOT.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if _HARNESS_ROOT.name != "harness":
    _spec = importlib.util.spec_from_file_location(
        "harness",
        _HARNESS_ROOT / "__init__.py",
        submodule_search_locations=[str(_HARNESS_ROOT)],
    )
    assert _spec is not None and _spec.loader is not None
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules["harness"] = _pkg
    _spec.loader.exec_module(_pkg)

from harness.errors import HarnessError, PolicyError, print_and_exit
from harness.repo_map import parser_bundle
from harness.repo_map.contract import validation_error as repo_map_validation_error
from harness.storage import storage_path
from harness.token_estimator import TOKEN_ESTIMATOR_VERSION, estimate_tokens

DEFAULT_MAX_TOKENS = 4000
DEFAULT_MAX_FILES = 10_000
DEFAULT_MAX_FILE_BYTES = 2_000_000
DEFAULT_MAX_PATH_LENGTH = 4_096
DEFAULT_MAX_SYMBOL_LENGTH = 256
DEFAULT_MAX_SIGNATURE_LENGTH = 2_048
DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_PARSER_BUNDLE_TIMEOUT_SECONDS = 30
DEFAULT_PARSER_BUNDLE_MAX_OUTPUT_BYTES = 10_000_000
# Names defined in this many files are too common to provide useful references.
DEFINITION_FILE_FANOUT_THRESHOLD = 5
# Ranking distance of a file that no seed reaches through the graph: after every reachable file.
UNREACHABLE_DISTANCE = sys.maxsize
# `degradation_reason` of a `full` map: the contract field is required even when nothing degraded.
BUNDLE_APPLIED_REASON = "parser bundle applied"
MINIMAL_POLICY_REASON = "policy requested minimal tier"
JS_EXTENSIONS = frozenset({".ts", ".tsx", ".js", ".jsx"})
# Name-ref edges never cross a family: two languages that happen to share an identifier (e.g. Go
# and TS both defining `Parse`) must not collide. Import edges stay Python/JS-only below regardless
# of family membership (ADR 0024, #279).
FAMILY_EXTENSIONS: dict[str, frozenset[str]] = {
    "python": frozenset({".py"}),
    "js": JS_EXTENSIONS,
    "go": frozenset({".go"}),
    "java": frozenset({".java"}),
    "csharp": frozenset({".cs"}),
}
_EXTENSION_FAMILY: dict[str, str] = {
    extension: family
    for family, extensions in FAMILY_EXTENSIONS.items()
    for extension in extensions
}
EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "deps",
        "dependencies",
        "third_party",
        "vendor",
        "dist",
        "build",
        "__pycache__",
        ".cache",
        ".mypy_cache",
        ".pytest_cache",
        "coverage",
        "generated",
        "tmp",
        "temp",
        "logs",
        "media",
        "images",
        "assets",
        "target",
        ".next",
        ".terraform",
        "site-packages",
    }
)
EXCLUDED_NAMES = frozenset({".env", ".env.local", "id_rsa", "id_ed25519"})
SENSITIVE_PATH_PARTS = frozenset({"secret", "credential", "private_key"})
EXCLUDED_SUFFIXES = frozenset(
    {
        ".pyc",
        ".pyo",
        ".min.js",
        ".map",
        ".log",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".mp4",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".lock",
    }
)


class FileRecord(TypedDict):
    """Запись файла в выходной карте: путь, видимые сигнатуры и статус разбора."""
    path: str
    signatures: list[str]
    parser_status: Literal["ok", "syntax_error", "invalid_encoding", "too_large"]


class EdgeRecord(TypedDict):
    """Ребро графа: источник, цель, вид связи и уверенность."""
    source: str
    target: str
    kind: str
    confidence: str


class Diagnostic(TypedDict):
    """Диагностика разбора файла для поля `diagnostics`."""
    code: Literal["syntax_error", "invalid_encoding", "file_too_large"]
    path: str


@dataclass(frozen=True)
class RepoMapPolicy:
    """Проверенная политика Repo Map: фильтры путей и символов, лимиты, уровень и настройки bundle.

    `sha256` — хеш исходного файла политики; `None` означает переносимые значения по умолчанию.
    """
    allow_paths: tuple[str, ...] = ()
    deny_paths: tuple[str, ...] = ()
    redact_paths: tuple[str, ...] = ()
    redact_symbols: tuple[str, ...] = ()
    max_files: int = DEFAULT_MAX_FILES
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_path_length: int = DEFAULT_MAX_PATH_LENGTH
    max_symbol_length: int = DEFAULT_MAX_SYMBOL_LENGTH
    max_signature_length: int = DEFAULT_MAX_SIGNATURE_LENGTH
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    max_tokens: int | None = None
    tier: Literal["minimal", "full"] = "full"
    parser_bundle_registry_paths: tuple[str, ...] = ()
    parser_bundle_timeout_seconds: int = DEFAULT_PARSER_BUNDLE_TIMEOUT_SECONDS
    parser_bundle_max_output_bytes: int = DEFAULT_PARSER_BUNDLE_MAX_OUTPUT_BYTES
    sha256: str | None = None

    @property
    def enforced(self) -> bool:
        """Политика загружена из файла проекта, а не взята по умолчанию."""
        return self.sha256 is not None


class RepoMapGitError(HarnessError):
    """Git-команда Repo Map завершилась ошибкой или превысила таймаут."""


def _git(repo: Path, timeout_seconds: int, *args: str) -> bytes:
    """Выполнить Git-команду в `repo` с таймаутом и вернуть stdout.

    Ошибка или превышение таймаута поднимают `RepoMapGitError` с рекомендацией.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RepoMapGitError(
            f"git {' '.join(args)} timed out after {timeout_seconds} seconds",
            remedy="raise repo_map_policy.timeout_seconds or retry on a less loaded machine",
        ) from exc
    if result.returncode:
        raise RepoMapGitError(
            result.stderr.decode("utf-8", "replace").strip() or f"git {args[0]} failed",
            remedy="check that --repo is a Git repository and --commit names an existing commit",
        )
    return result.stdout


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    """Проверить, совпадает ли путь или имя хотя бы с одним glob-шаблоном (с учётом регистра)."""
    return any(fnmatchcase(path, pattern) for pattern in patterns)


def _allowed(path: str, policy: RepoMapPolicy) -> bool:
    """Решить, попадает ли tracked-путь в карту.

    Отсекает служебные каталоги, секреты, сгенерированные и бинарные файлы, слишком длинные пути, а затем
    применяет `allow_paths`, `deny_paths` и `redact_paths` политики.
    """
    parts = Path(path).parts
    name = parts[-1]
    if any(part in EXCLUDED_DIRS or part.startswith(".env") for part in parts):
        return False
    if name in EXCLUDED_NAMES or name.endswith(("~", ".env", ".pem", ".key")):
        return False
    if any(
        marker in part.casefold() for part in parts for marker in SENSITIVE_PATH_PARTS
    ):
        return False
    if ".generated." in name.casefold():
        return False
    if len(path) > policy.max_path_length:
        return False
    if policy.allow_paths and not _matches(path, policy.allow_paths):
        return False
    if _matches(path, policy.deny_paths) or _matches(path, policy.redact_paths):
        return False
    return not any(name.endswith(suffix) for suffix in EXCLUDED_SUFFIXES)


def _non_empty_strings(value: object, field: str, kind: str) -> list[str]:
    """Проверить, что поле политики — список непустых строк, иначе поднять `PolicyError`."""
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise _policy_error(
            f"repo_map_policy.{field} must be a list of non-empty {kind}",
            f"set repo_map_policy.{field} to a list of non-empty {kind}",
        )
    return cast(list[str], value)


def _string_patterns(value: object, field: str) -> tuple[str, ...]:
    """Вернуть отсортированные glob-шаблоны поля политики без повторов."""
    return tuple(sorted(set(_non_empty_strings(value, field, "path globs"))))


def _positive_int(value: object, field: str) -> int:
    """Проверить, что поле политики — положительное целое число (bool не допускается)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PolicyError(
            f"repo_map_policy.{field} must be a positive integer",
            remedy=f"set repo_map_policy.{field} to a positive integer in the project orchestration config",
        )
    return value


def _policy_error(message: str, remedy: str) -> PolicyError:
    """Собрать `PolicyError` с сообщением и рекомендацией."""
    return PolicyError(message, remedy=remedy)


def load_policy(path: Path | None, *, explicit: bool) -> RepoMapPolicy:
    """Загрузить и проверить `repo_map_policy` из файла оркестрации.

    Без файла или без секции возвращает переносимые значения по умолчанию; если путь передан явно
    (`explicit`), их отсутствие — ошибка. Неизвестные поля и неверные типы поднимают `PolicyError`
    с рекомендацией.
    """
    if path is None or not path.is_file():
        if explicit:
            raise _policy_error(
                f"policy file does not exist: {path}",
                "create the policy file or omit --policy to use portable defaults",
            )
        return RepoMapPolicy()
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _policy_error(
            f"policy file cannot be read: {path}",
            "fix the policy file permissions or path, then retry",
        ) from exc
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _policy_error(
            f"invalid policy JSON: {exc.msg}",
            "fix the JSON syntax in the policy file and retry",
        ) from exc
    if not isinstance(decoded, dict):
        raise _policy_error(
            "policy JSON must be an object",
            "rewrite the policy file as a JSON object containing repo_map_policy",
        )
    section = decoded.get("repo_map_policy")
    if section is None:
        if explicit:
            raise _policy_error(
                "policy JSON must define repo_map_policy",
                "add a repo_map_policy object or omit --policy to use portable defaults",
            )
        return RepoMapPolicy()
    if not isinstance(section, dict):
        raise _policy_error(
            "repo_map_policy must be an object",
            "set repo_map_policy to an object that follows orchestration.schema.json",
        )
    allowed = {
        "allow_paths",
        "deny_paths",
        "redact_paths",
        "redact_symbols",
        "max_files",
        "max_file_bytes",
        "max_path_length",
        "max_symbol_length",
        "max_signature_length",
        "timeout_seconds",
        "max_tokens",
        "tier",
        "parser_bundle_registry_paths",
        "parser_bundle_timeout_seconds",
        "parser_bundle_max_output_bytes",
        # Dispatch-admission fields, not generation fields: the coordinator (not this module)
        # resolves and enforces them via harness.orchestration.contract.resolve_min_repo_map_tier.
        # Recognised here only so the same repo_map_policy object does not fail this module's
        # generation-time validation.
        "min_tier",
        "min_tier_by_role",
    }
    unknown = sorted(str(key) for key in section.keys() - allowed)
    if unknown:
        raise _policy_error(
            f"repo_map_policy has unknown fields: {', '.join(unknown)}",
            "remove unknown repo_map_policy fields and follow orchestration.schema.json",
        )
    patterns: dict[str, tuple[str, ...]] = {}
    for field in ("allow_paths", "deny_paths", "redact_paths", "redact_symbols"):
        value = section.get(field, [])
        patterns[field] = _string_patterns(value, field)
    registry_paths = tuple(
        _non_empty_strings(
            section.get("parser_bundle_registry_paths", []),
            "parser_bundle_registry_paths",
            "strings",
        )
    )
    numeric: dict[str, int] = {}
    for field, default in (
        ("max_files", DEFAULT_MAX_FILES),
        ("max_file_bytes", DEFAULT_MAX_FILE_BYTES),
        ("max_path_length", DEFAULT_MAX_PATH_LENGTH),
        ("max_symbol_length", DEFAULT_MAX_SYMBOL_LENGTH),
        ("max_signature_length", DEFAULT_MAX_SIGNATURE_LENGTH),
        ("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
        ("parser_bundle_timeout_seconds", DEFAULT_PARSER_BUNDLE_TIMEOUT_SECONDS),
        ("parser_bundle_max_output_bytes", DEFAULT_PARSER_BUNDLE_MAX_OUTPUT_BYTES),
    ):
        value = section.get(field, default)
        numeric[field] = _positive_int(value, field)
    max_tokens_value = section.get("max_tokens")
    max_tokens = (
        _positive_int(max_tokens_value, "max_tokens")
        if max_tokens_value is not None
        else None
    )
    tier = section.get("tier", "full")
    if not isinstance(tier, str) or tier not in {"minimal", "full"}:
        raise _policy_error(
            "repo_map_policy.tier must be one of: full, minimal",
            "set repo_map_policy.tier to 'full' or 'minimal'",
        )
    tier = cast(Literal["minimal", "full"], tier)
    return RepoMapPolicy(
        allow_paths=patterns["allow_paths"],
        deny_paths=patterns["deny_paths"],
        redact_paths=patterns["redact_paths"],
        redact_symbols=patterns["redact_symbols"],
        parser_bundle_registry_paths=registry_paths,
        parser_bundle_timeout_seconds=numeric["parser_bundle_timeout_seconds"],
        parser_bundle_max_output_bytes=numeric["parser_bundle_max_output_bytes"],
        max_files=numeric["max_files"],
        max_file_bytes=numeric["max_file_bytes"],
        max_path_length=numeric["max_path_length"],
        max_symbol_length=numeric["max_symbol_length"],
        max_signature_length=numeric["max_signature_length"],
        timeout_seconds=numeric["timeout_seconds"],
        max_tokens=max_tokens,
        tier=tier,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _symbol_visible(name: str, policy: RepoMapPolicy) -> bool:
    """Проверить, что символ укладывается в `max_symbol_length` и не попадает под `redact_symbols`."""
    return len(name) <= policy.max_symbol_length and not _matches(
        name, policy.redact_symbols
    )


def _visible_signatures(
    signatures: list[parser_bundle.SignatureFact], policy: RepoMapPolicy
) -> list[str]:
    """Оставить сигнатуры, чей текст укладывается в лимит, а каждый сериализуемый символ проходит политику."""
    return [
        signature["text"]
        for signature in signatures
        if len(signature["text"]) <= policy.max_signature_length
        and all(_symbol_visible(symbol, policy) for symbol in signature["symbols"])
    ]


def _python_module(path: str) -> str:
    """Преобразовать путь `.py` в dotted-имя модуля."""
    return path.removesuffix(".py").replace("/", ".")


def _module_paths(paths: set[str]) -> dict[str, str]:
    """Построить соответствие dotted-имени модуля его файлу; `pkg/__init__.py` становится `pkg`."""
    modules: dict[str, str] = {}
    for path in paths:
        module = path.removesuffix(".py").removesuffix("/__init__").replace("/", ".")
        if module:
            modules[module] = path
    return modules


def _import_modules(fact: parser_bundle.ImportFact, module: str) -> list[str]:
    """Вернуть кандидатов dotted-модулей для одного импорта: базовый модуль и каждое имя из `from ... import`.

    Относительный импорт разрешается от модуля `module` на `level` уровней вверх.
    """
    level = fact["level"]
    parent = module.rsplit(".", level)[0] if level else ""
    base = ".".join(part for part in (parent, fact["module"]) if part)
    if not base:
        return []
    return [base, *(f"{base}.{name}" for name in fact["names"] if name != "*")]


def _name_and_parts_visible(name: str, parts: list[str], policy: RepoMapPolicy) -> bool:
    """Проверить, что имя целиком и каждая его часть проходят политику символов."""
    return _symbol_visible(name, policy) and all(
        _symbol_visible(part, policy) for part in parts
    )


def _module_visible(module: str, policy: RepoMapPolicy) -> bool:
    """Применить политику символов к dotted-модулю и каждому его сегменту до создания ребра импорта."""
    return _name_and_parts_visible(
        module, [part for part in module.split(".") if part], policy
    )


def _specifier_visible(specifier: str, policy: RepoMapPolicy) -> bool:
    """Применить политику символов к спецификатору импорта JS/TS и каждому сегменту его пути."""
    parts = [
        part
        for part in posixpath.splitext(specifier)[0].split("/")
        if part not in {".", ".."}
    ]
    return _name_and_parts_visible(specifier, parts, policy)


def _js_import_target(source: str, specifier: str, paths: set[str]) -> str | None:
    """Разрешить относительный импорт TS/JS в tracked-файл.

    Пробует точный путь, расширения `.ts`, `.tsx`, `.js`, `.jsx` и `index`-файлы. Пакеты, aliases и выход
    за корень репозитория не разрешаются.
    """
    if not specifier.startswith(("./", "../")):
        return None
    base = posixpath.normpath(posixpath.join(posixpath.dirname(source), specifier))
    if base == ".." or base.startswith(("../", "/")):
        return None
    candidates = [base]
    if Path(base).suffix not in JS_EXTENSIONS:
        candidates.extend(f"{base}{extension}" for extension in (".ts", ".tsx", ".js", ".jsx"))
        candidates.extend(f"{base}/index{extension}" for extension in (".ts", ".tsx", ".js", ".jsx"))
    return next((candidate for candidate in candidates if candidate in paths), None)


def _encode(payload: dict[str, object]) -> str:
    """Сериализовать карту в канонический JSON: сортировка ключей, без пробелов, перевод строки в конце."""
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )


def _sized(payload: dict[str, object]) -> tuple[str, int]:
    # The count includes the count field itself. Iterate until its digit width settles.
    """Сериализовать карту и вычислить `estimated_tokens`, учитывающий само это поле.

    Оценка повторяется, пока ширина числа не стабилизируется; если за 8 итераций сходимости нет,
    поднимается `ValueError`.
    """
    size = 0
    for _ in range(8):
        payload["estimated_tokens"] = size
        encoded = _encode(payload)
        next_size = estimate_tokens(encoded)
        if next_size == size:
            return encoded, size
        size = next_size
    raise ValueError("token estimate did not converge")


@dataclass(frozen=True)
class _BundleParse:
    """Итог разбора в режиме `full`: записи файлов и факты парсера либо причина деградации."""

    applied: bool
    reason: str
    provenance: dict[str, object]
    records: dict[str, dict[str, object]]
    facts: dict[str, parser_bundle.FileFacts]
    diagnostics: list[Diagnostic]

    @classmethod
    def degraded(cls, reason: str, provenance: dict[str, object]) -> _BundleParse:
        """Создать итог деградации с причиной и provenance, без фактов."""
        return cls(False, reason, provenance, {}, {}, [])


def _parse_with_bundle(
    repo: Path, commit: str, paths: list[str], policy: RepoMapPolicy
) -> _BundleParse:
    """Разобрать поддерживаемые файлы проверенным offline parser bundle или деградировать.

    Все языки, включая Python, проходят через tree-sitter worker из bundle: встроенного запасного
    парсера нет, поэтому любой сбой даёт причину для уровня `minimal`. Сеть не используется: каждый шаг —
    чтение локальных файлов, offline-установка `uv pip install --offline --no-index --target` или
    ограниченный локальный subprocess worker (см. harness/repo_map/parser_bundle.py). Bundle ищется до
    чтения содержимого файлов, поэтому деградировавший запуск читает только пути.
    """
    bundle = parser_bundle.acquire_bundle(
        repo=repo,
        registry_paths=policy.parser_bundle_registry_paths,
        python_executable=sys.executable,
        timeout_seconds=policy.parser_bundle_timeout_seconds,
    )
    if isinstance(bundle, str):
        provenance = parser_bundle.build_provenance(
            None,
            bundle_mode="degraded",
            bundle_source="none",
            python_tag=None,
            platform_tag=None,
        )
        return _BundleParse.degraded(bundle, provenance)
    records: dict[str, dict[str, object]] = {}
    diagnostics: list[Diagnostic] = []
    request_paths: dict[str, str] = {}
    for path in paths:
        # Git object reads keep the working tree, including untracked files, outside the input.
        object_name = f"{commit}:{path}"
        size = int(
            _git(repo, policy.timeout_seconds, "cat-file", "-s", object_name)
            .decode()
            .strip()
        )
        if size > policy.max_file_bytes:
            records[path] = {
                "path": path,
                "signatures": [],
                "parser_status": "too_large",
            }
            diagnostics.append({"code": "file_too_large", "path": path})
            continue
        content = _git(repo, policy.timeout_seconds, "show", object_name)
        if b"\0" in content:
            continue
        records[path] = {"path": path, "signatures": [], "parser_status": "ok"}
        if Path(path).suffix in bundle.worker_extensions:
            request_paths[path] = base64.b64encode(content).decode("ascii")
    parse_result = parser_bundle.run_bundle_parser(
        sys.executable,
        bundle.worker_script,
        bundle.install_dir,
        {"paths": request_paths},
        timeout_seconds=policy.parser_bundle_timeout_seconds,
        max_output_bytes=policy.parser_bundle_max_output_bytes,
        expected_script_sha256=bundle.lock.script_sha256,
    )
    applied = not isinstance(parse_result, str)
    provenance = parser_bundle.build_provenance(
        bundle.lock,
        bundle_mode="applied" if applied else "degraded",
        bundle_source=bundle.bundle_source,
        python_tag=bundle.python_tag,
        platform_tag=bundle.platform_tag,
    )
    if isinstance(parse_result, str):
        return _BundleParse.degraded(parse_result, provenance)
    facts = {
        path: record
        for path, record in parse_result["files"].items()
        if path in request_paths
    }
    return _BundleParse(
        True, BUNDLE_APPLIED_REASON, provenance, records, facts, diagnostics
    )


def _family(path: str) -> str:
    """Вернуть языковое семейство расширения `path` для ссылок по имени.

    Расширение вне пяти известных семейств образует собственное семейство по суффиксу: имя семейства
    никогда не начинается с точки, поэтому такой файл не смешивается с `python` или `js`.
    """
    suffix = Path(path).suffix
    return _EXTENSION_FAMILY.get(suffix, suffix)


def _graph_from_facts(
    records: dict[str, dict[str, object]],
    facts: dict[str, parser_bundle.FileFacts],
    diagnostics: list[Diagnostic],
    policy: RepoMapPolicy,
) -> list[EdgeRecord]:
    """Применить политику символов к фактам worker, дополнить записи файлов и построить список рёбер.

    Импорты дают `import/high` (Python и относительные TS/JS), ссылки по имени —
    `unique-name-ref/medium` или `ambiguous-name-ref/low` в пределах одного семейства. Имена,
    определённые в `DEFINITION_FILE_FANOUT_THRESHOLD` и более файлах, отбрасываются.
    """
    module_paths = _module_paths({path for path in records if path.endswith(".py")})
    edges: set[tuple[str, str, str, str]] = set()
    definitions: dict[tuple[str, str], set[str]] = {}
    js_paths = {path for path in records if Path(path).suffix in JS_EXTENSIONS}
    for path in sorted(facts):
        record = facts[path]
        status = record["parser_status"]
        records[path]["parser_status"] = status
        if status != "ok":
            diagnostics.append({"code": status, "path": path})
        records[path]["signatures"] = _visible_signatures(record["signatures"], policy)
        family = _family(path)
        for name in record["definitions"]:
            if _symbol_visible(name, policy):
                definitions.setdefault((family, name), set()).add(path)
        if path in js_paths:
            for import_fact in record["imports"]:
                specifier = import_fact["module"]
                if not _specifier_visible(specifier, policy):
                    continue
                target = _js_import_target(path, specifier, js_paths)
                if target is not None and target != path:
                    edges.add((path, target, "import", "high"))
            continue
        if not path.endswith(".py"):
            continue
        current_module = _python_module(path)
        for import_fact in record["imports"]:
            for imported_module in _import_modules(import_fact, current_module):
                if not _module_visible(imported_module, policy):
                    continue
                target = module_paths.get(imported_module)
                if (
                    target
                    and target != path
                    and _module_visible(_python_module(target), policy)
                ):
                    edges.add((path, target, "import", "high"))
    for path in sorted(facts):
        family = _family(path)
        for name in set(facts[path]["references"]):
            if not _symbol_visible(name, policy):
                continue
            definition_paths = definitions.get((family, name), set())
            if len(definition_paths) >= DEFINITION_FILE_FANOUT_THRESHOLD:
                continue
            targets = definition_paths - {path}
            kind, confidence = (
                ("unique-name-ref", "medium")
                if len(targets) == 1
                else ("ambiguous-name-ref", "low")
            )
            for target in targets:
                edges.add((path, target, kind, confidence))
    diagnostics.sort(key=lambda item: (item["path"], item["code"]))
    return [
        {"source": source, "target": target, "kind": kind, "confidence": confidence}
        for source, target, kind, confidence in sorted(edges)
    ]


@lru_cache(maxsize=1)
def _parser_identity() -> str:
    """SHA-256 исходников, от которых зависит сериализация карты; вычисляется один раз на процесс."""
    return hashlib.sha256(
        Path(__file__).read_bytes()
        + Path(parser_bundle.__file__).read_bytes()
        + Path(__file__).with_name("contract.py").read_bytes()
        + Path(__file__).with_name("repo_map.schema.json").read_bytes()
        + (_HARNESS_ROOT / "repo_map" / "tree_sitter_worker.py").read_bytes()
    ).hexdigest()


def _cache_key(
    repo: Path,
    pinned: str,
    seeds: list[str],
    max_tokens: int,
    policy: RepoMapPolicy,
) -> str:
    """Вычислить ключ кэша по всем входам, влияющим на сериализованную карту."""
    identity = {
        "commit": pinned,
        "seeds": seeds,
        "max_tokens": max_tokens,
        "policy": asdict(policy),
        "parser_identity": _parser_identity(),
        "bundle_identity": _bundle_cache_identity(repo, policy) if policy.tier == "full" else None,
        "token_estimator_version": TOKEN_ESTIMATOR_VERSION,
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _bundle_cache_identity(repo: Path, policy: RepoMapPolicy) -> str:
    """Привязать результат `full` к выбранному lock, worker и wheels текущего интерпретатора.

    Недоступный или повреждённый bundle тоже даёт детерминированную идентичность, поэтому установка
    или замена bundle меняет ключ.
    """
    bundle_dir = parser_bundle.find_bundle(
        parser_bundle.search_dirs(repo, policy.parser_bundle_registry_paths)
    )
    if bundle_dir is None:
        return "unavailable"
    digest = hashlib.sha256(str(bundle_dir.resolve()).encode("utf-8"))
    try:
        raw = (bundle_dir / parser_bundle.LOCK_FILENAME).read_bytes()
        digest.update(raw)
        lock = parser_bundle.parse_lock(raw)
        pair = (
            f"cp{sys.version_info.major}{sys.version_info.minor}-"
            f"{sysconfig.get_platform().replace('-', '_').replace('.', '_')}"
        )
        paths = [bundle_dir / lock.worker_script]
        paths.extend(
            bundle_dir / "wheelhouse" / pair / item.filename
            for item in lock.wheelhouses.get(pair, ())
        )
        for path in paths:
            digest.update(path.read_bytes())
    except (OSError, parser_bundle.BundleFormatError):
        digest.update(b"invalid-or-incomplete")
    return digest.hexdigest()


def _read_cache(cache_dir: Path, key: str, pinned: str) -> str | None:
    """Вернуть запись, соответствующую схеме, запрошенному ключу и коммиту."""
    try:
        envelope = json.loads((cache_dir / f"{key}.json").read_text(encoding="utf-8"))
        if not isinstance(envelope, dict):
            return None
        payload = envelope.get("payload")
        digest = envelope.get("sha256")
        if envelope.get("key") != key or not isinstance(payload, str) or not isinstance(digest, str):
            return None
        if hashlib.sha256(payload.encode("utf-8")).hexdigest() != digest:
            return None
        parsed: object = json.loads(payload)
        if not isinstance(parsed, dict) or parsed.get("commit") != pinned:
            return None
        if repo_map_validation_error(parsed) is not None:
            return None
        return payload
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _write_cache(cache_dir: Path, key: str, payload: str) -> None:
    """Атомарно записать результат в кэш по возможности; кэш никогда не становится авторитетным."""
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        envelope = json.dumps(
            {
                "key": key,
                "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                "payload": payload,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        descriptor, temporary = tempfile.mkstemp(
            dir=cache_dir, prefix=f".{key}.", suffix=".tmp"
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(envelope)
        Path(temporary).replace(cache_dir / f"{key}.json")
    except OSError:
        return


def _build_map(
    repo: Path,
    pinned: str,
    paths: list[str],
    max_tokens: int,
    seeds: list[str],
    effective_policy: RepoMapPolicy,
) -> str:
    """Построить карту для уже отобранных путей: разбор, граф, ранжирование и отбор файлов в бюджет.

    С seeds файлы упорядочиваются по расстоянию в графе через рёбра высокой и средней уверенности,
    без seeds — по входящей степени; затем по пути.
    """
    files: dict[str, dict[str, object]] = {path: {"path": path} for path in paths}
    edges: list[EdgeRecord] = []
    diagnostics: list[Diagnostic] = []
    tier: Literal["minimal", "full"] = "minimal"
    parser: Literal["path-only", "bundle"] = "path-only"
    degradation_reason = MINIMAL_POLICY_REASON
    bundle_provenance: dict[str, object] = {}
    if effective_policy.tier == "full":
        parsed = _parse_with_bundle(repo, pinned, paths, effective_policy)
        degradation_reason = parsed.reason
        bundle_provenance = parsed.provenance
        if parsed.applied:
            tier, parser = "full", "bundle"
            files = parsed.records
            diagnostics = parsed.diagnostics
            edges = _graph_from_facts(
                files, parsed.facts, diagnostics, effective_policy
            )
    indegree = {path: 0 for path in files}
    for edge in edges:
        if edge["confidence"] != "low" and edge["target"] in indegree:
            indegree[edge["target"]] += 1
    effective_seeds = sorted(set(seeds) & files.keys())
    distances: dict[str, int] = {}
    queue = deque(effective_seeds)
    for path in queue:
        distances[path] = 0
    neighbors: dict[str, set[str]] = {path: set() for path in files}
    for edge in edges:
        if (
            edge["confidence"] != "low"
            and edge["source"] in neighbors
            and edge["target"] in neighbors
        ):
            neighbors[edge["source"]].add(edge["target"])
            neighbors[edge["target"]].add(edge["source"])
    while queue:
        path = queue.popleft()
        for neighbor in sorted(neighbors[path]):
            if neighbor not in distances:
                distances[neighbor] = distances[path] + 1
                queue.append(neighbor)
    ordered = sorted(
        files,
        key=(lambda path: (distances.get(path, UNREACHABLE_DISTANCE), path))
        if effective_seeds
        else (lambda path: (-indegree[path], path)),
    )
    payload: dict[str, object] = {
        "schema_version": 1,
        "commit": pinned,
        "tier": tier,
        "parser": parser,
        "degradation_reason": degradation_reason,
        "token_estimator_version": TOKEN_ESTIMATOR_VERSION,
        "parser_provenance": {
            "policy_mode": "enforced" if effective_policy.enforced else "portable",
            "policy_sha256": effective_policy.sha256,
            "policy_tier": effective_policy.tier,
            "max_file_bytes": effective_policy.max_file_bytes,
            "max_files": effective_policy.max_files,
            "max_path_length": effective_policy.max_path_length,
            "max_symbol_length": effective_policy.max_symbol_length,
            "max_signature_length": effective_policy.max_signature_length,
            "timeout_seconds": effective_policy.timeout_seconds,
            "max_tokens": max_tokens,
            **bundle_provenance,
        },
        "files": [],
        "edges": [],
        "diagnostics": [],
        "estimated_tokens": 0,
    }
    encoded, size = _sized(payload)
    if size > max_tokens:
        raise PolicyError(
            "--max-tokens is too small for Repo Map metadata",
            remedy="raise --max-tokens or use a smaller policy tier and limits",
        )
    selected: set[str] = set()
    for path in ordered:
        candidate = selected | {path}
        payload["files"] = [files[item] for item in ordered if item in candidate]
        payload["edges"] = [
            edge
            for edge in edges
            if edge["source"] in candidate and edge["target"] in candidate
        ]
        payload["diagnostics"] = [
            diagnostic for diagnostic in diagnostics if diagnostic["path"] in candidate
        ]
        trial, trial_size = _sized(payload)
        if trial_size <= max_tokens:
            selected = candidate
            encoded = trial
        else:
            payload["files"] = [files[item] for item in ordered if item in selected]
            payload["edges"] = [
                edge
                for edge in edges
                if edge["source"] in selected and edge["target"] in selected
            ]
            payload["diagnostics"] = [
                diagnostic
                for diagnostic in diagnostics
                if diagnostic["path"] in selected
            ]
    return encoded


def build_map(
    repo: Path,
    commit: str,
    max_tokens: int,
    seeds: list[str],
    policy: RepoMapPolicy | None = None,
    *,
    cache_dir: Path | None = None,
) -> str:
    """Построить Repo Map для коммита, по возможности переиспользуя проверенную запись кэша.

    Проверяет бюджет и политику, закрепляет коммит, отбирает tracked-пути и кэширует результат, если
    запрос `full` не деградировал.
    """
    effective_policy = policy or RepoMapPolicy()
    if max_tokens < 1:
        raise PolicyError(
            "--max-tokens must be positive",
            remedy="pass a positive integer to --max-tokens",
        )
    if (
        effective_policy.max_tokens is not None
        and max_tokens > effective_policy.max_tokens
    ):
        raise PolicyError(
            "--max-tokens exceeds repo_map_policy.max_tokens",
            remedy="lower --max-tokens or raise repo_map_policy.max_tokens in the project config",
        )
    pinned = (
        _git(
            repo,
            effective_policy.timeout_seconds,
            "rev-parse",
            "--verify",
            f"{commit}^{{commit}}",
        )
        .decode()
        .strip()
    )
    raw_paths = _git(
        repo, effective_policy.timeout_seconds, "ls-tree", "-rz", "--name-only", pinned
    )
    paths = sorted(
        path.decode("utf-8", "surrogateescape")
        for path in raw_paths.split(b"\0")
        if path
    )
    paths = [path for path in paths if _allowed(path, effective_policy)]
    if len(paths) > effective_policy.max_files:
        raise PolicyError(
            f"Repo Map has {len(paths)} policy-approved files, above repo_map_policy.max_files={effective_policy.max_files}",
            remedy="narrow repo_map_policy.allow_paths or raise repo_map_policy.max_files deliberately",
        )
    normalized_seeds = sorted(set(seeds) & set(paths))
    root = cache_dir if cache_dir is not None else storage_path(repo, ".cache", "repo_map", "results")
    key = _cache_key(repo, pinned, normalized_seeds, max_tokens, effective_policy)
    cached = _read_cache(root, key, pinned)
    if cached is not None:
        try:
            cached_payload = json.loads(cached)
        except json.JSONDecodeError:
            cached_payload = None
        if isinstance(cached_payload, dict) and (
            effective_policy.tier == "minimal" or cached_payload.get("tier") == "full"
        ):
            return cached
    result = _build_map(
        repo, pinned, paths, max_tokens, normalized_seeds, effective_policy
    )
    if effective_policy.tier == "minimal" or json.loads(result)["tier"] == "full":
        _write_cache(root, key, result)
    return result


def main() -> int:
    """Точка входа CLI: разобрать аргументы, загрузить политику и напечатать карту; вернуть код выхода."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--commit", required=True)
    parser.add_argument("--seed", action="append", default=[])
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--cache-dir", type=Path)
    args = parser.parse_args()
    try:
        policy_path = args.policy or args.repo / ".harness" / "orchestration.json"
        policy = load_policy(policy_path, explicit=args.policy is not None)
        if args.max_tokens is not None:
            max_tokens = args.max_tokens
        elif policy.max_tokens is not None:
            max_tokens = policy.max_tokens
        else:
            max_tokens = DEFAULT_MAX_TOKENS
        sys.stdout.write(
            build_map(
                args.repo,
                args.commit,
                max_tokens,
                args.seed,
                policy,
                cache_dir=args.cache_dir,
            )
        )
    except HarnessError as exc:
        return print_and_exit(exc)
    except ValueError as exc:
        return print_and_exit(
            PolicyError(
                str(exc),
                remedy="inspect the Repo Map input and project policy, then retry",
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
