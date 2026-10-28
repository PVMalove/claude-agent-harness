"""Слияние managed hook-записей файла настроек runtime с записями пользователя.

Модуль чистый: без файлового ввода-вывода и печати, только стандартная библиотека. Он
поставляется вместе с пакетом `harness/health/`, поэтому из остального `harness` импортирует
только `harness.json_types`.
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
from typing import Literal

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


@dataclass(frozen=True)
class HookSpec:
    """Строка матрицы hooks (ADR 0024, раздел 2a): какой скрипт, на каком событии и инструментах.

    `out_of_hook` — проверка вне hook, которая защищает, если runtime не доставляет событие;
    None — столбец «Проверка вне hook» равен «нет». `wiring` — где hook подключён: в файле
    настроек runtime или во frontmatter агента.
    """

    hook_id: str
    script: str
    event: str
    tools: tuple[str, ...]
    critical: bool
    out_of_hook: str | None = None
    wiring: Literal["settings", "agent"] = "settings"


# Критичность каждой строки сверяет tests/health/test_hooks_policy_check.py
# (test_hook_catalog_agrees_with_the_response_envelope) с `HOOKS` из
# harness/project/hooks/hook_response.py; проводку и состав второго runtime —
# test_hook_catalog_agrees_with_the_templates с шаблонами и `CODEX_SUPPORT`.
HOOK_CATALOG: tuple[HookSpec, ...] = (
    HookSpec(
        "protect-hook-policy",
        "protect-hook-policy.sh",
        "PreToolUse",
        ("Bash", "Write", "Edit", "MultiEdit", "NotebookEdit"),
        True,
        "hook_policy_sha256 в brief",
    ),
    HookSpec(
        "block-public-attribution",
        "block-public-attribution.sh",
        "PreToolUse",
        ("Bash",),
        True,
        "CI check_public_metadata.py",
    ),
    HookSpec(
        "block-direct-master",
        "block-direct-master.sh",
        "PreToolUse",
        ("Bash",),
        True,
        "dispatch publish пушит только ветку batch",
    ),
    HookSpec("block-pr-merge", "block-pr-merge.sh", "PreToolUse", ("Bash",), True),
    HookSpec(
        "check-branch-name",
        "check-branch-name.sh",
        "PreToolUse",
        ("Bash",),
        True,
        "batch create по branch_pattern",
    ),
    HookSpec(
        "require-qa-gate",
        "require-qa-gate.sh",
        "PreToolUse",
        ("Bash",),
        True,
        "qa_evidence_verify",
    ),
    HookSpec(
        "block-dangerous-git",
        "block-dangerous-git.sh",
        "PreToolUse",
        ("Bash",),
        True,
    ),
    HookSpec(
        "block-scratch-outside-docs-tasks",
        "block-scratch-outside-docs-tasks.sh",
        "PreToolUse",
        ("Write", "Edit"),
        True,
    ),
    HookSpec(
        "check-worktree-branch-name",
        "check-worktree-branch-name.sh",
        "PreToolUse",
        ("EnterWorktree",),
        True,
        "batch create по branch_pattern",
    ),
    HookSpec(
        "test-author-guard",
        "test-author-guard.py",
        "PreToolUse",
        ("Write", "Edit", "Bash"),
        True,
        wiring="agent",
    ),
    HookSpec(
        "require-bounded-check",
        "require-bounded-check.sh",
        "PreToolUse",
        ("Bash",),
        False,
        "VerificationPlan",
    ),
    HookSpec(
        "count-skill-usage", "count-skill-usage.sh", "PreToolUse", ("Skill",), False
    ),
    HookSpec(
        "mark-qa-gate-passed", "mark-qa-gate-passed.sh", "PostToolUse", ("Bash",), False
    ),
    # Advisory-hooks ChangeImpact (ADR 0024, раздел 2a). Инструмент `*` обозначает событие без
    # инструмента (Stop, SubagentStop): его покрывает группа без matcher.
    HookSpec(
        "impact-first-touch",
        "impact-first-touch.sh",
        "PreToolUse",
        ("Write", "Edit"),
        False,
        "ChangeImpact-gate по ADR 0023",
    ),
    HookSpec(
        "sensitive-path-recheck",
        "sensitive-path-recheck.sh",
        "PreToolUse",
        ("Write", "Edit"),
        False,
        "граница strict (dispatch propose, qa run, publish)",
    ),
    HookSpec(
        "targeted-check-reminder",
        "targeted-check-reminder.sh",
        "PreToolUse",
        ("Write", "Edit"),
        False,
        "сверка checks_run",
    ),
    HookSpec(
        "targeted-check-reminder",
        "targeted-check-reminder.sh",
        "Stop",
        ("*",),
        False,
        "сверка checks_run",
    ),
    HookSpec(
        "targeted-check-reminder",
        "targeted-check-reminder.sh",
        "SubagentStop",
        ("*",),
        False,
        "сверка checks_run",
    ),
)

EventTool = tuple[str, str]
# События и инструменты, которые доставляет runtime: все строки каталога.
CLAUDE_SUPPORT: frozenset[EventTool] = frozenset(
    (spec.event, tool) for spec in HOOK_CATALOG for tool in spec.tools
)


@dataclass(frozen=True)
class RuntimeAdapter:
    """Адаптер runtime: файл hooks, его шаблон и запись в каталоге интеграций.

    `support` — пары (событие, инструмент), которые runtime доставляет hooks; None — не
    подтверждено по первичному источнику.
    """

    runtime: str
    config: str
    template: str
    integration_id: str
    verify: str
    support: frozenset[EventTool] | None


# Второй runtime: формат подтверждён официальной документацией hooks
# (https://developers.openai.com/codex/hooks, 2026-10-22). Файл `<repo>/.codex/hooks.json` той же
# формы `{"hooks": {<событие>: [{matcher, hooks: [handler]}]}}`; stdin shell-инструмента `Bash`
# содержит `tool_name`, `tool_input.command`, `cwd`; код 2 блокирует. Правки файлов приходят как
# `apply_patch` с патчем в `tool_input.command` и без `file_path`, поэтому hooks на Write и Edit
# (адаптер payload — отдельная задача) не подключаются. События EnterWorktree и Skill в runtime
# нет. Проектные hooks работают только после доверия слою `.codex/` и каждому hook по хэшу.
# `mark-qa-gate-passed` на `PostToolUse` не подключается: runtime вызывает `PostToolUse` и после
# команды Bash с ненулевым кодом выхода, а в payload нет поля с кодом выхода. Hook записал бы
# маркер `.claude/.qa-gate/passed` для упавшего QA-прогона, и `require-qa-gate` разрешил бы PR.
# События Stop и SubagentStop подтверждены тем же источником (страница hooks, 2026-10-27): payload
# несёт `session_id`, `hook_event_name`, `stop_hook_active`, а SubagentStop — ещё `agent_id`; при коде 0
# stdout принимает только JSON или пустую строку. Журнал касаний `targeted-check-reminder` на Write и
# Edit ждёт адаптера `apply_patch`, поэтому до него напоминание второго runtime молчит.
CODEX_SUPPORT: frozenset[EventTool] = frozenset(
    {("PreToolUse", "Bash"), ("Stop", "*"), ("SubagentStop", "*")}
)


def canonical_integration_id(identifier: object) -> object:
    """Keep managed hook fingerprints usable across the suite rename."""
    if identifier == "pvmalove-suite-hooks":
        return "engineering-suite-hooks"
    if identifier == "pvmalove-suite-codex-hooks":
        return "engineering-suite-codex-hooks"
    return identifier


ADAPTERS: tuple[RuntimeAdapter, ...] = (
    RuntimeAdapter(
        runtime="claude",
        config=".claude/settings.local.json",
        template="settings.local.json.tmpl",
        integration_id="engineering-suite-hooks",
        verify=(
            "Open a fresh Claude Code session and confirm a guarded action "
            "(e.g. a direct commit to the base branch) is blocked by the wired hooks."
        ),
        support=CLAUDE_SUPPORT,
    ),
    RuntimeAdapter(
        runtime="codex",
        config=".codex/hooks.json",
        template="codex-hooks.json.tmpl",
        integration_id="engineering-suite-codex-hooks",
        verify=(
            "Trust the project hooks layer in the second coding runtime, open a fresh session "
            "and confirm a guarded action (e.g. a direct commit to the base branch) is blocked."
        ),
        support=CODEX_SUPPORT,
    ),
)


def matcher_covers(matcher: str, tool: str) -> bool:
    """Покрывает ли matcher группы инструмент: пустой и `*` — все, иначе regex целиком."""
    if matcher in ("", "*"):
        return True
    try:
        return re.fullmatch(matcher, tool) is not None
    except re.error:
        return False


def is_wired(document: JsonObject, spec: HookSpec, tool: str) -> bool:
    """Подключён ли скрипт `spec` на событии для инструмента `tool` в документе настроек."""
    groups = _groups(document)
    if isinstance(groups, str):
        return False
    return any(
        matcher_covers(matcher, tool) and script_name(handler) == spec.script
        for event, matcher, handler in _triples(groups)
        if event == spec.event
    )


def supported_tools(
    spec: HookSpec, support: frozenset[EventTool] | None
) -> tuple[str, ...]:
    """Инструменты `spec`, на которых runtime доставляет событие hook-а."""
    if support is None:
        return ()
    return tuple(tool for tool in spec.tools if (spec.event, tool) in support)


def critical_wiring(
    document: JsonObject, support: frozenset[EventTool] | None
) -> tuple[int, int, tuple[str, ...]]:
    """(подключено, всего, id неподключённых) среди критических hooks файла настроек.

    Считаются критические hooks, подключаемые в файле настроек, у которых runtime доставляет
    хотя бы один инструмент. Hook подключён, когда покрыт каждый такой инструмент.
    """
    specs = [
        (spec, tools)
        for spec in HOOK_CATALOG
        if spec.critical
        and spec.wiring == "settings"
        and (tools := supported_tools(spec, support))
    ]
    missing = tuple(
        spec.hook_id
        for spec, tools in specs
        if not all(is_wired(document, spec, tool) for tool in tools)
    )
    return len(specs) - len(missing), len(specs), missing
