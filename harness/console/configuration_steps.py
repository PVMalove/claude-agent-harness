"""Schema-driven field navigation with contextual, deterministic recommendations."""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass

from harness.configuration import parse_config_json
from harness.json_types import JsonObject
from .configuration import ConfigurationDraft, PACKAGE, parse_object

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
_EFFECTS = {
    "manual_all": "Каждый шаг требует ручного решения; подходит для первой настройки.",
    "milestone": "Чистые промежуточные этапы принимает политика; QA, publish и риск требуют человека.",
    "low_risk": "Автоматическое принятие ограничено low_risk_paths; без путей автопуть не сработает.",
    "auto": "Больше автономности; нужны trusted, attestation и готовые проверки. PR/merge вручную.",
    "trusted": "Согласование через записанное evidence; подходящий режим для поддерживаемого auto.",
    "tty": "Согласование только интерактивно в терминале; несовместимо с auto.",
    "in-process": "Роль запускается в текущем runtime координатора.",
    "external": "Нужен подключённый внешний runtime adapter и его доступы.",
    "local": "Локальные Markdown-задачи; онлайн-проверки трекера пропускаются.",
    "github": "Задачи и PR в GitHub; используется текущая авторизация gh.",
    "gitlab": "Задачи и MR в GitLab/self-hosted; используется glab и явный host.",
    "bash": "Linux/macOS/WSL; выполняется bash -lc.",
    "powershell": "Native Windows; команды должны соответствовать PowerShell.",
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


@dataclass(frozen=True)
class ConfigurationStep:
    file: str
    path: tuple[str, ...]
    schema: JsonObject
    description: str
    required: bool

    @property
    def kind(self) -> str:
        kind = self.schema.get("type", "string")
        if isinstance(kind, list):
            kind = next((k for k in kind if k != "null"), "string")
        return str(kind)

    @property
    def choices(self) -> tuple[object, ...]:
        values = (
            self.schema.get("items", {}).get("enum", [])
            if self.kind == "array"
            else self.schema.get("enum", [])
        )
        return tuple(values) if isinstance(values, list) else ()

    def choice_page(self, page: int) -> tuple[object, ...]:
        return self.choices[page * 4 : page * 4 + 4]

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
        if key == "approval_policy":
            return (
                "manual_all",
                "Для первой настройки рекомендуем ручное согласование каждого шага; автоматизацию включайте после проверки проекта.",
            )
        if key == "shell":
            return (
                ("powershell" if os.name == "nt" else "bash"),
                "Рекомендация по ОС процесса консоли; для другой целевой среды выберите её оболочку.",
            )
        if key == "concurrency_budget":
            return (
                1,
                "Последовательная работа упрощает первую проверку и ограничивает потребление ресурсов.",
            )
        if key == "transport":
            return (
                "in-process",
                "Внешний transport требует отдельно настроенного адаптера.",
            )
        if key == "human_approval_gate":
            return (
                "trusted",
                "Режим поддерживает записанное согласование; для обязательного живого терминала выберите tty.",
            )
        if (
            key == "worker_attestation_required"
            and draft.documents[self.file].get("approval_policy") == "auto"
        ):
            return (
                True,
                "Для approval_policy=auto attestation обязательна; без неё контракт отклонит конфигурацию.",
            )
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
        return _EFFECTS.get(
            str(value),
            "Значение применяется к "
            + ".".join(self.path)
            + "; последствия описаны выше.",
        )

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
        kind = self.kind
        valid = {
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "array": isinstance(value, list),
            "object": isinstance(value, dict),
        }.get(kind, True)
        if value is None and "null" in self.schema.get("type", []):
            valid = True
        if not valid:
            raise ValueError(f"Ожидается тип {kind}")
        if self.kind != "array" and self.choices and value not in self.choices:
            raise ValueError("Выберите одно из допустимых значений")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in self.schema and value < self.schema["minimum"]:
                raise ValueError(f"Минимум: {self.schema['minimum']}")
            if "maximum" in self.schema and value > self.schema["maximum"]:
                raise ValueError(f"Максимум: {self.schema['maximum']}")
        if isinstance(value, str) and len(value) < self.schema.get("minLength", 0):
            raise ValueError("Значение слишком короткое")
        if isinstance(value, str):
            if len(value) > self.schema.get("maxLength", len(value)):
                raise ValueError("Значение слишком длинное")
            if (
                "pattern" in self.schema
                and re.search(self.schema["pattern"], value) is None
            ):
                raise ValueError("Значение не соответствует формату поля")
        if isinstance(value, list):
            if len(value) < self.schema.get("minItems", 0):
                raise ValueError("Недостаточно элементов списка")
            if self.schema.get("uniqueItems") and len(
                {json.dumps(v, sort_keys=True) for v in value}
            ) != len(value):
                raise ValueError("Элементы списка должны быть уникальны")
            item_schema = self.schema.get("items")
            if isinstance(item_schema, dict):
                item = ConfigurationStep(
                    self.file, self.path, item_schema, self.description, True
                )
                for element in value:
                    item.validate(element)


def _resolve(node: JsonObject, schema: JsonObject) -> JsonObject:
    ref = node.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return node
    target = schema
    for part in ref[2:].split("/"):
        target = target[part.replace("~1", "/").replace("~0", "~")]
    return {**target, **{k: v for k, v in node.items() if k != "$ref"}}


def steps_for(draft: ConfigurationDraft) -> list[ConfigurationStep]:
    result: list[ConfigurationStep] = []
    for name, document in draft.documents.items():
        schema_path = (
            draft.repo
            / ".harness"
            / (
                "project.schema.json"
                if name == "project.json"
                else "orchestration/orchestration.schema.json"
            )
        )
        if not schema_path.is_file():
            schema_path = PACKAGE / (
                "project/project.schema.json"
                if name == "project.json"
                else "orchestration/orchestration.schema.json"
            )
        schema = parse_object(schema_path.read_bytes(), "schema")

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
                    ConfigurationStep(name, path, node, description, required)
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
            result.append(ConfigurationStep(name, path, node, description, required))
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
