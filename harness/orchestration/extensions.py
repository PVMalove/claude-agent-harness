"""Pluggable operational interfaces of the orchestration coordinator (issue #250).

The coordinator core keeps only lifecycle transitions, the ledger, routing, approval validation and
its candidate/base/Context Package invariants.  Everything that observes the outside world -- how
healthy the transport or the verification environment is, how a stopped role should be classified,
how much context a worker has consumed, and how a human is told -- sits behind one small interface
here.  Every interface defaults to an inert built-in named ``none``.

A project selects an implementation by name under ``extensions`` in ``.harness/orchestration.json``.
A name is either a built-in, an implementation a host process registered with :func:`register`, or
``module:attribute`` naming a zero-argument factory in an importable module.  The selected names are
frozen into every immutable brief; none of these interfaces adds a model tool or changes a prompt.
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
)
DEFAULT_EXTENSION = "none"
EXTENSION_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*(:[A-Za-z_][A-Za-z0-9_]*)?$")
_METHOD = {
    "transport_health": "probe",
    "verification_environment_health": "probe",
    "retry_reason_classifier": "classify",
    "context_telemetry_provider": "observe",
    "human_notifier": "notify",
}


class ExtensionError(HarnessError):
    """A selected extension does not exist or is not usable."""


@dataclass(frozen=True)
class HealthObservation:
    """A structured health fact from the runtime; ``request_id`` correlates it with the transport."""

    healthy: bool
    detail: str
    request_id: str | None = None


@dataclass(frozen=True)
class ClassificationFacts:
    """Structured facts about a stopped role; never the free text of its report."""

    stage: str
    outcome: str
    transport: HealthObservation | None
    verification: HealthObservation | None


@dataclass(frozen=True)
class ReasonHint:
    """A classifier's proposed retry reason category.  The coordinator still applies its own routing
    guards to it, exactly as it does to a category an approver names."""

    category: str
    basis: str


@dataclass(frozen=True)
class ContextObservation:
    """A token count observed by the provider or runtime, never reported by the model itself."""

    observed_tokens: int
    source: str


@dataclass(frozen=True)
class AttentionEvent:
    batch_id: str
    reason: str
    since: str
    last_safe_action: str
    recommended_human_action: str


class TransportHealth(Protocol):
    def probe(self, dispatch_id: str) -> HealthObservation | None: ...


class VerificationEnvironmentHealth(Protocol):
    def probe(self, dispatch_id: str) -> HealthObservation | None: ...


class RetryReasonClassifier(Protocol):
    def classify(self, facts: ClassificationFacts) -> ReasonHint | None: ...


class ContextTelemetryProvider(Protocol):
    def observe(self, dispatch_id: str) -> ContextObservation | None: ...


class HumanNotifier(Protocol):
    def notify(self, event: AttentionEvent) -> None: ...


class _Inert:
    """The ``none`` built-in of every interface: observes nothing, says nothing."""

    def probe(self, dispatch_id: str) -> HealthObservation | None:
        return None

    def classify(self, facts: ClassificationFacts) -> ReasonHint | None:
        return None

    def observe(self, dispatch_id: str) -> ContextObservation | None:
        return None

    def notify(self, event: AttentionEvent) -> None:
        return None


_REGISTRY: dict[str, dict[str, object]] = {
    kind: {DEFAULT_EXTENSION: _Inert()} for kind in EXTENSION_KINDS
}


def _known_kind(kind: str) -> None:
    if kind not in _REGISTRY:
        raise ExtensionError(
            f"unknown extension kind {kind!r}",
            remedy=f"use one of: {', '.join(EXTENSION_KINDS)}",
        )


def register(kind: str, name: str, implementation: object) -> None:
    """Make ``implementation`` selectable by ``name`` for one interface."""
    _known_kind(kind)
    if name == DEFAULT_EXTENSION or EXTENSION_NAME.fullmatch(name) is None:
        raise ExtensionError(
            f"invalid extension name {name!r}",
            remedy="use a name that is not 'none' and matches [A-Za-z_][A-Za-z0-9_.-]*",
        )
    _require_method(kind, name, implementation)
    _REGISTRY[kind][name] = implementation


def unregister(kind: str, name: str) -> None:
    if name != DEFAULT_EXTENSION and kind in _REGISTRY:
        _REGISTRY[kind].pop(name, None)


def _require_method(kind: str, name: str, implementation: object) -> None:
    if not callable(getattr(implementation, _METHOD[kind], None)):
        raise ExtensionError(
            f"extension {name!r} for {kind} has no {_METHOD[kind]}() method",
            remedy=f"implement {_METHOD[kind]}() on the {kind} extension",
        )


def _resolve(kind: str, name: str) -> object:
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
    """The extension names a project selects, every unselected interface being ``none``."""
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
    return cast(TransportHealth, _resolve("transport_health", name))


def verification_environment_health(name: str) -> VerificationEnvironmentHealth:
    return cast(
        VerificationEnvironmentHealth, _resolve("verification_environment_health", name)
    )


def retry_reason_classifier(name: str) -> RetryReasonClassifier:
    return cast(RetryReasonClassifier, _resolve("retry_reason_classifier", name))


def context_telemetry_provider(name: str) -> ContextTelemetryProvider:
    return cast(ContextTelemetryProvider, _resolve("context_telemetry_provider", name))


def human_notifier(name: str) -> HumanNotifier:
    return cast(HumanNotifier, _resolve("human_notifier", name))
