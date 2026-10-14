"""Runtime state store (runtimeState.v1): schema, record rules, binding, locking and invariants."""

from __future__ import annotations

import ast
import copy
import json
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness.cleanup import plan_cleanup
from harness.json_types import JsonObject
from harness.orchestration import runtime_state_store as store
from harness.orchestration.core.constants import STATE_REL, BatchState
from harness.orchestration.core.runtime_state_contract import (
    RUNTIME_STATE_CONTRACT,
    RUNTIME_STATE_DIR,
    RUNTIME_TRANSITIONS,
    RUNTIME_VOCABULARIES,
    transition_problem,
)
from harness.orchestration.core.system_fields import enum_problem, load_registry
from harness.orchestration.ledger import BatchRecord, LifecycleLedger
from harness.orchestration.ledger.lifecycle import _guard_byte
from harness.orchestration.runtime_state_store import (
    RuntimeStateError,
    RuntimeStateStore,
)

HASH = "a" * 64
BATCH = "batch-0123abcd"


def _identity(batch_id: str = BATCH) -> JsonObject:
    return {
        "repo_root": "/repo",
        "git_common_dir": "/repo/.git",
        "state_root": "/repo/.harness/orchestration/state",
        "namespace": "default",
        "batch_id": batch_id,
    }


def _record(**changes: object) -> JsonObject:
    record: JsonObject = {
        "contract": RUNTIME_STATE_CONTRACT,
        "identity": _identity(),
        "revision": 1,
        "updated_at": "2026-10-14T09:00:00+00:00",
        "state": "ready",
        "stage": None,
        "dispatch_id": None,
        "operation": None,
        "ledger": {"generation": None, "record_hashes": {}},
        "policy_snapshot_sha256": None,
        "input_refs": [],
        "output_refs": [],
        "last_confirmed_boundary": None,
        "failure": None,
    }
    record.update(changes)
    return record


def _operation(
    phase: str = "prepared", effect: str = "not-started", operation_id: str = "op-1"
) -> JsonObject:
    return {
        "operation_id": operation_id,
        "kind": "dispatch-send",
        "phase": phase,
        "idempotency_key": "key-1",
        "expected_before_sha256": HASH,
        "intended_after_sha256": None,
        "effect": {"state": effect, "evidence_ref": None, "observed_at": None},
    }


def _next(previous: JsonObject, **changes: object) -> JsonObject:
    record = copy.deepcopy(previous)
    record["revision"] = previous["revision"] + 1
    record.update(changes)
    return record


def _settled_ledger() -> JsonObject:
    return {"generation": "generation-1", "record_hashes": {"batches/b.json": HASH}}


def _system_nodes(node: object) -> Iterator[JsonObject]:
    if isinstance(node, dict):
        if store.SYSTEM_FIELD in node or "enum" in node:
            yield node
        for child in node.values():
            yield from _system_nodes(child)
    elif isinstance(node, list):
        for child in node:
            yield from _system_nodes(child)


# --- Schema and record rules -------------------------------------------------------------------


def test_a_complete_record_is_valid_and_every_nullable_field_takes_null() -> None:
    full = _record(
        stage="developer",
        dispatch_id="dispatch-0abc",
        operation=_operation(),
        policy_snapshot_sha256=HASH,
        input_refs=["brief.json"],
        output_refs=["report.json"],
        last_confirmed_boundary={
            "revision": 1,
            "state": "ready",
            "confirmed_at": "2026-10-14T09:00:00Z",
        },
        failure={"code": "execution-failure", "step": "send", "message": "exit 1"},
    )

    assert store.validate_record(_record(), None) == []
    assert store.validate_record(full, None) == []


def test_every_record_field_is_required() -> None:
    for key in _record():
        record = _record()
        del record[key]
        problems = store.validate_record(record, None)
        assert problems, key
        if key != "contract":
            assert any(key in problem for problem in problems), key


def test_unknown_fields_are_refused_at_every_level() -> None:
    nested = _record(operation=_operation())
    nested["operation"]["effect"]["extra"] = True

    assert store.validate_record(_record(extra=1), None)
    assert store.validate_record(nested, None)
    assert store.validate_record(_record(identity={**_identity(), "host": "x"}), None)


def test_a_missing_or_foreign_contract_is_a_compatibility_problem_without_defaults() -> (
    None
):
    record = _record()
    del record["contract"]

    assert store.compatibility_problem(record) is not None
    assert store.compatibility_problem(_record(contract="runtimeState.v0")) is not None
    assert store.compatibility_problem(_record()) is None
    assert "contract" in store.validate_record(record, None)[0]


