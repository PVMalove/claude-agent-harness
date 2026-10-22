"""The canonical JSON of the runtime store equals the canonical JSON of the file ledger.

``crates/state/tests/fixtures/canonical_golden.expected`` holds the bytes Python writes for
``canonical_golden.json``. This module proves that Python still writes exactly those bytes, and the
Rust test ``canonical::tests::the_golden_fixture_is_written_byte_for_byte_as_python_writes_it``
proves that the Rust writer does. Both languages therefore agree on every ``record_sha256``,
``brief_sha256`` and ``report_sha256`` that hashes a canonical form (ADR 0020, section 4).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

from harness.orchestration import operational_guards
from harness.orchestration.core import constants
from harness.orchestration.core.constants import BatchState, DispatchState
from harness.orchestration.ledger.lifecycle import _canonical
from harness.orchestration.workflow import approval, recovery_events, startup

ROOT = Path(__file__).resolve().parents[2]
STATE_CRATE = ROOT / "native/harness-runtime/crates/state"
FIXTURES = STATE_CRATE / "tests/fixtures"


def test_python_writes_the_golden_bytes() -> None:
    source = (FIXTURES / "canonical_golden.json").read_text(encoding="utf-8")

    written = _canonical(json.loads(source))

    assert written.encode("utf-8") == (FIXTURES / "canonical_golden.expected").read_bytes()


def test_the_golden_fixture_covers_the_cases_the_two_writers_could_differ_on() -> None:
    source = (FIXTURES / "canonical_golden.json").read_text(encoding="utf-8")
    expected = (FIXTURES / "canonical_golden.expected").read_text(encoding="utf-8")

    for needle in ("1e-05", "1e+16", "123456789012345678901234567890", "-0.0", "😀", "\\u0000"):
        assert needle in expected
    assert "\x7f" in expected
    assert '"keys \\"quoted\\"\\n"' in expected
    assert "1E-5" in source


def test_the_rust_state_vocabularies_equal_the_python_vocabularies() -> None:
    """The store validates batch and dispatch states against lists of its own."""
    rust = (STATE_CRATE / "src/vocabulary.rs").read_text(encoding="utf-8")

    for name, vocabulary in (("BATCH_STATES", BatchState), ("DISPATCH_STATES", DispatchState)):
        block = rust.split(f"pub const {name}")[1].split("];")[0]
        listed = [part.split('"')[1] for part in block.split(",") if '"' in part]
        assert listed == [state.value for state in vocabulary], name


def _rust_list(name: str) -> list[str]:
    rust = (STATE_CRATE / "src/vocabulary.rs").read_text(encoding="utf-8")
    block = rust.split(f"pub const {name}:")[1].split("];")[0].split("= [", 1)[1]
    return [part.split('"')[1] for part in block.split(",") if '"' in part]


def test_the_rust_batch_contract_lists_equal_their_python_sources() -> None:
    """``policy.rs`` checks a batch against lists of its own: a list that drifts from the one
    ``history._validate_batch_integrity`` uses makes the two validators disagree."""
    ordered: dict[str, Iterable[object]] = {
        "RECOVERY_ROUTES": constants.RECOVERY_ROUTES,
        "RETRY_REASONS": tuple(constants.RetryReason),
        "CONTEXT_TELEMETRY_SOURCES": constants.CONTEXT_TELEMETRY_SOURCES,
        "ATTENTION_REASONS": operational_guards.ATTENTION_REASONS,
        "ATTENTION_STATE_FIELDS": constants.ATTENTION_STATE_FIELDS,
        "AUTO_DECISION_KINDS": constants.AUTO_DECISION_KINDS,
        "AUTO_STOP_BUDGET_EXHAUSTED": constants.AUTO_STOP_REASONS["budget-exhausted"],
        "AUTO_STOP_INTEGRITY_FAILURE": constants.AUTO_STOP_REASONS["integrity-failure"],
        "AUTO_STOP_NO_AUTOMATIC_ROUTE": constants.AUTO_STOP_REASONS[
            "no-automatic-route"
        ],
        "PLAN_FIELDS": constants.PLAN_FIELDS,
    }
    for name, source in ordered.items():
        assert _rust_list(name) == [str(item) for item in source], name
    sets: dict[str, Iterable[str]] = {
        "CONTEXT_PRESSURE_FIELDS": constants.CONTEXT_PRESSURE_FIELDS,
        "CARRIED_ITEM_RECORD_FIELDS": constants.CARRIED_ITEM_RECORD_FIELDS,
        "ATTENTION_EVENT_KINDS": constants.ATTENTION_EVENT_KINDS,
        "AUTO_DECISION_FIELDS": constants.AUTO_DECISION_FIELDS,
        "AUTO_EVIDENCE_BATCH_APPROVE": constants.AUTO_EVIDENCE_FIELDS["batch-approve"],
        "AUTO_EVIDENCE_DISPATCH": constants.AUTO_EVIDENCE_FIELDS["dispatch"],
        "AUTO_EVIDENCE_CONTINUATION": constants.AUTO_EVIDENCE_FIELDS["continuation"],
        "AUTO_EVIDENCE_DECISION": constants.AUTO_EVIDENCE_FIELDS["decision"],
        "AUTO_EVIDENCE_CARRY_OVER": constants.AUTO_EVIDENCE_FIELDS["carry-over"],
        "AUTO_STOP_FIELDS": constants.AUTO_STOP_FIELDS,
        "AUTO_REPORT_FIELDS": constants.AUTO_REPORT_FIELDS,
        "RECOVERY_EVENT_FIELDS": recovery_events.EVENT_FIELDS,
        "STARTUP_EVIDENCE_FIELDS": startup.FIELDS,
    }
    for name, source in sets.items():
        assert _rust_list(name) == sorted(source), name
    legacy = tuple(
        field
        for field in constants.PLAN_FIELDS
        if field not in _rust_list("PLAN_FIELDS_BEFORE_PIN")
    )
    assert legacy == constants.LEGACY_PLAN_FIELDS


def test_the_rust_policy_names_the_approver_of_the_automatic_policy() -> None:
    rust = (STATE_CRATE / "src/policy.rs").read_text(encoding="utf-8")

    assert f'const AUTO_APPROVER: &str = "{approval.AUTO_APPROVER}";' in rust


def test_the_state_fixture_of_the_rust_tests_passes_ledger_validate(
    tmp_path: Path,
) -> None:
    """The fixture stands for a state the 1.x installation wrote: it must be one it accepts."""
    state = tmp_path / ".harness/orchestration/state"
    shutil.copytree(FIXTURES / "legacy_v3", state)

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "harness/orchestration/coordinator.py"),
            "--repo",
            str(tmp_path),
            "ledger",
            "validate",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONPATH": str(ROOT), "PATH": ""},
    )

    assert result.returncode == 0, result.stderr
