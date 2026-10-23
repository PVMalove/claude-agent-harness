"""The one owner of the failure-domain decision (ADR 0019); pure, no I/O.

Every failure at an orchestration step carries exactly one ``FailureDomain`` and the evidence that
shows it: a ``Failure`` with ``method`` (``structured``, ``heuristic``, ``approver`` or
``fallback``), ``matched`` (the field or pattern that decided) and ``source`` (where it was read).

Two invariants keep the classification fail-closed. ``unknown`` and ``fallback`` go together: a
failure nothing recognises is ``unknown``, and a recognised one is never ``fallback``. ``code_test``
needs a proven code or test defect, so only a ``structured`` field of a report, a stage or the
runtime, or an ``approver``, may name it; a text ``heuristic`` never does.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from harness.orchestration.core.constants import FailureDomain
from harness.orchestration.core.utils import JsonObject
from harness.orchestration.workflow import failure_classifier

METHODS = ("structured", "heuristic", "approver", "fallback")
# Methods that may prove a code or test defect.
_PROVING_METHODS = frozenset({"structured", "approver"})

# A category of ``failure_classifier`` (the runtime, #771, and the environment, #769) and its domain.
_CATEGORY_DOMAINS: dict[str, FailureDomain] = {
    **{
        category: FailureDomain.INFRASTRUCTURE
        for category, _ in failure_classifier.INFRASTRUCTURE_PATTERNS
        if category != "timeout"
    },
    "timeout": FailureDomain.TIMEOUT,
    "runtime-unavailable": FailureDomain.INFRASTRUCTURE,
    "read-only-filesystem": FailureDomain.ENVIRONMENT_PREPARATION,
    "permission-denied": FailureDomain.ENVIRONMENT_PREPARATION,
}
_STARTUP_ENVIRONMENT = frozenset({"worktree-mismatch", "model-mismatch"})


@dataclass(frozen=True)
class Failure:
    """One failure domain and the evidence that shows it."""

    domain: FailureDomain
    method: str
    matched: str
    source: str

    def __post_init__(self) -> None:
        """Refuse an unproven ``code_test``, a ``fallback`` that is not ``unknown`` and empty evidence."""
        if self.method not in METHODS:
            raise ValueError(f"unknown failure evidence method {self.method!r}")
        if (self.method == "fallback") != (self.domain == FailureDomain.UNKNOWN):
            raise ValueError("the fallback method and the unknown domain go together")
        if self.domain == FailureDomain.CODE_TEST and self.method not in _PROVING_METHODS:
            raise ValueError("code_test needs structured or approver evidence")
        if not self.matched.strip() or not self.source.strip():
            raise ValueError("failure evidence names what matched and where it was read")

    def to_json(self) -> JsonObject:
        """The record kept in routing records, stop evidence and startup assessments."""
        return {
            "domain": str(self.domain),
            "method": self.method,
            "matched": self.matched,
            "source": self.source,
        }


def unrecognised(matched: str, source: str) -> Failure:
    """The fail-closed answer: the cause is not proven, so it is ``unknown``."""
    return Failure(FailureDomain.UNKNOWN, "fallback", matched, source)


def of_runtime_error(error: object) -> Failure:
    """The domain a runtime error carries in its structured ``domain`` field."""
    domain = getattr(error, "domain", None)
    code = getattr(error, "code", None)
    known = {str(item) for item in FailureDomain} - {str(FailureDomain.UNKNOWN)}
    if isinstance(domain, str) and domain in known:
        return Failure(
            FailureDomain(domain),
            "structured",
            f"error.domain={domain} error.code={code}",
            "runtime",
        )
    return unrecognised(f"error.domain={domain!r} error.code={code!r}", "runtime")


def of_classification(found: Mapping[str, object], source: str) -> Failure:
    """The failure of a ``failure_classifier`` result."""
    category = str(found.get("category"))
    domain = _CATEGORY_DOMAINS.get(category)
    method = found.get("method")
    if domain is None or method not in {"structured", "heuristic"}:
        return unrecognised(f"category={category}", source)
    matched = (
        f"{found.get('field')}={found.get('value')}"
        if method == "structured" and "field" in found
        else str(found.get("matched") or f"category={category}")
    )
    return Failure(domain, str(method), matched, source)


def of_runtime_failure(failure: object) -> Failure:
    """The domain of a recorded runtime failure (``dispatch-status.runtime_failure``)."""
    source = "runtime_failure"
    if not isinstance(failure, Mapping):
        return unrecognised("no runtime failure record", source)
    found = failure_classifier.classify_runtime_failure(failure)
    if found is None:
        return unrecognised("no structured category or infrastructure pattern", source)
    return of_classification(found, source)


def of_startup(classification: str | None, runtime_failure: object) -> Failure:
    """The domain of a stopped startup, from ``assess_startup``'s classification."""
    source = "startup_assessment"
    matched = f"classification={classification}"
    if classification in _STARTUP_ENVIRONMENT:
        return Failure(FailureDomain.ENVIRONMENT_PREPARATION, "structured", matched, source)
    if isinstance(runtime_failure, Mapping) and runtime_failure.get("category") == classification:
        failure = of_classification(runtime_failure, source)
        if failure.domain != FailureDomain.UNKNOWN:
            return failure
    domain = _CATEGORY_DOMAINS.get(classification or "")
    if domain is not None:
        return Failure(domain, "structured", matched, source)
    return unrecognised(matched, source)