@pytest.mark.parametrize(
    ("changes", "field_id"),
    [
        ({"state": "paused"}, "runtime.state"),
        ({"operation": _operation(phase="done")}, "runtime.operation_phase"),
        ({"operation": {**_operation(), "kind": "push"}}, "runtime.operation_kind"),
        ({"operation": _operation(effect="maybe")}, "runtime.effect_state"),
        (
            {"failure": {"code": "oops", "step": "send", "message": "x"}},
            "runtime.failure_code",
        ),
    ],
)
def test_an_unknown_enum_value_names_the_allowed_values(
    changes: JsonObject, field_id: str
) -> None:
    problems = store.validate_record(_record(**changes), None)

    assert len(problems) == 1
    assert all(value in problems[0] for value in RUNTIME_VOCABULARIES[field_id])


def test_an_enum_spelling_variant_is_refused_with_the_exact_spelling() -> None:
    problems = store.validate_record(_record(state="Ready"), None)

    assert problems == [
        enum_problem("record.state", "Ready", RUNTIME_VOCABULARIES["runtime.state"])
    ]
    assert "write exactly 'ready'" in problems[0]


def test_every_schema_enum_spells_its_registry_field() -> None:
    registry = load_registry()
    nodes = list(_system_nodes(store._schema()))

    assert {node[store.SYSTEM_FIELD] for node in nodes} == set(RUNTIME_VOCABULARIES)
    for node in nodes:
        assert sorted(node["enum"]) == sorted(
            registry.fields[node[store.SYSTEM_FIELD]].values
        ), node[store.SYSTEM_FIELD]


def test_the_contract_constant_matches_the_schema() -> None:
    schema = json.loads(store.SCHEMA_PATH.read_text(encoding="utf-8"))

    assert schema["properties"]["contract"] == {"const": RUNTIME_STATE_CONTRACT}
    assert schema["$id"] == RUNTIME_STATE_CONTRACT


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"revision": 2}, "revision 1"),
        ({"state": "running"}, "state 'ready'"),
        ({"operation": _operation(phase="effect-observed")}, "prepared"),
        ({"updated_at": "yesterday"}, "ISO-8601"),
        ({"updated_at": "2026-10-14T09:00:00"}, "timezone"),
    ],
)
def test_creation_rules(changes: JsonObject, fragment: str) -> None:
    problems = store.validate_record(_record(**changes), None)

    assert any(fragment in problem for problem in problems), problems


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (before, after)
        for before in RUNTIME_VOCABULARIES["runtime.state"]
        for after in RUNTIME_VOCABULARIES["runtime.state"]
    ],
)
def test_state_follows_the_transition_table(before: str, after: str) -> None:
    previous = _record(state=before, revision=3)
    problems = store.validate_record(_next(previous, state=after), previous)

    allowed = before != "completed" and (
        after == before or after in RUNTIME_TRANSITIONS[before]
    )
    assert (problems == []) is allowed, problems


def test_the_revision_advances_by_exactly_one_and_identity_is_immutable() -> None:
    previous = _record(revision=2)

    assert store.validate_record(_next(previous, revision=4), previous)
    assert store.validate_record(_next(previous, revision=2), previous)
    assert store.validate_record(
        _next(previous, identity=_identity("batch-ffff")), previous
    )


def test_an_operation_keeps_its_identity_and_follows_its_tables() -> None:
    previous = _record(operation=_operation())
    observed = _next(previous, operation=_operation("effect-observed", "confirmed"))
    changed_key = _next(previous, operation={**_operation(), "idempotency_key": "k2"})
    backwards = _next(observed, operation=_operation("prepared", "confirmed"))
    unknown_back = _next(observed, operation=_operation("effect-observed", "unknown"))

    assert store.validate_record(observed, previous) == []
    assert "operation idempotency_key is immutable" in store.validate_record(
        changed_key, previous
    )
    assert store.validate_record(backwards, observed)
    assert store.validate_record(unknown_back, observed)


def test_a_new_operation_waits_for_the_previous_commit() -> None:
    settled = {"ledger": _settled_ledger()}
    open_op = _record(operation=_operation(), **settled)
    done = _record(operation=_operation("committed", "confirmed"), **settled)
    following = _operation(operation_id="op-2")

    assert store.validate_record(_next(open_op, operation=following), open_op)
    assert store.validate_record(_next(open_op, operation=None), open_op)
    assert store.validate_record(_next(done, operation=following), done) == []
    assert store.validate_record(_next(done, operation=None), done) == []


