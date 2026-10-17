"""Runtime, adapter and extension compatibility with the systemFields.v1 contract (issue #684)."""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Iterator
from contextlib import redirect_stderr
from pathlib import Path

import pytest

from harness.orchestration import (
    coordinator,
    extensions,
    recovery_api,
    runtime_compatibility,
)
from harness.orchestration.core.system_fields import CONTRACT_VERSION
from harness.orchestration.core.utils import JsonObject
from harness.orchestration.runtime_compatibility import (
    Declaration,
    RuntimeCompatibilityError,
    adapter_declaration,
    assignment_declaration,
    dispatch_declarations,
    extension_declarations,
    require_compatible,
    status_of,
)
from harness.orchestration.runtime_emergency import report_failure
from harness.orchestration.runtime_state_store import RuntimeStateStore
from tests.orchestration import test_recovery
from tests.orchestration.test_runtime_effect import (
    external_brief,
    launches,
    send_external,
)

ADAPTER = """import json, sys
from pathlib import Path

if sys.argv[1] == "contract":
    print(json.dumps({answer}))
    sys.exit({code})
with (Path({here!r}) / "launches.log").open("a", encoding="utf-8") as log:
    log.write(sys.argv[-1] + "\\n")
"""


class Undeclared:
    """A project extension that declares no contract."""

    def notify(self, event: object) -> None:
        return None


class Declared(Undeclared):
    system_fields_contract = CONTRACT_VERSION


@pytest.fixture
def notifier() -> Iterator[str]:
    yield "test-compatibility-notifier"
    extensions.unregister("human_notifier", "test-compatibility-notifier")


def _adapter(path: Path, answer: object, code: int = 0) -> Path:
    adapter = path / "contract-adapter.py"
    adapter.write_text(
        ADAPTER.format(answer=repr(answer), code=code, here=str(path)),
        encoding="utf-8",
    )
    return adapter


def _declare(tmp_path: Path, answer: object, code: int = 0) -> Declaration | None:
    adapter = _adapter(tmp_path, answer, code)
    return adapter_declaration(
        [sys.executable, str(adapter)],
        [],
        repo=tmp_path,
        brief=tmp_path / "brief.json",
        cwd=tmp_path,
    )


# -- statuses ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "status"),
    [
        (CONTRACT_VERSION, "compatible"),
        (None, "missing"),
        ("", "missing"),
        ("   ", "missing"),
        (1, "missing"),
        ("systemFields.v2", "unsupported"),
        ("SystemFields.V1", "unsupported"),
        (" systemFields.v1", "unsupported"),
    ],
)
def test_a_declaration_is_compared_exactly(declared: object, status: str) -> None:
    assert status_of(declared) == status


def test_a_supported_version_other_than_the_pinned_one_is_a_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_compatibility,
        "SUPPORTED_CONTRACTS",
        ("systemFields.v0", CONTRACT_VERSION),
    )

    assert status_of("systemFields.v0", CONTRACT_VERSION) == "mismatch"
    assert status_of(CONTRACT_VERSION, "systemFields.v0") == "mismatch"


def test_the_statuses_and_subjects_are_the_registry_values() -> None:
    from harness.orchestration.core.system_fields import load_registry

    fields = load_registry().fields
    assert (
        runtime_compatibility.STATUSES == fields["runtime_compatibility.status"].allowed
    )
    assert (
        runtime_compatibility.SUBJECTS
        == fields["runtime_compatibility.subject"].allowed
    )


# -- runtime assignments -------------------------------------------------------------------------


def _config(**runtime: object) -> JsonObject:
    return {
        "assignment_plans": {
            "developer": {
                "runtimes": {
                    "claude": {
                        "profiles": ["p"],
                        "model": "sonnet",
                        "effort": "high",
                        **runtime,
                    }
                }
            }
        }
    }


def test_an_in_process_runtime_without_the_field_speaks_the_harness_version() -> None:
    declaration = assignment_declaration(_config(), "developer", "claude", "in-process")

    assert declaration is not None
    assert (declaration.source, declaration.status) == ("built-in", "compatible")


