#!/usr/bin/env python3
"""Regression tests for coordinator workspace invariants."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.orchestration.core.workspace import _harness_runtime_sha256


class HarnessRuntimeSnapshotTests(unittest.TestCase):
    def test_hash_ignores_generated_bytecode_and_state(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            runtime = Path(temporary) / ".harness" / "orchestration"
            runtime.mkdir(parents=True)
            source = runtime / "runner.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")

            initial = _harness_runtime_sha256(Path(temporary))
            pycache = runtime / "__pycache__"
            pycache.mkdir()
            (pycache / "runner.cpython-312.pyc").write_bytes(b"first bytecode")
            state = runtime / "state"
            state.mkdir()
            (state / "ledger.json").write_text("{}", encoding="utf-8")

            self.assertEqual(_harness_runtime_sha256(Path(temporary)), initial)

            (pycache / "runner.cpython-312.pyc").write_bytes(b"changed bytecode")
            self.assertEqual(_harness_runtime_sha256(Path(temporary)), initial)

            source.write_text("VALUE = 2\n", encoding="utf-8")
            self.assertNotEqual(_harness_runtime_sha256(Path(temporary)), initial)


if __name__ == "__main__":
    unittest.main()
