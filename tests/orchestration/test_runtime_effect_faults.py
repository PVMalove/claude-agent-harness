"""Reconcile of a hand-off whose outcome is unknown: the adapter's or provider's launch
observation, a person's confirmed stop, a live worker, and the commands that settle first."""

from __future__ import annotations

import errno
import hashlib
import json
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from harness.json_types import JsonObject
from harness.orchestration import coordinator, extensions
from harness.orchestration.core.utils import CoordinatorError, _canonical
from harness.orchestration.ledger.lifecycle import LifecycleLedger
from harness.orchestration.runtime_effect import (
    DispatchSend,
    EffectResult,
    adapter_command,
)
from harness.orchestration.runtime_reconcile import settle
from harness.orchestration.runtime_state_store import (
    RuntimeStateError,
    RuntimeStateStore,
)
from harness.orchestration.workflow import delivery
from tests.orchestration import test_recovery
from tests.orchestration.test_runtime_effect import (
    BATCH_RECORD,
    EFFECT,
    FAKE_ADAPTER,
    STATUS,
    assert_settled,
    external_brief,
    launches,
    make_send_scene,
    runtime,
    send_external,
    settle_error,
    states,
)
from tests.orchestration.test_runtime_intent import DISPATCH, Scene
from tests.orchestration.test_runtime_reconcile_faults import (
    Crash,
    crash_on,
    settle_scene,
)


@pytest.fixture
def adapter_scene(tmp_path: Path) -> tuple[Scene, JsonObject]:
    """An approved dispatch and the hand-off effect of a fake adapter that logs its launches
    and answers ``observe`` from ``observation.json``."""
    scene = make_send_scene(tmp_path)
    adapter = tmp_path / "adapter.py"
    adapter.write_text(FAKE_ADAPTER.format(here=str(tmp_path)), encoding="utf-8")
    return scene, {**EFFECT, "adapter": str(adapter), "worker_worktree": str(tmp_path)}


def stop_during_hand_off(scene: Scene, effect: JsonObject, *, launch: bool) -> None:
    """The sender stops inside the hand-off, after (``launch``) or before the adapter ran."""

    def hand_off() -> EffectResult:
        if launch:
            command = adapter_command(Path(effect["adapter"]))
            subprocess.run([*command, "dispatch", DISPATCH], check=True)
        raise Crash("hand-off")

    with pytest.raises(Crash):
        DispatchSend(
            scene.ledger,
            scene.store(),
            batch=scene.read(BATCH_RECORD),
            status=scene.read(STATUS),
            effect=effect,
        ).run(hand_off)


def answer(directory: Path, dispatch_id: str = DISPATCH, **observation: object) -> None:
    (directory / "observation.json").write_text(
        json.dumps({"dispatch_id": dispatch_id, **observation}), encoding="utf-8"
    )


def logged(directory: Path) -> int:
    log = directory / "launches.log"
    return len(log.read_text(encoding="utf-8").splitlines()) if log.exists() else 0


def test_an_observed_launch_is_rolled_forward_as_dispatched(
    adapter_scene: tuple[Scene, JsonObject], tmp_path: Path
) -> None:
    scene, effect = adapter_scene
    stop_during_hand_off(scene, effect, launch=True)
    answer(tmp_path, launched=True, live=True, launch_id="launch-1")

    assert settle_scene(scene) is True

    assert states(scene) == ("active", "dispatched", "dispatched")
    assert scene.read(STATUS)["runtime_access"] == {
        "status": "legacy-inherit",
        "reconciled_launch": {"source": "adapter-observe", "launch_id": "launch-1"},
    }
    assert_settled(scene)
    assert logged(tmp_path) == 1


def test_an_observed_absence_proves_the_hand_off_unchanged(
    adapter_scene: tuple[Scene, JsonObject], tmp_path: Path
) -> None:
    scene, effect = adapter_scene
    ledger = scene.tree()
    stop_during_hand_off(scene, effect, launch=False)
    answer(tmp_path, launched=False, live=False)

    assert settle_scene(scene) is True

    assert scene.tree() == ledger
    assert_settled(scene)


