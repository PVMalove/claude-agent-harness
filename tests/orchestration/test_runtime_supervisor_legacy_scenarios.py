"""The scenarios of the removed Python runtime state, at seam 1 (ADR 0021, ADR 0022, ADR 0026).

``tests/orchestration/test_runtime_reconcile_faults.py``, ``test_runtime_effect.py``,
``test_runtime_effect_faults.py`` and ``test_runtime_emergency.py`` of commit ``b69eed1`` tested
the Python ``runtimeState.v1`` store: its durable intent, reconcile, the effect of a hand-off and
the emergency record. ADR 0026 supersedes that design by the Rust runtime store and its process
supervisor, so each of the 49 scenarios has an equivalent test at the process boundary of the
coordinator CLI and the real ``harness-runtime``, or a written refusal. ``SCENARIOS`` is that
record; ``pr_body_table`` renders it for the PR body, and the guard tests keep it complete and true.
"""

from __future__ import annotations

import importlib
import subprocess
from collections.abc import Mapping

import pytest

from .runtime_store_support import ROOT
from .test_runtime_store_legacy_scenarios import QUARANTINE

LEGACY_COMMIT = "b69eed1"
RECONCILE = "tests/orchestration/test_runtime_reconcile_faults.py"
EFFECT = "tests/orchestration/test_runtime_effect.py"
EFFECT_FAULTS = "tests/orchestration/test_runtime_effect_faults.py"
EMERGENCY = "tests/orchestration/test_runtime_emergency.py"
SUPERVISOR = "tests/orchestration/test_runtime_supervisor.py"
WRITE = "tests/orchestration/test_runtime_store_write.py"
WRITE_FAULTS = "tests/orchestration/test_runtime_store_write_faults.py"
AUDIT = "tests/orchestration/test_runtime_store_audit.py"
PARALLEL = "tests/orchestration/test_runtime_store_parallel.py"
STORE_SCENARIOS = "tests/orchestration/test_runtime_store_legacy_scenarios.py"
CLIENT = "tests/orchestration/test_runtime_client.py"

