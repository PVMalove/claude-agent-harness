#!/usr/bin/env python3
"""Regression tests for the clean-room QA queue bootstrap."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.orchestration import coordinator, qa_lane
from harness.orchestration.ledger import LifecycleLedger


class QaLaneBootstrapTests(unittest.TestCase):
    def test_first_enqueue_creates_the_ledger_sequence_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_root = Path(temporary) / "state"
            ledger = LifecycleLedger(state_root)
            ledger.ensure()

            queue_path, entry = qa_lane._enqueue(ledger, "dispatch-0123456789abcdef", coordinator)

            self.assertTrue(queue_path.is_file())
            self.assertEqual(entry["sequence"], 1)
            sequence_path = ledger.records_root() / "qa-lane" / "sequence.json"
            self.assertEqual(coordinator._read_object(sequence_path, "sequence"), {"next": 2})

            _, second = qa_lane._enqueue(ledger, "dispatch-fedcba9876543210", coordinator)
            self.assertEqual(second["sequence"], 2)
            self.assertEqual(coordinator._read_object(sequence_path, "sequence"), {"next": 3})


if __name__ == "__main__":
    unittest.main()