@pytest.mark.parametrize(
    "observation",
    [
        None,
        {"dispatch_id": "dispatch-0000", "launched": True, "live": True},
        {"launched": "yes", "live": False},
        {"launched": False, "live": False, "approved_by": "adapter"},
    ],
)
def test_an_unproved_observation_stops_ambiguous_and_keeps_the_ledger(
    adapter_scene: tuple[Scene, JsonObject],
    tmp_path: Path,
    observation: JsonObject | None,
) -> None:
    scene, effect = adapter_scene
    stop_during_hand_off(scene, effect, launch=True)
    if observation is not None:
        (tmp_path / "observation.json").write_text(
            json.dumps({"dispatch_id": DISPATCH, **observation}), encoding="utf-8"
        )
    ledger = scene.tree()

    stopped = settle_error(scene)

    assert (stopped.code, stopped.step) == ("ambiguous-effect", "reconcile:effect")
    assert "--runtime-stopped" in stopped.remedy
    revision = runtime(scene)["revision"]
    assert settle_error(scene).code == "ambiguous-effect"
    assert runtime(scene)["revision"] == revision
    assert runtime(scene)["state"] == "suspended"
    # suspended is a runtime stop only: the batch in the ledger keeps its state byte for byte.
    assert scene.tree() == ledger
    assert logged(tmp_path) == 1


def retire(scene: Scene) -> None:
    """A person's confirmed stop, as ``dispatch cancel --runtime-stopped`` records it."""
    cancellation = {"approved_by": "operator", "runtime_stopped": True}
    batch = scene.read(BATCH_RECORD)
    batch["dispatches"][0].update(state="cancelled", cancellation=cancellation)
    batch["state"] = "awaiting-approval"
    status = {
        "dispatch_id": DISPATCH,
        "state": "cancelled",
        "cancellation": cancellation,
    }
    for path, record in ((BATCH_RECORD, batch), (STATUS, status)):
        (scene.records / path).write_text(json.dumps(record), encoding="utf-8")


@pytest.mark.parametrize("retired", [False, True])
def test_a_live_worker_that_contradicts_the_outcome_is_a_concurrency_conflict(
    adapter_scene: tuple[Scene, JsonObject], tmp_path: Path, retired: bool
) -> None:
    scene, effect = adapter_scene
    stop_during_hand_off(scene, effect, launch=True)
    if retired:
        retire(scene)
    answer(tmp_path, launched=retired, live=True)

    stopped = settle_error(scene)

    assert (stopped.code, stopped.step) == ("concurrency-conflict", "reconcile:effect")
    assert stopped.diagnostics["evidence"]["observation"]["live"] is True


def test_a_confirmed_stop_retires_an_unknown_hand_off(
    adapter_scene: tuple[Scene, JsonObject],
) -> None:
    scene, effect = adapter_scene
    stop_during_hand_off(scene, effect, launch=True)
    assert settle_error(scene).code == "ambiguous-effect"
    retire(scene)
    ledger = scene.tree()

    assert settle_scene(scene) is True

    assert scene.tree() == ledger
    assert_settled(scene)
    record = runtime(scene)
    seen = scene.store().read_intent(
        record["identity"]["batch_id"], record["operation"]["effect"]["evidence_ref"]
    )
    assert (seen["outcome"], seen["reason"]) == ("unchanged", "human-retired")


class Observer:
    """A native provider that answers only the launch observation."""

    def __init__(self, observation: extensions.LaunchObservation | None) -> None:
        self.observation = observation

    def observe(self, plan: JsonObject, transport: str) -> None:
        return None

    def observe_launch(self, brief: JsonObject) -> extensions.LaunchObservation | None:
        return self.observation


