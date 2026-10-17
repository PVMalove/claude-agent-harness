"""Durable intent of the external effect of ``dispatch send``: the order of the intent, the
in-flight marker, the hand-off and its observation, and the reconcile of every interruption."""

from __future__ import annotations

import errno
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from harness.errors import HarnessError
from harness.json_types import JsonObject
from harness.orchestration import coordinator, runtime_intent, runtime_state_store
from harness.orchestration.core.constants import STATE_REL
from harness.orchestration.core.utils import CoordinatorError
from harness.orchestration.ledger.lifecycle import (
    BatchRecord,
    DispatchStatusRecord,
    JsonValue,
    LifecycleLedger,
)
from harness.orchestration.runtime_effect import (
    DispatchSend,
    EffectResult,
    observation_id,
)
from harness.orchestration.runtime_state_store import (
    RuntimeStateError,
    RuntimeStateStore,
)
from tests.orchestration import test_recovery
from tests.orchestration.test_runtime_intent import BATCH, DISPATCH, Scene
from tests.orchestration.test_runtime_reconcile_faults import (
    Crash,
    crash_on,
    settle_scene,
    store_bytes,
)

EFFECT: JsonObject = {
    "channel": "adapter",
    "transport": "external",
    "adapter": "adapter.py",
    "adapter_args": [],
    "worker_worktree": "worktree",
    "worker_snapshot_commit": "0" * 40,
    "brief_sha256": "1" * 64,
    "plan_digest": None,
}
STATUS = f"dispatch-status/{DISPATCH}.json"
BATCH_RECORD = f"batches/{BATCH}.json"
EVIDENCE: JsonObject = {"status": "legacy-inherit"}


