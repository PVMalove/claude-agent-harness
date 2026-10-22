"""Слияние managed hook-записей файла настроек runtime с записями пользователя.

Модуль чистый: без файлового ввода-вывода и печати, только стандартная библиотека. Он
поставляется вместе с пакетом `harness/health/`, поэтому не импортирует остальной `harness`.
Чтение и запись файлов, шаблоны и отчёт установки остаются в `harness/bin/harness.py`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass

from harness.json_types import JsonObject


def entry_digest(event: str, matcher: str, handler: JsonObject) -> str:
    """Отпечаток тройки (событие, matcher, handler): sha256 канонического JSON."""
    canonical = json.dumps(
        {"event": event, "matcher": matcher, "hook": handler},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Путь managed-скрипта в команде handler: первый `.claude/hooks/<имя>` после разделителя.
_SCRIPT_PATH = re.compile(
    r"(?:^|[\s\"'/=])\.claude/hooks/([A-Za-z0-9][A-Za-z0-9._-]*)(?=[\"'\s]|$)"
)

Triple = tuple[str, str, JsonObject]


@dataclass(frozen=True)
class HookConflict:
    """Managed handler, изменённый пользователем: остаётся без изменений."""

    event: str
    matcher: str
    script: str


@dataclass(frozen=True)
class HookMerge:
    """Итог слияния: новый документ и счётчики по единицам (событие, скрипт).

    `problem` не пуст, когда документ нельзя слить; тогда `document` равен None.
    """

    document: JsonObject | None
    changed: bool
    added: int = 0
    replaced: int = 0
    current: int = 0
    user: int = 0
    conflicts: tuple[HookConflict, ...] = ()
    problem: str | None = None


def script_name(handler: object) -> str | None:
    """Имя hook-скрипта из `command` handler; None, если команда не ссылается на `.claude/hooks/`."""
    if not isinstance(handler, dict):
        return None
    command = handler.get("command")
    match = _SCRIPT_PATH.search(command) if isinstance(command, str) else None
    return match.group(1) if match else None


def _groups(document: JsonObject) -> dict[str, list[JsonObject]] | str:
    """Событие -> группы документа; строка — причина, по которой форма `hooks` не подходит."""
    hooks = document.get("hooks", {})
    if not isinstance(hooks, dict):
        return "`hooks` must be an object"
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            return f"`hooks.{event}` must be a list"
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                return f"`hooks.{event}` groups must be objects with a `hooks` list"
            if not isinstance(group.get("matcher", ""), str):
                return f"`hooks.{event}` matcher must be a string"
    return hooks


def _triples(groups: dict[str, list[JsonObject]]) -> list[tuple[str, str, JsonObject]]:
    """Все handler-ы документа как (событие, matcher, handler) в порядке файла."""
    return [
        (event, str(group.get("matcher", "")), handler)
        for event, event_groups in groups.items()
        for group in event_groups
        for handler in group["hooks"]
        if isinstance(handler, dict)
    ]


def template_digests(template: JsonObject) -> tuple[str, ...]:
    """Отпечатки всех managed-троек шаблона: их harness пишет в файл и добавляет к базе."""
    groups = _groups(template)
    if isinstance(groups, str):
        raise ValueError(f"hook template is invalid: {groups}")
    return tuple(
        sorted(
            {
                entry_digest(*triple)
                for triple in _triples(groups)
                if script_name(triple[2]) is not None
            }
        )
    )


def merge_hooks(
    document: object, template: JsonObject, baseline: Collection[str]
) -> HookMerge:
    """Слить managed hook-записи шаблона в документ, не трогая записи пользователя.

    Единица слияния — (событие, скрипт) из шаблона. Если handler-ы документа в единице равны
    шаблону, она актуальна. Если каждый из них входит в шаблон или в `baseline` (отпечатки
    всего, что harness когда-либо записал), единица заменяется шаблонной. Иначе пользователь
    изменил managed-запись: это конфликт, единица остаётся как есть.
    """
    if not isinstance(document, dict):
        return HookMerge(None, False, problem="settings must be a JSON object")
    existing = _groups(document)
    if isinstance(existing, str):
        return HookMerge(None, False, problem=existing)
    wanted = _groups(template)
    if isinstance(wanted, str):
        raise ValueError(f"hook template is invalid: {wanted}")

    units: dict[tuple[str, str], list[Triple]] = {}
    for event, matcher, handler in _triples(wanted):
        script = script_name(handler)
        if script is not None:
            units.setdefault((event, script), []).append((event, matcher, handler))
    allowed = set(baseline) | set(template_digests(template))

    result = copy.deepcopy(document)
    result_groups = _groups(result)
    assert isinstance(result_groups, dict)
    present: dict[tuple[str, str], list[Triple]] = {}
    for event, matcher, handler in _triples(result_groups):
        script = script_name(handler)
        if (event, script) in units and script is not None:
            present.setdefault((event, script), []).append((event, matcher, handler))
    user = sum(
        1
        for event, _, handler in _triples(result_groups)
        if (event, script_name(handler)) not in units
    )

    counts = {"added": 0, "replaced": 0, "current": 0}
    conflicts: list[HookConflict] = []
    inserts: list[tuple[str, str, JsonObject]] = []
    removals: list[JsonObject] = []
    for key, template_triples in units.items():
        found = present.get(key, [])
        found_digests = Counter(entry_digest(*triple) for triple in found)
        if found_digests == Counter(entry_digest(*t) for t in template_triples):
            counts["current"] += 1
        elif all(digest in allowed for digest in found_digests):
            counts["replaced" if found else "added"] += 1
            removals.extend(handler for _, _, handler in found)
            inserts.extend(template_triples)
        else:
            for event, matcher, handler in found:
                if entry_digest(event, matcher, handler) not in allowed:
                    conflict = HookConflict(event, matcher, key[1])
                    if conflict not in conflicts:
                        conflicts.append(conflict)

    for event in list(result_groups):
        kept_groups = []
        for group in result_groups[event]:
            before = len(group["hooks"])
            group["hooks"] = [
                handler
                for handler in group["hooks"]
                if not any(handler is removed for removed in removals)
            ]
            if group["hooks"] or len(group["hooks"]) == before:
                kept_groups.append(group)
        if kept_groups or not result_groups[event]:
            result_groups[event] = kept_groups
        else:
            del result_groups[event]
    for event, matcher, handler in inserts:
        event_groups = result_groups.get(event)
        if event_groups is None:
            event_groups = []
            hooks_value = result.setdefault("hooks", {})
            assert isinstance(hooks_value, dict)
            hooks_value[event] = event_groups
            result_groups[event] = event_groups
        target = next(
            (g for g in event_groups if str(g.get("matcher", "")) == matcher), None
        )
        if target is None:
            target = _template_group(wanted, event, matcher)
            event_groups.append(target)
        target["hooks"].append(copy.deepcopy(handler))

    return HookMerge(
        document=result,
        changed=result != document,
        added=counts["added"],
        replaced=counts["replaced"],
        current=counts["current"],
        user=user,
        conflicts=tuple(conflicts),
    )


def _template_group(
    wanted: dict[str, list[JsonObject]], event: str, matcher: str
) -> JsonObject:
    """Пустая группа с теми же ключами, что у группы шаблона для (событие, matcher)."""
    for group in wanted.get(event, []):
        if str(group.get("matcher", "")) == matcher:
            return {
                key: copy.deepcopy(value)
                for key, value in group.items()
                if key != "hooks"
            } | {"hooks": []}
    return {"matcher": matcher, "hooks": []}
