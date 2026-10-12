"""Schema-driven field navigation with contextual, deterministic recommendations."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Callable

from harness.configuration import parse_config_json
from harness.configuration_schema import load_schema, resolve_ref, value_problems
from harness.json_types import JsonObject
from .configuration import ConfigurationDraft
from .project_facts import ProjectFacts, project_facts

CHOICE_PAGE_SIZE = 4

_HELP = {
    "language": "Язык отчётов и описания PR: ru — русский, en — английский. На выполнение команд не влияет.",
    "base_branch": "Ветка релиза и база задач без эпика. Укажите действительную ветку проекта; интеграционные ветки эпиков создаются от неё.",
    "branch_pattern": "Регулярное выражение имён issue-веток. Hooks блокируют коммиты в ветках, не соответствующих этому правилу.",
    "qa_gate_commands": "Полная проверка перед PR: команды lint, typecheck и тестов. Ввод команд их не запускает. Пустой список остановит qa-gate.",
    "ci_required_checks": "Имена обязательных CI checks. Пустой список оставляет локальную QA-проверку; точные имена берите из CI проекта.",
    "shell": "Оболочка QA: bash для Linux/macOS/WSL, powershell для native Windows. Неподходящая оболочка приводит к ошибке запуска.",
    "tracker": "Трекер проекта: github, gitlab или local. Явное значение важнее origin. Для хостинга укажите host и полный путь project; токены сюда не входят.",
    "story_points": "Шкала сложности и границы pipeline::fast/full. Применяется к afk-задачам; hitl не получает pipeline-метку.",
    "memory": "Память проекта ищет предыдущие решения в разрешённых источниках. Включайте после заполнения memory_policy; индекс не заменяет исходные документы.",
    "memory_policy": "Разрешённые источники/пути и лимиты поиска памяти. Пустые allow_paths/source_types не включают чтение всего проекта.",
    "concurrency_budget": "Максимум одновременно активных batch. 1 — последовательная работа; 2/4 — больше параллелизма и потребления ресурсов.",
    "verification_commands": "Полный gate в чистом QA checkout. Пустой список оставляет QA без проверок; задайте реальные команды проекта.",
    "developer_verification_commands": "Быстрые проверки developer. При отсутствии используется полный verification_commands, что увеличивает время каждого цикла.",
    "review_verification_commands": "Проверки code-review; при отсутствии используется полный gate. Они не заменяют независимую QA.",
    "approval_policy": "manual_all — человек согласует каждый шаг; milestone — автоматические чистые этапы, ключевые вехи вручную; low_risk — только разрешённые пути; auto — автоматический цикл, требует trusted и worker_attestation_required=true. PR и merge остаются ручными.",
    "access_policy": "Доступ QA/Git/publish и ролей к сети/файлам. Без extensions.runtime_access такая настройка блокирует dispatch send: допустимый JSON ещё не означает готовность запуска.",
    "tracker.type": "Где хранить задачи и PR: GitHub/GitLab используют текущий CLI, local — Markdown. Для hosted-трекера нужны host и project; local не запускает сетевые проверки.",
    "tracker.host": "Хост GitHub/GitLab с необязательным портом, например gitlab.example.test:4443. Без схемы URL, пути и учётных данных. Определяет сервер онлайн-проверок.",
    "tracker.project": "Полный путь проекта в трекере, например group/sub/backend. Определяет репозиторий для задач, PR и проверок доступа.",
    "memory.enabled": "Включить поиск памяти проекта. Сначала задайте разрешённые типы источников, пути и лимиты в memory_policy; разрешение не даёт доступа ко всем файлам.",
    "memory_policy.source_types": "Какие документы индексировать: ADR, словарь, архив задач, QA findings, completion reports и ledger. Пустой список не разрешает все источники.",
    "memory_policy.allow_paths": "Относительные разрешённые пути памяти без .., абсолютных путей и секретов. Пустой список не включает весь репозиторий.",
    "memory_policy.redact_rules": "Регулярные выражения удаления чувствительных данных при индексации. Корректный regex ещё не гарантирует распознавание всех секретов.",
    "memory_policy.min_similarity": "Порог сходства для будущего cosine-vector поиска; текущие ранги FTS5 этим полем не фильтруются.",
    "memory_policy.top_k": "Максимальное число результатов памяти. Больший лимит добавляет контекст и увеличивает объём обработки.",
    "memory_policy.max_tokens": "Бюджет токенов контекста памяти. Больший бюджет может увеличить расход модели.",
    "story_points.scale": "Допустимые оценки сложности задач: положительные целые числа. Например [1,2,3,5,8,13]. На само выполнение QA не влияет.",
    "story_points.fast_threshold": "Оценки не выше этого порога предлагают pipeline::fast; большой порог отправляет больше задач в короткий путь.",
    "story_points.full_threshold": "Оценки не ниже этого порога предлагают pipeline::full с отдельными этапами проверки.",
    "story_points.gray_zone": "Промежуточная оценка вызывает дополнительную оценку сложности. Настройте согласованно с обоими порогами.",
}
# value -> (what it does, limitation, when to choose it); plain strings are one-line effects.
_EFFECTS: dict[str, str | tuple[str, str, str]] = {
    "manual_all": (
        "Каждый шаг требует ручного решения.",
        "Медленнее: человек нужен на каждом этапе.",
        "Первая настройка, чувствительные проекты, отладка процесса.",
    ),
    "milestone": (
        "Чистые промежуточные этапы принимает политика; QA, publish и риск требуют человека.",
        "Нужны надёжные проверки и evidence на каждом этапе.",
        "Проект с проверенными QA-командами и умеренным риском.",
    ),
    "low_risk": (
        "Автоматическое принятие ограничено путями из low_risk_paths.",
        "Без low_risk_paths автопуть не сработает.",
        "Документация, тесты и другие заранее перечисленные безопасные области.",
    ),
    "auto": (
        "Максимальная автономность цикла; PR и merge остаются ручными.",
        "Требует human_approval_gate=trusted и worker_attestation_required=true.",
        "Зрелый проект с готовыми проверками и проверенной политикой доступа.",
    ),
    "trusted": (
        "Согласование через записанное evidence.",
        "Доверие определяется записанными данными, а не живым терминалом.",
        "Режим, поддерживаемый для approval_policy=auto и неинтерактивных запусков.",
    ),
    "tty": (
        "Согласование только интерактивно в терминале.",
        "Несовместимо с approval_policy=auto.",
        "Нужно живое подтверждение человеком каждого решения.",
    ),
    "in-process": (
        "Роль запускается в текущем runtime координатора.",
        "Использует доступы и ограничения самого координатора.",
        "Локальный запуск без внешнего адаптера.",
    ),
    "external": (
        "Роль запускается во внешнем runtime через адаптер.",
        "Нужны подключённый адаптер и его доступы.",
        "Отдельная изолированная среда выполнения ролей.",
    ),
    "local": (
        "Локальные Markdown-задачи; онлайн-проверки трекера пропускаются.",
        "Нет PR/MR и совместной работы через хостинг.",
        "Проект без GitHub/GitLab или автономная работа.",
    ),
    "github": (
        "Задачи и PR в GitHub; используется текущая авторизация gh.",
        "Нужны host/project и право доступа к репозиторию.",
        "Репозиторий на GitHub.",
    ),
    "gitlab": (
        "Задачи и MR в GitLab/self-hosted; используется glab и явный host.",
        "Нужны host/project и право доступа к проекту.",
        "Репозиторий на GitLab, в том числе self-hosted.",
    ),
    "bash": (
        "Команды выполняются через bash -lc.",
        "Нужен bash: Linux, macOS или WSL.",
        "Linux/macOS/WSL-окружение.",
    ),
    "powershell": (
        "Команды выполняются в PowerShell.",
        "Команды должны соответствовать синтаксису PowerShell.",
        "Native Windows без WSL.",
    ),
    "ru": "Русские отчёты и описания PR.",
    "en": "Английские отчёты и описания PR.",
    "none": "Без reasoning, если модель и runtime поддерживают такой режим.",
    "minimal": "Минимальная глубина reasoning; быстрее, подходит для простых задач.",
    "low": "Небольшая глубина reasoning; ограничивает время и расход.",
    "medium": "Средняя глубина reasoning для обычных задач.",
    "high": "Больше reasoning для сложных задач; может увеличить время и расход.",
    "xhigh": "Очень высокая глубина; поддержка зависит от модели и версии CLI.",
    "max": "Максимальный режим данного runtime; проверьте поддержку модели и бюджет.",
    "ultra": "Наиболее глубокий режим у поддерживающих runtime; повышает требования к бюджету.",
    "adr": "Архитектурные решения: помогает не повторять уже выбранные компромиссы.",
    "glossary": "Словарь проекта: помогает сохранять единые термины.",
    "task_archive": "Архив задач: контекст прежних реализаций.",
    "qa_finding": "Замечания QA: помогает учитывать прошлые ошибки.",
    "completion_report": "Отчёты завершения: сведения о результатах прежних задач.",
    "ledger": "Леджер оркестрации: подтверждённая история этапов.",
}


Recommendation = tuple[object, str] | None


def _approval_policy(
    step: ConfigurationStep, draft: ConfigurationDraft, facts: ProjectFacts
) -> Recommendation:
    if facts.qa_commands:
        return (
            "manual_all",
            "Ручное согласование безопасно для первой настройки; "
            f"в проекте найдена проверка «{facts.qa_commands[0]}», после её проверки можно перейти на milestone.",
        )
    return (
        "manual_all",
        "В проекте не найдены очевидные QA-команды, поэтому автоматизировать согласование рано: оставьте ручное.",
    )


def _shell(
    step: ConfigurationStep, draft: ConfigurationDraft, facts: ProjectFacts
) -> Recommendation:
    shell = "powershell" if facts.platform == "windows" else "bash"
    return (
        shell,
        "Выбрано по ОС процесса консоли; если QA выполняется в другой среде (WSL, контейнер), укажите её оболочку.",
    )


def _concurrency(
    step: ConfigurationStep, draft: ConfigurationDraft, facts: ProjectFacts
) -> Recommendation:
    return (
        1,
        "Последовательная работа упрощает первую проверку и ограничивает потребление ресурсов.",
    )


def _transport(
    step: ConfigurationStep, draft: ConfigurationDraft, facts: ProjectFacts
) -> Recommendation:
    return "in-process", "Внешний transport требует отдельно настроенного адаптера."


def _approval_gate(
    step: ConfigurationStep, draft: ConfigurationDraft, facts: ProjectFacts
) -> Recommendation:
    return (
        "trusted",
        "Режим поддерживает записанное согласование; для обязательного живого терминала выберите tty.",
    )


def _attestation(
    step: ConfigurationStep, draft: ConfigurationDraft, facts: ProjectFacts
) -> Recommendation:
    if draft.documents[step.file].get("approval_policy") == "auto":
        return (
            True,
            "Для approval_policy=auto attestation обязательна; без неё контракт отклонит конфигурацию.",
        )
    return None


def _base_branch(
    step: ConfigurationStep, draft: ConfigurationDraft, facts: ProjectFacts
) -> Recommendation:
    if facts.default_branch:
        return (
            facts.default_branch,
            f"Ветка по умолчанию origin в этом репозитории: {facts.default_branch}.",
        )
    return None


def _qa_commands(
    step: ConfigurationStep, draft: ConfigurationDraft, facts: ProjectFacts
) -> Recommendation:
    if facts.qa_commands and not step.value(draft):
        return (
            list(facts.qa_commands),
            f"Команда выведена из файлов проекта ({', '.join(facts.markers)}); проверьте её перед использованием.",
        )
    return None


def _tracker_type(
    step: ConfigurationStep, draft: ConfigurationDraft, facts: ProjectFacts
) -> Recommendation:
    from harness.health.project_tracker import resolve_project_tracker

    if step.path != ("tracker", "type"):
        return None
    effective = resolve_project_tracker(draft.repo).effective
    return (
        effective.type,
        "Тип определён по текущему окружению проекта (remote origin или локальный режим); при необходимости задайте явно.",
    )


_RECOMMENDERS: dict[
    str, Callable[[ConfigurationStep, ConfigurationDraft, ProjectFacts], Recommendation]
] = {
    "approval_policy": _approval_policy,
    "shell": _shell,
    "concurrency_budget": _concurrency,
    "transport": _transport,
    "human_approval_gate": _approval_gate,
    "worker_attestation_required": _attestation,
    "base_branch": _base_branch,
    "qa_gate_commands": _qa_commands,
    "verification_commands": _qa_commands,
    "type": _tracker_type,
}


@dataclass(frozen=True)
class ConfigurationStep:
    file: str
    path: tuple[str, ...]
    schema: JsonObject
    description: str
    required: bool
    root: JsonObject = field(default_factory=dict, compare=False, repr=False)

    @property
    def kind(self) -> str:
        kind = self.schema.get("type", "string")
        if isinstance(kind, list):
            kind = next((k for k in kind if k != "null"), "string")
        return str(kind)

    @property
    def is_section(self) -> bool:
        """An object with its own fields: edited field by field on the steps that follow."""
        return self.kind == "object" and isinstance(self.schema.get("properties"), dict)

    @property
    def is_map(self) -> bool:
        """An object keyed by free names (plans, profiles); each entry has steps of its own."""
        return (
            self.kind == "object"
            and not self.is_section
            and isinstance(self.schema.get("additionalProperties"), dict)
        )

    @property
    def has_menu(self) -> bool:
        """Edited through a menu (entries, items) as well as through JSON text."""
        return self.is_map or self.is_text_list

    @property
    def is_text_list(self) -> bool:
        items = self.schema.get("items")
        return (
            self.kind == "array"
            and not self.choices
            and isinstance(items, dict)
            and items.get("type") == "string"
        )

    @property
    def entry_schema(self) -> JsonObject:
        """Schema of a map entry, with its reference resolved."""
        node = self.schema.get("additionalProperties")
        return _resolve(node, self.root) if isinstance(node, dict) else {}

    @property
    def child_names(self) -> tuple[str, ...]:
        properties = self.schema.get("properties")
        return tuple(properties) if isinstance(properties, dict) else ()

    @property
    def required_names(self) -> tuple[str, ...]:
        required = self.schema.get("required")
        return tuple(str(n) for n in required) if isinstance(required, list) else ()

    @property
    def choices(self) -> tuple[object, ...]:
        values = (
            self.schema.get("items", {}).get("enum", [])
            if self.kind == "array"
            else self.schema.get("enum", [])
        )
        return tuple(values) if isinstance(values, list) else ()

    @property
    def pages(self) -> int:
        return -(-len(self.choices) // CHOICE_PAGE_SIZE)

    def choice_page(self, page: int) -> tuple[object, ...]:
        start = page * CHOICE_PAGE_SIZE
        return self.choices[start : start + CHOICE_PAGE_SIZE]

    def present(self, draft: ConfigurationDraft) -> bool:
        target: object = draft.documents[self.file]
        for part in self.path:
            if not isinstance(target, dict) or part not in target:
                return False
            target = target[part]
        return True

    def value(self, draft: ConfigurationDraft) -> object:
        target: object = draft.documents[self.file]
        for part in self.path:
            if not isinstance(target, dict) or part not in target:
                return copy.deepcopy(self.schema.get("default"))
            target = target[part]
        return copy.deepcopy(target)

    def recommendation(self, draft: ConfigurationDraft) -> tuple[object, str]:
        key = self.path[-1]
        facts = project_facts(draft.repo)
        rule = _RECOMMENDERS.get(key)
        if rule is not None:
            advice = rule(self, draft, facts)
            if advice is not None:
                return advice
        value = self.value(draft)
        if value is not None and self.present(draft):
            try:
                self.validate(value)
            except ValueError:
                return (
                    None,
                    "Текущее значение нарушает ограничения поля. Выберите допустимое значение с учётом проекта.",
                )
            return (
                value,
                "Сохранить текущую настройку проекта; без дополнительных данных нет основания автоматически её менять.",
            )
        return self.schema.get(
            "default"
        ), "Штатное значение схемы; проверьте его пригодность для вашего проекта."

    def effect(self, value: object) -> str:
        if isinstance(value, bool):
            return "Включает этот параметр." if value else "Отключает этот параметр."
        known = _EFFECTS.get(str(value))
        if isinstance(known, tuple):
            effect, limit, scenario = known
            return f"{effect} Ограничение: {limit} Когда выбирать: {scenario}"
        if known is not None:
            return known
        summary = self.description.split(". ")[0].rstrip(".")
        return f"Значение задаёт {'.'.join(self.path)}: {summary}."

    def recommended(self, draft: ConfigurationDraft) -> object:
        """The recommended value, to mark it in the list of options."""
        return self.recommendation(draft)[0]

    def parse(self, text: str) -> object:
        if self.kind == "string" and not self.choices:
            value: object = text
        else:
            try:
                value = parse_config_json(text)
            except (ValueError, RecursionError):
                raise ValueError("Введите корректное JSON-значение") from None
        self.validate(value)
        return value

    def validate(self, value: object) -> None:
        try:
            json.dumps(value, allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            raise ValueError("Введите корректное JSON-значение") from None
        problems = value_problems(value, self.schema, self.root)
        if problems:
            raise ValueError(problems[0])


def _resolve(node: JsonObject, schema: JsonObject) -> JsonObject:
    if "$ref" not in node:
        return node
    return {
        **resolve_ref(schema, node["$ref"]),
        **{k: v for k, v in node.items() if k != "$ref"},
    }


def skeleton(node: JsonObject, root: JsonObject, depth: int = 0) -> object:
    """A starting value for a new entry: defaults and required fields, so its steps can be filled."""
    node = _resolve(node, root)
    if "default" in node:
        return copy.deepcopy(node["default"])
    choices = node.get("enum")
    if isinstance(choices, list) and choices:
        return choices[0]
    kind = node.get("type", "string")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "string")
    if kind == "object":
        properties = node.get("properties")
        required = node.get("required")
        if (
            depth > 6
            or not isinstance(properties, dict)
            or not isinstance(required, list)
        ):
            return {}
        return {
            name: skeleton(properties[name], root, depth + 1)
            for name in required
            if name in properties and isinstance(properties[name], dict)
        }
    if kind == "array":
        return []
    if kind in ("integer", "number"):
        minimum = node.get("minimum")
        return minimum if isinstance(minimum, (int, float)) else 0
    if kind == "boolean":
        return False
    return ""


def steps_for(draft: ConfigurationDraft) -> list[ConfigurationStep]:
    result: list[ConfigurationStep] = []
    for name, document in draft.documents.items():
        schema = load_schema(draft.repo, name)

        def visit(
            node: JsonObject,
            path: tuple[str, ...],
            value: object,
            required: bool,
            inherited: str,
        ) -> None:
            node = _resolve(node, schema)
            description = _HELP.get(
                path[0] if len(path) == 1 else ".".join(path),
                str(node.get("description", inherited)),
            )
            props = node.get("properties")
            if node.get("type") == "object" and isinstance(props, dict):
                # Optional objects can be omitted, added or removed as a unit. An absent
                # optional parent must not force its required children into the draft.
                result.append(
                    ConfigurationStep(name, path, node, description, required, schema)
                )
                if not isinstance(value, dict) and not required:
                    return
                for key, child in props.items():
                    child_value = value.get(key) if isinstance(value, dict) else None
                    visit(
                        child,
                        (*path, key),
                        child_value,
                        key in node.get("required", []),
                        description,
                    )
                return
            result.append(
                ConfigurationStep(name, path, node, description, required, schema)
            )
            additional = node.get("additionalProperties")
            if (
                node.get("type") == "object"
                and isinstance(additional, dict)
                and isinstance(value, dict)
            ):
                for key, child_value in value.items():
                    visit(additional, (*path, key), child_value, True, description)

        for key, node in schema["properties"].items():
            if key == "$schema":
                continue
            visit(
                node, (key,), document.get(key), key in schema.get("required", []), key
            )
    return result


def step_for_problem(steps: list[ConfigurationStep], problem: str) -> int | None:
    """Index of the step a validation message is about, or ``None`` when unclear."""
    files = [name for name in ("project.json", "orchestration.json") if name in problem]
    best: tuple[int, int] | None = None
    for index, step in enumerate(steps):
        if files and step.file not in files:
            continue
        dotted = ".".join(step.path)
        if re.search(r"(?<![\w])" + re.escape(dotted) + r"(?![\w])", problem):
            if best is None or len(dotted) > best[0]:
                best = (len(dotted), index)
    return None if best is None else best[1]
