#!/usr/bin/env python3
"""Tests for the clean-room playbook rule of scripts/clean_room/backend_orchestration.py: the
"Recovery route table" directly follows "Retry routing and abandon" and has at least one row per
RECOVERY_ROUTES value (issue #497)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import types
import unittest
from pathlib import Path

from harness.orchestration.core.constants import RECOVERY_ROUTES

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "clean_room" / "backend_orchestration.py"
PLAYBOOK = ROOT / "harness" / "orchestration" / "playbook.md"


def _load() -> types.ModuleType:
    loader = importlib.machinery.SourceFileLoader(
        "clean_room_backend_orchestration_under_test", str(SCRIPT)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


backend_orchestration = _load()


class RecoveryRouteTableRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.playbook = PLAYBOOK.read_text(encoding="utf-8")
        self.section = self.playbook.split("## Recovery route table", 1)[1].split(
            "\n## ", 1
        )[0]

    def _rows(self, route: str) -> list[str]:
        """The table rows of ``route``; the retry table above also names routes in its cells."""
        return [
            line
            for line in self.section.splitlines()
            if line.startswith("|") and f"| `{route}` |" in line
        ]

    def _without_rows(self, route: str) -> str:
        playbook = self.playbook
        for row in self._rows(route):
            playbook = playbook.replace(f"{row}\n", "", 1)
        return playbook

    def _assert_refused(self, playbook: str, fragment: str) -> None:
        with self.assertRaises(SystemExit) as raised:
            backend_orchestration._require_recovery_route_table(playbook)
        message = str(raised.exception.code)
        self.assertIn("missing rule: recovery route table", message)
        self.assertIn(fragment, message)

    def test_the_source_playbook_carries_the_table(self) -> None:
        backend_orchestration._require_recovery_route_table(self.playbook)

    def test_a_playbook_without_the_table_is_refused(self) -> None:
        self._assert_refused(
            self.playbook.replace("## Recovery route table", "## Route notes"),
            "## Recovery route table",
        )

    def test_the_table_must_directly_follow_the_retry_section(self) -> None:
        table = f"## Recovery route table{self.section}"
        moved = self.playbook.replace(table, "", 1)
        moved = f"{moved.rstrip()}\n\n{table}"

        self._assert_refused(moved, "must directly follow ## Retry routing and abandon")

    def test_the_table_needs_its_header(self) -> None:
        self._assert_refused(
            self.playbook.replace(
                "| Situation | Route | Who approves | Evidence |",
                "| Situation | Route | Evidence |",
            ),
            "header must be",
        )

    def test_every_route_needs_its_row(self) -> None:
        for route in RECOVERY_ROUTES:
            with self.subTest(route=route):
                self._assert_refused(self._without_rows(route), route)

    def test_a_route_may_cover_several_situations(self) -> None:
        rows = self._rows("developer-retry")
        self.assertGreater(len(rows), 1)

        backend_orchestration._require_recovery_route_table(
            self.playbook.replace(f"{rows[0]}\n", "", 1)
        )

    def test_a_route_outside_the_enum_is_refused(self) -> None:
        row = self._rows("abandon")[0]
        extra = row.replace("`abandon`", "`rebase`")

        self._assert_refused(self.playbook.replace(row, f"{row}\n{extra}", 1), "rebase")


if __name__ == "__main__":
    unittest.main()
