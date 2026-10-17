"""The shared recovery status and action API (issue #684), on a real ledger and store."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from harness.orchestration import coordinator, operational_hold, recovery_api
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.recovery_api import (
    RecoveryInternalError,
    RecoveryInterrupted,
    RecoverySession,
)
from harness.orchestration.runtime_state_store import RuntimeStateStore
from tests.orchestration import test_recovery
from tests.orchestration.test_coordinator import pinned_sha256, repin_entry

Fixture = test_recovery.RecoveryTests
PROFILE = {
    "capabilities": ["architecture-analysis", "backend-development", "code-review"],
    "fallback": [],
    "known_limitations": ["none"],
}


def _configure(fixture: Fixture, *, effort: str = "high", **extra: object) -> Path:
    path = fixture.repo / ".harness/orchestration.json"
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


def _approved_architect(fixture: Fixture) -> JsonObject:
    batch = fixture._create_batch()
    brief: JsonObject = fixture._dispatch(batch["batch_id"], "architect")["brief"]
    return brief


def _status(fixture: Fixture, batch_id: str, **options: object) -> JsonObject:
    return recovery_api.status(fixture.repo, fixture.state_dir, batch_id, **options)  # type: ignore[arg-type]


def _check(document: JsonObject, name: str) -> JsonObject:
    return next(item for item in document["checks"] if item["check"] == name)


def _store(fixture: Fixture) -> RuntimeStateStore:
    return RuntimeStateStore(fixture.repo, fixture.state_dir)


def _store_bytes(fixture: Fixture, batch_id: str) -> bytes | None:
    path = _store(fixture).path(batch_id)
    return path.read_bytes() if path.exists() else None


def _hold(fixture: Fixture, batch_id: str) -> None:
    operational_hold.hold(
        _store(fixture), batch_id, "runtime-compatibility", "the adapter speaks v2"
    )


# -- status --------------------------------------------------------------------------------------


def test_status_of_a_healthy_batch_is_resumable_and_read_only(
    recovery: Fixture,
) -> None:
    brief = _approved_architect(recovery)
    ledger, store = recovery._ledger_bytes(), _store_bytes(recovery, brief["batch_id"])

    document = _status(recovery, brief["batch_id"])

    assert document["contract"] == "recoveryStatus.v1"
    assert document["resumable"] is True
    assert {item["check"] for item in document["checks"]} == set(recovery_api.CHECKS)
    assert all(item["status"] != "refused" for item in document["checks"])
    assert document["lifecycle"]["state"] == "active"
    assert document["hold"] is None and document["intent"] is None
    actions = {item["action"]: item["available"] for item in document["actions"]}
    assert actions == {
        "fix": False,
        "reload": True,
        "resume": True,
        "skip": False,
        "abort": True,
    }
    assert document["next"]["command"] == f"recovery resume --batch {brief['batch_id']}"
    assert recovery._ledger_bytes() == ledger
    assert _store_bytes(recovery, brief["batch_id"]) == store


def test_status_shows_a_releasable_hold_as_pending(recovery: Fixture) -> None:
    brief = _approved_architect(recovery)
    _hold(recovery, brief["batch_id"])

    document = _status(recovery, brief["batch_id"])

    assert document["hold"]["reason"] == "runtime-compatibility"
    assert document["hold"]["releasable"] is True
    assert _check(document, "store")["status"] == "pending"
    assert document["resumable"] is True


def test_an_unreadable_store_record_blocks_without_a_lifecycle_write(
    recovery: Fixture,
) -> None:
    brief = _approved_architect(recovery)
    _hold(recovery, brief["batch_id"])
    _store(recovery).path(brief["batch_id"]).write_text("{", encoding="utf-8")
    ledger = recovery._ledger_bytes()

    document = _status(recovery, brief["batch_id"])

    assert _check(document, "store")["status"] == "refused"
    assert document["hold"]["reason"] == "unreadable-record"
    assert document["hold"]["releasable"] is False
    assert document["resumable"] is False
    [diagnostic] = _check(document, "store")["diagnostics"]
    assert diagnostic["code"] == "integrity-failure"
    assert recovery._ledger_bytes() == ledger


def test_an_unknown_batch_is_a_refused_check_not_an_exception(
    recovery: Fixture,
) -> None:
    recovery._create_batch()

    document = _status(recovery, "batch-00000000-0000-0000-0000-000000000000")

    assert _check(document, "batch")["status"] == "refused"
    assert _check(document, "lifecycle")["status"] == "skipped"
    assert document["resumable"] is False


def test_a_missing_snapshot_is_refused_with_its_pointer(recovery: Fixture) -> None:
    brief = _approved_architect(recovery)
    path = recovery._records() / "dispatches" / f"{brief['dispatch_id']}.json"
    corrupted = json.loads(path.read_bytes())
    del corrupted["orchestration_policy"]["configuration"]["config"]
    path.write_text(json.dumps(corrupted), encoding="utf-8")
    repin_entry(
        recovery.state_dir,
        brief["batch_id"],
        brief["dispatch_id"],
        brief_sha256=pinned_sha256(corrupted),
    )

    document = _status(recovery, brief["batch_id"])

    check = _check(document, "snapshot")
    assert check["status"] == "refused"
    [diagnostic] = check["diagnostics"]
    assert diagnostic["pointer"].endswith("/orchestration_policy/configuration")
    assert document["resumable"] is False


def test_an_expired_approval_of_an_unsent_brief_is_refused(recovery: Fixture) -> None:
    brief = _approved_architect(recovery)
    _configure(recovery, approval_ttl_seconds=60)

    document = _status(recovery, brief["batch_id"])

    check = _check(document, "approval")
    assert check["status"] == "refused"
    assert [item["code"] for item in check["diagnostics"]][0] == "approval-stale"


def test_open_attention_is_shown_and_never_resolved(recovery: Fixture) -> None:
    brief = _approved_architect(recovery)
    _hold(recovery, brief["batch_id"])
    coordinator.attention_check(recovery._args(batch=brief["batch_id"]))

    document = _status(recovery, brief["batch_id"])

    assert _check(document, "attention")["status"] == "refused"
    assert document["lifecycle"]["needs_attention"] is True
    assert recovery._batch_record(brief["batch_id"])["needs_attention"] is True


# -- configuration diagnostics, reload and fix ---------------------------------------------------


def test_config_diagnostics_carry_a_pointer_redacted_values_and_a_suggestion(
    recovery: Fixture,
) -> None:
    brief = _approved_architect(recovery)
    _configure(recovery, effort=" HIGH ")

    document = _status(recovery, brief["batch_id"])

    [advisory] = [
        item
        for item in document["diagnostics"]
        if item["advisory"] is not None and "/architect/" in item["pointer"]
    ]
    assert advisory["code"] == "config-advisory"
    assert advisory["advisory"] == "config-normalized"
    assert advisory["pointer"] == "/assignment_plans/architect/runtimes/claude/effort"
    assert (advisory["received_redacted"], advisory["suggestion"]) == (" HIGH ", "high")
    assert advisory["fatal"] is False
    assert {item["action"]: item["available"] for item in document["actions"]}[
        "fix"
    ] is True


def test_a_secret_in_the_configuration_is_never_echoed(recovery: Fixture) -> None:
    brief = _approved_architect(recovery)
    _configure(recovery, provider_note="token=ghp_" + "a" * 36)

    document = _status(recovery, brief["batch_id"])

    assert _check(document, "config")["status"] == "refused"
    assert "ghp_" not in json.dumps(document)


def test_fix_previews_then_applies_through_the_transaction_without_resuming(
    recovery: Fixture,
) -> None:
    from harness.console.configuration import default_document

    brief = _approved_architect(recovery)
    # The transaction validates the whole configuration set, project.json included.
    (recovery.repo / ".harness/project.json").write_text(
        json.dumps(default_document(recovery.repo, "project.json")), encoding="utf-8"
    )
    path = _configure(recovery, effort=" HIGH ")
    _hold(recovery, brief["batch_id"])
    authored = path.read_bytes()
    store = _store_bytes(recovery, brief["batch_id"])

    preview = recovery_api.fix(recovery.repo, recovery.state_dir, brief["batch_id"])

    assert preview["outcome"] == "done" and preview["applied"] is False
    assert "HIGH" in preview["diff"]
    assert path.read_bytes() == authored

    applied = recovery_api.fix(
        recovery.repo, recovery.state_dir, brief["batch_id"], apply=True
    )

    assert applied["applied"] is True and applied["resumed"] is False
    runtime = json.loads(path.read_bytes())["assignment_plans"]["architect"]
    assert runtime["runtimes"]["claude"]["effort"] == "high"
    backup = recovery.repo / ".harness/config_versions" / applied["backup_version"]
    assert (backup / "orchestration.json").read_bytes() == authored
    # Nothing resumed: the held store record is byte for byte the one before the fix.
    assert _store_bytes(recovery, brief["batch_id"]) == store
    assert _status(recovery, brief["batch_id"])["hold"] is not None
    assert _status(recovery, brief["batch_id"])["intent"]["action"] == "fix"


def test_reload_validates_and_never_starts_a_worker(recovery: Fixture) -> None:
    brief = _approved_architect(recovery)
    status_before = coordinator._load_dispatch_status(
        recovery.state_dir, brief["dispatch_id"]
    )

    result = recovery_api.reload(recovery.repo, recovery.state_dir, brief["batch_id"])

    assert (result["outcome"], result["worker_started"]) == ("done", False)
    assert result["next"]["command"] == f"recovery resume --batch {brief['batch_id']}"
    assert (
        coordinator._load_dispatch_status(recovery.state_dir, brief["dispatch_id"])
        == status_before
    )

    _configure(recovery, effort="hgh")
    refused = recovery_api.reload(recovery.repo, recovery.state_dir, brief["batch_id"])

    assert refused["outcome"] == "refused"
    pointers = {item["pointer"] for item in refused["diagnostics"]}
    assert "/assignment_plans/architect/runtimes/claude/effort" in pointers
    intent = _status(recovery, brief["batch_id"])["intent"]
    assert (intent["action"], intent["status"]) == ("reload", "refused")


def test_reload_refuses_an_incompatible_runtime_declaration(recovery: Fixture) -> None:
    brief = _approved_architect(recovery)
    path = _configure(recovery)
    config = json.loads(path.read_bytes())
    config["assignment_plans"]["developer"]["runtimes"]["claude"][
        "system_fields_contract"
    ] = "systemFields.v2"
    path.write_text(json.dumps(config), encoding="utf-8")

    result = recovery_api.reload(recovery.repo, recovery.state_dir, brief["batch_id"])

    assert result["outcome"] == "refused"
    assert result["runtime_compatibility"]["compatible"] is False


# -- skip and abort ------------------------------------------------------------------------------


def test_config_declarations_pass_over_a_plan_that_is_not_an_object() -> None:
    evidence = recovery_api.config_declarations(
        {"assignment_plans": {"developer": "inherit"}}
    )

    assert evidence["compatible"] is True
    assert evidence["declarations"] == []


def test_skip_hides_only_an_allowlisted_advisory_for_one_session(
    recovery: Fixture,
) -> None:
    brief = _approved_architect(recovery)
    _configure(recovery, effort=" HIGH ")
    _hold(recovery, brief["batch_id"])
    document = _status(recovery, brief["batch_id"])
    [advisory] = [
        item
        for item in document["diagnostics"]
        if item["advisory"] is not None and "/architect/" in item["pointer"]
    ]
    [held] = [item for item in document["diagnostics"] if item["check"] == "store"]

    session = recovery_api.skip(RecoverySession(), document, advisory["id"])

    hidden = _status(recovery, brief["batch_id"], session=session)
    assert advisory["id"] not in {item["id"] for item in hidden["diagnostics"]}
    assert hidden["skipped_advisories"] == [advisory["id"]]
    fresh = _status(recovery, brief["batch_id"])
    assert advisory["id"] in {item["id"] for item in fresh["diagnostics"]}
    with pytest.raises(CoordinatorError, match="not a skippable advisory"):
        recovery_api.skip(session, document, held["id"])
    with pytest.raises(CoordinatorError, match="no diagnostic"):
        recovery_api.skip(session, document, "0" * 16)


def test_abort_flushes_its_intent_and_changes_nothing_else(recovery: Fixture) -> None:
    brief = _approved_architect(recovery)
    _hold(recovery, brief["batch_id"])
    ledger, record = (
        recovery._ledger_bytes(),
        _store(recovery).read(brief["batch_id"]),
    )

    result = recovery_api.abort(recovery.repo, recovery.state_dir, brief["batch_id"])

    assert (
        result["abandoned"],
        result["worker_stopped"],
        result["worktree_cleaned"],
    ) == (
        False,
        False,
        False,
    )
    assert result["intent"]["status"] == "aborted"
    assert recovery._ledger_bytes() == ledger
    assert _store(recovery).read(brief["batch_id"]) == record
    assert result["lifecycle"]["state"] == "active"


# -- durable intent and internal errors ----------------------------------------------------------


@pytest.mark.parametrize(
    "signal", [KeyboardInterrupt, EOFError, asyncio.CancelledError]
)
def test_an_interrupted_action_keeps_its_intent_for_a_restart(
    recovery: Fixture, monkeypatch: pytest.MonkeyPatch, signal: type[BaseException]
) -> None:
    brief = _approved_architect(recovery)

    def interrupted(self: recovery_api.Survey) -> None:
        raise signal()

    with monkeypatch.context() as patch:
        patch.setattr(recovery_api.Survey, "_config", interrupted)
        with pytest.raises(RecoveryInterrupted) as raised:
            recovery_api.reload(recovery.repo, recovery.state_dir, brief["batch_id"])
    assert "recovery status" in raised.value.remedy
    intent = _status(recovery, brief["batch_id"])["intent"]
    assert (intent["action"], intent["status"]) == ("reload", "interrupted")

    recovery_api.reload(recovery.repo, recovery.state_dir, brief["batch_id"])

    intent = _status(recovery, brief["batch_id"])["intent"]
    assert (intent["action"], intent["status"]) == ("reload", "done")


def test_an_internal_exception_is_never_shown_as_a_configuration_typo(
    recovery: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    brief = _approved_architect(recovery)

    def broken(*_: object, **__: object) -> None:
        raise RuntimeError("index out of range in the normalizer")

    monkeypatch.setattr(recovery_api, "inspect_configuration", broken)
    with pytest.raises(RecoveryInternalError) as raised:
        recovery_api.reload(recovery.repo, recovery.state_dir, brief["batch_id"])

    error = raised.value
    assert error.code == "internal-error"
    assert error.diagnostics["internal_invariant"] is True
    assert error.correlation_id in error.remedy
    assert "config" not in error.code
    intent = recovery_api.read_intent(_store(recovery), brief["batch_id"])
    assert intent is not None
    assert intent["status"] == "refused"
    assert intent["correlation_id"] == error.correlation_id


def test_an_unreadable_batch_shows_its_ledger_inspection(recovery: Fixture) -> None:
    brief = _approved_architect(recovery)
    path = recovery._records() / "batches" / f"{brief['batch_id']}.json"
    path.write_text("{", encoding="utf-8")

    document = _status(recovery, brief["batch_id"])

    check = _check(document, "batch")
    assert check["status"] == "refused"
    assert check["evidence"] is not None
    assert check["evidence"]["batch_id"] == brief["batch_id"]
