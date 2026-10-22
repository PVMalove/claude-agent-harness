"""The coordinator read commands after the cutover (ADR 0020, sections 1 and 3).

A state the real coordinator wrote answers ``batch list``, ``dispatch status``, ``batch
decision-packet`` and ``qa status`` once from the file ledger and once, after ``harness-runtime
migrate``, from the runtime store through ``runtime_client``. The two answers are the same JSON,
and the second run never opens the file ledger or ``runtime.db`` (an audit hook of the Python
process proves it). The real Rust process serves the second run; CI builds it first.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from harness.json_types import JsonObject
from harness.orchestration.core import state_view
from harness.orchestration.ledger import ledger_ops
from harness.orchestration.runtime_client import RuntimeClientError
from harness.orchestration.workflow import batch

from .runtime_store_support import (
    ROOT,
    STATE_REL,
    ServingRuntime,
    coordinator_with,
    migrate,
    tree,
    write_coordinator_state,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the seam tests drive a Unix-socket runtime"
)


def _commands(ids: dict[str, str]) -> dict[str, list[str]]:
    return {
        "batch list": ["batch", "list"],
        "dispatch status": ["dispatch", "status"],
        "batch decision-packet": ["batch", "decision-packet", "--batch", ids["batch_id"]],
        "qa status": ["qa", "status"],
    }


def _answer(repo: Path, runtime: ServingRuntime | None, *command: str) -> dict[str, object]:
    result = coordinator_with(repo, runtime, *command)
    assert result.returncode == 0, result.stderr
    answer: dict[str, object] = json.loads(result.stdout)
    return answer


def _comparable(name: str, answer: dict[str, object]) -> dict[str, object]:
    """The answer without what differs by construction: elapsed time, and the address of the report."""
    comparable = json.loads(json.dumps(answer))
    if name == "dispatch status":
        for entry in comparable["dispatches"]:
            entry.pop("silent_seconds")
            entry["startup"].pop("startup_elapsed_seconds")
    if name == "batch decision-packet":
        comparable["report"] = comparable["report"].rsplit("/reports/", 1)[1]
    return comparable  # type: ignore[no-any-return]


@pytest.fixture
def states(tmp_path: Path) -> Iterator[tuple[Path, dict[str, str], dict[str, dict[str, object]]]]:
    """A repository, the answers of the file ledger, and the same repository after the cutover."""
    repo, ids = write_coordinator_state(tmp_path)
    before = {
        name: _comparable(name, _answer(repo, None, *command))
        for name, command in _commands(ids).items()
    }
    result = migrate(repo)
    assert result.returncode == 0, result.stderr
    yield repo, ids, before


def test_the_four_commands_answer_the_same_json_from_the_store(
    states: tuple[Path, dict[str, str], dict[str, dict[str, object]]],
) -> None:
    repo, ids, before = states
    runtime = ServingRuntime(repo)
    try:
        for name, command in _commands(ids).items():
            after = _comparable(name, _answer(repo, runtime, *command))
            assert after == before[name], name
    finally:
        runtime.stop()


def test_the_decision_packet_names_the_report_inside_the_store_not_a_file(
    states: tuple[Path, dict[str, str], dict[str, dict[str, object]]],
) -> None:
    repo, ids, _ = states
    runtime = ServingRuntime(repo)
    try:
        packet = _answer(repo, runtime, *_commands(ids)["batch decision-packet"])
    finally:
        runtime.stop()

    report = packet["report"]
    assert isinstance(report, str)
    assert report == str(repo / STATE_REL / "runtime.db" / "reports" / f"{ids['dispatch_id']}.json")
    assert not Path(report).exists()


AUDITING = """
import json, os, runpy, sys
FORBIDDEN = (".coordinator.lock", "runtime.db", "/generations/")
CALLS = {"open", "os.scandir", "os.listdir", "os.mkdir", "os.rmdir", "os.remove", "os.rename", "os.replace", "os.stat"}
seen = []

def hook(event, arguments):
    if event not in CALLS:
        return
    text = " ".join(str(a) for a in arguments if isinstance(a, (str, bytes, os.PathLike)))
    if any(marker in text for marker in FORBIDDEN):
        seen.append([event, text])

sys.addaudithook(hook)
script = sys.argv[1]
sys.argv = [script] + sys.argv[2:]
code = 0
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit as exit_:
    code = exit_.code or 0
finally:
    with open(os.environ["AUDIT_LOG"], "w", encoding="utf-8") as out:
        json.dump(seen, out)