@pytest.mark.parametrize("phase", ["ledger-committed", "committed"])
def test_legacy_metadata_never_counts_as_a_settled_effect(phase: str) -> None:
    unknown = _record(operation=_operation(phase, "unknown"), ledger=_settled_ledger())
    no_hashes = _record(operation=_operation(phase, "confirmed"))
    settled = _record(
        operation=_operation(phase, "confirmed"), ledger=_settled_ledger()
    )

    assert any("confirmed effect" in p for p in store.validate_record(unknown, None))
    assert any("record hashes" in p for p in store.validate_record(no_hashes, None))
    assert store._settled_problems(settled) == []


@pytest.mark.parametrize(
    "message",
    ["token: ghp_" + "x" * 30, "cannot open /home/user/secret.json", "x" * 300],
)
def test_a_failure_message_must_already_be_sanitized(message: str) -> None:
    failure = {"code": "execution-failure", "step": "send", "message": message}

    assert any(
        "sanitized" in problem
        for problem in store.validate_record(_record(failure=failure), None)
    )


def test_sanitize_detail_masks_secrets_and_absolute_paths_and_is_idempotent() -> None:
    text = (
        "read /var/x/state.json and C:\\Users\\a\\b failed: password=hunter2 ghp_"
        + ("y" * 30)
    )
    cleaned = store.sanitize_detail(text)

    assert "/var/x" not in cleaned and "C:\\Users" not in cleaned
    assert "hunter2" not in cleaned and "ghp_" not in cleaned
    assert store.sanitize_detail(cleaned) == cleaned
    assert store.sanitize_detail("orchestration/runtime-state/x.json") == (
        "orchestration/runtime-state/x.json"
    )


def test_transition_problem_refuses_unknown_tables_and_sources() -> None:
    assert transition_problem("runtime.state", "ready", "ready") is None
    assert transition_problem("runtime.state", "ready", "running") is None
    assert "terminal" in str(transition_problem("runtime.state", "completed", "ready"))
    assert transition_problem("runtime.state", "paused", "ready") is not None
    assert transition_problem("batch.state", "active", "paused") is not None
    assert transition_problem("runtime.effect_state", "unknown", "confirmed") is None
    assert (
        transition_problem("runtime.operation_phase", "committed", "prepared")
        is not None
    )


# --- Canonical binding, self-ignoring directory and cleanup ------------------------------------


