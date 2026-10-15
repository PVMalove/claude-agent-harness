"""Shared fixtures of the orchestration tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.orchestration import test_recovery


@pytest.fixture
def recovery() -> Iterator[test_recovery.RecoveryTests]:
    """The recovery fixture, by composition so its own suite does not run again."""
    fixture = test_recovery.RecoveryTests()
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.tearDown()