sys.exit(code)
"""


def _audited(
    repo: Path, runtime: ServingRuntime | None, command: list[str], log: Path
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    """Run one coordinator command with an audit hook that logs every use of the file ledger."""
    environ = {"XDG_RUNTIME_DIR": str(runtime.xdg)} if runtime else {}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            AUDITING,
            str(ROOT / "harness/orchestration/coordinator.py"),
            "--repo",
            str(repo),
            *command,
        ],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "AUDIT_LOG": str(log),
            **environ,
        },
    )
    events: list[list[str]] = json.loads(log.read_text(encoding="utf-8"))
    return result, events


def test_the_audit_hook_does_see_a_read_of_the_file_ledger(tmp_path: Path) -> None:
    repo, ids = write_coordinator_state(tmp_path)

    result, events = _audited(repo, None, _commands(ids)["batch list"], tmp_path / "audit.json")

    assert result.returncode == 0, result.stderr
    assert any(event == "os.scandir" and "/generations/" in path for event, path in events)
    assert any(".coordinator.lock" in path for _, path in events)


@pytest.mark.parametrize(
    "name", ["batch list", "dispatch status", "batch decision-packet", "qa status"]
)
def test_a_read_never_opens_the_file_ledger_or_the_store(
    states: tuple[Path, dict[str, str], dict[str, dict[str, object]]],
    tmp_path: Path,
    name: str,
) -> None:
    repo, ids, _ = states
    runtime = ServingRuntime(repo)
    try:
        result, events = _audited(repo, runtime, _commands(ids)[name], tmp_path / "audit.json")
    finally:
        runtime.stop()

    assert result.returncode == 0, result.stderr
    assert events == []
    assert not (repo / STATE_REL / ".coordinator.lock").exists()


def test_without_a_runtime_a_read_is_runtime_unavailable_and_falls_back_to_nothing(
    states: tuple[Path, dict[str, str], dict[str, dict[str, object]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _, _ = states
    monkeypatch.setattr(ledger_ops, "READ_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(repo / "no-runtime-here"))
    files = tree(repo / STATE_REL, skip=("runtime.db-",))
    args = argparse.Namespace(repo=str(repo), state_dir=None, ticket=None, state=None, open=False)

    with pytest.raises(RuntimeClientError) as refused:
        batch.list_batches(args)

    assert refused.value.code == "runtime_unavailable"
    assert tree(repo / STATE_REL, skip=("runtime.db-",)) == files
    assert not (repo / STATE_REL / ".coordinator.lock").exists()


# -- the view asks for a whole collection once it is read record by record --------------------------


class _CountingStore:
    """A store of ``count`` dispatches that counts the requests a view makes."""

    def __init__(self, count: int, *, too_large: bool = False) -> None:
        self.records = {
            f"dispatches/dispatch-{number}.json": {"dispatch_id": f"dispatch-{number}"}
            for number in range(count)
        }
        self.too_large = too_large
        self.requests: list[tuple[list[str], list[str]]] = []

    def fetch(
        self, collections: Sequence[str], paths: Sequence[str], revision: int | None
    ) -> tuple[dict[str, JsonObject], int | None]:
        self.requests.append((list(collections), list(paths)))
        if collections and self.too_large:
            raise state_view.AnswerTooLarge("the answer would not fit one frame")
        found = {
            path: record
            for path, record in self.records.items()
            if path in paths or path.split("/")[0] in collections
        }
        return found, 7


def _view(store: _CountingStore, tmp_path: Path) -> state_view.StateView:
    return state_view.StateView(tmp_path, store.fetch)


def test_a_view_asks_for_a_whole_collection_after_a_few_point_reads(tmp_path: Path) -> None:
    store = _CountingStore(50)
    view = _view(store, tmp_path)

    found = [view.read(f"dispatches/dispatch-{number}.json") for number in range(50)]

    assert all(record is not None for record in found)
    assert store.requests == [
        ([], ["dispatches/dispatch-0.json"]),
        ([], ["dispatches/dispatch-1.json"]),
        ([], ["dispatches/dispatch-2.json"]),
        (["dispatches"], []),
    ]
    assert view.revision == 7
    assert view.read("dispatches/dispatch-none.json") is None


def test_a_view_that_is_refused_a_whole_collection_goes_on_with_point_reads(
    tmp_path: Path,
) -> None:
    store = _CountingStore(8, too_large=True)
    view = _view(store, tmp_path)

    found = [view.read(f"dispatches/dispatch-{number}.json") for number in range(8)]

    assert all(record is not None for record in found)
    assert [request for request in store.requests if request[0]] == [(["dispatches"], [])]
    assert len([request for request in store.requests if request[1]]) == 8


def test_a_read_of_a_record_outside_a_bulk_collection_stays_a_point_read(tmp_path: Path) -> None:
    store = _CountingStore(0)
    view = _view(store, tmp_path)

    for number in range(6):
        assert view.read(f"reports/dispatch-{number}.json") is None

    assert all(not collections for collections, _ in store.requests)