def make_send_scene(tmp_path: Path, **brief: JsonValue) -> Scene:
    """A Git checkout with a ledger whose batch has one approved, unsent dispatch; ``brief``
    adds fields to its dispatch record."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    root = repo / STATE_REL
    ledger = LifecycleLedger(root)
    ledger.ensure()
    records = ledger.records_root()
    ledger.write_immutable(records / f"plans/{BATCH}.json", {"batch_id": BATCH})
    ledger.write_immutable(
        records / f"dispatches/{DISPATCH}.json",
        {"dispatch_id": DISPATCH, "batch_id": BATCH, "state": "approved", **brief},
    )
    ledger.write_record(
        BatchRecord.from_dict(
            {
                "batch_id": BATCH,
                "state": "active",
                "dispatches": [{"dispatch_id": DISPATCH, "state": "approved"}],
            }
        )
    )
    ledger.write_record(
        DispatchStatusRecord.from_dict({"dispatch_id": DISPATCH, "state": "approved"})
    )
    return Scene(repo.resolve(), root.resolve(), ledger)


@pytest.fixture
def scene(tmp_path: Path) -> Scene:
    return make_send_scene(tmp_path)


def send(
    scene: Scene,
    hand_off: Callable[[], EffectResult],
    channel: str = "adapter",
) -> EffectResult:
    return DispatchSend(
        scene.ledger,
        scene.store(),
        batch=scene.read(BATCH_RECORD),
        status=scene.read(STATUS),
        effect={**EFFECT, "channel": channel},
    ).run(hand_off)


def started(launches: list[str]) -> Callable[[], EffectResult]:
    def hand_off() -> EffectResult:
        launches.append(DISPATCH)
        return EffectResult("dispatched", "adapter-started", EVIDENCE)

    return hand_off


def states(scene: Scene) -> tuple[str, str, str]:
    """The batch, its dispatch entry and the dispatch status as the ledger holds them."""
    batch = scene.read(BATCH_RECORD)
    return (
        batch["state"],
        batch["dispatches"][0]["state"],
        scene.read(STATUS)["state"],
    )


def runtime(scene: Scene) -> JsonObject:
    record = scene.runtime()
    assert record is not None
    return record


def assert_settled(scene: Scene) -> None:
    """The store is ready over a committed hand-off and a repeated settle changes nothing."""
    record = runtime(scene)
    assert (record["state"], record["operation"]["phase"]) == ("ready", "committed")
    assert record["operation"]["effect"]["state"] == "confirmed"
    store, ledger = store_bytes(scene), scene.tree()

    assert settle_scene(scene) is False

    assert (store_bytes(scene), scene.tree()) == (store, ledger)


def settle_error(scene: Scene) -> RuntimeStateError:
    with pytest.raises(RuntimeStateError) as caught:
        settle_scene(scene)
    return caught.value


# --- The order of the send ---------------------------------------------------------------------


@pytest.mark.parametrize(("channel", "marked"), [("adapter", True), ("none", False)])
def test_the_intent_and_the_in_flight_marker_are_durable_before_the_hand_off(
    scene: Scene, channel: str, marked: bool
) -> None:
    seen: list[JsonObject] = []

    def hand_off() -> EffectResult:
        record = runtime(scene)
        seen.append(record)
        payload = scene.store().read_intent(BATCH, record["input_refs"][0])
        assert payload["effect"]["channel"] == channel
        assert sorted(payload["before"]) == [BATCH_RECORD, STATUS]
        assert states(scene) == ("active", "approved", "approved")
        return EffectResult("dispatched", "in-process", EVIDENCE)

    result = send(scene, hand_off, channel)

    record = seen[0]
    assert (record["state"], record["operation"]["phase"]) == ("running", "prepared")
    assert record["operation"]["kind"] == "dispatch-send"
    assert record["operation"]["effect"]["state"] == (
        "unknown" if marked else "not-started"
    )
    assert record["ledger"]["record_hashes"] == {}
    assert result.outcome == "dispatched"
    assert states(scene) == ("active", "dispatched", "dispatched")
    status = scene.read(STATUS)
    assert (status["runtime_access"], status["worker_worktree"]) == (
        EVIDENCE,
        "worktree",
    )
    committed = runtime(scene)
    assert (committed["state"], committed["operation"]["phase"]) == (
        "running",
        "committed",
    )
    assert committed["operation"]["effect"]["evidence_ref"].endswith(
        f"{observation_id(committed['operation']['operation_id'])}.json"
    )
    assert sorted(committed["output_refs"])[2:] == [BATCH_RECORD, STATUS]


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            EffectResult(
                "blocked",
                "adapter-unavailable",
                {"kind": "runtime-unavailable", "worker_started": False},
                CoordinatorError("adapter could not be started", remedy="fix it"),
            ),
            ("blocked", "blocked", "blocked"),
        ),
        (
            EffectResult(
                "unchanged",
                "adapter-rejected",
                None,
                CoordinatorError("adapter rejected", remedy="inspect it"),
            ),
            ("active", "approved", "approved"),
        ),
        (
            EffectResult(
                "unchanged",
                "access-refused",
                None,
                HarnessError("no handoff", remedy="select a provider"),
            ),
            ("active", "approved", "approved"),
        ),
    ],
)
def test_a_refused_hand_off_is_committed_and_then_raised(
    scene: Scene, result: EffectResult, expected: tuple[str, str, str]
) -> None:
    before = scene.read(STATUS)

    with pytest.raises(HarnessError) as caught:
        send(scene, lambda: result)

    assert caught.value is result.error
    assert states(scene) == expected
    if result.outcome == "blocked":
        assert scene.read(STATUS)["runtime_failure"] == result.evidence
    else:
        assert scene.read(STATUS) == before
    record = runtime(scene)
    assert record["operation"]["phase"] == "committed"
    assert sorted(record["ledger"]["record_hashes"]) == [BATCH_RECORD, STATUS]


# --- Storage failures and an unknown outcome ---------------------------------------------------


def fail_replace(monkeypatch: pytest.MonkeyPatch, call: int) -> None:
    """The ``call``-th atomic replace of the store fails as a full disk does."""
    original = runtime_state_store._replace_atomically
    calls = [0]

    def replace(source: Path, target: Path) -> None:
        calls[0] += 1
        if calls[0] == call:
            raise OSError(errno.ENOSPC, "No space left on device")
        original(source, target)

    monkeypatch.setattr(runtime_state_store, "_replace_atomically", replace)


# Replaces: first record, intent payload, prepared, marker, observation payload.
@pytest.mark.parametrize("call", [2, 4])
def test_a_storage_failure_before_the_hand_off_never_calls_it(
    scene: Scene, monkeypatch: pytest.MonkeyPatch, call: int
) -> None:
    ledger = scene.tree()
    launches: list[str] = []
    fail_replace(monkeypatch, call)

    with pytest.raises(RuntimeStateError) as caught:
        send(scene, started(launches))

    assert caught.value.code == "state-persist-failed"
    assert launches == []
    assert scene.tree() == ledger


def test_a_storage_failure_after_the_hand_off_is_honest_and_keeps_the_marker(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = scene.tree()
    launches: list[str] = []
    fail_replace(monkeypatch, 5)

    with pytest.raises(RuntimeStateError) as caught:
        send(scene, started(launches))

    assert (caught.value.code, caught.value.step) == (
        "state-persist-failed",
        "effect-observe",
    )
    assert "returned dispatched" in caught.value.message
    assert "effect unknown" in caught.value.message
    assert launches == [DISPATCH]
    assert scene.tree() == ledger
    record = runtime(scene)
    assert (record["state"], record["operation"]["effect"]["state"]) == (
        "suspended",
        "unknown",
    )


def test_an_unclassified_hand_off_failure_suspends_with_an_ambiguous_effect(
    scene: Scene,
) -> None:
    ledger = scene.tree()

    def hand_off() -> EffectResult:
        raise RuntimeError("transport vanished")

    with pytest.raises(RuntimeStateError) as caught:
        send(scene, hand_off)

    assert (caught.value.code, caught.value.step) == ("ambiguous-effect", "effect")
    assert "never starts the worker again" in caught.value.remedy
    record = runtime(scene)
    assert record["state"] == "suspended"
    assert record["failure"]["code"] == "ambiguous-effect"
    assert scene.tree() == ledger


def test_an_unfinished_hand_off_refuses_a_new_one(scene: Scene) -> None:
    def hand_off() -> EffectResult:
        raise Crash("hand-off")

    with pytest.raises(Crash):
        send(scene, hand_off)
    launches: list[str] = []

    with pytest.raises(RuntimeStateError) as caught:
        send(scene, started(launches))

    assert caught.value.code == "concurrency-conflict"
    assert launches == []


# --- Reconcile of an interrupted send ----------------------------------------------------------


def interrupt(
    scene: Scene, stop: Callable[[], None], monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    launches: list[str] = []
    stop()
    with pytest.raises(Crash):
        send(scene, started(launches))
    monkeypatch.undo()
    return launches


@pytest.mark.parametrize(
    ("owner", "name", "call", "after"),
    [
        # After the observation payload, before the store records it.
        (RuntimeStateStore, "write_intent", 2, True),
        # Between the batch and the status record.
        (LifecycleLedger, "_append_audit", 1, True),
        # Between the status record and its audit event.
        (LifecycleLedger, "_append_audit", 2, False),
        # Before the store commit.
        (runtime_intent, "settle_phases", 1, False),
    ],
)
def test_a_send_stopped_after_its_observation_is_rolled_forward_once(
    scene: Scene,
    monkeypatch: pytest.MonkeyPatch,
    owner: object,
    name: str,
    call: int,
    after: bool,
) -> None:
    launches = interrupt(
        scene,
        lambda: crash_on(monkeypatch, owner, name, call, after=after),  # type: ignore[arg-type]
        monkeypatch,
    )

    assert settle_scene(scene) is True

    assert launches == [DISPATCH]
    assert states(scene) == ("active", "dispatched", "dispatched")
    assert scene.read(STATUS)["runtime_access"] == EVIDENCE
    assert_settled(scene)


def test_a_send_stopped_before_its_marker_is_proved_unchanged(
    scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = scene.tree()
    # Store writes: first record, prepared, marker.
    launches = interrupt(
        scene, lambda: crash_on(monkeypatch, RuntimeStateStore, "write", 3), monkeypatch
    )
    assert runtime(scene)["operation"]["effect"]["state"] == "not-started"

    assert settle_scene(scene) is True

    assert launches == []
    assert scene.tree() == ledger
    assert_settled(scene)
    assert send(scene, started(launches)).outcome == "dispatched"
    assert launches == [DISPATCH]


def test_a_send_stopped_during_the_hand_off_stops_ambiguous_without_a_new_revision(
    scene: Scene,
) -> None:
    def hand_off() -> EffectResult:
        raise Crash("hand-off")

    with pytest.raises(Crash):
        send(scene, hand_off)

    stopped = settle_error(scene)

    assert (stopped.code, stopped.step) == ("ambiguous-effect", "reconcile:effect")
    assert stopped.diagnostics["evidence"]["dispatch_id"] == DISPATCH
    assert DISPATCH in stopped.remedy
    revision = runtime(scene)["revision"]
    assert settle_error(scene).code == "ambiguous-effect"
    assert runtime(scene)["revision"] == revision
    assert states(scene) == ("active", "approved", "approved")


def test_a_record_changed_behind_an_unknown_hand_off_is_an_integrity_failure(
    scene: Scene,
) -> None:
    def hand_off() -> EffectResult:
        raise Crash("hand-off")

    with pytest.raises(Crash):
        send(scene, hand_off)
    (scene.records / STATUS).write_text(
        json.dumps({"dispatch_id": DISPATCH, "state": "dispatched"}), encoding="utf-8"
    )

    stopped = settle_error(scene)

    assert (stopped.code, stopped.step) == ("integrity-failure", "reconcile:effect")
    assert stopped.diagnostics["evidence"]["path"] == STATUS


def test_a_tampered_hand_off_intent_stops_before_any_write(scene: Scene) -> None:
    def hand_off() -> EffectResult:
        raise Crash("hand-off")

    with pytest.raises(Crash):
        send(scene, hand_off)
    record = runtime(scene)
    path = scene.store().location.directory / record["input_refs"][0]
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["effect"]["adapter"] = "another-adapter.py"
    path.write_text(json.dumps(payload), encoding="utf-8")
    ledger = scene.tree()

    stopped = settle_error(scene)

    assert (stopped.code, stopped.step) == ("integrity-failure", "reconcile:intent")
    assert scene.tree() == ledger


# --- The public command ------------------------------------------------------------------------

FAKE_ADAPTER = """import sys
from pathlib import Path