def _git(checkout: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(checkout), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path]:
    """A main checkout without any root ``.gitignore`` and one linked worktree."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    _git(main, "config", "user.email", "test@example.invalid")
    _git(main, "config", "user.name", "Test")
    (main / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(main, "add", "README.md")
    _git(main, "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "-q", str(linked))
    return main.resolve(), linked.resolve()


def _put(runtime: RuntimeStateStore, record: JsonObject) -> Path:
    """Place a record on disk as an earlier writer left it."""
    runtime._prepare()
    path = runtime.path(record["identity"]["batch_id"])
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def _own(
    runtime: RuntimeStateStore, batch_id: str = BATCH, **changes: object
) -> JsonObject:
    return _record(identity=runtime.location.identity(batch_id), **changes)


def _refusal(code: str, step: str | None = None) -> pytest.RaisesExc[RuntimeStateError]:
    pattern = f"runtime state {step or '[a-z-]+'} stopped with {code}"
    return pytest.raises(RuntimeStateError, match=pattern)


def _tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_linked_worktrees_share_the_default_store(repos: tuple[Path, Path]) -> None:
    main, linked = repos
    from_main = RuntimeStateStore(main).location
    from_linked = RuntimeStateStore(linked).location

    assert from_linked == from_main
    assert RuntimeStateStore(linked, main / STATE_REL).location == from_main
    assert from_main.namespace == "default"
    assert from_main.directory == main / ".harness" / RUNTIME_STATE_DIR
    assert from_main.repo_root == main
    assert from_main.git_common_dir == main / ".git"
    assert from_main.state_root == main / STATE_REL


def test_an_alternate_state_dir_is_an_isolated_namespace_of_the_same_store(
    repos: tuple[Path, Path],
) -> None:
    main, linked = repos
    scratch = main / ".harness" / ".sandboxes" / "scratch"
    alternate = RuntimeStateStore(linked, scratch / "state").location
    other = RuntimeStateStore(main, scratch / "other").location

    assert RuntimeStateStore(main, scratch / "state").location == alternate
    assert alternate.namespace.startswith("alt-") and other.namespace.startswith("alt-")
    assert alternate.namespace != other.namespace
    assert alternate.directory == (
        main / ".harness" / RUNTIME_STATE_DIR / "alternate" / alternate.namespace[4:]
    )
    assert alternate.identity(BATCH)["state_root"] == str(scratch / "state")
    assert (
        store.validate_record(_record(identity=alternate.identity(BATCH)), None) == []
    )


def test_a_record_of_another_identity_is_a_controlled_stop(
    repos: tuple[Path, Path],
) -> None:
    main, _ = repos
    default = RuntimeStateStore(main)
    alternate = RuntimeStateStore(main, main / "elsewhere")
    foreign = _put(default, _own(alternate))
    renamed = foreign.with_name("batch-ffff.json")
    renamed.write_bytes(_put(default, _own(default)).read_bytes())

    with _refusal("integrity-failure", "identity"):
        RuntimeStateStore(main).read("batch-ffff")
    foreign.write_text(json.dumps(_own(alternate)), encoding="utf-8")
    with _refusal("integrity-failure", "identity"):
        default.read(BATCH)
    assert alternate.read(BATCH) is None


def test_unreadable_foreign_or_invalid_records_are_never_defaulted(
    repos: tuple[Path, Path],
) -> None:
    main, _ = repos
    runtime = RuntimeStateStore(main)
    path = _put(runtime, _own(runtime))

    assert runtime.read(BATCH) == _own(runtime)
    path.write_text("{not json", encoding="utf-8")
    with _refusal("integrity-failure", "read"):
        runtime.read(BATCH)
    legacy = _own(runtime)
    del legacy["contract"]
    path.write_text(json.dumps(legacy), encoding="utf-8")
    with _refusal("runtime-compatibility", "read"):
        runtime.read(BATCH)
    path.write_text(json.dumps(_own(runtime, state="Suspended")), encoding="utf-8")
    with _refusal("integrity-failure", "read"):
        runtime.read(BATCH)


@pytest.mark.parametrize("effect", ["unknown", "not-started"])
def test_a_stored_settled_phase_without_a_confirmed_effect_is_refused_on_read(
    repos: tuple[Path, Path], effect: str
) -> None:
    runtime = RuntimeStateStore(repos[0])
    _put(
        runtime,
        _own(
            runtime, operation=_operation("committed", effect), ledger=_settled_ledger()
        ),
    )

    with _refusal("integrity-failure", "read") as caught:
        runtime.read(BATCH)
    assert "requires a confirmed effect" in caught.value.message


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"updated_at": "2026-10-14T09:00:00"}, "updated_at must include a timezone"),
        (
            {
                "failure": {
                    "code": "execution-failure",
                    "step": "send",
                    "message": "token: ghp_" + "x" * 30,
                }
            },
            "failure.message must be sanitized",
        ),
    ],
)
def test_a_stored_record_is_read_by_the_rules_a_write_is_checked_by(
    repos: tuple[Path, Path], changes: JsonObject, fragment: str
) -> None:
    runtime = RuntimeStateStore(repos[0])
    placed = _own(runtime, **changes)
    assert any(fragment in problem for problem in store.validate_record(placed, None))
    _put(runtime, placed)

    with _refusal("integrity-failure", "read") as caught:
        runtime.read(BATCH)
    assert fragment in caught.value.message
    assert "ghp_" not in caught.value.message


def test_outside_a_git_checkout_the_store_is_unavailable(tmp_path: Path) -> None:
    with _refusal("storage-unavailable", "resolve-root") as caught:
        RuntimeStateStore(tmp_path)

    assert caught.value.progress_saved is False
    assert caught.value.diagnostics["code"] == "storage-unavailable"


@pytest.mark.parametrize("batch_id", ["../batch-1", "dispatch-12", "batch-XYZ", ""])
def test_only_a_coordinator_batch_id_names_a_record(
    repos: tuple[Path, Path], batch_id: str
) -> None:
    with _refusal("integrity-failure", "identity"):
        RuntimeStateStore(repos[0]).path(batch_id)


def test_a_symlinked_store_directory_is_refused(
    repos: tuple[Path, Path], tmp_path: Path
) -> None:
    runtime = RuntimeStateStore(repos[0])
    runtime.location.base.parent.mkdir(parents=True)
    target = tmp_path / "outside"
    target.mkdir()
    try:
        runtime.location.base.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are unavailable on this platform")

    with _refusal("integrity-failure", "prepare"):
        runtime._prepare()
    assert list(target.iterdir()) == []


def test_the_store_ignores_itself_and_keeps_every_checkout_clean(
    repos: tuple[Path, Path],
) -> None:
    main, linked = repos
    _put(RuntimeStateStore(linked), _own(RuntimeStateStore(linked)))
    alternate = RuntimeStateStore(linked, linked / ".harness" / "state")
    _put(alternate, _own(alternate))
    ignore = main / ".harness" / RUNTIME_STATE_DIR / ".gitignore"

    assert ignore.read_bytes() == b"*\n"
    for checkout in repos:
        assert _git(checkout, "status", "--porcelain", "--untracked-files=all") == ""
    ignore.write_bytes(b"*\n# kept\n")
    RuntimeStateStore(main)._prepare()
    assert ignore.read_bytes() == b"*\n# kept\n"


def test_generic_cleanup_and_ledger_maintenance_leave_the_store_alone(
    repos: tuple[Path, Path],
) -> None:
    main, linked = repos
    scratch = main / ".harness" / ".sandboxes" / "scratch"
    stores = [
        RuntimeStateStore(main),
        RuntimeStateStore(linked, scratch / "state"),
        RuntimeStateStore(linked, linked / ".harness" / "orchestration" / "state"),
    ]
    for runtime in stores:
        _put(runtime, _own(runtime))
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "transit.txt").write_text("old", encoding="utf-8")
    base = stores[0].location.base
    before = _tree(base)

    for checkout in repos:
        plan = plan_cleanup(checkout, "hard", min_age_hours=0)
        removed = [Path(item["path"]).resolve() for item in plan["remove"]]
        assert removed, checkout
        assert not [path for path in removed if path.is_relative_to(base)], checkout
    for runtime in stores:
        ledger = LifecycleLedger(runtime.location.state_root)
        ledger.ensure()
        ledger.clean()
        ledger.reset("RESET")
    assert len(before) == 4
    assert _tree(base) == before


# --- Locked write with an expected revision ----------------------------------------------------


def test_a_record_round_trips_revision_by_revision(repos: tuple[Path, Path]) -> None:
    runtime = RuntimeStateStore(repos[1])
    first = _own(runtime)
    second = _next(first, state="running", stage="developer")

    assert runtime.write(first, expected_revision=0).revision == 1
    result = runtime.write(second, expected_revision=1)

    assert result.revision == 2
    assert result.directory_fsync in {"done", "unsupported"}
    assert RuntimeStateStore(repos[0]).read(BATCH) == second
    assert list(runtime.location.directory.glob(".*.tmp")) == []


def test_a_stale_expected_revision_is_a_conflict_without_overwrite(
    repos: tuple[Path, Path],
) -> None:
    runtime = RuntimeStateStore(repos[0])
    first = _own(runtime)
    runtime.write(first, expected_revision=0)
    path = runtime.path(BATCH)
    runtime.write(_next(first, state="running"), expected_revision=1)
    before = path.read_bytes()

    with _refusal("concurrency-conflict", "revision") as caught:
        runtime.write(_next(first, state="running", stage="other"), expected_revision=1)
    with _refusal("concurrency-conflict", "revision"):
        runtime.write(first, expected_revision=0)
    assert caught.value.durable_revision == 2
    assert path.read_bytes() == before


def test_an_invalid_write_is_refused_before_anything_is_written(
    repos: tuple[Path, Path],
) -> None:
    main, linked = repos
    runtime = RuntimeStateStore(main)
    other = RuntimeStateStore(main, main / "elsewhere")

    with _refusal("integrity-failure", "validate"):
        runtime.write(_own(runtime, state="paused"), expected_revision=0)
    with _refusal("runtime-compatibility", "validate"):
        runtime.write(_own(runtime, contract="runtimeState.v0"), expected_revision=0)
    with _refusal("integrity-failure", "identity"):
        runtime.write(_own(other), expected_revision=0)
    assert not runtime.location.base.exists()

    first = _own(runtime)
    runtime.write(first, expected_revision=0)
    with _refusal("integrity-failure", "validate"):
        runtime.write(_next(first, revision=3), expected_revision=1)
    with _refusal("integrity-failure", "validate"):
        runtime.write(_next(first, state="completed"), expected_revision=1)
    running = _next(first, state="running")
    done = _next(running, state="completed")
    runtime.write(running, expected_revision=1)
    runtime.write(done, expected_revision=2)
    with _refusal("integrity-failure", "validate") as caught:
        RuntimeStateStore(linked).write(_next(done), expected_revision=3)
    assert "completed runtime state record is immutable" in caught.value.message
    assert runtime.read(BATCH) == done


def test_a_busy_lock_is_a_bounded_conflict_without_overwrite(
    repos: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = RuntimeStateStore(repos[0])
    first = _own(runtime)
    runtime.write(first, expected_revision=0)
    before = runtime.path(BATCH).read_bytes()
    monkeypatch.setattr(store, "LOCK_TIMEOUT_SECONDS", 0.3)

    with (runtime.location.directory / f"{BATCH}.lock").open("r+b") as holder:
        _guard_byte(holder, acquire=True)
        started = time.monotonic()
        with _refusal("concurrency-conflict", "lock"):
            RuntimeStateStore(repos[1]).write(
                _next(first, state="running"), expected_revision=1
            )
        elapsed = time.monotonic() - started
        _guard_byte(holder, acquire=False)

    assert 0.3 <= elapsed < 2.0
    assert runtime.path(BATCH).read_bytes() == before
    assert (
        runtime.write(_next(first, state="running"), expected_revision=1).revision == 2
    )


# --- Ledger and legacy-metadata invariants -----------------------------------------------------

LEDGER_WRITES = {"write_record", "replace_record", "write_immutable", "_atomic_write"}


def test_the_store_never_reaches_a_ledger_write_api() -> None:
    tree = ast.parse(Path(store.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name.rsplit(".", 1)[-1])
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)

    assert names & LEDGER_WRITES == set()
    assert not [m for m in modules if m.endswith(("ledger_ops", "ledger_admin"))]


def test_runtime_progress_leaves_every_ledger_file_untouched(
    repos: tuple[Path, Path],
) -> None:
    runtime = RuntimeStateStore(repos[1])
    ledger = LifecycleLedger(runtime.location.state_root)
    ledger.ensure()
    batch = BatchRecord(
        batch_id=BATCH,
        state=BatchState.PLANNED,
        dispatches=[],
        coordinator_approval={"approved_by": "policy:auto", "approved_at": "2026"},
    )
    stored = ledger.records_root() / "batches" / f"{BATCH}.json"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_text(json.dumps(batch.to_dict()), encoding="utf-8")
    before = _tree(ledger.root)
    record = _own(runtime)
    runtime.write(record, expected_revision=0)
    steps: list[JsonObject] = [
        {"state": "running", "operation": _operation()},
        {"operation": _operation("effect-observed", "confirmed")},
        {"state": "suspended"},
        {"state": "reconciling"},
        {"operation": _operation("ledger-committed", "confirmed")},
        {"operation": _operation("committed", "confirmed"), "state": "ready"},
        {"state": "running"},
        {"state": "completed"},
    ]
    record["ledger"] = _settled_ledger()

    for changes in steps:
        record = _next(record, **changes)
        runtime.write(record, expected_revision=record["revision"] - 1)

    assert runtime.read(BATCH) == record
    assert _tree(ledger.root) == before
    assert json.loads(stored.read_text("utf-8"))["state"] == "planned"


def test_suspended_stays_a_runtime_state_and_never_a_batch_state() -> None:
    batch_states = load_registry().fields["batch.state"].values

    assert "suspended" in RUNTIME_VOCABULARIES["runtime.state"]
    assert "suspended" not in batch_states
    assert "suspended" not in {str(state) for state in BatchState}


def test_the_store_never_accepts_legacy_metadata_as_success(
    repos: tuple[Path, Path],
) -> None:
    runtime = RuntimeStateStore(repos[0])
    legacy = _own(runtime)
    del legacy["contract"]
    first = _own(runtime, operation=_operation(), ledger=_settled_ledger())
    observed = _next(first, operation=_operation("effect-observed", "unknown"))

    with _refusal("runtime-compatibility", "validate"):
        runtime.write(legacy, expected_revision=0)
    with _refusal("integrity-failure", "validate"):
        runtime.write(_own(runtime, state="READY"), expected_revision=0)
    runtime.write(first, expected_revision=0)
    runtime.write(observed, expected_revision=1)
    with _refusal("integrity-failure", "validate") as caught:
        runtime.write(
            _next(observed, operation=_operation("ledger-committed", "unknown")),
            expected_revision=2,
        )
    assert "requires a confirmed effect" in caught.value.message
    assert runtime.read(BATCH) == observed