def test_an_explicit_unsupported_assignment_names_its_pointer() -> None:
    declaration = assignment_declaration(
        _config(system_fields_contract="systemFields.v2"),
        "developer",
        "claude",
        "in-process",
    )

    assert declaration is not None
    shown = declaration.to_dict()
    assert shown["status"] == "unsupported"
    assert shown["source"] == "config"
    assert (
        shown["pointer"]
        == "/assignment_plans/developer/runtimes/claude/system_fields_contract"
    )


def test_an_external_assignment_without_the_field_leaves_it_to_its_adapter() -> None:
    assert assignment_declaration(_config(), "developer", "claude", "external") is None
    declared = assignment_declaration(
        _config(system_fields_contract=CONTRACT_VERSION),
        "developer",
        "claude",
        "external",
    )
    assert declared is not None and declared.status == "compatible"


def test_a_zero_config_session_runtime_speaks_the_harness_version() -> None:
    declaration = assignment_declaration({}, "developer", "session", "in-process")

    assert declaration is not None and declaration.status == "compatible"


# -- extensions ----------------------------------------------------------------------------------


def test_the_none_extensions_need_no_declaration() -> None:
    assert extension_declarations(extensions.selected({})) == []


def test_a_project_extension_declares_through_its_attribute(notifier: str) -> None:
    extensions.register("human_notifier", notifier, Undeclared())
    [missing] = extension_declarations({"human_notifier": notifier})
    assert (missing.subject, missing.status) == ("extension", "missing")
    assert missing.to_dict()["pointer"] == "/extensions/human_notifier"

    extensions.register("human_notifier", notifier, Declared())
    [declared] = extension_declarations({"human_notifier": notifier})
    assert declared.status == "compatible"


def test_an_extension_that_cannot_load_declares_nothing() -> None:
    [declaration] = extension_declarations(
        {"human_notifier": "no_such_module_684:factory"}
    )

    assert declaration.status == "missing"
    assert "could not be loaded" in str(declaration.detail)


# -- external adapters ---------------------------------------------------------------------------


def test_an_adapter_declares_through_its_contract_verb(tmp_path: Path) -> None:
    declaration = _declare(tmp_path, {"system_fields_contract": CONTRACT_VERSION})

    assert declaration is not None and declaration.status == "compatible"


@pytest.mark.parametrize(
    ("answer", "code", "status"),
    [
        ({"system_fields_contract": "systemFields.v2"}, 0, "unsupported"),
        ({"system_fields_contract": CONTRACT_VERSION}, 2, "missing"),
        ({"contract": CONTRACT_VERSION}, 0, "missing"),
        (
            {"system_fields_contract": CONTRACT_VERSION, "extra": True},
            0,
            "missing",
        ),
        ("systemFields.v1", 0, "missing"),
    ],
)
def test_any_other_adapter_answer_is_not_compatible(
    tmp_path: Path, answer: object, code: int, status: str
) -> None:
    declaration = _declare(tmp_path, answer, code)

    assert declaration is not None and declaration.status == status


def test_an_adapter_that_cannot_start_is_left_to_the_hand_off(tmp_path: Path) -> None:
    declaration = adapter_declaration(
        [str(tmp_path / "missing-adapter")],
        [],
        repo=tmp_path,
        brief=tmp_path / "brief.json",
        cwd=tmp_path,
    )

    assert declaration is None


def test_a_silent_adapter_times_out_into_a_missing_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_compatibility, "ADAPTER_TIMEOUT_SECONDS", 0.5)
    adapter = tmp_path / "slow-adapter.py"
    adapter.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")

    declaration = adapter_declaration(
        [sys.executable, str(adapter)],
        [],
        repo=tmp_path,
        brief=tmp_path / "brief.json",
        cwd=tmp_path,
    )

    assert declaration is not None and declaration.status == "missing"
    assert "did not answer" in str(declaration.detail)


# -- the refusal ---------------------------------------------------------------------------------