@pytest.fixture
def native_scene(tmp_path: Path) -> Iterator[tuple[Scene, JsonObject]]:
    scene = make_send_scene(
        tmp_path, runtime_access={"runtime_extension": "test-observer"}
    )
    brief = scene.read(f"dispatches/{DISPATCH}.json")
    sha256 = hashlib.sha256(_canonical(brief).encode("utf-8")).hexdigest()
    yield (
        scene,
        {**EFFECT, "channel": "native", "adapter": None, "brief_sha256": sha256},
    )
    extensions.unregister("runtime_access", "test-observer")


@pytest.mark.parametrize("launched", [True, False])
def test_the_native_provider_observation_settles_an_unconfirmed_handoff(
    native_scene: tuple[Scene, JsonObject], launched: bool
) -> None:
    scene, effect = native_scene
    observation = extensions.LaunchObservation(DISPATCH, launched, launched, "native-1")
    extensions.register("runtime_access", "test-observer", Observer(observation))
    stop_during_hand_off(scene, effect, launch=False)

    assert settle_scene(scene) is True

    expected = "dispatched" if launched else "approved"
    assert states(scene)[1:] == (expected, expected)
    if launched:
        assert scene.read(STATUS)["runtime_access"]["reconciled_launch"] == {
            "source": "provider-observe",
            "launch_id": "native-1",
        }
    assert_settled(scene)


def test_a_provider_without_an_observation_leaves_the_handoff_ambiguous(
    native_scene: tuple[Scene, JsonObject],
) -> None:
    scene, effect = native_scene
    extensions.register("runtime_access", "test-observer", Observer(None))
    stop_during_hand_off(scene, effect, launch=False)

    assert settle_error(scene).code == "ambiguous-effect"


# --- The public commands -----------------------------------------------------------------------


def stop_after_adapter(monkeypatch: pytest.MonkeyPatch, *, ran: bool) -> None:
    """The sender stops right after (``ran``) or right before the adapter call."""
    original = delivery._run_adapter

    def stopping(command: list[str], *, cwd: Path | None = None) -> None:
        if ran:
            original(command, cwd=cwd)
        raise Crash("adapter call")

    monkeypatch.setattr(delivery, "_run_adapter", stopping)


def test_a_repeated_send_after_an_observed_launch_never_starts_a_second_worker(
    recovery: test_recovery.RecoveryTests, monkeypatch: pytest.MonkeyPatch
) -> None:
    brief, adapter = external_brief(recovery)
    stop_after_adapter(monkeypatch, ran=True)
    with pytest.raises(Crash):
        send_external(recovery, brief, adapter)
    monkeypatch.undo()
    answer(recovery.tmp, brief["dispatch_id"], launched=True, live=True)

    with pytest.raises(CoordinatorError) as refused:
        send_external(recovery, brief, adapter)

    assert "only an approved dispatch" in refused.value.message
    assert len(launches(recovery)) == 1
    coordinator.self_report_dispatch(
        recovery._args(
            dispatch=brief["dispatch_id"],
            model="sonnet",
            worktree=str(recovery.worktree),
        )
    )


def test_a_send_proved_unchanged_hands_off_once_on_repeat(
    recovery: test_recovery.RecoveryTests, monkeypatch: pytest.MonkeyPatch
) -> None:
    brief, adapter = external_brief(recovery)
    stop_after_adapter(monkeypatch, ran=False)
    with pytest.raises(Crash):
        send_external(recovery, brief, adapter)
    monkeypatch.undo()
    answer(recovery.tmp, brief["dispatch_id"], launched=False, live=False)

    assert send_external(recovery, brief, adapter)["state"] == "dispatched"

    assert len(launches(recovery)) == 1


def test_self_report_finishes_a_send_stopped_after_its_observation(
    recovery: test_recovery.RecoveryTests, monkeypatch: pytest.MonkeyPatch
) -> None:
    brief, adapter = external_brief(recovery)
    crash_on(monkeypatch, RuntimeStateStore, "write_intent", 2, after=True)
    with pytest.raises(Crash):
        send_external(recovery, brief, adapter)
    monkeypatch.undo()

    reported = coordinator.self_report_dispatch(
        recovery._args(
            dispatch=brief["dispatch_id"],
            model="sonnet",
            worktree=str(recovery.worktree),
        )
    )

    assert reported["state"] == "working"
    assert len(launches(recovery)) == 1