# The 16 + 12 + 12 + 9 test functions of the four files at LEGACY_COMMIT, in file order.
LEGACY_TESTS: tuple[tuple[str, str], ...] = (
    (RECONCILE, "test_a_stop_between_a_record_and_its_audit_is_rolled_forward"),
    (RECONCILE, "test_a_stop_between_two_ledger_records_is_rolled_forward"),
    (RECONCILE, "test_a_stop_before_the_store_commit_finishes_the_phases_only"),
    (
        RECONCILE,
        "test_reconciliation_records_no_decision_and_writes_only_the_planned_paths",
    ),
    (RECONCILE, "test_settling_without_an_unfinished_operation_changes_nothing"),
    (
        RECONCILE,
        "test_a_quarantined_batch_is_never_settled_and_takes_no_composite_write",
    ),
    (
        RECONCILE,
        "test_a_report_whose_markdown_write_stopped_is_finished_by_the_next_command",
    ),
    (
        RECONCILE,
        "test_a_violated_record_hash_stops_with_evidence_and_no_new_revision_on_repeat",
    ),
    (RECONCILE, "test_a_tampered_intent_payload_stops_before_any_ledger_write"),
    (RECONCILE, "test_a_tampered_audit_event_stops_the_reconcile"),
    (
        RECONCILE,
        "test_a_ready_state_with_an_unfinished_operation_is_unknown_and_stops_unchanged",
    ),
    (
        RECONCILE,
        "test_a_record_another_writer_changes_during_reconcile_is_a_concurrency_conflict",
    ),
    (RECONCILE, "test_a_record_another_writer_changes_during_the_write_suspends_it"),
    (RECONCILE, "test_the_matrix_lists_every_write_a_clean_run_performs"),
    (
        RECONCILE,
        "test_a_stop_at_any_write_boundary_is_reconciled_or_leaves_nothing_behind",
    ),
    (RECONCILE, "test_a_hold_over_an_interrupted_write_waits_for_its_release"),
    (
        EFFECT,
        "test_the_intent_and_the_in_flight_marker_are_durable_before_the_hand_off",
    ),
    (EFFECT, "test_a_refused_hand_off_is_committed_and_then_raised"),
    (EFFECT, "test_a_storage_failure_before_the_hand_off_never_calls_it"),
    (
        EFFECT,
        "test_a_storage_failure_after_the_hand_off_is_honest_and_keeps_the_marker",
    ),
    (EFFECT, "test_an_unclassified_hand_off_failure_suspends_with_an_ambiguous_effect"),
    (EFFECT, "test_an_unfinished_hand_off_refuses_a_new_one"),
    (EFFECT, "test_a_send_stopped_after_its_observation_is_rolled_forward_once"),
    (EFFECT, "test_a_send_stopped_before_its_marker_is_proved_unchanged"),
    (
        EFFECT,
        "test_a_send_stopped_during_the_hand_off_stops_ambiguous_without_a_new_revision",
    ),
    (
        EFFECT,
        "test_a_record_changed_behind_an_unknown_hand_off_is_an_integrity_failure",
    ),
    (EFFECT, "test_a_tampered_hand_off_intent_stops_before_any_write"),
    (EFFECT, "test_dispatch_send_hands_off_once_behind_a_committed_intent"),
    (EFFECT_FAULTS, "test_an_observed_launch_is_rolled_forward_as_dispatched"),
    (EFFECT_FAULTS, "test_an_observed_absence_proves_the_hand_off_unchanged"),
    (
        EFFECT_FAULTS,
        "test_an_unproved_observation_stops_ambiguous_and_keeps_the_ledger",
    ),
    (
        EFFECT_FAULTS,
        "test_a_live_worker_that_contradicts_the_outcome_is_a_concurrency_conflict",
    ),
    (EFFECT_FAULTS, "test_a_confirmed_stop_retires_an_unknown_hand_off"),
    (
        EFFECT_FAULTS,
        "test_the_native_provider_observation_settles_an_unconfirmed_handoff",
    ),
    (
        EFFECT_FAULTS,
        "test_a_provider_without_an_observation_leaves_the_handoff_ambiguous",
    ),
    (
        EFFECT_FAULTS,
        "test_a_repeated_send_after_an_observed_launch_never_starts_a_second_worker",
    ),
    (EFFECT_FAULTS, "test_a_send_proved_unchanged_hands_off_once_on_repeat"),
    (EFFECT_FAULTS, "test_self_report_finishes_a_send_stopped_after_its_observation"),
    (EFFECT_FAULTS, "test_only_a_confirmed_stop_cancels_an_unknown_hand_off"),
    (EFFECT_FAULTS, "test_a_cancel_commits_an_observed_hand_off_before_it_decides"),
    (EMERGENCY, "test_an_invalid_config_is_recorded_and_the_ledger_stays_unchanged"),
    (EMERGENCY, "test_a_busy_ledger_is_recorded_without_touching_its_lock"),
    (EMERGENCY, "test_an_unreadable_ledger_invents_no_state"),
    (
        EMERGENCY,
        "test_a_corrupt_record_is_an_unreadable_ledger_and_an_unknown_id_is_not",
    ),
    (EMERGENCY, "test_a_failed_bootstrap_prints_diagnostics_and_records_nothing"),
    (EMERGENCY, "test_a_held_emergency_lock_leaves_the_record_unwritten"),
    (EMERGENCY, "test_a_runtime_state_failure_prints_its_own_diagnostics"),
    (EMERGENCY, "test_an_emergency_record_authorizes_nothing"),
    (EMERGENCY, "test_diagnostics_that_cannot_be_built_still_print_a_line"),
)

