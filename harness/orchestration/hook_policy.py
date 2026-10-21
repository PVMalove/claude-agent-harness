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
from pathlib import Path

from ..errors import PolicyError
from ..health.project_files import HOOK_PROFILES, hook_policy_problems

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


def _glob_regex(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(out) + r"\Z")


def _path_matches(path: str, patterns: Iterable[str]) -> bool:
    normalized = path.replace("\\", "/").removeprefix("./")
    return any(_glob_regex(pattern).match(normalized) for pattern in patterns)


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
