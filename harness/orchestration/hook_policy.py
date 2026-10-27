"""Политика hooks проекта (issue #794): профиль, эскалации и стабильный хеш.

Единственный источник — ключ ``hook_policy`` в ``.harness/project.json``. Переменные окружения
(``HOOK_PROFILE`` и подобные) и параметры инструментов не читаются: ни этот модуль, ни его вызывающие
не получают их на вход, поэтому решение зависит только от файла проекта, роли и затрагиваемых путей.
Проект без ключа получает профиль ``standard`` без эскалаций.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from ..errors import PolicyError
from ..health.project_files import HOOK_PROFILES, hook_policy_problems
from .contract import ContractError
from .core.system_fields import enum_problem, enum_remedy, load_registry

PROJECT_JSON_REL = ".harness/project.json"
DEFAULT_PROFILE = "standard"

_PROBLEM_REMEDY = "fix hook_policy in .harness/project.json; run 'harness health' for the exact problems"


def _fail(message: str) -> PolicyError:
    return PolicyError(message, remedy=_PROBLEM_REMEDY)


def normalize_policy(value: object) -> dict[str, object]:
    """Привести значение ``hook_policy`` к каноническому документу.

    ``None`` (ключ отсутствует) даёт профиль по умолчанию без эскалаций. Пути и роли эскалаций
    сортируются без повторов, сами эскалации упорядочиваются, поэтому смена порядка в файле не
    меняет хеш. Некорректное значение — ``PolicyError``.
    """
    if value is None:
        return {"profile": DEFAULT_PROFILE, "escalations": []}
    problems = hook_policy_problems(value)
    if problems:
        raise _fail("; ".join(problems))
    assert isinstance(value, Mapping)
    escalations = [
        {
            "paths": sorted(set(item.get("paths", []))),
            "roles": sorted(set(item.get("roles", []))),
            "profile": item["profile"],
        }
        for item in value.get("escalations", [])
    ]
    escalations.sort(key=lambda item: json.dumps(item, sort_keys=True))
    return {"profile": value["profile"], "escalations": escalations}


def load_policy(repo: Path) -> dict[str, object]:
    """Прочитать политику только из ``.harness/project.json`` репозитория.

    Отсутствие файла или ключа — профиль по умолчанию; нечитаемый или не объектный файл —
    ``PolicyError``, чтобы повреждённая конфигурация не превращалась в более слабую политику.
    """
    path = repo / PROJECT_JSON_REL
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return normalize_policy(None)
    except OSError as exc:
        raise _fail(f"{PROJECT_JSON_REL} cannot be read: {exc.strerror}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise _fail(f"{PROJECT_JSON_REL} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise _fail(f"{PROJECT_JSON_REL} must contain a JSON object")
    return normalize_policy(data.get("hook_policy"))


def _path_matches(path: str, patterns: Iterable[str]) -> bool:
    """``fnmatchcase``: ``*`` и ``?`` пересекают ``/``, поэтому эскалация ``src/*`` срабатывает и для
    ``src/a/b.py`` — совпадений больше, а не меньше, и профиль только строже."""
    normalized = path.replace("\\", "/").removeprefix("./")
    return any(fnmatchcase(normalized, pattern) for pattern in patterns)


# Чувствительные пути ADR 0024, раздел 2: фиксированная константа политики, ключа конфигурации нет.
# Копию держит hook `sensitive-path-recheck` (harness/project/hooks/change_impact_hooks.py), который
# не видит пакет harness; tests/core/test_change_impact_hooks.py сверяет её с этой функцией.
SENSITIVE_NAMES = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        "poetry.lock",
        "Pipfile",
        "Pipfile.lock",
        "setup.py",
        "setup.cfg",
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Cargo.toml",
        "Cargo.lock",
        "rust-toolchain",
        "rust-toolchain.toml",
        "go.mod",
        "go.sum",
        "Gemfile",
        "Gemfile.lock",
        "composer.json",
        "composer.lock",
        "CODEOWNERS",
    }
)
SENSITIVE_NAME_GLOBS = ("requirements*.txt", "constraints*.txt")
SENSITIVE_PATH_GLOBS = (
    ".github/*",
    ".claude/settings*.json",
    ".claude/hooks/*",
    ".claude/agents/*",
    ".codex/hooks.json",
    ".codex/hooks/*",
    ".codex/config.toml",
    ".harness/project.json",
)
# Миграции, auth и права доступа: сравниваются токены сегмента пути и stem файла, а не подстроки.
SENSITIVE_TOKENS = frozenset(
    {
        "migration",
        "migrations",
        "migrate",
        "alembic",
        "auth",
        "authn",
        "authz",
        "oauth",
        "oauth2",
        "authentication",
        "authorization",
        "permission",
        "permissions",
        "acl",
        "rbac",
    }
)
_TOKEN_SEPARATORS = re.compile(r"[._-]")


def is_sensitive_path(path: str) -> bool:
    """Чувствительный ли путь репозитория (ADR 0024, раздел 2).

    Манифест или lock-файл по имени на любой глубине, `.github/*`, миграции, auth и права доступа
    (токен сегмента пути или stem файла, разбитых по `.`, `_` и `-`; `author` не совпадает с
    `auth`), `CODEOWNERS`, файлы hooks runtime и `.harness/project.json`.
    """
    normalized = path.replace("\\", "/").removeprefix("./")
    parts = normalized.split("/")
    name = parts[-1]
    if name in SENSITIVE_NAMES or any(
        fnmatchcase(name, pattern) for pattern in SENSITIVE_NAME_GLOBS
    ):
        return True
    if any(fnmatchcase(normalized, pattern) for pattern in SENSITIVE_PATH_GLOBS):
        return True
    pieces = [*parts[:-1], PurePosixPath(name).stem]
    return any(
        token.lower() in SENSITIVE_TOKENS
        for piece in pieces
        for token in _TOKEN_SEPARATORS.split(piece)
    )


def resolve_profile(
    policy: Mapping[str, object], *, role: str | None = None, paths: Iterable[str] = ()
) -> str:
    """Вычислить действующий профиль: базовый, затем самая строгая подходящая эскалация.

    Эскалация подходит, когда выполнены все заданные в ней условия: роль входит в ``roles``, а
    хотя бы один из затронутых ``paths`` совпадает с одним из её glob. Условие не задано — оно
    выполнено. Эскалация не может ослабить базовый профиль.
    """
    base = policy["profile"]
    if base not in HOOK_PROFILES:
        raise _fail("hook_policy.profile is not a known profile")
    touched = list(paths)
    effective = HOOK_PROFILES.index(str(base))
    escalations = policy.get("escalations", [])
    assert isinstance(escalations, list)
    for item in escalations:
        if item["roles"] and role not in item["roles"]:
            continue
        if item["paths"] and not any(_path_matches(p, item["paths"]) for p in touched):
            continue
        effective = max(effective, HOOK_PROFILES.index(item["profile"]))
    return HOOK_PROFILES[effective]


def hook_policy_sha256(policy: Mapping[str, object]) -> str:
    """Стабильный SHA-256 канонического документа политики."""
    canonical = json.dumps(
        policy, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def project_hook_policy_sha256(repo: Path) -> str:
    """Хеш политики проекта ``repo``, прочитанной только из ``.harness/project.json``."""
    return hook_policy_sha256(load_policy(repo))


HOOK_RESPONSE_FIELDS = (
    "hook_id",
    "profile",
    "decision",
    "reason_code",
    "remedy",
    "evidence_ref",
)
# The verdict key and the systemFields.v1 field whose values it must take.
_HOOK_RESPONSE_ENUMS = (
    ("hook_id", "hook.id"),
    ("profile", "hook.profile"),
    ("decision", "hook.decision"),
    ("reason_code", "hook.reason_code"),
)


def _contract_error(problem: str, remedy: str) -> ContractError:
    return ContractError(f"hook response {problem}", remedy=remedy)


def parse_hook_response(text: str) -> dict[str, object]:
    """Разобрать вердикт управляемого hook: последняя непустая строка вывода — JSON-объект.

    Объект несёт ровно поля ``HOOK_RESPONSE_FIELDS``; ``hook_id``, ``profile``, ``decision`` и
    ``reason_code`` обязаны быть значениями реестра systemFields.v1, иначе — ``ContractError``.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise _contract_error(
            "is empty", "run the hook through hook_response.py and pass its output"
        )
    try:
        data = json.loads(lines[-1])
    except ValueError as exc:
        raise _contract_error(
            "is not valid JSON", "the last output line must be the JSON verdict"
        ) from exc
    if not isinstance(data, dict) or set(data) != set(HOOK_RESPONSE_FIELDS):
        raise _contract_error(
            f"must be an object with exactly: {', '.join(HOOK_RESPONSE_FIELDS)}",
            "emit the six verdict fields and no others",
        )
    registry = load_registry()
    for key, field_id in _HOOK_RESPONSE_ENUMS:
        allowed = registry.fields[field_id].allowed
        if problem := enum_problem(f"hook response {key}", data[key], allowed):
            raise _contract_error(
                problem.removeprefix("hook response "),
                enum_remedy(f"hook response {key}", allowed),
            )
    if not isinstance(data["remedy"], str):
        raise _contract_error("remedy must be a string", "set remedy to text")
    if data["evidence_ref"] is not None and not isinstance(data["evidence_ref"], str):
        raise _contract_error(
            "evidence_ref must be a string or null",
            "set evidence_ref to a path or null",
        )
    return data