ONE_TRANSACTION = (
    "Roll-forward is not carried: a record and its audit record are one SQLite transaction "
    "(ADR 0020, section 3), so no stop can leave half a command to finish."
)
NO_PHASES = (
    "Operation phases (prepared, effect-observed, ledger-committed) do not exist: the operation "
    "row is written in the transaction of its effect."
)
NO_INTENT_FILE = (
    "No intent payload file exists: an intent is an operations row whose request_digest binds "
    "the request and the stored launch spec, and a repeat with another digest is "
    "idempotency_conflict."
)
NO_SUSPEND = (
    "The suspended runtime state does not exist: the losing writer gets revision_conflict and "
    "nothing changes."
)
HOLDS_809 = (
    "Operational holds (#684, operational_hold.py) are ported onto the Rust store by #809, whose "
    "acceptance criteria move operational_hold.py from 7719682; this ticket has no hold."
)
PROVIDER_807 = (
    "A runtime_access provider's launch observation belongs to the container provider that #807 "
    "moves to Rust; the process supervisor adopts by its own labels."
)
PROVIDER_807_ACCESS = (
    f"{PROVIDER_807} This covers the access-refused variant of the hand-off."
)
NO_ADAPTER_OBSERVE = (
    "The adapter `observe` answer is not carried: the runtime observes the process identity "
    "itself and never trusts an adapter's claim."
)
ABSENCE_IS_LOST = (
    "An absence after the intent no longer returns the dispatch to approved: for a process it "
    "cannot tell never-started from already-exited, so the execution is lost and a new launch "
    "needs a new attempt (ADR 0021, section 4)."
)
UNKNOWN_NOT_SUSPEND = (
    "The ambiguous-effect suspension is replaced by the execution state unknown, which only a "
    "repeated observation leaves (ADR 0022, section 2)."
)
HUMAN_NOT_OVERRIDE = (
    "A person's confirmation never overrides the runtime's observation: the store refuses a "
    "terminal dispatch state while the execution is unknown, launching or running (ADR 0021, "
    "section 6)."
)
NO_EMERGENCY = (
    "The independent emergency record of runtimeState.v1 (#699) is superseded by ADR 0026: a "
    "refused command writes nothing and prints ERROR and REMEDY with its code (ADR 0021, section "
    "8)."
)
NO_FLAG_APPROVAL = (
    f"{NO_EMERGENCY} Also: the runtime launches only from the stored brief and its approval; a "
    "client flag is never approval (ADR 0021, section 1)."
)
NO_DIAGNOSTICS = (
    "The DIAGNOSTICS line is not carried: the CLI contract is ERROR and REMEDY with the protocol "
    "code in brackets (ADR 0021, section 8)."
)
NO_MATRIX = (
    "There is no per-file write sequence to list: the mutations of a command reach the runtime "
    "as one request at the flush."
)

