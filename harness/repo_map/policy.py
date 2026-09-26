"""Политика Repo Map: загрузка `repo_map_policy`, фильтр путей и видимость символов.

Модуль отвечает только за правила: какие tracked-пути попадают в карту и какие символы можно
раскрыть. Он не читает Git и не запускает парсер.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal, cast

from harness.errors import PolicyError

DEFAULT_MAX_TOKENS = 4000
DEFAULT_MAX_FILES = 10_000
DEFAULT_MAX_FILE_BYTES = 2_000_000
DEFAULT_MAX_PATH_LENGTH = 4_096
DEFAULT_MAX_SYMBOL_LENGTH = 256
DEFAULT_MAX_SIGNATURE_LENGTH = 2_048
DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_PARSER_BUNDLE_TIMEOUT_SECONDS = 30
DEFAULT_PARSER_BUNDLE_MAX_OUTPUT_BYTES = 10_000_000
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

Tier = Literal["minimal", "full"]


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
    tier: Tier = "full"
    parser_bundle_registry_paths: tuple[str, ...] = ()
    parser_bundle_timeout_seconds: int = DEFAULT_PARSER_BUNDLE_TIMEOUT_SECONDS
    parser_bundle_max_output_bytes: int = DEFAULT_PARSER_BUNDLE_MAX_OUTPUT_BYTES
    sha256: str | None = None

    @property
    def enforced(self) -> bool:
        """Политика загружена из файла проекта, а не взята по умолчанию."""
        return self.sha256 is not None


_DEFAULTS = RepoMapPolicy()
_PATTERN_FIELDS = ("allow_paths", "deny_paths", "redact_paths", "redact_symbols")
# Validation order is part of the contract: the first invalid field is the one reported.
_POSITIVE_INT_FIELDS = (
    "max_files",
    "max_file_bytes",
    "max_path_length",
    "max_symbol_length",
    "max_signature_length",
    "timeout_seconds",
    "parser_bundle_timeout_seconds",
    "parser_bundle_max_output_bytes",
)
# Dispatch-admission fields, not generation fields: the coordinator resolves and enforces them via
# harness.orchestration.contract.resolve_min_repo_map_tier. They are recognised here only so the
# same repo_map_policy object does not fail generation-time validation.
_ADMISSION_FIELDS = frozenset({"min_tier", "min_tier_by_role"})
# Every generation field is a `RepoMapPolicy` field except the file hash computed on load.
_ALLOWED_FIELDS = (
    frozenset(field.name for field in fields(RepoMapPolicy)) - {"sha256"}
) | _ADMISSION_FIELDS


def matches(value: str, patterns: tuple[str, ...]) -> bool:
    """Проверить, совпадает ли путь или имя хотя бы с одним glob-шаблоном (с учётом регистра)."""
    return any(fnmatchcase(value, pattern) for pattern in patterns)


def path_allowed(path: str, policy: RepoMapPolicy) -> bool:
    """Решить, попадает ли tracked-путь в карту.

    Отсекает служебные каталоги, секреты, сгенерированные и бинарные по расширению файлы, слишком
    длинные пути, а затем применяет `allow_paths`, `deny_paths` и `redact_paths` политики.
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
    if policy.allow_paths and not matches(path, policy.allow_paths):
        return False
    if matches(path, policy.deny_paths) or matches(path, policy.redact_paths):
        return False
    return not any(name.endswith(suffix) for suffix in EXCLUDED_SUFFIXES)


def symbol_visible(name: str, policy: RepoMapPolicy) -> bool:
    """Проверить, что символ укладывается в `max_symbol_length` и не попадает под `redact_symbols`."""
    return len(name) <= policy.max_symbol_length and not matches(name, policy.redact_symbols)


def _policy_error(message: str, remedy: str) -> PolicyError:
    """Собрать `PolicyError` с сообщением и рекомендацией."""
    return PolicyError(message, remedy=remedy)


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
        raise _policy_error(
            f"repo_map_policy.{field} must be a positive integer",
            f"set repo_map_policy.{field} to a positive integer in the project orchestration config",
        )
    return value


def _tier(value: object) -> Tier:
    """Проверить уровень карты: `full` или `minimal`."""
    if not isinstance(value, str) or value not in {"minimal", "full"}:
        raise _policy_error(
            "repo_map_policy.tier must be one of: full, minimal",
            "set repo_map_policy.tier to 'full' or 'minimal'",
        )
    return cast(Tier, value)


def _read_policy_section(path: Path | None, *, explicit: bool) -> tuple[dict[str, object], bytes] | None:
    """Прочитать файл политики и вернуть секцию `repo_map_policy` вместе с исходными байтами.

    `None` означает переносимые значения по умолчанию: файла или секции нет, а путь не был задан явно.
    """
    if path is None or not path.is_file():
        if explicit:
            raise _policy_error(
                f"policy file does not exist: {path}",
                "create the policy file or omit --policy to use portable defaults",
            )
        return None
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
        return None
    if not isinstance(section, dict):
        raise _policy_error(
            "repo_map_policy must be an object",
            "set repo_map_policy to an object that follows orchestration.schema.json",
        )
    return section, raw


def _policy_from_section(section: dict[str, object], raw: bytes) -> RepoMapPolicy:
    """Проверить поля секции `repo_map_policy` и собрать неизменяемую политику."""
    unknown = sorted(str(key) for key in section.keys() - _ALLOWED_FIELDS)
    if unknown:
        raise _policy_error(
            f"repo_map_policy has unknown fields: {', '.join(unknown)}",
            "remove unknown repo_map_policy fields and follow orchestration.schema.json",
        )
    patterns = {field: _string_patterns(section.get(field, []), field) for field in _PATTERN_FIELDS}
    registry_paths = tuple(
        _non_empty_strings(
            section.get("parser_bundle_registry_paths", []),
            "parser_bundle_registry_paths",
            "strings",
        )
    )
    numeric = {
        field: _positive_int(section.get(field, getattr(_DEFAULTS, field)), field)
        for field in _POSITIVE_INT_FIELDS
    }
    max_tokens_value = section.get("max_tokens")
    max_tokens = (
        _positive_int(max_tokens_value, "max_tokens") if max_tokens_value is not None else None
    )
    return RepoMapPolicy(
        allow_paths=patterns["allow_paths"],
        deny_paths=patterns["deny_paths"],
        redact_paths=patterns["redact_paths"],
        redact_symbols=patterns["redact_symbols"],
        max_files=numeric["max_files"],
        max_file_bytes=numeric["max_file_bytes"],
        max_path_length=numeric["max_path_length"],
        max_symbol_length=numeric["max_symbol_length"],
        max_signature_length=numeric["max_signature_length"],
        timeout_seconds=numeric["timeout_seconds"],
        max_tokens=max_tokens,
        tier=_tier(section.get("tier", "full")),
        parser_bundle_registry_paths=registry_paths,
        parser_bundle_timeout_seconds=numeric["parser_bundle_timeout_seconds"],
        parser_bundle_max_output_bytes=numeric["parser_bundle_max_output_bytes"],
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def load_policy(path: Path | None, *, explicit: bool) -> RepoMapPolicy:
    """Загрузить и проверить `repo_map_policy` из файла оркестрации.

    Без файла или без секции возвращает переносимые значения по умолчанию; если путь передан явно
    (`explicit`), их отсутствие — ошибка. Неизвестные поля и неверные типы поднимают `PolicyError`
    с рекомендацией.
    """
    loaded = _read_policy_section(path, explicit=explicit)
    if loaded is None:
        return RepoMapPolicy()
    section, raw = loaded
    return _policy_from_section(section, raw)
