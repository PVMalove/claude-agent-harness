"""Hardening of the conflict-resolver route: a planning race and invariant errors of its events.

Real ledger and real Git (a local bare remote).  Only ``create_batch`` is wrapped, to move the
integration ref at the exact moment the resolver batch is planned.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from unittest import mock

from harness.errors import INTERNAL_INVARIANT_REMEDY
from harness.orchestration import coordinator
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.orchestration.workflow import resolver
from harness.orchestration.workflow.batch import create_batch
from tests.orchestration.test_conflict_resolver import ResolverFixture


class ResolverHardeningTests(ResolverFixture):
    def _event(self, brief: JsonObject) -> JsonObject:
        return coordinator.resolver_event(
            self.branch.args(
                record=self.record_id,
                ticket=None,
                branch=None,
                batch=None,
                kind="human-decision",
                dispatch=brief["dispatch_id"],
                decided_by="Malove",
                note="keep the ticket value",
                option="keep-ticket",
                extends_budget=False,
            )
        )

    def _edit_batch(self, batch_id: str, change: Callable[[JsonObject], None]) -> None:
        """Damage the ledger the way an interrupted or hand-made write would."""
        path = self.fx._records() / "batches" / f"{batch_id}.json"
        batch = json.loads(path.read_text(encoding="utf-8"))
        change(batch)
        path.write_text(json.dumps(batch), encoding="utf-8")

    def test_a_target_moving_during_planning_names_the_planned_batch_to_close(
        self,
    ) -> None:
        self.land()

        def moving_target(args: argparse.Namespace) -> JsonObject:
            self.land("services/y.py", "Y = 1\n")
            return create_batch(args)

        with mock.patch.object(resolver, "create_batch", moving_target):
            with self.assertRaises(CoordinatorError) as raised:
                self.resolve()

        planned = coordinator.list_batches(
            self.branch.args(ticket=None, state="planned", open=False)
        )["batches"]
        self.assertEqual(len(planned), 1)
        batch_id = planned[0]["batch_id"]
        self.assertIn(batch_id, raised.exception.message)
        self.assertIn(f"batch abandon --batch {batch_id}", raised.exception.remedy)
        # The remedy holds: once the planned batch is closed, the route plans the new target.
        coordinator.abandon_batch(
            self.branch.args(
                batch=batch_id, reason="target moved", **self.fx._approval()
            )
        )
        self.assertEqual(self.resolve()["state"], "created")

    def test_an_event_for_a_dispatch_missing_from_its_batch_is_an_invariant_error(
        self,
    ) -> None:
        self.land()
        created = self.resolve()
        brief = self.approved_resolver_dispatch(created)["brief"]
        self._edit_batch(
            created["batch_id"],
            lambda batch: batch.update(
                dispatches=[
                    entry
                    for entry in batch["dispatches"]
                    if entry["dispatch_id"] != brief["dispatch_id"]
                ]
            ),
        )

        with self.assertRaises(CoordinatorError) as raised:
            self._event(brief)

        self.assertIn("not registered in its batch", raised.exception.message)
        self.assertIn(INTERNAL_INVARIANT_REMEDY, raised.exception.remedy)

    def test_a_checkpointed_dispatch_without_its_checkpoint_is_an_invariant_error(
        self,
    ) -> None:
        self.land()
        created = self.resolve()
        brief = self.approved_resolver_dispatch(created)["brief"]

        def checkpointed_without_record(batch: JsonObject) -> None:
            for entry in batch["dispatches"]:
                if entry["dispatch_id"] == brief["dispatch_id"]:
                    entry["state"] = "checkpointed"
            batch.pop("checkpoints", None)

        self._edit_batch(created["batch_id"], checkpointed_without_record)

        with self.assertRaises(CoordinatorError) as raised:
            self._event(brief)

        self.assertIn("has no checkpoint", raised.exception.message)
        self.assertIn(INTERNAL_INVARIANT_REMEDY, raised.exception.remedy)
