"""Canonical results for completion-report checks, including historical spellings."""

from __future__ import annotations

from typing import overload

from .constants import ARCHITECT_READ_ONLY_CHECK_RESULT, QA_NOT_RUN_RESULT


@overload
def canonical_check_result(result: str) -> str: ...


@overload
def canonical_check_result(result: object) -> str | None: ...


def canonical_check_result(result: object) -> str | None:
    """Resolve supported aliases without turning malformed or unknown results into success."""
    if not isinstance(result, str):
        return None
    return {"passed": "pass", "not-run": QA_NOT_RUN_RESULT}.get(result, result)


def check_counts_as_success(role: object, result: object) -> bool:
    """Whether a check result proves success for the role that reported it."""
    return canonical_check_result(result) == "pass" or (
        role == "architect" and result == ARCHITECT_READ_ONLY_CHECK_RESULT
    )
