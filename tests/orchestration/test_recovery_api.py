"""The shared recovery status and action API (ADR 0026): its vocabulary, its diagnostics and its
refusals, on every platform."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness.json_types import JsonObject as Document
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
from harness.orchestration.ledger.lifecycle import _canonical
from harness.orchestration.runtime_client import RuntimeClientError
from tests.orchestration.runtime_store_support import ORCHESTRATION_ROOT, Recovery

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


# -- seam 1: the coordinator CLI against the real Rust runtime ----------------------------------

seam = pytest.mark.skipif(
    sys.platform == "win32", reason="the seam tests drive a Unix-socket runtime"
)
PROFILE = {
    "capabilities": ["architecture-analysis", "backend-development", "code-review"],
    "fallback": [],
    "known_limitations": ["none"],
}


@pytest.fixture
def recovery(tmp_path: Path) -> Iterator[Recovery]:
    found = Recovery(tmp_path)
    try:
        yield found
    finally:
        found.stop()


def _configure(recovery: Recovery, *, effort: str = "high", **extra: object) -> Path:
    # The configuration names the mandatory code-review gate, so its role manifest is installed.
    manifest = recovery.repo / ".harness/orchestration/roles/code-review.md"
    manifest.write_text(
        (ORCHESTRATION_ROOT / "roles" / "code-review.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    path = recovery.repo / ".harness/orchestration.json"
    runtimes = {"claude": {"profiles": ["p"], "model": "sonnet", "effort": effort}}
    path.write_text(
        json.dumps(
            {
                "provider_profiles": {"p": PROFILE},
                "assignment_plans": {
                    role: {"runtimes": runtimes}
                    for role in ("architect", "developer", "code-review")
                },
                "concurrency_budget": 1,
                "verification_commands": ["true"],
                **extra,
            }
        ),
        encoding="utf-8",
    )
    return path


def _check(document: Document, name: str) -> Document:
    return next(item for item in document["checks"] if item["check"] == name)


def _status(recovery: Recovery, *extra: str, python: str | None = None) -> Document:
    result, document = recovery.recovery("status", *extra, python=python)
    assert result.returncode == 0, result.stderr
    assert document is not None
    return document


def _edit_batch(recovery: Recovery, **changes: object) -> None:
    batch = recovery.records(recovery.batch_path)[recovery.batch_path]
    recovery.replace(recovery.batch_path, {**batch, **changes})


@seam
def test_status_of_a_healthy_batch_is_resumable_and_read_only(recovery: Recovery) -> None:
    before = recovery.audit_records()

    document = _status(recovery)

    assert document["resumable"] is True
    assert {item["status"] for item in document["checks"]} == {"passed"}
    assert (document["hold"], document["intent"]) == (None, None)
    assert document["next"]["command"] == f"recovery resume --batch {recovery.batch}"
    assert recovery.audit_records() == before


@seam
def test_status_shows_a_releasable_hold_as_pending(recovery: Recovery) -> None:
    recovery.hold("unknown-value", "batch state 'parked' is unknown")

    document = _status(recovery)

    assert _check(document, "store")["status"] == "pending"
    assert document["hold"]["reason"] == "unknown-value"
    assert document["hold"]["releasable"] is True
    assert document["resumable"] is True


@seam
def test_an_unreadable_hold_record_blocks_without_a_lifecycle_write(
    recovery: Recovery,
) -> None:
    recovery.write(
        {
            "op": "create",
            "path": recovery.hold_path,
            "content": _canonical({"batch_id": recovery.batch, "contract": "runtimeState.v1"}),
        }
    )
    revision = recovery.revisions(recovery.batch_path)[recovery.batch_path]

    document = _status(recovery)
    resumed, refused = recovery.recovery("resume")

    assert _check(document, "store")["status"] == "refused"
    assert document["hold"]["reason"] == "unreadable-record"
    assert document["hold"]["releasable"] is False
    assert resumed.returncode == 2
    assert refused is not None and refused["steps"] == []
    assert recovery.revisions(recovery.batch_path)[recovery.batch_path] == revision
    gated = recovery.cli("dispatch", "status", "--batch", recovery.batch)
    assert gated.returncode == 0, gated.stderr


@seam
def test_an_unknown_batch_is_a_refused_check_not_an_exception(recovery: Recovery) -> None:
    result = recovery.cli("recovery", "status", "--batch", "batch-unknown")

    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert _check(document, "batch")["status"] == "refused"
    assert _check(document, "batch")["evidence"] == {"present": False}
    assert document["resumable"] is False
    action = recovery.cli("recovery", "resume", "--batch", "batch-unknown")
    assert action.returncode == 2
    assert "holds no batch batch-unknown" in action.stderr


@seam
def test_a_missing_snapshot_is_refused_with_its_pointer(recovery: Recovery) -> None:
    missing = (
        "from harness.orchestration import pinned_configuration\n"
        "from harness.orchestration.core.utils import CoordinatorError\n"
        "def _missing(dispatch):\n"
        "    raise CoordinatorError('the brief pins no configuration snapshot',\n"
        "                           remedy='restore the approved brief')\n"
        "pinned_configuration.read_snapshot = _missing\n"
    )

    document = _status(recovery, python=missing)

    check = _check(document, "snapshot")
    assert check["status"] == "refused"
    [diagnostic] = check["diagnostics"]
    assert diagnostic["code"] == "integrity-failure"
    assert diagnostic["pointer"] == (
        f"/dispatches/{recovery.dispatch}/orchestration_policy/configuration"
    )
    assert "dispatch cancel" in diagnostic["remedy"]


@seam
def test_a_changed_harness_runtime_without_its_snapshot_is_refused(
    recovery: Recovery,
) -> None:
    changed = (
        "from harness.orchestration.core import workspace\n"
        "workspace._runtime_matches = lambda *_: False\n"
    )

    document = _status(recovery, python=changed)

    check = _check(document, "runtime")
    assert check["status"] == "refused"
    [diagnostic] = check["diagnostics"]
    assert diagnostic["code"] == "integrity-failure"
    assert document["resumable"] is False
    assert document["next"]["command"] is None


@seam
def test_a_batch_on_its_pinned_runtime_snapshot_passes_the_runtime_check(
    recovery: Recovery,
) -> None:
    pinned = (
        "from pathlib import Path\n"
        "from harness.orchestration.core import workspace\n"
        "workspace._runtime_matches = lambda *_: False\n"
        "workspace._pinned_runtime_entry = lambda *_: Path(workspace.__file__)\n"
    )

    document = _status(recovery, python=pinned)

    check = _check(document, "runtime")
    assert check["status"] == "passed"
    assert check["evidence"]["runs_on"] == "pinned-snapshot"
    assert document["resumable"] is True


@seam
def test_a_stale_context_package_of_an_open_brief_is_refused(recovery: Recovery) -> None:
    _edit_batch(recovery, integration_base_commit="f" * 40)

    document = _status(recovery)
    resumed, refused = recovery.recovery("resume")

    check = _check(document, "context")
    assert check["status"] == "refused"
    [diagnostic] = check["diagnostics"]
    assert diagnostic["code"] == "stale-evidence"
    assert diagnostic["pointer"] == f"/dispatches/{recovery.dispatch}/context_package_id"
    assert diagnostic["expected"]["base_commit"] == "f" * 40
    assert resumed.returncode == 2
    assert refused is not None
    assert {item["code"] for item in refused["refusals"]} == {"stale-evidence"}


@seam
def test_the_context_check_shows_the_pinned_base_and_candidate(recovery: Recovery) -> None:
    check = _check(_status(recovery), "context")

    assert check["status"] == "passed"
    [evidence] = check["evidence"]["briefs"]
    assert evidence["dispatch_id"] == recovery.dispatch
    assert evidence["base_commit"] == check["evidence"]["current_base"]
    assert evidence["stale"] is False


@seam
def test_an_expired_approval_of_an_unsent_brief_is_refused(recovery: Recovery) -> None:
    _configure(recovery, approval_ttl_seconds=60)

    check = _check(_status(recovery), "approval")

    assert check["status"] == "refused"
    assert [item["code"] for item in check["diagnostics"]][0] == "approval-stale"


@seam
def test_open_attention_is_shown_and_never_resolved(recovery: Recovery) -> None:
    _edit_batch(
        recovery,
        needs_attention=True,
        attention_reason="stale-dispatch",
        attention_since="2026-10-29T00:00:00+00:00",
        last_safe_action="dispatch heartbeat",
        recommended_human_action="check the worker",
    )

    document = _status(recovery)
    recovery.recovery("resume")

    check = _check(document, "attention")
    assert check["status"] == "refused"
    assert "batch attention resolve" in check["diagnostics"][0]["remedy"]
    batch = recovery.records(recovery.batch_path)[recovery.batch_path]
    assert batch["needs_attention"] is True


@seam
def test_config_diagnostics_carry_a_pointer_redacted_values_and_a_suggestion(
    recovery: Recovery,
) -> None:
    _configure(recovery, effort=" HIGH ")

    document = _status(recovery)

    [advisory] = [
        item
        for item in document["diagnostics"]
        if item["advisory"] is not None and "/architect/" in str(item["pointer"])
    ]
    assert advisory["code"] == "config-advisory"
    assert advisory["pointer"] == "/assignment_plans/architect/runtimes/claude/effort"
    assert (advisory["received_redacted"], advisory["suggestion"]) == (" HIGH ", "high")
    assert advisory["fatal"] is False
    assert {item["action"]: item["available"] for item in document["actions"]}["fix"]


@seam
def test_a_secret_in_the_configuration_is_never_echoed(recovery: Recovery) -> None:
    secret = "ghp_" + "a" * 36
    _configure(recovery, provider_note=f"token={secret}")

    result, document = recovery.recovery("status")

    assert document is not None
    assert _check(document, "config")["status"] == "refused"
    assert secret not in result.stdout + result.stderr


@seam
def test_fix_previews_then_applies_through_the_transaction_without_resuming(
    recovery: Recovery,
) -> None:
    from harness.console.configuration import default_document

    (recovery.repo / ".harness/project.json").write_text(
        json.dumps(default_document(recovery.repo, "project.json")), encoding="utf-8"
    )
    path = _configure(recovery, effort=" HIGH ")
    recovery.hold()
    authored = path.read_bytes()
    hold = recovery.records(recovery.hold_path)

    preview_run, preview = recovery.recovery("fix")

    assert preview_run.returncode == 0, preview_run.stderr
    assert preview is not None and preview["applied"] is False
    assert "HIGH" in preview["diff"]
    assert path.read_bytes() == authored

    applied_run, applied = recovery.recovery("fix", "--apply")

    assert applied_run.returncode == 0, applied_run.stderr
    assert applied is not None
    assert (applied["applied"], applied["resumed"]) == (True, False)
    runtime = json.loads(path.read_bytes())["assignment_plans"]["architect"]
    assert runtime["runtimes"]["claude"]["effort"] == "high"
    backup = recovery.repo / ".harness/config_versions" / applied["backup_version"]
    assert (backup / "orchestration.json").read_bytes() == authored
    assert recovery.records(recovery.hold_path) == hold
    assert _status(recovery)["intent"]["action"] == "fix"


@seam
def test_reload_validates_and_never_starts_a_worker(recovery: Recovery) -> None:
    status_path = f"dispatch-status/{recovery.dispatch}.json"
    before = recovery.records(status_path)

    result, reloaded = recovery.recovery("reload")

    assert result.returncode == 0, result.stderr
    assert reloaded is not None
    assert (reloaded["outcome"], reloaded["worker_started"]) == ("done", False)
    assert reloaded["next"]["command"] == f"recovery resume --batch {recovery.batch}"
    assert recovery.records(status_path) == before

    _configure(recovery, effort="hgh")
    refused_run, refused = recovery.recovery("reload")

    assert refused_run.returncode == 2
    assert refused is not None and refused["outcome"] == "refused"
    pointers = {item["pointer"] for item in refused["diagnostics"]}
    assert "/assignment_plans/architect/runtimes/claude/effort" in pointers
    intent = _status(recovery)["intent"]
    assert (intent["action"], intent["status"]) == ("reload", "refused")


@seam
def test_reload_refuses_an_unsupported_runtime_declaration(recovery: Recovery) -> None:
    path = _configure(recovery)
    config = json.loads(path.read_bytes())
    config["assignment_plans"]["developer"]["runtimes"]["claude"][
        "system_fields_contract"
    ] = "systemFields.v2"
    path.write_text(json.dumps(config), encoding="utf-8")

    result, reloaded = recovery.recovery("reload")

    assert result.returncode == 2
    assert reloaded is not None and reloaded["outcome"] == "refused"
    assert any(
        str(item["pointer"]).startswith("/assignment_plans/developer/runtimes/claude")
        for item in reloaded["diagnostics"]
    )


@seam
def test_reload_refuses_a_runtime_without_the_recovery_feature(recovery: Recovery) -> None:
    older = (
        "from harness.orchestration import runtime_client\n"
        "runtime_client.PROTOCOL_VERSION = (1, 4)\n"
    )

    result, document = recovery.recovery("status", python=older)

    assert result.returncode == 0, result.stderr
    assert document is not None
    check = _check(document, "compatibility")
    assert check["status"] == "refused"
    assert check["diagnostics"][0]["code"] == "runtime-compatibility"
    action = recovery.cli("recovery", "reload", "--batch", recovery.batch, python=older)
    assert action.returncode == 2
    assert "update harness-runtime" in action.stderr


@seam
def test_skip_hides_only_an_allowlisted_advisory_for_one_session(recovery: Recovery) -> None:
    _configure(recovery, effort=" HIGH ")
    document = _status(recovery)
    advisory = next(item for item in document["diagnostics"] if item["advisory"])
    fatal = next(
        (item for item in document["diagnostics"] if item["fatal"]), None
    )

    hidden = _status(recovery, "--skip", advisory["id"])

    assert hidden["skipped_advisories"] == [advisory["id"]]
    assert advisory["id"] not in {item["id"] for item in hidden["diagnostics"]}
    assert _status(recovery)["skipped_advisories"] == []
    if fatal is not None:
        refused = recovery.cli(
            "recovery", "status", "--batch", recovery.batch, "--skip", fatal["id"]
        )
        assert refused.returncode == 2
        assert "not a skippable advisory" in refused.stderr


@seam
def test_abort_flushes_its_intent_and_changes_nothing_else(recovery: Recovery) -> None:
    recovery.hold()
    batch = recovery.records(recovery.batch_path)
    hold = recovery.records(recovery.hold_path)

    result, aborted = recovery.recovery("abort")

    assert result.returncode == 0, result.stderr
    assert aborted is not None
    assert (aborted["abandoned"], aborted["worker_stopped"], aborted["worktree_cleaned"]) == (
        False,
        False,
        False,
    )
    assert (aborted["intent"]["action"], aborted["intent"]["status"]) == ("abort", "aborted")
    assert aborted["hold"]["reason"] == "unknown-value"
    assert recovery.records(recovery.batch_path) == batch
    assert recovery.records(recovery.hold_path) == hold


@seam
@pytest.mark.parametrize(
    "raised", ["KeyboardInterrupt", "EOFError", "asyncio.CancelledError"]
)
def test_an_interrupted_action_keeps_its_intent_for_a_restart(
    recovery: Recovery, raised: str
) -> None:
    interrupt = (
        "import asyncio\n"
        "from harness.orchestration import recovery_api\n"
        "def _interrupted(self):\n"
        f"    raise {raised}()\n"
        "recovery_api.Survey._config = _interrupted\n"
    )

    result, document = recovery.recovery("reload", python=interrupt)

    assert result.returncode == 2
    assert document is None
    assert "interrupted" in result.stderr
    intent = _status(recovery)["intent"]
    assert (intent["action"], intent["status"]) == ("reload", "interrupted")


@seam
def test_an_internal_exception_is_never_shown_as_a_configuration_typo(
    recovery: Recovery,
) -> None:
    broken = (
        "from harness.orchestration import recovery_api\n"
        "def _broken(self):\n"
        "    raise RuntimeError('token=hunter2-secret broke')\n"
        "recovery_api.Survey._config = _broken\n"
    )

    result, document = recovery.recovery("reload", python=broken)

    assert result.returncode == 2
    assert document is None
    assert "internal invariant" in result.stderr
    assert "hunter2-secret" not in result.stderr
    intent = _status(recovery)["intent"]
    assert intent["status"] == "refused"
    assert intent["correlation_id"] in result.stderr
    assert "config-invalid" not in result.stderr


@seam
def test_an_unreadable_batch_shows_the_evidence_of_its_store_row(recovery: Recovery) -> None:
    _edit_batch(recovery, goal="another goal than the plan")

    document = _status(recovery)

    check = _check(document, "batch")
    assert check["status"] == "refused"
    assert check["evidence"]["present"] is True
    assert check["evidence"]["state"] == "active"
    assert check["evidence"]["hold_reason"] == "unreadable-record"
    assert isinstance(check["evidence"]["revision"], int)
    assert document["lifecycle"] is None