here = Path({here!r})
if sys.argv[1] == "contract":
    print('{{"system_fields_contract": "systemFields.v1"}}')
    sys.exit(0)
if sys.argv[1] == "dispatch":
    with (here / "launches.log").open("a", encoding="utf-8") as log:
        log.write(sys.argv[-1] + "\\n")
    sys.exit(0)
answer = here / "observation.json"
if sys.argv[1] == "observe" and answer.exists():
    print(answer.read_text(encoding="utf-8"))
    sys.exit(0)
sys.exit(2)
"""


def external_brief(fixture: test_recovery.RecoveryTests) -> tuple[JsonObject, Path]:
    """An approved architect brief on the external transport and a fake adapter for it."""
    runtime_ = {"claude": {"profiles": ["p"], "model": "sonnet", "effort": "high"}}
    (fixture.repo / ".harness/orchestration.json").write_text(
        json.dumps(
            {
                "provider_profiles": {
                    "p": {
                        "capabilities": [
                            "architecture-analysis",
                            "backend-development",
                            "code-review",
                        ],
                        "fallback": [],
                        "known_limitations": ["none"],
                    }
                },
                "assignment_plans": {
                    role: {
                        "zone": "repository",
                        "runtimes": runtime_,
                        "transport": "external"
                        if role == "architect"
                        else "in-process",
                    }
                    for role in ("architect", "developer", "code-review")
                },
                "backend_zones": {"repository": {"paths": ["**"]}},
                "concurrency_budget": 1,
                "verification_commands": ["true"],
            }
        ),
        encoding="utf-8",
    )
    batch = fixture._create_batch()
    brief = fixture._dispatch(batch["batch_id"], "architect")["brief"]
    adapter = fixture.tmp / "adapter.py"
    adapter.write_text(FAKE_ADAPTER.format(here=str(fixture.tmp)), encoding="utf-8")
    return brief, adapter


def send_external(
    fixture: test_recovery.RecoveryTests, brief: JsonObject, adapter: Path
) -> JsonObject:
    return coordinator.send_dispatch(
        fixture._args(
            dispatch=brief["dispatch_id"],
            adapter=str(adapter),
            adapter_arg=None,
            checkout=None,
            worktree=str(fixture.worktree),
        )
    )


def launches(fixture: test_recovery.RecoveryTests) -> list[str]:
    log = fixture.tmp / "launches.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_dispatch_send_hands_off_once_behind_a_committed_intent(
    recovery: test_recovery.RecoveryTests,
) -> None:
    brief, adapter = external_brief(recovery)

    sent = send_external(recovery, brief, adapter)

    assert sent["state"] == "dispatched"
    assert len(launches(recovery)) == 1
    store = RuntimeStateStore(recovery.repo, recovery.state_dir)
    record = store.read(brief["batch_id"])
    assert record is not None
    assert record["operation"]["kind"] == "dispatch-send"
    assert record["operation"]["phase"] == "committed"
    payload = store.read_intent(brief["batch_id"], record["input_refs"][0])
    assert payload["effect"]["channel"] == "adapter"
    with pytest.raises(CoordinatorError):
        send_external(recovery, brief, adapter)
    assert len(launches(recovery)) == 1