type Scenario = Mapping[str, object]
SCENARIOS: dict[str, Scenario] = {
    "test_a_stop_between_a_record_and_its_audit_is_rolled_forward": {
        "tests": [
            f"{WRITE_FAULTS}::test_a_runtime_killed_inside_the_transaction_leaves_nothing_and_the_client_commits_once",
            f"{WRITE}::test_a_report_and_its_transitions_are_one_commit_with_one_audit_record_each",
        ],
        "refused": ONE_TRANSACTION,
    },
    "test_a_stop_between_two_ledger_records_is_rolled_forward": {
        "tests": [
            f"{WRITE_FAULTS}::test_a_runtime_killed_inside_the_transaction_leaves_nothing_and_the_client_commits_once",
            f"{WRITE}::test_a_report_and_its_transitions_are_one_commit_with_one_audit_record_each",
        ],
        "refused": ONE_TRANSACTION,
    },
    "test_a_stop_before_the_store_commit_finishes_the_phases_only": {
        "tests": [
            f"{WRITE_FAULTS}::test_a_lost_response_after_the_commit_is_answered_from_the_stored_result",
            f"{WRITE_FAULTS}::test_a_runtime_killed_inside_the_transaction_leaves_nothing_and_the_client_commits_once",
        ],
        "refused": NO_PHASES,
    },
    "test_reconciliation_records_no_decision_and_writes_only_the_planned_paths": {
        "tests": [
            f"{SUPERVISOR}::test_a_restart_writes_the_execution_rows_and_never_a_dispatch_state",
            f"{WRITE}::test_a_report_and_its_transitions_are_one_commit_with_one_audit_record_each",
        ],
    },
    "test_settling_without_an_unfinished_operation_changes_nothing": {
        "tests": [
            f"{SUPERVISOR}::test_a_restart_without_a_live_execution_changes_nothing",
        ],
    },
    "test_a_quarantined_batch_is_never_settled_and_takes_no_composite_write": {
        "tests": [
            f"{STORE_SCENARIOS}::test_a_defective_state_is_refused_as_a_whole_and_nothing_changes",
        ],
        "refused": QUARANTINE,
    },
    "test_a_report_whose_markdown_write_stopped_is_finished_by_the_next_command": {
        "tests": [
            f"{WRITE}::test_a_report_and_its_transitions_are_one_commit_with_one_audit_record_each",
            f"{WRITE_FAULTS}::test_a_runtime_killed_inside_the_transaction_leaves_nothing_and_the_client_commits_once",
        ],
        "refused": ONE_TRANSACTION,
    },
    "test_a_violated_record_hash_stops_with_evidence_and_no_new_revision_on_repeat": {
        "tests": [
            f"{AUDIT}::test_a_row_changed_outside_the_runtime_is_found_by_the_last_audit_record_of_its_path",
            f"{AUDIT}::test_a_record_added_after_the_checkpoint_stops_every_ordinary_command",
        ],
    },
    "test_a_tampered_intent_payload_stops_before_any_ledger_write": {
        "tests": [
            f"{WRITE_FAULTS}::test_a_repeat_of_a_committed_write_is_its_stored_result_and_another_request_is_refused",
        ],
        "refused": NO_INTENT_FILE,
    },
    "test_a_tampered_audit_event_stops_the_reconcile": {
        "tests": [
            f"{AUDIT}::test_a_deleted_inserted_or_reordered_record_is_found_and_the_store_is_not_changed",
            f"{SUPERVISOR}::test_a_broken_audit_chain_refuses_the_launch_before_any_process_starts",
        ],
    },
    "test_a_ready_state_with_an_unfinished_operation_is_unknown_and_stops_unchanged": {
        "tests": [
            f"{SUPERVISOR}::test_a_runtime_killed_before_the_launch_leaves_a_lost_execution_and_no_process",
            f"{AUDIT}::test_a_row_that_breaks_a_check_constraint_is_found_by_the_integrity_check",
        ],
    },
    "test_a_record_another_writer_changes_during_reconcile_is_a_concurrency_conflict": {
        "tests": [
            f"{WRITE_FAULTS}::test_two_commands_that_read_the_same_revision_have_exactly_one_winner",
            f"{SUPERVISOR}::test_a_second_send_during_a_launch_returns_the_same_receipt",
        ],
    },
    "test_a_record_another_writer_changes_during_the_write_suspends_it": {
        "tests": [
            f"{WRITE_FAULTS}::test_two_commands_that_read_the_same_revision_have_exactly_one_winner",
            f"{PARALLEL}::test_read_commands_answer_the_state_before_a_writer_paused_in_its_transaction",
        ],
        "refused": NO_SUSPEND,
    },
    "test_the_matrix_lists_every_write_a_clean_run_performs": {
        "tests": [
            f"{WRITE}::test_staged_writes_are_read_back_and_reach_the_runtime_only_at_the_flush",
            f"{WRITE}::test_the_mutations_are_cut_where_the_batch_changes_with_the_revision_each_read",
        ],
        "refused": NO_MATRIX,
    },
    "test_a_stop_at_any_write_boundary_is_reconciled_or_leaves_nothing_behind": {
        "tests": [
            f"{WRITE_FAULTS}::test_a_runtime_killed_inside_the_transaction_leaves_nothing_and_the_client_commits_once",
            f"{WRITE_FAULTS}::test_a_failure_inside_the_transaction_rolls_it_back_and_a_repeat_succeeds",
            f"{SUPERVISOR}::test_a_runtime_killed_before_the_launch_leaves_a_lost_execution_and_no_process",
            f"{SUPERVISOR}::test_a_runtime_killed_between_launch_and_receipt_adopts_the_process_without_a_second_launch",
            f"{SUPERVISOR}::test_a_lost_response_after_the_receipt_is_answered_from_the_stored_receipt_with_one_launch",
        ],
    },
    "test_a_hold_over_an_interrupted_write_waits_for_its_release": {
        "refused": HOLDS_809,
    },
    "test_the_intent_and_the_in_flight_marker_are_durable_before_the_hand_off": {
        "tests": [
            f"{SUPERVISOR}::test_the_intent_is_committed_before_the_process_starts",
        ],
    },
    "test_a_refused_hand_off_is_committed_and_then_raised": {
        "tests": [
            f"{SUPERVISOR}::test_a_launch_the_os_refuses_is_a_failed_operation_and_blocks_the_dispatch",
        ],
        "refused": PROVIDER_807_ACCESS,
    },
    "test_a_storage_failure_before_the_hand_off_never_calls_it": {
        "tests": [
            f"{SUPERVISOR}::test_a_store_failure_before_the_launch_starts_nothing_and_a_repeat_launches_once",
        ],
    },
    "test_a_storage_failure_after_the_hand_off_is_honest_and_keeps_the_marker": {
        "tests": [
            f"{SUPERVISOR}::test_a_failed_receipt_write_keeps_the_intent_and_the_repeat_adopts_the_process",
        ],
    },
    "test_an_unclassified_hand_off_failure_suspends_with_an_ambiguous_effect": {
        "tests": [
            f"{SUPERVISOR}::test_an_execution_seen_from_another_boot_or_pid_namespace_stays_unknown_and_is_never_stopped",
            f"{SUPERVISOR}::test_a_failed_receipt_write_keeps_the_intent_and_the_repeat_adopts_the_process",
        ],
        "refused": UNKNOWN_NOT_SUSPEND,
    },
    "test_an_unfinished_hand_off_refuses_a_new_one": {
        "tests": [
            f"{SUPERVISOR}::test_a_second_send_during_a_launch_returns_the_same_receipt",
        ],
    },
    "test_a_send_stopped_after_its_observation_is_rolled_forward_once": {
        "tests": [
            f"{SUPERVISOR}::test_a_runtime_killed_between_launch_and_receipt_adopts_the_process_without_a_second_launch",
            f"{SUPERVISOR}::test_dispatch_send_launches_once_from_the_runtime_and_a_repeat_returns_the_same_receipt",
        ],
    },
    "test_a_send_stopped_before_its_marker_is_proved_unchanged": {
        "tests": [
            f"{SUPERVISOR}::test_a_send_refused_before_the_intent_launches_once_on_repeat",
        ],
    },
    "test_a_send_stopped_during_the_hand_off_stops_ambiguous_without_a_new_revision": {
        "tests": [
            f"{SUPERVISOR}::test_a_runtime_killed_before_the_launch_leaves_a_lost_execution_and_no_process",
            f"{SUPERVISOR}::test_a_restart_without_a_live_execution_changes_nothing",
        ],
    },
    "test_a_record_changed_behind_an_unknown_hand_off_is_an_integrity_failure": {
        "tests": [
            f"{AUDIT}::test_a_row_changed_outside_the_runtime_is_found_by_the_last_audit_record_of_its_path",
        ],
    },
    "test_a_tampered_hand_off_intent_stops_before_any_write": {
        "tests": [
            f"{WRITE_FAULTS}::test_a_repeat_of_a_committed_write_is_its_stored_result_and_another_request_is_refused",
            f"{AUDIT}::test_a_row_changed_outside_the_runtime_is_found_by_the_last_audit_record_of_its_path",
        ],
        "refused": NO_INTENT_FILE,
    },
    "test_dispatch_send_hands_off_once_behind_a_committed_intent": {
        "tests": [
            f"{SUPERVISOR}::test_dispatch_send_launches_once_from_the_runtime_and_a_repeat_returns_the_same_receipt",
        ],
    },
    "test_an_observed_launch_is_rolled_forward_as_dispatched": {
        "tests": [
            f"{SUPERVISOR}::test_a_runtime_killed_between_launch_and_receipt_adopts_the_process_without_a_second_launch",
        ],
    },
    "test_an_observed_absence_proves_the_hand_off_unchanged": {
        "tests": [
            f"{SUPERVISOR}::test_a_runtime_killed_before_the_launch_leaves_a_lost_execution_and_no_process",
        ],
        "refused": ABSENCE_IS_LOST,
    },
    "test_an_unproved_observation_stops_ambiguous_and_keeps_the_ledger": {
        "tests": [
            f"{SUPERVISOR}::test_an_execution_seen_from_another_boot_or_pid_namespace_stays_unknown_and_is_never_stopped",
        ],
        "refused": NO_ADAPTER_OBSERVE,
    },
    "test_a_live_worker_that_contradicts_the_outcome_is_a_concurrency_conflict": {
        "tests": [
            f"{SUPERVISOR}::test_a_cancel_of_a_running_execution_is_refused_until_its_stop_is_proven",
        ],
    },
    "test_a_confirmed_stop_retires_an_unknown_hand_off": {
        "tests": [
            f"{SUPERVISOR}::test_a_cancel_of_a_running_execution_is_refused_until_its_stop_is_proven",
            f"{SUPERVISOR}::test_an_execution_seen_from_another_boot_or_pid_namespace_stays_unknown_and_is_never_stopped",
        ],
        "refused": HUMAN_NOT_OVERRIDE,
    },
    "test_the_native_provider_observation_settles_an_unconfirmed_handoff": {
        "tests": [
            f"{SUPERVISOR}::test_a_runtime_killed_between_launch_and_receipt_adopts_the_process_without_a_second_launch",
        ],
        "refused": PROVIDER_807,
    },
    "test_a_provider_without_an_observation_leaves_the_handoff_ambiguous": {
        "tests": [
            f"{SUPERVISOR}::test_an_execution_seen_from_another_boot_or_pid_namespace_stays_unknown_and_is_never_stopped",
        ],
        "refused": PROVIDER_807,
    },
    "test_a_repeated_send_after_an_observed_launch_never_starts_a_second_worker": {
        "tests": [
            f"{SUPERVISOR}::test_dispatch_send_launches_once_from_the_runtime_and_a_repeat_returns_the_same_receipt",
            f"{SUPERVISOR}::test_a_runtime_killed_between_launch_and_receipt_adopts_the_process_without_a_second_launch",
        ],
    },
    "test_a_send_proved_unchanged_hands_off_once_on_repeat": {
        "tests": [
            f"{SUPERVISOR}::test_a_send_refused_before_the_intent_launches_once_on_repeat",
        ],
    },
    "test_self_report_finishes_a_send_stopped_after_its_observation": {
        "tests": [
            f"{SUPERVISOR}::test_self_report_after_an_adopted_launch_moves_the_dispatch_to_working",
        ],
    },
    "test_only_a_confirmed_stop_cancels_an_unknown_hand_off": {
        "tests": [
            f"{SUPERVISOR}::test_a_cancel_of_a_running_execution_is_refused_until_its_stop_is_proven",
        ],
        "refused": HUMAN_NOT_OVERRIDE,
    },
    "test_a_cancel_commits_an_observed_hand_off_before_it_decides": {
        "tests": [
            f"{SUPERVISOR}::test_a_cancel_of_a_running_execution_is_refused_until_its_stop_is_proven",
            f"{SUPERVISOR}::test_a_restart_writes_the_execution_rows_and_never_a_dispatch_state",
        ],
    },
    "test_an_invalid_config_is_recorded_and_the_ledger_stays_unchanged": {
        "tests": [
            f"{SUPERVISOR}::test_an_invalid_config_refuses_dispatch_send_before_any_launch_and_changes_nothing",
        ],
        "refused": NO_EMERGENCY,
    },
    "test_a_busy_ledger_is_recorded_without_touching_its_lock": {
        "tests": [
            f"{WRITE_FAULTS}::test_a_locked_store_is_a_bounded_store_busy_that_a_repeat_overcomes",
        ],
        "refused": NO_EMERGENCY,
    },
    "test_an_unreadable_ledger_invents_no_state": {
        "tests": [
            f"{WRITE}::test_an_unreadable_store_file_is_a_refusal_that_writes_nothing",
        ],
    },
    "test_a_corrupt_record_is_an_unreadable_ledger_and_an_unknown_id_is_not": {
        "tests": [
            f"{AUDIT}::test_a_row_that_breaks_a_check_constraint_is_found_by_the_integrity_check",
            f"{SUPERVISOR}::test_an_unknown_dispatch_is_refused_and_launches_nothing",
        ],
    },
    "test_a_failed_bootstrap_prints_diagnostics_and_records_nothing": {
        "tests": [
            f"{WRITE}::test_without_a_runtime_a_write_is_runtime_unavailable_and_touches_no_file",
            f"{WRITE_FAULTS}::test_the_three_failures_have_different_remedies_that_start_with_their_code",
        ],
        "refused": NO_DIAGNOSTICS,
    },
    "test_a_held_emergency_lock_leaves_the_record_unwritten": {
        "refused": NO_EMERGENCY,
    },
    "test_a_runtime_state_failure_prints_its_own_diagnostics": {
        "tests": [
            f"{WRITE_FAULTS}::test_the_three_failures_have_different_remedies_that_start_with_their_code",
        ],
        "refused": NO_DIAGNOSTICS,
    },
    "test_an_emergency_record_authorizes_nothing": {
        "refused": NO_FLAG_APPROVAL,
    },
    "test_diagnostics_that_cannot_be_built_still_print_a_line": {
        "tests": [
            f"{CLIENT}::test_an_error_of_the_runtime_exits_2_with_its_code_in_the_remedy",
        ],
        "refused": NO_DIAGNOSTICS,
    },
}