def test_the_refusal_is_a_structured_runtime_compatibility_failure() -> None:
    declarations = [
        Declaration(
            "runtime-assignment", "developer/claude", "built-in", CONTRACT_VERSION
        ),
        Declaration(
            "extension",
            "human_notifier=x",
            "extension",
            "token ghp_" + "a" * 36,
            location=("extensions", "human_notifier"),
        ),
    ]

    with pytest.raises(RuntimeCompatibilityError) as raised:
        require_compatible("send", declarations)

    error = raised.value
    assert error.code == "runtime-compatibility"
    assert error.step == "send"
    assert "nothing was started or accepted" in error.message
    evidence = error.diagnostics["evidence"]
    assert evidence["compatible"] is False
    assert [item["status"] for item in evidence["declarations"]] == [
        "compatible",
        "unsupported",
    ]
    assert "ghp_" not in json.dumps(evidence)
    assert "ghp_" not in error.message


def test_the_cli_prints_the_refusal_as_diagnostics_with_its_evidence() -> None:
    error = RuntimeCompatibilityError(
        "preflight",
        [Declaration("extension", "human_notifier=x", "extension", None)],
        remedy="declare it",
    )
    stream = io.StringIO()
    with redirect_stderr(stream):
        report_failure(
            coordinator.parser().parse_args(
                ["dispatch", "send", "--dispatch", "dispatch-0"]
            ),
            error,
        )

    line = stream.getvalue().strip()
    assert line.startswith("DIAGNOSTICS: ")
    shown = json.loads(line.removeprefix("DIAGNOSTICS: "))
    assert shown["code"] == "runtime-compatibility"
    assert shown["evidence"]["declarations"][0]["status"] == "missing"
    assert shown["emergency_record"] is None


def test_a_brief_pinned_to_an_unsupported_contract_is_refused() -> None:
    dispatch: JsonObject = {
        "dispatch_id": "dispatch-1",
        "role": "developer",
        "resolved_runtime": "claude",
        "resolved_transport": "in-process",
        "orchestration_policy": {
            "configuration": {"contract": "systemFields.v2"},
            "extensions": {},
        },
    }

    declarations = dispatch_declarations(_config(), dispatch)

    assert [item.status for item in declarations] == ["unsupported", "compatible"]


# -- the lifecycle steps -------------------------------------------------------------------------


def _ledger(fixture: test_recovery.RecoveryTests) -> dict[str, bytes]:
    return {
        str(path.relative_to(fixture.state_dir)): path.read_bytes()
        for path in fixture.state_dir.rglob("*")
        if path.is_file()
    }


def test_send_refuses_an_incompatible_adapter_before_any_effect(
    recovery: test_recovery.RecoveryTests,
) -> None:
    brief, _ = external_brief(recovery)
    adapter = _adapter(recovery.tmp, {"system_fields_contract": "systemFields.v2"})
    status_before = coordinator._load_dispatch_status(
        recovery.state_dir, brief["dispatch_id"]
    )
    batch_before = recovery._batch_record(brief["batch_id"])

    with pytest.raises(RuntimeCompatibilityError) as raised:
        send_external(recovery, brief, adapter)

    assert raised.value.step == "send"
    assert f"recovery resume --batch {brief['batch_id']}" in raised.value.remedy
    statuses = {
        item["subject"]: item["status"]
        for item in raised.value.evidence["declarations"]
    }
    assert statuses["external-adapter"] == "unsupported"
    assert statuses["dispatch-brief"] == "compatible"
    assert launches(recovery) == []
    # Nothing started and no lifecycle moved: the store holds the batch, attention is raised.
    assert (
        coordinator._load_dispatch_status(recovery.state_dir, brief["dispatch_id"])
        == status_before
    )
    batch = recovery._batch_record(brief["batch_id"])
    assert (batch["state"], batch.get("next_action"), batch["dispatches"]) == (
        batch_before["state"],
        batch_before.get("next_action"),
        batch_before["dispatches"],
    )
    assert (batch["needs_attention"], batch["attention_reason"]) == (
        True,
        "operational-hold",
    )
    record = RuntimeStateStore(recovery.repo, recovery.state_dir).read(
        brief["batch_id"]
    )
    assert record is not None and record["operation"] is None
    assert (record["state"], record["failure"]["step"]) == (
        "suspended",
        "hold:runtime-compatibility",
    )
    # Every command that acts through the store stops on the hold, a second send included.
    with pytest.raises(coordinator.CoordinatorError, match="is held"):
        send_external(
            recovery,
            brief,
            _adapter(recovery.tmp, {"system_fields_contract": CONTRACT_VERSION}),
        )
    assert launches(recovery) == []


