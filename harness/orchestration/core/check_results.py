"""Strict English enum for completion-report checks."""

from __future__ import annotations

from .constants import ReportCheckResult


def parse_check_result(result: object) -> ReportCheckResult | None:
    """Accept only an exact enum value; aliases and translated values are invalid."""
    if not isinstance(result, str):
        return None
    try:
        return ReportCheckResult(result)
    except ValueError:
        return None


def check_counts_as_success(result: object) -> bool:
    """Only the exact PASS enum proves a successful command."""
    return parse_check_result(result) == ReportCheckResult.PASS