def cancel(
    recovery: test_recovery.RecoveryTests, brief: JsonObject, *, stopped: bool
) -> JsonObject:
    return coordinator.cancel_dispatch(
        recovery._args(
            dispatch=brief["dispatch_id"],
            reason="the worker runtime was stopped",
            runtime_stopped=stopped,
            **recovery._approval(),
        )
    )


def test_only_a_confirmed_stop_cancels_an_unknown_hand_off(
    recovery: test_recovery.RecoveryTests, monkeypatch: pytest.MonkeyPatch
) -> None:
    brief, adapter = external_brief(recovery)
    stop_after_adapter(monkeypatch, ran=True)
    with pytest.raises(Crash):
        send_external(recovery, brief, adapter)
    monkeypatch.undo()
    records = LifecycleLedger(recovery.state_dir).records_root()
    status = records / "dispatch-status" / f"{brief['dispatch_id']}.json"
    before = status.read_bytes()

    with pytest.raises(CoordinatorError) as refused:
        cancel(recovery, brief, stopped=False)

    assert "--runtime-stopped" in refused.value.remedy
    assert status.read_bytes() == before
    assert cancel(recovery, brief, stopped=True)["state"] == "cancelled"
    with pytest.raises(CoordinatorError):
        send_external(recovery, brief, adapter)
    store = RuntimeStateStore(recovery.repo, recovery.state_dir)
    record = store.read(brief["batch_id"])
    assert record is not None
    assert (record["state"], record["operation"]["phase"]) == ("ready", "committed")
    assert len(launches(recovery)) == 1


def fill_disk_on_ledger_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hand-off's ledger records cannot be written, as on a full disk, after the store
    recorded the observed outcome."""

    def full_disk(*arguments: object, **keywords: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(LifecycleLedger, "replace", full_disk)


def stop_after_the_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sender stops once the observation payload is durable, before the store record."""
    crash_on(monkeypatch, RuntimeStateStore, "write_intent", 2, after=True)


@pytest.mark.parametrize("stopped", [False, True])
@pytest.mark.parametrize(
    "interrupt", [fill_disk_on_ledger_write, stop_after_the_observation]
)
def test_a_cancel_commits_an_observed_hand_off_before_it_decides(
    recovery: test_recovery.RecoveryTests,
    monkeypatch: pytest.MonkeyPatch,
    interrupt: Callable[[pytest.MonkeyPatch], None],
    stopped: bool,
) -> None:
    brief, adapter = external_brief(recovery)
    interrupt(monkeypatch)
    with pytest.raises((Crash, RuntimeStateError)):
        send_external(recovery, brief, adapter)
    monkeypatch.undo()
    store = RuntimeStateStore(recovery.repo, recovery.state_dir)
    assert (store.read(brief["batch_id"]) or {})["operation"]["phase"] != "committed"

    if stopped:
        assert cancel(recovery, brief, stopped=True)["state"] == "cancelled"
    else:
        # The launch is committed first, so a cancel without a confirmed stop is refused.
        with pytest.raises(CoordinatorError) as refused:
            cancel(recovery, brief, stopped=False)
        assert "only an approved, unsent dispatch" in refused.value.message

    record = store.read(brief["batch_id"])
    assert record is not None
    assert (record["state"], record["operation"]["phase"]) == ("ready", "committed")
    records = LifecycleLedger(recovery.state_dir).records_root()
    status = json.loads(
        (records / "dispatch-status" / f"{brief['dispatch_id']}.json").read_text(
            encoding="utf-8"
        )
    )
    assert status["state"] == ("cancelled" if stopped else "dispatched")
    ledger = LifecycleLedger(recovery.state_dir)
    assert settle(recovery.repo, recovery.state_dir, ledger, brief["batch_id"]) is False
    assert len(launches(recovery)) == 1