def _before(text: str, first: str, second: str) -> bool:
    return first in text and second in text and text.index(first) < text.index(second)


def test_the_route_the_send_hold_names_releases_it(
    recovery: test_recovery.RecoveryTests,
) -> None:
    """The remedy and the attention name attention resolve before recovery resume, and
    following them in that order releases the hold without a refusal."""
    brief, _ = external_brief(recovery)
    batch_id = brief["batch_id"]
    with pytest.raises(RuntimeCompatibilityError) as raised:
        send_external(
            recovery,
            brief,
            _adapter(recovery.tmp, {"system_fields_contract": "systemFields.v2"}),
        )
    resolve, resume = "batch attention resolve", f"recovery resume --batch {batch_id}"
    assert _before(raised.value.remedy, resolve, resume)
    held = recovery._batch_record(batch_id)
    assert _before(held["recommended_human_action"], resolve, resume)

    coordinator.attention_resolve(
        recovery._args(
            batch=batch_id, note="the adapter now speaks v1", **recovery._approval()
        )
    )
    resumed = recovery_api.resume(recovery.repo, recovery.state_dir, batch_id)

    assert resumed["outcome"] == "done", resumed["refusals"]
    assert [step["step"] for step in resumed["steps"]] == ["release"]
    assert (
        resumed["next"]["command"] == f"dispatch send --dispatch {brief['dispatch_id']}"
    )
    sent = send_external(
        recovery,
        brief,
        _adapter(recovery.tmp, {"system_fields_contract": CONTRACT_VERSION}),
    )
    assert sent["state"] == "dispatched"
    assert len(launches(recovery)) == 1


def test_send_hands_off_once_to_a_compatible_adapter(
    recovery: test_recovery.RecoveryTests,
) -> None:
    brief, _ = external_brief(recovery)
    adapter = _adapter(recovery.tmp, {"system_fields_contract": CONTRACT_VERSION})

    sent = send_external(recovery, brief, adapter)

    assert sent["runtime_compatibility"]["compatible"] is True
    assert [
        item["subject"] for item in sent["runtime_compatibility"]["declarations"]
    ] == ["dispatch-brief", "external-adapter"]
    assert len(launches(recovery)) == 1


def test_create_and_preflight_refuse_an_unsupported_runtime_assignment(
    recovery: test_recovery.RecoveryTests,
) -> None:
    brief, _ = external_brief(recovery)
    path = recovery.repo / ".harness/orchestration.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    for plan in config["assignment_plans"].values():
        plan["runtimes"]["claude"]["system_fields_contract"] = "systemFields.v2"
    path.write_text(json.dumps(config), encoding="utf-8")
    coordinator.cancel_dispatch(
        recovery._args(
            dispatch=brief["dispatch_id"],
            reason="re-plan on the new runtime",
            runtime_stopped=False,
            **recovery._approval(),
        )
    )
    before = _ledger(recovery)

    with pytest.raises(RuntimeCompatibilityError) as preflight:
        coordinator.preflight_dispatch(
            recovery._args(
                batch=brief["batch_id"],
                role="architect",
                runtime=None,
                purpose="work",
                candidate_commit=None,
                worktree=str(recovery.worktree),
            )
        )
    assert preflight.value.step == "preflight"
    [declaration] = preflight.value.evidence["declarations"]
    assert declaration["status"] == "unsupported"
    assert _ledger(recovery) == before

    with pytest.raises(RuntimeCompatibilityError) as create:
        recovery._dispatch(brief["batch_id"], "architect")
    assert create.value.step == "create"