def _tests(scenario: Scenario) -> list[str]:
    tests = scenario.get("tests", [])
    assert isinstance(tests, list)
    return [str(node) for node in tests]


def pr_body_table() -> str:
    """The mapping as a Markdown table for the PR body."""
    rows = [
        "| Scenario of b69eed1 | Equivalent at seam 1 | Refused, and why |",
        "|---|---|---|",
    ]
    for _, name in LEGACY_TESTS:
        scenario = SCENARIOS[name]
        tests = "<br>".join(f"`{node}`" for node in _tests(scenario))
        refused = str(scenario.get("refused", ""))
        rows.append(f"| `{name}` | {tests} | {refused} |")
    return "\n".join(rows)


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


def test_every_scenario_of_the_four_legacy_files_is_mapped_once() -> None:
    names = [name for _, name in LEGACY_TESTS]

    assert len(names) == len(set(names)) == 49
    assert set(SCENARIOS) == set(names)
    for file in (RECONCILE, EFFECT, EFFECT_FAULTS, EMERGENCY):
        found = _legacy_names(file)
        if found is None:
            pytest.skip(f"commit {LEGACY_COMMIT} is not in this checkout")
        assert found == [name for owner, name in LEGACY_TESTS if owner == file]


def test_every_scenario_has_an_equivalent_test_or_a_written_refusal() -> None:
    for name, scenario in SCENARIOS.items():
        refused = scenario.get("refused", "")
        assert isinstance(refused, str)
        assert _tests(scenario) or refused.strip(), name
        assert refused == "" or len(refused.split()) >= 8, f"{name}: say why"


def _node_exists(node: str) -> bool:
    path, _, name = node.partition("::")
    module = importlib.import_module(path.removesuffix(".py").replace("/", "."))
    return callable(getattr(module, name, None))


def test_every_equivalent_test_exists() -> None:
    nodes = {node for scenario in SCENARIOS.values() for node in _tests(scenario)}

    missing = sorted(node for node in nodes if not _node_exists(node))

    assert missing == []


def test_the_table_for_the_pr_body_names_every_scenario() -> None:
    table = pr_body_table()

    assert len(table.splitlines()) == 2 + 49
    assert all(f"`{name}`" in table for _, name in LEGACY_TESTS)
