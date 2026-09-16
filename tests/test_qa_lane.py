#!/usr/bin/env python3
"""Regression tests for the clean-room QA queue bootstrap."""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path


ORCHESTRATION_ROOT = Path(__file__).resolve().parents[1] / "harness" / "orchestration"
sys.path.insert(0, str(ORCHESTRATION_ROOT))

import coordinator  # noqa: E402
import qa_lane  # noqa: E402
from ledger import LifecycleLedger  # noqa: E402


class QaLaneBootstrapTests(unittest.TestCase):
    def test_first_enqueue_creates_the_ledger_sequence_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            ledger = LifecycleLedger(state_root)
            ledger.ensure()
            ledger.records_root()
            ops = types.SimpleNamespace(
                CoordinatorError=coordinator.CoordinatorError,
                QA_QUEUE_FIELDS=coordinator.QA_QUEUE_FIELDS,
                _records_root=coordinator._records_root,
                _read_object=coordinator._read_object,
                _safe_id=coordinator._safe_id,
                _non_empty=coordinator._non_empty,
                _now=coordinator._now,
                _write_exclusive=coordinator._write_exclusive,
                _replace=coordinator._replace,
            )

            queue_path, entry = qa_lane._enqueue(state_root, "dispatch-0123456789abcdef", ops)

            self.assertTrue(queue_path.is_file())
            self.assertEqual(entry["sequence"], 1)
            sequence_path = coordinator._records_root(state_root) / "qa-lane" / "sequence.json"
            self.assertEqual(coordinator._read_object(sequence_path, "sequence"), {"next": 2})

            _, second = qa_lane._enqueue(state_root, "dispatch-fedcba9876543210", ops)
            self.assertEqual(second["sequence"], 2)
            self.assertEqual(coordinator._read_object(sequence_path, "sequence"), {"next": 3})


if __name__ == "__main__":
    unittest.main()
