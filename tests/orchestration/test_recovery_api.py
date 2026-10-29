"""The shared recovery status and action API (ADR 0026): its vocabulary, its diagnostics and its
refusals, on every platform."""

from __future__ import annotations

import pytest

from harness.errors import HarnessError
from harness.orchestration import recovery_api
from harness.orchestration.core.system_fields import load_registry
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.ledger.ledger_ops import LedgerBusyError
from harness.orchestration.operational_hold import OperationalHoldError
from harness.orchestration.recovery_api import (
    RecoveryDiagnostic,
    RecoveryInternalError,
    RecoverySession,
)
from harness.orchestration.runtime_client import RuntimeClientError

# -- unit: the vocabulary is the registry's ------------------------------------------------------


@pytest.mark.parametrize(
    ("constant", "field"),
    [
        ("ACTIONS", "recovery.action"),
        ("CHECKS", "recovery.check"),
        ("CHECK_STATUSES", "recovery.check_status"),
        ("DIAGNOSTIC_CODES", "recovery.diagnostic_code"),
        ("OUTCOMES", "recovery.outcome"),
        ("INTENT_STATUSES", "recovery.intent_status"),
        ("ADVISORIES", "recovery.advisory"),
    ],
)
def test_the_constants_are_the_registry_values(constant: str, field: str) -> None:
    assert getattr(recovery_api, constant) == load_registry().fields[field].allowed


def test_a_diagnostic_outside_the_registry_is_refused() -> None:
    with pytest.raises(ValueError, match="unregistered"):
        RecoveryDiagnostic("unknown-code", "config", "x")
    with pytest.raises(ValueError, match="unregistered"):
        RecoveryDiagnostic(
            "config-advisory", "config", "x", severity="warning", advisory="unconfirmed-contract"
        )


# -- unit: diagnostics ---------------------------------------------------------------------------


def test_the_id_of_a_diagnostic_is_stable_and_names_what_it_is() -> None:
    first = RecoveryDiagnostic("config-invalid", "config", "bad effort", location="/a")
    again = RecoveryDiagnostic("config-invalid", "config", "bad effort", location="/a")
    other = RecoveryDiagnostic("config-invalid", "config", "bad effort", location="/b")

    assert first.id == again.id
    assert first.id != other.id
    assert len(first.id) == 16


def test_a_diagnostic_never_echoes_a_secret_or_an_absolute_path() -> None:
    secret = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
    document = RecoveryDiagnostic(
        "config-invalid",
        "config",
        f"api_key={secret} read from /home/someone/repo/.harness/orchestration.json",
        remedy=f"rotate token: {secret}",
        received={"api_key": secret},
    ).to_dict()

    text = repr(document)
    assert secret not in text
    assert "/home/someone" not in text
    assert document["fatal"] is True
    assert document["pointer"] is None


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("runtime_unavailable", "storage-unavailable"),
        ("store_busy", "storage-unavailable"),
        ("revision_conflict", "concurrency-conflict"),
        ("idempotency_conflict", "concurrency-conflict"),
        ("invariant_violation", "integrity-failure"),
        ("not_found", "integrity-failure"),
        ("protocol_unsupported", "runtime-compatibility"),
        ("internal", "internal-error"),
    ],
)
def test_a_runtime_error_keeps_its_code_as_a_diagnostic_code(code: str, expected: str) -> None:
    error = RuntimeClientError(code, "refused", remedy="repeat", domain="tooling")

    diagnostic = RecoveryDiagnostic.from_error("store", error)

    assert diagnostic.code == expected
    assert diagnostic.remedy == f"[{code}] repeat"


def test_the_errors_of_the_core_keep_their_own_code() -> None:
    busy = LedgerBusyError("the store is busy", remedy="repeat")
    held = OperationalHoldError(
        "integrity-failure", "hold:unknown-value", "held", remedy="recovery resume"
    )
    internal = RecoveryInternalError("reload", RuntimeError("boom"), "c-1")

    assert RecoveryDiagnostic.from_error("store", busy).code == "storage-unavailable"
    assert RecoveryDiagnostic.from_error("batch", held).code == "integrity-failure"
    assert RecoveryDiagnostic.from_error("config", internal).code == "internal-error"
    plain = CoordinatorError("the batch record is not valid JSON", remedy="x")
    assert RecoveryDiagnostic.from_error("batch", plain).code == "integrity-failure"
    assert RecoveryDiagnostic.from_error("batch", HarnessError("y", remedy="z")).code == (
        "integrity-failure"
    )


def test_an_internal_error_is_a_coordinator_error_with_its_correlation_id() -> None:
    error = RecoveryInternalError("resume", KeyError("token=hunter2-secret"), "c-42")

    assert isinstance(error, CoordinatorError)
    assert error.code == "internal-error"
    assert error.step == "recovery-resume"
    assert error.diagnostics["correlation_id"] == "c-42"
    assert error.diagnostics["internal_invariant"] is True
    assert "c-42" in error.message and "c-42" in error.remedy
    assert "hunter2-secret" not in error.message


# -- unit: skip ----------------------------------------------------------------------------------


def _document(*diagnostics: RecoveryDiagnostic) -> JsonObject:
    return {"diagnostics": [item.to_dict() for item in diagnostics]}


def test_skip_hides_an_allowlisted_advisory_for_one_session_only() -> None:
    advisory = RecoveryDiagnostic(
        "config-advisory",
        "config",
        "effort 'High' is normalized to 'high'",
        severity="warning",
        advisory="config-normalized",
    )
    session = RecoverySession()

    hidden = recovery_api.skip(session, _document(advisory), advisory.id)

    assert hidden.skipped == frozenset({advisory.id})
    assert session.skipped == frozenset()


@pytest.mark.parametrize(
    "diagnostic",
    [
        RecoveryDiagnostic("config-invalid", "config", "effort 'huge' is not allowed"),
        RecoveryDiagnostic(
            "live-worker", "liveness", "the worker may still run", severity="warning"
        ),
    ],
)
def test_skip_refuses_a_fatal_or_unlisted_diagnostic(diagnostic: RecoveryDiagnostic) -> None:
    with pytest.raises(CoordinatorError, match="not a skippable advisory"):
        recovery_api.skip(RecoverySession(), _document(diagnostic), diagnostic.id)


def test_skip_refuses_a_diagnostic_the_status_does_not_list() -> None:
    with pytest.raises(CoordinatorError, match="no diagnostic 0123456789abcdef"):
        recovery_api.skip(RecoverySession(), _document(), "0123456789abcdef")
