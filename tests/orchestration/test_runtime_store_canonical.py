"""The canonical JSON of the runtime store equals the canonical JSON of the file ledger.

``crates/state/tests/fixtures/canonical_golden.expected`` holds the bytes Python writes for
``canonical_golden.json``. This module proves that Python still writes exactly those bytes, and the
Rust test ``canonical::tests::the_golden_fixture_is_written_byte_for_byte_as_python_writes_it``
proves that the Rust writer does. Both languages therefore agree on every ``record_sha256``,
``brief_sha256`` and ``report_sha256`` that hashes a canonical form (ADR 0020, section 4).
"""

from __future__ import annotations

import json
from pathlib import Path

from harness.orchestration.core.constants import BatchState, DispatchState
from harness.orchestration.ledger.lifecycle import _canonical

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