def independent_infrastructure_facts(stages: Mapping[str, object]) -> bool:
    """Whether ``qa_stages`` itself records a failed environment probe and only passing project-file
    checks: an environment diagnosis is never taken on trust from a category alone."""
    raw = stages.get("stages")
    entries = [e for e in raw if isinstance(e, Mapping)] if isinstance(raw, list) else []
    probes = [e for e in entries if e.get("stage") == "environment-probe"]
    checks = [e for e in entries if e.get("stage") == "project-file-check"]
    return (
        any(e.get("result") == "fail" for e in probes)
        and bool(checks)
        and all(e.get("result") == "pass" for e in checks)
    )


def _preparation(stages: Mapping[str, object]) -> Failure:
    """A QA preparation failure: proven only by what the clean-room runner recorded in stages."""
    source = "qa_stages"
    diagnosis = stages.get("diagnosis")
    category = diagnosis.get("category") if isinstance(diagnosis, Mapping) else None
    if stages.get("code_checks_started") != "not_started":
        return unrecognised("preparation failed and code_checks_started is not not_started", source)
    if category == "project-defect":
        return Failure(
            FailureDomain.CODE_TEST,
            "structured",
            "diagnosis.category=project-defect; code_checks_started=not_started",
            source,
        )
    if category == "infrastructure" and independent_infrastructure_facts(stages):
        return Failure(
            FailureDomain.ENVIRONMENT_PREPARATION,
            "structured",
            "diagnosis.category=infrastructure; failed environment-probe; every project-file-check passed",
            source,
        )
    return unrecognised(f"diagnosis.category={category!r} without independent proof", source)


def _gate(stages: Mapping[str, object], repo: Path | None) -> Failure:
    """A failed QA gate after a green preparation: the code, unless the environment refused a write."""
    source = "qa_stages"
    raw = stages.get("stages")
    failed = next(
        (
            entry
            for entry in (raw if isinstance(raw, list) else [])
            if isinstance(entry, Mapping)
            and entry.get("stage") == "gate"
            and entry.get("result") == "fail"
        ),
        None,
    )
    if failed is None:
        return unrecognised("failed_stage=gate without a failed gate stage", source)
    found = (
        failure_classifier.classify_environment(failed.get("diagnostics"), repo)
        if repo is not None
        else None
    )
    if found is not None:
        return of_classification(found, source)
    return Failure(
        FailureDomain.CODE_TEST,
        "structured",
        "failed_stage=gate; stage gate result=fail",
        source,
    )


def of_qa_stages(stages: object, *, repo: Path | None = None) -> Failure | None:
    """The domain of a QA report's failed stage; ``None`` when no stage failed."""
    if not isinstance(stages, Mapping):
        return None
    failed = stages.get("failed_stage")
    if failed == "preparation":
        return _preparation(stages)
    if failed == "gate":
        return _gate(stages, repo)
    return None
