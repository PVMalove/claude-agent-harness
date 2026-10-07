"""Подключаемые операционные интерфейсы координатора оркестрации (issue #250).

Ядро координатора сохраняет только переходы жизненного цикла, реестр, маршрутизацию, валидацию
подтверждений и инварианты candidate/base/Context Package. Всё, что наблюдает внешний мир —
работоспособность транспорта или среды верификации, классификация причин остановки роли,
объём потреблённого контекста и информирование человека — вынесено за небольшие интерфейсы здесь.
Каждый интерфейс по умолчанию использует неактивную встроенную реализацию «none».

Проект выбирает реализацию по имени в секции extensions файла .harness/orchestration.json.
Имя может быть встроенным, именем зарегистрированной через register реализации или формата
«module:attribute», указывающего на функцию-фабрику без аргументов в импортируемом модуле.
Выбранные имена замораживаются в каждом неизменяемом задании; ни один из этих интерфейсов
не добавляет инструментов модели и не изменяет системные промпты.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from ..errors import HarnessError

EXTENSION_KINDS = (
    "transport_health",
    "verification_environment_health",
    "retry_reason_classifier",
    "context_telemetry_provider",
    "human_notifier",
    "runtime_access",
)
DEFAULT_EXTENSION = "none"
EXTENSION_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*(:[A-Za-z_][A-Za-z0-9_]*)?$")
_METHOD = {
    "transport_health": "probe",
    "verification_environment_health": "probe",
    "retry_reason_classifier": "classify",
    "context_telemetry_provider": "observe",
    "human_notifier": "notify",
    "runtime_access": "observe",
}


class ExtensionError(HarnessError):
    """Выбранное расширение не существует или непригодно для использования."""


@dataclass(frozen=True)
class HealthObservation:
    """Структурированный факт о здоровье от рантайма; request_id сопоставляет его с транспортом."""

    healthy: bool
    detail: str
    request_id: str | None = None


@dataclass(frozen=True)
class ClassificationFacts:
    """Структурированные факты об остановившейся роли; никогда не свободный текст её отчёта."""

    stage: str
    outcome: str
    transport: HealthObservation | None
    verification: HealthObservation | None


@dataclass(frozen=True)
class ReasonHint:
    """Предлагаемая классификатором категория причины повтора. Координатор применяет к ней свои защитные правила маршрутизации."""

    category: str
    basis: str


@dataclass(frozen=True)
class ContextObservation:
    """Количество токенов, зафиксированное провайдером или рантаймом, но никогда не самой моделью."""

    observed_tokens: int
    source: str


@dataclass(frozen=True)
class AttentionEvent:
    """Событие привлечения внимания оператора при возникновении блокера или нештатной ситуации."""

    batch_id: str
    reason: str
    since: str
    last_safe_action: str
    recommended_human_action: str


@dataclass(frozen=True)
class RuntimeAccessObservation:
    """Native evidence for the reserved worker environment, never the parent process.

    The provider must observe the current environment and must apply settings through the
    supported native launch mechanism. It must preserve native permission approval.
    """

    plan_digest: str
    transport: str
    supported_modes: tuple[str, ...]
    effective_mode: str
    mechanism: str
    environment_id: str
    launch_id: str
    source: str
    observed_at: str
    hosts: tuple[str, ...]
    filesystem: tuple[tuple[str, str], ...]
    applied: bool = False
    dispatch_id: str | None = None
    handed_off: bool = False


class RuntimeAccess(Protocol):
    """Confirm and apply permissions for a specific native worker launch."""

    def observe(self, plan: Mapping[str, object], transport: str) -> RuntimeAccessObservation | None:
        ...

    def apply(self, brief: Mapping[str, object], observation: RuntimeAccessObservation) -> RuntimeAccessObservation | None:
        ...

    def handoff(self, brief: Mapping[str, object], observation: RuntimeAccessObservation,
                command: tuple[str, ...] | None) -> RuntimeAccessObservation | None:
        """Launch this exact worker natively and return its bound receipt.

        The implementation owns the native launch and preserves native approval. For external
        transport it integrates the supplied adapter command into that same launch. It must not
        merely apply permissions to another environment or return parent-process evidence.
        """
        ...


class TransportHealth(Protocol):
    """Интерфейс зондирования работоспособности транспорта диспетчеризации."""

    def probe(self, dispatch_id: str) -> HealthObservation | None:
        """Проверить работоспособность транспорта для указанного dispatch_id."""
        ...


class VerificationEnvironmentHealth(Protocol):
    """Интерфейс зондирования среды выполнения проверок."""

    def probe(self, dispatch_id: str) -> HealthObservation | None:
        """Проверить работоспособность среды проверок для указанного dispatch_id."""
        ...


class RetryReasonClassifier(Protocol):
    """Интерфейс классификации причины остановки роли для принятия решения о повторе."""

    def classify(self, facts: ClassificationFacts) -> ReasonHint | None:
        """Классифицировать причину завершения роли на основе структурированных фактов."""
        ...


class ContextTelemetryProvider(Protocol):
    """Интерфейс получения наблюдаемой телеметрии контекста токенов."""

    def observe(self, dispatch_id: str) -> ContextObservation | None:
        """Получить фактические данные об использовании токенов для указанного dispatch_id."""
        ...


class HumanNotifier(Protocol):
    """Интерфейс оповещения оператора-человека."""

    def notify(self, event: AttentionEvent) -> None:
        """Отправить оповещение о событии внимания человека."""
        ...


class _Inert:
    """Встроенная заглушка «none» для всех интерфейсов: ничего не наблюдает и не отправляет."""

    def probe(self, dispatch_id: str) -> HealthObservation | None:
        """Вернуть отсутствие наблюдений о здоровье."""
        return None

    def classify(self, facts: ClassificationFacts) -> ReasonHint | None:
        """Вернуть отсутствие подсказки классификации."""
        return None

    def observe(self, dispatch_id: str) -> ContextObservation | None:
        """Вернуть отсутствие телеметрии контекста."""
        return None

    def notify(self, event: AttentionEvent) -> None:
        """Игнорировать событие внимания оператора."""
        return None


_REGISTRY: dict[str, dict[str, object]] = {
    kind: {DEFAULT_EXTENSION: _Inert()} for kind in EXTENSION_KINDS
}


def _known_kind(kind: str) -> None:
    """Проверить, что переданный вид расширения зарегистрирован."""
    if kind not in _REGISTRY:
        raise ExtensionError(
            f"unknown extension kind {kind!r}",
            remedy=f"use one of: {', '.join(EXTENSION_KINDS)}",
        )


def register(kind: str, name: str, implementation: object) -> None:
    """Зарегистрировать реализацию под указанным именем для заданного вида интерфейса."""
    _known_kind(kind)
    if name == DEFAULT_EXTENSION or EXTENSION_NAME.fullmatch(name) is None:
        raise ExtensionError(
            f"invalid extension name {name!r}",
            remedy="use a name that is not 'none' and matches [A-Za-z_][A-Za-z0-9_.-]*",
        )
    _require_method(kind, name, implementation)
    _REGISTRY[kind][name] = implementation


def unregister(kind: str, name: str) -> None:
    """Удалить регистрацию расширения по имени."""
    if name != DEFAULT_EXTENSION and kind in _REGISTRY:
        _REGISTRY[kind].pop(name, None)


def _require_method(kind: str, name: str, implementation: object) -> None:
    """Проверить наличие требуемого вызываемого метода у реализации расширения."""
    if not callable(getattr(implementation, _METHOD[kind], None)):
        raise ExtensionError(
            f"extension {name!r} for {kind} has no {_METHOD[kind]}() method",
            remedy=f"implement {_METHOD[kind]}() on the {kind} extension",
        )


def _resolve(kind: str, name: str) -> object:
    """Разрешить и загрузить реализацию расширения по имени."""
    _known_kind(kind)
    registered = _REGISTRY[kind].get(name)
    if registered is not None:
        return registered
    module_name, separator, attribute = name.partition(":")
    if not separator or EXTENSION_NAME.fullmatch(name) is None:
        raise ExtensionError(
            f"extension {name!r} is not registered for {kind}",
            remedy=f"select 'none', a registered name or a module:factory for {kind} in .harness/orchestration.json",
        )
    try:
        factory = getattr(importlib.import_module(module_name), attribute)
        implementation = factory()
    except (
        Exception
    ) as exc:  # a project-supplied module may fail in any way; the run must fail closed
        raise ExtensionError(
            f"extension {name!r} for {kind} could not be loaded: {exc}",
            remedy=f"make {name} importable and its factory callable with no arguments, or select another extension",
        ) from exc
    _require_method(kind, name, implementation)
    _REGISTRY[kind][name] = implementation
    return implementation


def selected(config: Mapping[str, object]) -> dict[str, str]:
    """Словарь имён расширений, выбранных проектом, где каждое неуказанное расширение равно «none»."""
    configured = config.get("extensions")
    names = {kind: DEFAULT_EXTENSION for kind in EXTENSION_KINDS}
    if configured is None:
        return names
    if not isinstance(configured, dict):
        raise ExtensionError(
            "orchestration extensions must be an object",
            remedy="set extensions to an object of interface names",
        )
    for kind, name in configured.items():
        _known_kind(kind)
        if not isinstance(name, str) or (
            name != DEFAULT_EXTENSION and EXTENSION_NAME.fullmatch(name) is None
        ):
            raise ExtensionError(
                f"extension name for {kind} must be a string like 'none' or 'module:factory'",
                remedy=f"fix extensions.{kind} in .harness/orchestration.json",
            )
        names[kind] = name
    return names


def transport_health(name: str) -> TransportHealth:
    """Получить реализацию проверки работоспособности транспорта по имени."""
    return cast(TransportHealth, _resolve("transport_health", name))


def verification_environment_health(name: str) -> VerificationEnvironmentHealth:
    """Получить реализацию проверки среды проверок по имени."""
    return cast(
        VerificationEnvironmentHealth, _resolve("verification_environment_health", name)
    )


def retry_reason_classifier(name: str) -> RetryReasonClassifier:
    """Получить реализацию классификатора причин повтора по имени."""
    return cast(RetryReasonClassifier, _resolve("retry_reason_classifier", name))


def context_telemetry_provider(name: str) -> ContextTelemetryProvider:
    """Получить реализацию провайдера телеметрии контекста по имени."""
    return cast(ContextTelemetryProvider, _resolve("context_telemetry_provider", name))


def human_notifier(name: str) -> HumanNotifier:
    """Получить реализацию оповещения человека по имени."""
    return cast(HumanNotifier, _resolve("human_notifier", name))


def runtime_access(name: str) -> RuntimeAccess:
    """Resolve the native worker access implementation; none supplies no proof."""
    return cast(RuntimeAccess, _resolve("runtime_access", name))
