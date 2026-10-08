"""Hardening of the delivery boundary: the runtime adapter call and the bounded publish push.

Real ledger and real Git (a local bare remote); only the push itself is replaced where a hung or
missing transport has to be simulated.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol, cast
from unittest import mock

import pytest

from harness.orchestration import coordinator
from harness.orchestration.core.utils import CoordinatorError
from harness.orchestration.workflow import delivery
from tests.orchestration import test_coordinator as coordinator_tests

# Never bind the TestCase class to a module name here: pytest would collect and run its suite again.


class _Run(Protocol):
    def __call__(self, *args: object, **kwargs: object) -> object: ...


@pytest.fixture
def routing() -> Iterator[coordinator_tests.CoordinatorRetryRoutingTests]:
    """The coordinator routing fixture, by composition so its own suite does not run again."""
    fixture = coordinator_tests.CoordinatorRetryRoutingTests()
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.tearDown()


def _push_fails(error: BaseException, timeouts: list[object]) -> _Run:
    """``subprocess.run`` that raises ``error`` for ``git push`` and runs everything else."""
    real = cast(_Run, subprocess.run)

    def run(*args: object, **kwargs: object) -> object:
        command = args[0] if args else kwargs.get("args")
        if isinstance(command, list) and "push" in command:
            timeouts.append(kwargs.get("timeout"))
            raise error
        return real(*args, **kwargs)

    return run


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (subprocess.TimeoutExpired(["git", "push"], 1), "did not finish within"),
        (FileNotFoundError("git"), "could not be started"),
    ],
)
def test_a_push_that_hangs_or_cannot_start_leaves_the_dispatch_approved(
    routing: coordinator_tests.CoordinatorRetryRoutingTests,
    error: BaseException,
    message: str,
) -> None:
    _, brief, candidate = routing._publish_brief_under(None)
    args = routing._args(dispatch=brief["dispatch_id"], remote="origin")
    timeouts: list[object] = []

    with mock.patch.object(subprocess, "run", new=_push_fails(error, timeouts)):
        with pytest.raises(CoordinatorError) as raised:
            coordinator.publish_dispatch(args)

    assert timeouts == [delivery.PUBLISH_PUSH_TIMEOUT_SECONDS]
    assert message in raised.value.message
    assert "repeat dispatch publish" in raised.value.remedy
    routing._assert_nothing_published(brief, "origin", candidate)
    # The remedy holds: the same approved dispatch publishes once the transport works again.
    assert coordinator.publish_dispatch(args)["candidate_commit"] == candidate


def test_publish_reads_back_exactly_the_branch_it_pushed(
    routing: coordinator_tests.CoordinatorRetryRoutingTests,
) -> None:
    _, brief, candidate = routing._publish_brief_under(None)
    # A deeper ref that ends with the branch name sorts first in an ls-remote pattern match.
    base = coordinator_tests._git(routing.repo, "rev-parse", "master")
    coordinator_tests._git(
        routing.repo,
        "push",
        "origin",
        f"{base}:refs/heads/a/refs/heads/{brief['branch']}",
    )

    published = coordinator.publish_dispatch(
        routing._args(dispatch=brief["dispatch_id"], remote="origin")
    )

    assert published["candidate_commit"] == candidate


def test_an_adapter_that_cannot_start_is_a_coordinator_error(tmp_path: Path) -> None:
    adapter = tmp_path / "adapter"
    adapter.write_text("not a program\n", encoding="utf-8")
    adapter.chmod(0o644)

    with pytest.raises(CoordinatorError) as raised:
        delivery._run_adapter([str(adapter), "dispatch"])

    assert "could not be started" in raised.value.message
    assert "executable" in raised.value.remedy


def test_an_adapter_rejection_carries_its_output() -> None:
    script = "import sys; sys.stderr.write('brief refused'); sys.exit(3)"

    with pytest.raises(CoordinatorError) as raised:
        delivery._run_adapter([sys.executable, "-c", script])

    assert "brief refused" in raised.value.message
    assert "brief refused" in raised.value.remedy


def test_an_adapter_that_starts_the_worker_returns() -> None:
    delivery._run_adapter([sys.executable, "-c", "pass"])
