#!/usr/bin/env python3
"""Access of the public QA, Git and publish operations, against the real execution environment.

Real worktrees, a real ledger and a local bare remote: the public coordinator commands run with
nothing replaced. Every denial is injected into the environment (file permissions, a missing or
unlisted remote, an unprovable mode), never through a prepared report or a replaced route.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import unittest
from collections.abc import Iterator
from pathlib import Path

from harness.orchestration import coordinator, operation_access
from harness.orchestration.core import git_utils
from harness.orchestration.core.utils import CoordinatorError, JsonObject
from harness.storage import storage_path
from tests.orchestration import test_conflict_resolver as resolver_tests
from tests.orchestration import test_coordinator as coordinator_tests
from tests.orchestration.test_integration_record import PublishedBranch, _git

NON_ROOT = unittest.skipIf(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    "a privileged process is not denied by file permissions",
)
INHERIT = {"defaults": {"mode": "inherit"}}


@contextlib.contextmanager
def read_only(*paths: Path, tree: bool = False) -> Iterator[None]:
    """Remove write permission, as a sandbox without that write root would, then restore it."""
    targets = [
        item
        for path in paths
        for item in ([path, *path.rglob("*")] if tree else [path])
    ]
    modes = {item: item.stat().st_mode & 0o7777 for item in targets}
    for item in targets:
        item.chmod(modes[item] & ~0o222)
    try:
        yield
    finally:
        for item in reversed(targets):
            item.chmod(modes[item])


def write_policy(repo: Path, policy: JsonObject | None) -> None:
    (repo / ".harness/orchestration.json").write_text(
        json.dumps({"access_policy": policy} if policy is not None else {}),
        encoding="utf-8",
    )


class QaFixture(unittest.TestCase):
    """A reviewed candidate whose approved QA dispatch has not run yet."""

    def setUp(self) -> None:
        self.fx = coordinator_tests.CoordinatorRetryRoutingTests()
        self.fx.setUp()
        self.addCleanup(self.fx.tearDown)
        self.repo = self.fx.repo
        # The QA lane is repository-scoped: its ledger is the repository's own state directory.
        self.fx.state_dir = self.repo / coordinator.STATE_REL
        self.batch_id = self.fx._create_batch()["batch_id"]
        self.fx._accepted_architect(self.batch_id)
        self.candidate = self.fx._accepted_candidate(self.batch_id)
        self.fx._reported_review(self.batch_id, self.candidate)
        self.fx._decide(self.batch_id, "accept")
        self.clean_room = storage_path(self.repo, "runs", "qa")
        self.clean_room.mkdir(parents=True, exist_ok=True)

    def qa_brief(self, policy: JsonObject | None) -> JsonObject:
        write_policy(self.repo, policy)
        created: JsonObject = self.fx._dispatch(
            self.batch_id, "qa", candidate=self.candidate
        )
        brief: JsonObject = created["brief"]
        return brief

    def lane_args(self, **values: object) -> argparse.Namespace:
        return argparse.Namespace(repo=str(self.repo), state_dir=None, **values)

    def run_qa(self, brief: JsonObject) -> JsonObject:
        return coordinator.run_qa(
            self.lane_args(dispatch=brief["dispatch_id"], lease_seconds=None)
        )

    def assert_lane_is_free_and_dispatch_approved(self, brief: JsonObject) -> None:
        lane = coordinator.qa_status(self.lane_args())
        self.assertEqual((lane["lease"], lane["queue"]), (None, []))
        status = coordinator._load_dispatch_status(
            self.repo / coordinator.STATE_REL, brief["dispatch_id"]
        )
        self.assertEqual(status["state"], "approved")

    def attempts(self) -> list[JsonObject]:
        directory = self.fx._records() / "qa-lane" / "attempts"
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(directory.glob("*.json"))
        ]


@NON_ROOT
class CleanRoomQaTests(QaFixture):
    def test_a_denied_clean_room_directory_stops_qa_before_the_lane_and_a_later_run_works(
        self,
    ) -> None:
        brief = self.qa_brief(INHERIT)

        with read_only(self.clean_room):
            with self.assertRaises(operation_access.OperationAccessError) as stopped:
                self.run_qa(brief)

        evidence = stopped.exception.evidence
        denied = [c for c in evidence["checks"] if c["state"] == "denied"]
        self.assertEqual(evidence["status"], "denied")
        self.assertEqual([c["label"] for c in denied], ["clean-room checkout"])
        self.assertIn(str(self.clean_room), stopped.exception.remedy)
        self.assert_lane_is_free_and_dispatch_approved(brief)
        (attempt,) = self.attempts()
        self.assertEqual(attempt["stage"], "access")
        self.assertEqual(attempt["evidence"]["status"], "denied")

        result = self.run_qa(brief)

        self.assertEqual(result["state"], "reported")
        self.assertEqual(len(self.attempts()), 1)
        report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
        self.assertEqual(report["outcome"], "completed")
        self.assertEqual(report["checks_run"][0]["result"], "pass")

    def test_denied_git_metadata_stops_qa_with_the_resource_named(self) -> None:
        brief = self.qa_brief(INHERIT)

        with read_only(self.repo / ".git"):
            with self.assertRaises(operation_access.OperationAccessError) as stopped:
                self.run_qa(brief)

        denied = [
            c["requirement"]
            for c in stopped.exception.evidence["checks"]
            if c["state"] == "denied"
        ]
        self.assertEqual(denied, ["git_common"])
        self.assert_lane_is_free_and_dispatch_approved(brief)
        self.assertEqual(self.run_qa(brief)["state"], "reported")

    def test_a_mode_the_coordinator_cannot_prove_stops_qa(self) -> None:
        brief = self.qa_brief(
            {**INHERIT, "operations": {"qa": {"mode": "sandbox"}}},
        )

        with self.assertRaises(operation_access.OperationAccessError) as stopped:
            self.run_qa(brief)

        self.assertEqual(stopped.exception.evidence["status"], "unsupported")
        self.assert_lane_is_free_and_dispatch_approved(brief)

    def test_a_role_override_never_replaces_the_qa_operation_choice(self) -> None:
        brief = self.qa_brief({**INHERIT, "roles": {"qa": {"mode": "sandbox"}}})

        self.assertEqual(brief["runtime_access"]["mode"], "inherit")
        self.assertEqual(self.run_qa(brief)["state"], "reported")

    def test_qa_is_never_routed_through_a_runtime_adapter(self) -> None:
        brief = self.qa_brief(INHERIT)

        with self.assertRaises(CoordinatorError) as refused:
            self.fx._start(brief["dispatch_id"])

        self.assertIn("clean-room QA lane", refused.exception.message)

    def test_without_authored_access_a_failed_checkout_still_leaves_no_lane(
        self,
    ) -> None:
        brief = self.qa_brief(None)

        with read_only(self.clean_room):
            with self.assertRaises(CoordinatorError) as failed:
                self.run_qa(brief)

        self.assertIn("clean QA checkout storage", failed.exception.message)
        self.assertIn(str(self.clean_room), failed.exception.remedy)
        self.assert_lane_is_free_and_dispatch_approved(brief)
        (attempt,) = self.attempts()
        self.assertEqual(attempt["stage"], "gate-run")
        self.assertEqual(self.run_qa(brief)["state"], "reported")


@NON_ROOT
class PublishTests(QaFixture):
    """Publish needs an accepted QA candidate, so the fixture runs the real QA lane first."""

    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(self.run_qa(self.qa_brief(INHERIT))["state"], "reported")
        self.fx._decide(self.batch_id, "accept")

    def publish_brief(self, policy: JsonObject) -> JsonObject:
        write_policy(self.repo, policy)
        created: JsonObject = self.fx._dispatch(
            self.batch_id, "developer", purpose="publish", candidate=self.candidate
        )
        brief: JsonObject = created["brief"]
        return brief

    def publish(self, brief: JsonObject, remote: str = "origin") -> JsonObject:
        return coordinator.publish_dispatch(
            self.fx._args(dispatch=brief["dispatch_id"], remote=remote)
        )

    def remote_has_candidate(self) -> bool:
        return self.candidate in _git(self.repo, "ls-remote", "--heads", "origin")

    def test_denied_git_metadata_stops_publish_before_the_push(self) -> None:
        brief = self.publish_brief(INHERIT)

        with read_only(self.repo / ".git"):
            with self.assertRaises(operation_access.OperationAccessError) as stopped:
                self.publish(brief)

        self.assertEqual(stopped.exception.evidence["operation"], "publish")
        self.assertFalse(self.remote_has_candidate())

        published = self.publish(brief)

        self.assertEqual(published["candidate_commit"], self.candidate)
        self.assertTrue(self.remote_has_candidate())

    def test_an_unlisted_remote_host_stops_publish_and_is_never_contacted(self) -> None:
        brief = self.publish_brief(
            {"defaults": {"mode": "inherit", "network": {"hosts": ["github.com"]}}}
        )
        _git(self.repo, "remote", "add", "mirror", "git@example.invalid:o/r.git")

        with self.assertRaises(operation_access.OperationAccessError) as stopped:
            self.publish(brief, "mirror")

        self.assertEqual(stopped.exception.evidence["status"], "denied")
        self.assertIn("example.invalid", stopped.exception.remedy)
        self.assertFalse(self.remote_has_candidate())

    def test_a_remote_that_refuses_the_push_is_classified_and_publishes_nothing(
        self,
    ) -> None:
        brief = self.publish_brief(INHERIT)
        origin = Path(_git(self.repo, "remote", "get-url", "origin"))

        with read_only(origin, tree=True):
            with self.assertRaises(git_utils.GitAccessError) as refused:
                self.publish(brief)

        self.assertEqual(refused.exception.category, git_utils.REMOTE_DENIED)
        self.assertFalse(self.remote_has_candidate())
        self.assertEqual(self.publish(brief)["state"], "reported")


@NON_ROOT
class LocalQaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.branch = PublishedBranch()
        self.addCleanup(self.branch.close)
        self.repo = self.branch.repo
        # Local QA is repository-scoped: the integration record lives in the repository state.
        self.branch.fixture.state_dir = self.repo / coordinator.STATE_REL
        self.branch.publish()
        self.record = self.branch.prepare()["integration_record_id"]
        write_policy(self.repo, INHERIT)

    def local_qa(self, **changes: object) -> JsonObject:
        values: JsonObject = {
            "record": self.record,
            "ci_condition": "absent",
            "reason": "No combined CI",
            "request": None,
            "retry": False,
            "lease_seconds": None,
        }
        values.update(changes)
        return coordinator.integration_local_qa(self.branch.args(**values))

    def test_local_qa_records_an_access_attempt_without_a_lease_and_runs_after_retry(
        self,
    ) -> None:
        with read_only(self.repo / ".git"):
            refused = self.local_qa()

        self.assertEqual(refused["state"], "unavailable")
        self.assertEqual(refused["stage"], "access")
        self.assertEqual(refused["access"]["status"], "denied")
        self.assertTrue(refused["remedy"])
        lane = coordinator.qa_status(
            argparse.Namespace(repo=str(self.repo), state_dir=None)
        )
        self.assertEqual((lane["lease"], lane["queue"]), (None, []))

        completed = self.local_qa(retry=True)

        self.assertEqual(completed["state"], "completed")
        self.assertEqual(completed["verification"], "verified")


@NON_ROOT
class IntegrationGitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = resolver_tests.ResolverFixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.branch = self.fixture.branch
        self.repo = self.branch.repo
        write_policy(self.repo, INHERIT)

    def test_refresh_stops_before_changing_anything_when_git_metadata_is_denied(
        self,
    ) -> None:
        self.fixture.land("landed.txt", "landed\n")
        before = self.branch.snapshot()

        with read_only(self.repo / ".git"):
            with self.assertRaises(operation_access.OperationAccessError) as stopped:
                coordinator.integration_refresh(
                    self.branch.args(
                        record=self.fixture.record_id,
                        ticket=None,
                        branch=None,
                        batch=None,
                    )
                )

        self.assertEqual(stopped.exception.evidence["operation"], "git")
        self.assertEqual(self.branch.snapshot(), before)

    def test_resolve_stops_before_creating_a_batch_when_the_remote_is_unreachable(
        self,
    ) -> None:
        self.fixture.land()
        before = self.branch.snapshot()
        origin = Path(_git(self.repo, "remote", "get-url", "origin"))
        moved = origin.with_name("origin-moved.git")

        # Prove the route works first, then remove the remote from the environment.
        origin.rename(moved)
        try:
            with self.assertRaises(CoordinatorError):
                self.fixture.resolve()
        finally:
            moved.rename(origin)

        self.assertEqual(self.branch.snapshot(), before)
        self.assertEqual(self.fixture.resolve()["state"], "created")

    def test_resolve_stops_when_git_metadata_is_denied(self) -> None:
        self.fixture.land()
        before = self.branch.snapshot()

        with read_only(self.repo / ".git"):
            with self.assertRaises(operation_access.OperationAccessError) as stopped:
                self.fixture.resolve()

        self.assertEqual(stopped.exception.evidence["status"], "denied")
        self.assertEqual(self.branch.snapshot(), before)
        self.assertEqual(self.fixture.resolve()["state"], "created")


if __name__ == "__main__":
    unittest.main()