# -- the worker's confirmation -------------------------------------------------------------------


def _self_report(
    fixture: test_recovery.RecoveryTests, dispatch_id: str, contract: str | None
) -> JsonObject:
    return coordinator.self_report_dispatch(
        fixture._args(
            dispatch=dispatch_id,
            model="sonnet",
            worktree=str(fixture.worktree),
            system_fields_contract=contract,
        )
    )


def _status(fixture: test_recovery.RecoveryTests, dispatch_id: str) -> JsonObject:
    return coordinator._load_dispatch_status(fixture.state_dir, dispatch_id)


def test_the_cli_takes_an_optional_contract_confirmation() -> None:
    parsed = coordinator.parser().parse_args(
        [
            "dispatch",
            "self-report",
            "--dispatch",
            "dispatch-0",
            "--model",
            "m",
            "--system-fields-contract",
            CONTRACT_VERSION,
        ]
    )
    assert parsed.system_fields_contract == CONTRACT_VERSION
    plain = coordinator.parser().parse_args(
        ["dispatch", "self-report", "--dispatch", "dispatch-0", "--model", "m"]
    )
    assert plain.system_fields_contract is None


def test_a_confirmed_contract_rides_into_the_report_evidence(
    recovery: test_recovery.RecoveryTests,
) -> None:
    batch = recovery._create_batch()
    brief = recovery._dispatch(batch["batch_id"], "architect")["brief"]
    coordinator.send_dispatch(
        recovery._args(
            dispatch=brief["dispatch_id"], adapter=None, adapter_arg=None, checkout=None
        )
    )

    started = _self_report(recovery, brief["dispatch_id"], CONTRACT_VERSION)

    assert started["state"] == "working"
    assert started["system_fields_contract"]["status"] == "compatible"
    assert _status(recovery, brief["dispatch_id"])["contract_self_report"][
        "status"
    ] == ("compatible")
    result = recovery._submit(
        brief["dispatch_id"], recovery._base_report(brief, "architect")
    )
    statuses = {
        item["subject"]: item["status"]
        for item in result["runtime_compatibility"]["declarations"]
    }
    assert statuses["worker-self-report"] == "compatible"


def test_an_incompatible_confirmation_is_evidence_and_never_a_working_dispatch(
    recovery: test_recovery.RecoveryTests,
) -> None:
    batch = recovery._create_batch()
    brief = recovery._dispatch(batch["batch_id"], "architect")["brief"]
    coordinator.send_dispatch(
        recovery._args(
            dispatch=brief["dispatch_id"], adapter=None, adapter_arg=None, checkout=None
        )
    )

    with pytest.raises(RuntimeCompatibilityError) as raised:
        _self_report(recovery, brief["dispatch_id"], "systemFields.v2")

    assert raised.value.step == "self-report"
    status = _status(recovery, brief["dispatch_id"])
    assert status["state"] == "dispatched"
    assert "model_self_report" not in status
    assert status["contract_self_report"]["status"] == "unsupported"
    assert recovery._batch_record(batch["batch_id"])["state"] != "blocked"
    # The batch is held: no report is taken until recovery resume releases it, but the
    # refused report is kept as unaccepted evidence at once.
    record = RuntimeStateStore(recovery.repo, recovery.state_dir).read(
        batch["batch_id"]
    )
    assert record is not None and record["state"] == "suspended"
    report = recovery._base_report(brief, "architect")
    with pytest.raises(coordinator.CoordinatorError, match="is held") as refused:
        recovery._submit(brief["dispatch_id"], report)
    assert not (
        recovery._records() / "reports" / f"{brief['dispatch_id']}.json"
    ).exists()
    [evidence] = (recovery._records() / "unaccepted-evidence").glob(
        f"{brief['dispatch_id']}-*.json"
    )
    assert str(evidence) in refused.value.remedy
    captured = json.loads(evidence.read_text(encoding="utf-8"))
    assert (captured["accepted"], captured["input"]) == (False, report)
