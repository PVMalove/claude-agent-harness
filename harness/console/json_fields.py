"""Lenient readers for JSON payloads the console only displays (ledger records, completion
reports, Repo Map): a field of the wrong type reads as empty instead of failing the screen."""

from __future__ import annotations


def text(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
