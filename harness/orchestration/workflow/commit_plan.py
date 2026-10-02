"""The commit plan a developer brief carries.

Without an operator decision the coordinator derives one plan entry per definition-of-done item.
When the operator accepts an architect report with ``--commit-plan-file``, the architect's plan is
validated here and pinned on the batch instead; every later developer brief of the batch carries it.

Pure: no Git and no ledger access, so every rule is testable on plain data.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath

from harness.orchestration.core.constants import COMMIT_PLAN_ENTRY_FIELDS
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _canonical,
    _non_empty,
)

PLAN_ENTRY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
PLAN_FILE_REMEDY = (
    'write the plan file as {"commit_plan": [{"id": ..., "summary": ..., '
    '"expected_paths": [...], "covers": [1, ...]}, ...]} and pass it again with --commit-plan-file'
)


def _plan_error(message: str, remedy: str = PLAN_FILE_REMEDY) -> CoordinatorError:
    return CoordinatorError(f"commit plan file is invalid: {message}", remedy=remedy)


def _is_item_number(value: object, count: int) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= count
    )


def _expected_paths(entry_id: str, value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise _plan_error(
            f"entry {entry_id!r} expected_paths must be a non-empty list of relative paths"
        )
    paths: list[str] = []
    for path in value:
        if (
            not _non_empty(path)
            or path.startswith("/")
            or ".." in PurePosixPath(path.replace("\\", "/")).parts
        ):
            raise _plan_error(
                f"entry {entry_id!r} expected_paths must hold relative paths or globs "
                f"without a leading '/' or a '..' segment (got {path!r})"
            )
        paths.append(path)
    return paths


def pinned_plan(document: object, definition_of_done: list[str]) -> list[JsonObject]:
    """Validate an operator-supplied commit plan against the batch's definition of done.

    ``covers`` names definition-of-done items by their 1-based position. Every item must be
    covered by at least one entry and no entry may name an item the batch does not have.
    Entry order is the commit order the developer follows.
    """
    if not isinstance(document, dict) or set(document) != {"commit_plan"}:
        raise _plan_error("it must be a JSON object with exactly one key, commit_plan")
    entries = document["commit_plan"]
    if not isinstance(entries, list) or not entries:
        raise _plan_error("commit_plan must be a non-empty list of entries")
    count = len(definition_of_done)
    plan: list[JsonObject] = []
    seen: set[str] = set()
    covered: set[int] = set()
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict) or set(entry) != COMMIT_PLAN_ENTRY_FIELDS:
            raise _plan_error(
                f"entry {position} must have exactly the fields "
                f"{', '.join(sorted(COMMIT_PLAN_ENTRY_FIELDS))}"
            )
        entry_id = entry["id"]
        if not isinstance(entry_id, str) or PLAN_ENTRY_ID.fullmatch(entry_id) is None:
            raise _plan_error(
                f"entry {position} id must match {PLAN_ENTRY_ID.pattern} (got {entry_id!r})"
            )
        if entry_id in seen:
            raise _plan_error(f"entry id {entry_id!r} is used more than once")
        seen.add(entry_id)
        if not _non_empty(entry["summary"]):
            raise _plan_error(f"entry {entry_id!r} summary must be a non-empty string")
        covers = entry["covers"]
        if not isinstance(covers, list) or not covers:
            raise _plan_error(
                f"entry {entry_id!r} covers must be a non-empty list of definition-of-done item numbers"
            )
        unknown = [item for item in covers if not _is_item_number(item, count)]
        if unknown:
            raise _plan_error(
                f"entry {entry_id!r} covers unknown definition-of-done items {unknown}; "
                f"this batch has items 1..{count}",
                remedy=f"name only definition-of-done items 1..{count} in covers, then pass the plan again",
            )
        if len(set(covers)) != len(covers):
            raise _plan_error(f"entry {entry_id!r} covers an item more than once")
        covered.update(covers)
        plan.append(
            {
                "id": entry_id,
                "summary": entry["summary"],
                "expected_paths": _expected_paths(entry_id, entry["expected_paths"]),
                "covers": list(covers),
            }
        )
    uncovered = [item for item in range(1, count + 1) if item not in covered]
    if uncovered:
        raise _plan_error(
            f"no entry covers definition-of-done items {uncovered}",
            remedy="add an entry, or extend an entry's covers, so every definition-of-done item "
            f"1..{count} is covered, then pass the plan again",
        )
    return plan


def default_plan(
    definition_of_done: list[str], write_paths: list[str]
) -> list[JsonObject]:
    """One entry per definition-of-done item, each covering exactly its own item."""
    return [
        {
            "id": f"step-{index}",
            "summary": item,
            "expected_paths": write_paths,
            "covers": [index],
        }
        for index, item in enumerate(definition_of_done, start=1)
    ]


def plan_sha256(plan: list[JsonObject]) -> str:
    """The digest the architect accept records for the plan it pinned."""
    return hashlib.sha256(_canonical({"commit_plan": plan}).encode("utf-8")).hexdigest()


def accepted_plan_sha256(batch: JsonObject) -> str | None:
    """The plan digest recorded by the batch's accepted architect decision, if it pinned one."""
    for item in reversed(batch.get("dispatches", [])):
        decision = item.get("decision")
        if (
            item.get("role") == "architect"
            and isinstance(decision, dict)
            and decision.get("decision") == "accept"
        ):
            digest = decision.get("commit_plan_sha256")
            return digest if isinstance(digest, str) else None
    return None
