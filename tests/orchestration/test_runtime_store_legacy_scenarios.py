"""The scenarios of the removed file-ledger migration, at seam 1 (ADR 0020, section 5).

``tests/orchestration/test_ledger_migration.py`` and ``test_ledger_migration_faults.py`` of commit
``b69eed1`` tested the system-fields migration of the Python ledger: a quarantine of unknown or
unreadable batches, an overlay of mapped values, inspect and export commands, and a durable intent
with ten stop points. The runtime store replaced that design (ADR 0020, ADR 0026), so each of the 26
scenarios either has an equivalent test here or in the other ``test_runtime_store_*`` modules, at
the process boundary of the real ``harness-runtime`` and the coordinator CLI, or is refused with a
written reason. ``SCENARIOS`` is that record; ``pr_body_table`` renders it for the PR body, and the
guard tests keep it complete and true.

The tests below add the equivalents that no other module has.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Mapping
from contextlib import closing
from pathlib import Path

import pytest

from harness.orchestration.ledger.lifecycle import JsonObject, JsonValue, _canonical

from .runtime_store_support import (
    BATCH_ID,
    DISPATCH_IDS,
    ROOT,
    STATE_REL,
    PausedMigrate,
    ServingRuntime,
    build_state,
    coordinator,
    coordinator_with,
    load,
    migrate,
    tree,
    write_coordinator_state,
)

LEGACY_COMMIT = "b69eed1"
MIGRATION_FILE = "tests/orchestration/test_ledger_migration.py"
FAULTS_FILE = "tests/orchestration/test_ledger_migration_faults.py"
MIGRATE = "tests/orchestration/test_runtime_store_migrate.py"
FAULTS = "tests/orchestration/test_runtime_store_migrate_faults.py"
READ = "tests/orchestration/test_runtime_store_read.py"
HERE = "tests/orchestration/test_runtime_store_legacy_scenarios.py"

# The 21 + 5 test functions of the two files at LEGACY_COMMIT.
LEGACY_TESTS: tuple[tuple[str, str], ...] = (
    (MIGRATION_FILE, "test_a_migration_selects_a_verified_copy_and_keeps_the_source_byte_for_byte"),
    (MIGRATION_FILE, "test_a_mapped_check_result_keeps_the_report_bytes_and_its_hash"),
    (MIGRATION_FILE, "test_the_generation_switch_is_a_compare_and_swap_on_the_pointer_bytes"),
    (MIGRATION_FILE, "test_an_unknown_or_unreadable_value_quarantines_only_its_batch"),
    (MIGRATION_FILE, "test_an_unreadable_report_moves_with_its_batch"),
    (MIGRATION_FILE, "test_a_finding_no_batch_owns_refuses_before_anything_changes"),
    (MIGRATION_FILE, "test_a_quarantined_batch_id_must_be_a_coordinator_batch_id"),
    (MIGRATION_FILE, "test_a_changed_migration_file_is_refused"),
    (MIGRATION_FILE, "test_a_schema_upgrade_and_ledger_clean_keep_the_quarantine"),
    (MIGRATION_FILE, "test_a_later_migration_carries_an_earlier_quarantine"),
    (MIGRATION_FILE, "test_ledger_migrate_selects_the_migration_and_denies_the_quarantined_batch"),
    (MIGRATION_FILE, "test_a_repeated_migrate_and_reconcile_change_nothing"),
    (MIGRATION_FILE, "test_a_migrated_ledger_migrates_again_only_under_changed_rules"),
    (MIGRATION_FILE, "test_a_batch_with_an_unfinished_operation_holds_the_migration"),
    (MIGRATION_FILE, "test_an_empty_state_has_nothing_to_migrate"),
    (MIGRATION_FILE, "test_before_migrate_a_loader_failure_denies_every_action_without_a_store_record"),
    (MIGRATION_FILE, "test_inspect_and_export_read_a_denied_batch_without_writing"),
    (MIGRATION_FILE, "test_a_quarantined_batch_stays_denied_in_the_operational_store"),
    (MIGRATION_FILE, "test_inspect_and_export_read_a_legacy_ledger_no_migration_selected"),
    (MIGRATION_FILE, "test_an_export_keeps_bytes_that_are_not_utf8"),
    (MIGRATION_FILE, "test_an_unknown_batch_is_refused_by_inspect_and_export"),
    (FAULTS_FILE, "test_a_stop_at_any_boundary_is_finished_by_the_next_run"),
    (FAULTS_FILE, "test_a_migration_finished_after_any_stop_passes_the_strict_read"),
    (FAULTS_FILE, "test_a_failed_pointer_replace_suspends_and_keeps_the_pointer"),
    (FAULTS_FILE, "test_a_pointer_on_another_generation_stops_without_a_new_revision"),
    (FAULTS_FILE, "test_a_source_changed_after_a_stop_is_migrated_as_it_is_now"),
)

QUARANTINE = (
    "The quarantine is not carried: the store holds no `migration/` directory and no per-batch "
    "denial. A state with an unknown or unreadable value is refused as a whole (exit code 2, "
    "nothing changed), so no half-trusted record can enter the store."
)
OVERLAY = (
    "The system-fields overlay is superseded (ADR 0026, section 1): a value is imported as it "
    "is and never mapped, so there is no rule set to change."
)
NO_INSPECT = (
    "`inspect_ledger` and `export_ledger` do not exist in this tree (the commands of the removed "
    "design), and the store has no quarantine to show."
)

# legacy test name -> the tests that stand for it, and/or the reason the rest is refused.
type Scenario = Mapping[str, object]
SCENARIOS: dict[str, Scenario] = {
    "test_a_migration_selects_a_verified_copy_and_keeps_the_source_byte_for_byte": {
        "tests": [
            f"{MIGRATE}::test_migrate_cuts_the_state_over_and_reports_one_json_line",
            f"{MIGRATE}::test_the_snapshot_is_a_state_a_1x_installation_still_validates",
        ],
    },
    "test_a_mapped_check_result_keeps_the_report_bytes_and_its_hash": {
        "tests": [f"{HERE}::test_a_report_keeps_its_bytes_and_its_hash_in_the_store"],
        "refused": OVERLAY,
    },
    "test_the_generation_switch_is_a_compare_and_swap_on_the_pointer_bytes": {
        "tests": [
            f"{FAULTS}::test_a_legacy_write_during_the_import_exits_4_and_commits_no_cutover"
        ],
    },
    "test_an_unknown_or_unreadable_value_quarantines_only_its_batch": {
        "tests": [f"{HERE}::test_a_defective_state_is_refused_as_a_whole_and_nothing_changes"],
        "refused": QUARANTINE,
    },
    "test_an_unreadable_report_moves_with_its_batch": {
        "tests": [f"{HERE}::test_a_defective_state_is_refused_as_a_whole_and_nothing_changes"],
        "refused": QUARANTINE,
    },
    "test_a_finding_no_batch_owns_refuses_before_anything_changes": {
        "tests": [f"{HERE}::test_a_dispatch_no_batch_owns_refuses_before_anything_changes"],
    },
    "test_a_quarantined_batch_id_must_be_a_coordinator_batch_id": {
        "tests": [f"{HERE}::test_a_defective_state_is_refused_as_a_whole_and_nothing_changes"],
        "refused": QUARANTINE,
    },
    "test_a_changed_migration_file_is_refused": {
        "tests": [f"{HERE}::test_a_tampered_legacy_audit_record_refuses_the_migration"],
        "refused": "There is no `migration/` directory to tamper with; the evidence that stays "
        "verified is the legacy audit (its `record_sha256`) and the snapshot with its manifests.",
    },
    "test_a_schema_upgrade_and_ledger_clean_keep_the_quarantine": {
        "tests": [f"{HERE}::test_a_1x_installation_refuses_clean_and_migrate_after_the_cutover"],
        "refused": QUARANTINE,
    },
    "test_a_later_migration_carries_an_earlier_quarantine": {
        "tests": [
            f"{MIGRATE}::test_a_repeated_migrate_reports_already_cut_over_and_changes_no_byte"
        ],
        "refused": QUARANTINE,
    },
    "test_ledger_migrate_selects_the_migration_and_denies_the_quarantined_batch": {
        "tests": [
            f"{MIGRATE}::test_migrate_cuts_the_state_over_and_reports_one_json_line",
            f"{HERE}::test_a_defective_state_is_refused_as_a_whole_and_nothing_changes",
        ],
        "refused": QUARANTINE,
    },
    "test_a_repeated_migrate_and_reconcile_change_nothing": {
        "tests": [
            f"{MIGRATE}::test_a_repeated_migrate_reports_already_cut_over_and_changes_no_byte"
        ],
    },
    "test_a_migrated_ledger_migrates_again_only_under_changed_rules": {
        "tests": [
            f"{MIGRATE}::test_a_repeated_migrate_reports_already_cut_over_and_changes_no_byte"
        ],
        "refused": OVERLAY,
    },
    "test_a_batch_with_an_unfinished_operation_holds_the_migration": {
        "tests": [
            f"{MIGRATE}::test_a_working_dispatch_refuses_with_exit_code_2_and_a_remedy",
            f"{HERE}::test_a_qa_lease_or_a_queued_run_refuses_the_migration",
        ],
    },
    "test_an_empty_state_has_nothing_to_migrate": {
        "tests": [f"{MIGRATE}::test_an_empty_repository_has_nothing_to_migrate"],
    },
    "test_before_migrate_a_loader_failure_denies_every_action_without_a_store_record": {
        "tests": [f"{HERE}::test_a_defective_state_is_refused_as_a_whole_and_nothing_changes"],
        "refused": "The `CompositeWrite` intent of the removed design is superseded: a defective "
        "state is refused before it reaches the store, and a 1.x installation cannot write at all "
        "after the cutover (the pointer fence).",
    },
    "test_inspect_and_export_read_a_denied_batch_without_writing": {
        "refused": NO_INSPECT,
    },
    "test_a_quarantined_batch_stays_denied_in_the_operational_store": {
        "tests": [f"{HERE}::test_a_defective_state_is_refused_as_a_whole_and_nothing_changes"],
        "refused": QUARANTINE,
    },
    "test_inspect_and_export_read_a_legacy_ledger_no_migration_selected": {
        "tests": [f"{MIGRATE}::test_a_pre_ledger_layout_names_the_1x_ledger_migrate"],
        "refused": NO_INSPECT,
    },
    "test_an_export_keeps_bytes_that_are_not_utf8": {
        "tests": [f"{HERE}::test_a_file_that_is_not_utf8_keeps_its_bytes_in_the_store"],
    },
    "test_an_unknown_batch_is_refused_by_inspect_and_export": {
        "tests": [f"{HERE}::test_an_unknown_batch_is_refused_in_both_epochs"],
        "refused": NO_INSPECT,
    },
    "test_a_stop_at_any_boundary_is_finished_by_the_next_run": {
        "tests": [
            f"{FAULTS}::test_a_sigkill_at_a_step_then_a_repeat_leaves_exactly_one_authority"
        ],
    },
    "test_a_migration_finished_after_any_stop_passes_the_strict_read": {
        "tests": [f"{HERE}::test_a_migration_finished_after_a_kill_answers_the_reads_as_reference"],
    },
    "test_a_failed_pointer_replace_suspends_and_keeps_the_pointer": {
        "tests": [
            f"{FAULTS}::test_an_injected_failure_exits_3_and_leaves_the_file_ledger_the_authority"
        ],
    },
    "test_a_pointer_on_another_generation_stops_without_a_new_revision": {
        "tests": [f"{FAULTS}::test_a_source_changed_after_the_stop_is_migrated_as_it_is_now"],
        "refused": "A pointer that changed during one run is exit code 4 of that run and no store "
        "record is written (`test_a_legacy_write_during_the_import_exits_4_and_commits_no_cutover`); "
        "the next run starts again from the pointer it finds, so there is no suspended revision "
        "to keep.",
    },
    "test_a_source_changed_after_a_stop_is_migrated_as_it_is_now": {
        "tests": [f"{FAULTS}::test_a_source_changed_after_the_stop_is_migrated_as_it_is_now"],
    },
}


def pr_body_table() -> str:
    """The mapping as a Markdown table for the PR body."""
    rows = ["| Scenario of b69eed1 | Equivalent at seam 1 | Refused, and why |", "|---|---|---|"]
    for _, name in LEGACY_TESTS:
        scenario = SCENARIOS[name]
        tests = "<br>".join(f"`{node}`" for node in scenario.get("tests", []))  # type: ignore[attr-defined]
        refused = str(scenario.get("refused", ""))
        rows.append(f"| `{name}` | {tests} | {refused} |")
    return "\n".join(rows)


# -- the guard ---------------------------------------------------------------------------------


def _legacy_names(path: str) -> list[str] | None:
    """The test functions of ``path`` at the legacy commit, or ``None`` without that commit."""
    shown = subprocess.run(
        ["git", "show", f"{LEGACY_COMMIT}:{path}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if shown.returncode != 0:
        return None
    return [
        line.split("(")[0].removeprefix("def ")
        for line in shown.stdout.splitlines()
        if line.startswith("def test_")
    ]


def test_every_scenario_of_the_two_legacy_files_is_mapped_once() -> None:
    names = [name for _, name in LEGACY_TESTS]

    assert len(names) == len(set(names)) == 26
    assert set(SCENARIOS) == set(names)
    for file in (MIGRATION_FILE, FAULTS_FILE):
        found = _legacy_names(file)
        if found is None:
            pytest.skip(f"commit {LEGACY_COMMIT} is not in this checkout")
        assert found == [name for owner, name in LEGACY_TESTS if owner == file]


def test_every_scenario_has_an_equivalent_test_or_a_written_refusal() -> None:
    for name, scenario in SCENARIOS.items():
        tests = scenario.get("tests", [])
        refused = scenario.get("refused", "")
        assert isinstance(tests, list) and isinstance(refused, str)
        assert tests or refused.strip(), name
        assert refused == "" or len(refused.split()) >= 8, f"{name}: say why"


def _node_exists(node: str) -> bool:
    path, _, name = node.partition("::")
    module = importlib.import_module(path.removesuffix(".py").replace("/", "."))
    return callable(getattr(module, name, None))


def test_every_equivalent_test_exists() -> None:
    nodes = {
        node
        for scenario in SCENARIOS.values()
        for node in scenario.get("tests", [])  # type: ignore[attr-defined]
    }

    missing = sorted(node for node in nodes if not _node_exists(node))

    assert missing == []


def test_the_table_for_the_pr_body_names_every_scenario() -> None:
    table = pr_body_table()

    assert len(table.splitlines()) == 2 + 26
    assert all(f"`{name}`" in table for _, name in LEGACY_TESTS)


# -- the equivalents that no other module has --------------------------------------------------


def _generation(repo: Path) -> Path:
    state = repo / STATE_REL
    return state / "generations" / str(load(state / "ledger.json")["generation"])


def _put(repo: Path, relative: str, data: bytes | JsonObject) -> None:
    path = _generation(repo) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else _canonical(data).encode("utf-8"))


def _edit(repo: Path, relative: str, **changes: JsonValue) -> None:
    path = _generation(repo) / relative
    record = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(_canonical({**record, **changes}), encoding="utf-8", newline="\n")


def _refused(repo: Path) -> subprocess.CompletedProcess[str]:
    before = tree(repo / STATE_REL)
    result = migrate(repo)
    assert result.returncode == 2, result.stderr
    assert result.stdout == ""
    assert result.stderr.startswith("ERROR: ")
    assert "REMEDY:" in result.stderr
    assert tree(repo / STATE_REL) == before
    assert not (repo / ".harness/orchestration/state-snapshots").exists()
    assert not [path for path in (repo / STATE_REL).iterdir() if path.name.startswith("runtime.db")]
    return result


def test_a_report_keeps_its_bytes_and_its_hash_in_the_store(tmp_path: Path) -> None:
    build_state(tmp_path)
    report_path = _generation(tmp_path) / "reports" / f"{DISPATCH_IDS[0]}.json"
    report_bytes = report_path.read_bytes()
    batch = json.loads(
        (_generation(tmp_path) / "batches" / f"{BATCH_ID}.json").read_text(encoding="utf-8")
    )
    recorded = batch["dispatches"][0]["report_sha256"]

    assert migrate(tmp_path).returncode == 0

    with closing(sqlite3.connect(f"file:{tmp_path / STATE_REL / 'runtime.db'}?mode=ro", uri=True)) as store:
        (body,) = store.execute(
            "SELECT body_json FROM record WHERE path = ?", (f"reports/{DISPATCH_IDS[0]}.json",)
        ).fetchone()
    assert hashlib.sha256(_canonical(json.loads(body)).encode("utf-8")).hexdigest() == recorded
    assert hashlib.sha256(report_bytes).hexdigest() == recorded
    assert report_path.read_bytes() == report_bytes


DEFECTS: dict[str, Callable[[Path], None]] = {
    "unknown batch.state": lambda repo: _edit(repo, f"batches/{BATCH_ID}.json", state="on-hold"),
    "unknown dispatch-status.state": lambda repo: _edit(
        repo, f"dispatch-status/{DISPATCH_IDS[1]}.json", state="Reported"
    ),
    "non-string batch.state": lambda repo: _edit(repo, f"batches/{BATCH_ID}.json", state=None),
    "unreadable batch": lambda repo: _put(repo, f"batches/{BATCH_ID}.json", b"{ not JSON"),
    "unreadable report": lambda repo: _put(
        repo, f"reports/{DISPATCH_IDS[0]}.json", b"[not a report"
    ),
    "damaged batch with a non-coordinator id": lambda repo: _put(
        repo, "batches/batch-damaged.json", b"{ not JSON"
    ),
}


@pytest.mark.parametrize("defect", sorted(DEFECTS))
def test_a_defective_state_is_refused_as_a_whole_and_nothing_changes(
    tmp_path: Path, defect: str
) -> None:
    build_state(tmp_path)
    DEFECTS[defect](tmp_path)

    _refused(tmp_path)


def test_a_dispatch_no_batch_owns_refuses_before_anything_changes(tmp_path: Path) -> None:
    build_state(tmp_path)
    for directory in ("dispatches", "dispatch-status"):
        _put(
            tmp_path,
            f"{directory}/dispatch-0000ffff.json",
            {"dispatch_id": "dispatch-0000ffff", "batch_id": "batch-none", "state": "approved"},
        )

    result = _refused(tmp_path)

    assert "orphaned dispatch evidence" in result.stderr


def test_a_tampered_legacy_audit_record_refuses_the_migration(tmp_path: Path) -> None:
    build_state(tmp_path)
    victim = sorted((_generation(tmp_path) / "audit").glob("*.json"))[0]
    victim.write_text(victim.read_text(encoding="utf-8").replace('"action": "', '"action": "x'))

    result = _refused(tmp_path)

    assert "integrity" in result.stderr


def test_a_qa_lease_or_a_queued_run_refuses_the_migration(tmp_path: Path) -> None:
    build_state(tmp_path)
    lease: JsonObject = {
        "dispatch_id": DISPATCH_IDS[1],
        "host": "h",
        "pid": 1,
        "acquired_at": "2026-10-01T00:00:00+00:00",
        "expires_at": "2026-10-01T01:00:00+00:00",
    }
    entry: JsonObject = {"dispatch_id": DISPATCH_IDS[0], "sequence": 1, "queued_at": "2026-10-01T00:00:00+00:00"}
    _put(tmp_path, "qa-lane/queue/00000000000000000001-x.json", entry)
    assert "queued" in _refused(tmp_path).stderr
    _put(tmp_path, "qa-lane/lease.json", lease)
    (_generation(tmp_path) / "qa-lane/queue/00000000000000000001-x.json").unlink()
    assert "lease" in _refused(tmp_path).stderr


def test_a_file_that_is_not_utf8_keeps_its_bytes_in_the_store(tmp_path: Path) -> None:
    build_state(tmp_path)
    broken = b"\xff\xfe not text \x00 at all"
    _put(tmp_path, f"reports/{DISPATCH_IDS[0]}.md", broken)

    assert migrate(tmp_path).returncode == 0

    with closing(sqlite3.connect(f"file:{tmp_path / STATE_REL / 'runtime.db'}?mode=ro", uri=True)) as store:
        (stored, sha256) = store.execute(
            "SELECT bytes, sha256 FROM artifact WHERE path = ?", (f"reports/{DISPATCH_IDS[0]}.md",)
        ).fetchone()
    assert stored == broken
    assert sha256 == hashlib.sha256(broken).hexdigest()
    assert (_generation(tmp_path) / f"reports/{DISPATCH_IDS[0]}.md").read_bytes() == broken


def test_a_1x_installation_refuses_clean_and_migrate_after_the_cutover(tmp_path: Path) -> None:
    build_state(tmp_path)
    assert migrate(tmp_path).returncode == 0
    files = tree(tmp_path / STATE_REL, skip=("runtime.db-",))

    for command in (("ledger", "clean"), ("ledger", "migrate"), ("ledger", "validate")):
        result = coordinator(tmp_path, *command)
        assert result.returncode == 2, command
        # The file ledger is closed; `validate` asks the runtime, which a scratch directory that
        # is no git working tree has no address for.
        assert (
            "does not run in the runtime-store epoch" in result.stderr
            or "[runtime_unavailable]" in result.stderr
        ), command

    assert tree(tmp_path / STATE_REL, skip=("runtime.db-",)) == files


@pytest.mark.skipif(sys.platform == "win32", reason="the seam tests drive a Unix-socket runtime")
def test_an_unknown_batch_is_refused_in_both_epochs(tmp_path: Path) -> None:
    repo, _ = write_coordinator_state(tmp_path)
    unknown = ["batch", "decision-packet", "--batch", "batch-00000fff"]
    traversal = ["batch", "decision-packet", "--batch", "../batch-0000000a"]
    before = [coordinator_with(repo, None, *command) for command in (unknown, traversal)]
    assert migrate(repo).returncode == 0
    runtime = ServingRuntime(repo)
    try:
        after = [coordinator_with(repo, runtime, *command) for command in (unknown, traversal)]
    finally:
        runtime.stop()

    for was, now in zip(before, after, strict=True):
        assert was.returncode == now.returncode == 2
        assert now.stderr.splitlines()[0].replace(
            str(repo / STATE_REL / "runtime.db"), "<records>"
        ) == was.stderr.splitlines()[0]


@pytest.mark.skipif(sys.platform == "win32", reason="the seam tests drive a Unix-socket runtime")
@pytest.mark.parametrize(
    "step",
    [
        "preconditions",
        "locked",
        "snapshot",
        "import",
        "parity",
        "renamed",
        "authority",
        "pointer-replace",
        "pointer",
    ],
)
def test_a_migration_finished_after_a_kill_answers_the_reads_as_reference(
    tmp_path: Path, step: str
) -> None:
    repo, ids = write_coordinator_state(tmp_path)
    commands = {
        "batch list": ["batch", "list"],
        "decision-packet": ["batch", "decision-packet", "--batch", ids["batch_id"]],
        "qa status": ["qa", "status"],
    }

    def answers(runtime: ServingRuntime | None) -> dict[str, object]:
        found: dict[str, object] = {}
        for name, command in commands.items():
            result = coordinator_with(repo, runtime, *command)
            assert result.returncode == 0, result.stderr
            answer = json.loads(result.stdout)
            if name == "decision-packet":
                answer["report"] = answer["report"].rsplit("/reports/", 1)[1]
            found[name] = answer
        return found

    reference = answers(None)
    process = PausedMigrate(repo, step)
    process.wait_paused()
    process.kill()
    assert migrate(repo).returncode == 0
    runtime = ServingRuntime(repo)
    try:
        assert answers(runtime) == reference
    finally:
        runtime.stop()
