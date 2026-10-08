"""The commit plan a developer brief carries, and how a developer report is checked against it.

Without an operator decision the coordinator derives one plan entry per definition-of-done item.
When the operator accepts an architect report with ``--commit-plan-file``, the architect's plan is
validated here and pinned on the batch instead; every later developer brief of the batch carries it.

An initial or rebase developer report maps its created commits to plan entries as a relation: one
commit may close several entries (merged), one entry may be closed by several commits (split), and
an entry may stay unclosed. Anything but one-to-one is a divergence, which the report must justify
next to a definition-of-done coverage record. A developer-retry report keeps the strict rule: each
new commit closes exactly one distinct plan entry. Under an approved rebase target (issue #504) it
also accounts for every previous-candidate commit exactly once, as the ``rebased_from`` of its
rebased copy or as ``dropped`` with a reason.

Pure: no Git and no ledger access. The caller passes the created commits and a SHA resolver, so
every rule is testable on plain data.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterator
from pathlib import PurePosixPath

from harness.orchestration.core.constants import (
    COMMIT_MAP_DROPPED_FIELDS,
    COMMIT_MAP_PLANNED_FIELDS,
    COMMIT_MAP_REBASED_FIELDS,
    COMMIT_PLAN_ENTRY_FIELDS,
    DOD_COVERED_FIELDS,
    DOD_NOT_COVERED_FIELDS,
)
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _canonical,
    _non_empty,
)

COVERAGE_FIELDS = ("dod_coverage", "divergence_justification")
COVERAGE_REMEDY = (
    "set dod_coverage to one record per definition-of-done item: "
    '{"dod_item": <n>, "commits": [<sha>, ...]} or {"dod_item": <n>, "not_covered": "<reason>"}'
)
PLAN_ENTRY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
PLAN_FILE_REMEDY = (
    'write the plan file as {"commit_plan": [{"id": ..., "summary": ..., '
    '"expected_paths": [...], "covers": [1, ...]}, ...]} and pass it again with --commit-plan-file'
)
REBASE_MAP_REMEDY = (
    "map every commit after rebase_target_commit once: a rebased copy as "
    '{"commit_sha": <sha>, "rebased_from": <original sha>} and a new commit as '
    '{"commit_sha": <sha>, "plan_entry_id": <id>}; list a previous-candidate commit the rebase '
    'did not carry as {"rebased_from": <original sha>, "dropped": "<reason>"}'
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


def decided_entries(
    batch: JsonObject, role: str, decisions: set[str]
) -> Iterator[JsonObject]:
    """The batch's ``role`` dispatch entries decided with one of ``decisions``, newest first.

    A decision is only ever recorded on a reported entry, so it needs no separate state check.
    """
    for item in reversed(batch.get("dispatches", [])):
        decision = item.get("decision")
        if (
            item.get("role") == role
            and isinstance(decision, dict)
            and decision.get("decision") in decisions
        ):
            yield item


def accepted_plan_sha256(batch: JsonObject) -> str | None:
    """The plan digest recorded by the batch's accepted architect decision, if it pinned one.

    A superseding batch without an architect accept of its own falls back to the digest of the
    architect it carried from the abandoned batch (issue #506).
    """
    for item in decided_entries(batch, "architect", {"accept"}):
        digest = item["decision"].get("commit_plan_sha256")
        return digest if isinstance(digest, str) else None
    link = batch.get("supersedes")
    carried = link.get("architect") if isinstance(link, dict) else None
    digest = carried.get("commit_plan_sha256") if isinstance(carried, dict) else None
    return digest if isinstance(digest, str) else None


def is_developer_retry(dispatch: JsonObject) -> bool:
    """A developer-retry brief keeps the strict one-commit-one-entry commit_map."""
    transition = dispatch.get("transition")
    return (
        isinstance(transition, dict)
        and transition.get("next_action") == "developer-retry"
    )


def rebase_target(dispatch: JsonObject) -> str | None:
    """The human-approved rebase target a developer-retry brief carries (issue #504), if any."""
    target = dispatch.get("rebase_target_commit")
    return target if isinstance(target, str) and target else None


def _relation_applies(dispatch: JsonObject) -> bool:
    """Only an initial or rebase developer work report maps commits as a relation."""
    return (
        dispatch.get("role") == "developer"
        and bool(dispatch.get("commit_plan"))
        and not is_developer_retry(dispatch)
    )


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _covers(entry: JsonObject, position: int) -> list[int]:
    """An entry's definition-of-done items; a brief written before ``covers`` maps by position."""
    covers = entry.get("covers")
    return covers if isinstance(covers, list) else [position]


def _plan_covers(dispatch: JsonObject) -> dict[object, list[int]]:
    """Each plan entry id with its definition-of-done items, in plan order."""
    return {
        entry.get("id"): _covers(entry, position)
        for position, entry in enumerate(dispatch["commit_plan"], start=1)
        if isinstance(entry, dict)
    }


def _covering_commits(
    item: int, pairs: list[tuple[str, str]], covers: dict[object, list[int]]
) -> list[str]:
    """The commits commit_map maps to a plan entry whose ``covers`` lists ``item``."""
    return _unique([sha for sha, plan_id in pairs if item in covers.get(plan_id, [])])


def _relation(pairs: list[tuple[str, str]], plan_ids: list[str]) -> JsonObject:
    by_commit: dict[str, list[str]] = {}
    by_entry: dict[str, list[str]] = {}
    for sha, plan_id in pairs:
        by_commit.setdefault(sha, []).append(plan_id)
        by_entry.setdefault(plan_id, []).append(sha)
    order = {plan_id: index for index, plan_id in enumerate(plan_ids)}
    return {
        "merged_commits": [
            {
                "commit_sha": sha,
                "plan_entry_ids": sorted(
                    ids, key=lambda plan_id: order.get(plan_id, len(order))
                ),
            }
            for sha, ids in by_commit.items()
            if len(ids) > 1
        ],
        "split_entries": [
            {"plan_entry_id": plan_id, "commit_shas": by_entry[plan_id]}
            for plan_id in plan_ids
            if len(by_entry.get(plan_id, [])) > 1
        ],
        "unclosed_entries": [
            plan_id for plan_id in plan_ids if plan_id not in by_entry
        ],
    }


def _describe(relation: JsonObject) -> str:
    parts = []
    for merged in relation["merged_commits"]:
        parts.append(
            f"commit {merged['commit_sha']} merges {', '.join(merged['plan_entry_ids'])}"
        )
    for split in relation["split_entries"]:
        parts.append(
            f"entry {split['plan_entry_id']} is split across {len(split['commit_shas'])} commits"
        )
    if relation["unclosed_entries"]:
        parts.append(f"entries {', '.join(relation['unclosed_entries'])} are unclosed")
    return "; ".join(parts)


def check_fields_allowed(report: JsonObject, dispatch: JsonObject) -> None:
    """dod_coverage and divergence_justification belong only to an initial or rebase developer
    report against a commit plan; a developer-retry report keeps its strict commit_map."""
    present = [field for field in COVERAGE_FIELDS if field in report]
    if present and not _relation_applies(dispatch):
        raise CoordinatorError(
            f"completion report fields {present} belong only to an initial or rebase developer "
            "report against a commit plan",
            remedy="drop dod_coverage and divergence_justification: a developer-retry report maps "
            "each new commit to one distinct plan entry, and other roles have no commit plan",
        )


def _plan_ids(dispatch: JsonObject) -> list[str]:
    plan = dispatch["commit_plan"]
    plan_ids = [entry.get("id") for entry in plan if isinstance(entry, dict)]
    if len(plan_ids) != len(plan) or not all(
        isinstance(item, str) for item in plan_ids
    ):
        raise CoordinatorError(
            "dispatch commit_plan is malformed",
            remedy="create a new developer dispatch with a valid immutable commit plan",
        )
    return [str(item) for item in plan_ids]


def _required_commit_map(report: JsonObject, retry: bool) -> list[object]:
    commit_map = report.get("commit_map")
    if not isinstance(commit_map, list) or not commit_map:
        raise CoordinatorError(
            "developer completion report requires commit_map for the immutable commit plan",
            remedy="map every commit created after snapshot_commit to one distinct commit_plan entry"
            if retry
            else "map every commit created after the dispatch base (snapshot_commit, or the "
            "rebase target for a rebase) to one or more commit_plan entries",
        )
    return commit_map


def _resolve_reported(resolve: Callable[[str], str], sha: str, field: str) -> str:
    """Resolve a reported SHA, naming the report field instead of candidate_commit on failure."""
    try:
        return resolve(sha)
    except CoordinatorError as exc:
        raise CoordinatorError(
            f"{field} entry {sha!r} is not the hexadecimal SHA of a commit in this repository",
            remedy=f"report every SHA in {field} as the 7-64 character hex SHA of a commit "
            "this dispatch created",
        ) from exc


def _commit_map_pairs(commit_map: list[object]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for entry in commit_map:
        if isinstance(entry, dict) and {"rebased_from", "dropped"} & set(entry):
            raise CoordinatorError(
                "commit_map entries with rebased_from or dropped belong only to a "
                "developer-retry brief with rebase_target_commit",
                remedy="only a brief with rebase_target_commit maps rebased_from and dropped: "
                "map every created commit to a commit_plan entry as "
                '{"commit_sha": <sha>, "plan_entry_id": <id>}',
            )
        if not isinstance(entry, dict) or set(entry) != COMMIT_MAP_PLANNED_FIELDS:
            raise CoordinatorError(
                "commit_map entries must contain only commit_sha and plan_entry_id",
                remedy="report one SHA-to-plan-entry mapping for every created commit",
            )
        sha, plan_id = entry["commit_sha"], entry["plan_entry_id"]
        if not isinstance(sha, str) or not isinstance(plan_id, str):
            raise CoordinatorError(
                "commit_map entries must use string SHA and plan entry id",
                remedy="report canonical commit SHA strings and commit plan entry ids",
            )
        pairs.append((sha, plan_id))
    return pairs


def _check_retry_mapping(
    pairs: list[tuple[str, str]], created: list[str], plan_ids: list[str]
) -> None:
    mapped = dict(pairs)
    mapped_plan_ids = set(mapped.values())
    if (
        set(mapped) != set(created)
        or not mapped_plan_ids.issubset(plan_ids)
        or len(mapped) != len(created)
        or len(mapped) != len(pairs)
        or len(mapped_plan_ids) != len(mapped)
    ):
        raise CoordinatorError(
            "commit_map must map each created commit to one distinct immutable plan entry",
            remedy="report every commit created after snapshot_commit once, each against its own "
            "commit_plan entry; a developer-retry need not close every plan entry",
        )


def _rebase_entries(
    commit_map: list[object], resolve: Callable[[str], str]
) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[tuple[str, str]]]:
    """Split a rebase developer-retry commit_map into its three entry shapes, with resolved SHAs:
    new commits ``(sha, plan_entry_id)``, rebased copies ``(original, copy)`` and dropped
    previous-candidate commits ``(original, reason)``."""
    planned: list[tuple[str, str]] = []
    rebased: list[tuple[str, str]] = []
    dropped: list[tuple[str, str]] = []
    for entry in commit_map:
        if not isinstance(entry, dict) or set(entry) not in (
            COMMIT_MAP_PLANNED_FIELDS,
            COMMIT_MAP_REBASED_FIELDS,
            COMMIT_MAP_DROPPED_FIELDS,
        ):
            raise CoordinatorError(
                "commit_map entries of a developer-retry with rebase_target_commit must be "
                "{commit_sha, plan_entry_id}, {commit_sha, rebased_from} or {rebased_from, dropped}",
                remedy=REBASE_MAP_REMEDY,
            )
        if not all(isinstance(value, str) for value in entry.values()):
            raise CoordinatorError(
                "commit_map entries must use string SHAs, plan entry ids and reasons",
                remedy=REBASE_MAP_REMEDY,
            )
        if "dropped" in entry:
            if not _non_empty(entry["dropped"]):
                raise CoordinatorError(
                    f"commit_map drops previous-candidate commit {entry['rebased_from']} "
                    "without a reason",
                    remedy="state in dropped why the rebase did not carry that commit onto "
                    "rebase_target_commit, or map its rebased copy with rebased_from",
                )
            original = _resolve_reported(
                resolve, entry["rebased_from"], "commit_map[].rebased_from"
            )
            dropped.append((original, entry["dropped"]))
            continue
        sha = _resolve_reported(resolve, entry["commit_sha"], "commit_map[].commit_sha")
        if "rebased_from" in entry:
            original = _resolve_reported(
                resolve, entry["rebased_from"], "commit_map[].rebased_from"
            )
            rebased.append((original, sha))
        else:
            planned.append((sha, entry["plan_entry_id"]))
    return planned, rebased, dropped


def _check_rebase_mapping(
    commit_map: list[object],
    created: list[str],
    previous: list[str],
    plan_ids: list[str],
    resolve: Callable[[str], str],
) -> None:
    """A developer-retry under an approved rebase target accounts for every previous-candidate
    commit (those above the old base, up to ``snapshot_commit``) exactly once, as ``rebased_from``
    of its rebased copy or as ``dropped``, and maps every commit it created after the target exactly
    once: a rebased copy inherits its original's plan entry, a new commit closes its own entry. A
    copy is never its own original: that pair would claim a merged commit as reviewed by patch-id."""
    planned, rebased, dropped = _rebase_entries(commit_map, resolve)
    originals = [original for original, _ in rebased] + [
        original for original, _ in dropped
    ]
    unknown = _unique([sha for sha in originals if sha not in previous])
    if unknown:
        raise CoordinatorError(
            f"commit_map names rebased_from commits that are not previous-candidate commits: {unknown}",
            remedy="name in rebased_from only the previous-candidate commits between "
            "git merge-base <snapshot_commit> <rebase_target_commit> and snapshot_commit: "
            f"{', '.join(previous) or 'none'}",
        )
    repeated = _unique([sha for sha in originals if originals.count(sha) > 1])
    if repeated:
        raise CoordinatorError(
            f"commit_map lists previous-candidate commits more than once: {repeated}",
            remedy="list every previous-candidate commit exactly once: as the rebased_from of its "
            "rebased copy, or with dropped and a reason",
        )
    missing = [sha for sha in previous if sha not in originals]
    if missing:
        raise CoordinatorError(
            f"commit_map does not account for previous-candidate commits {missing}",
            remedy='add {"commit_sha": <copy>, "rebased_from": <original>} for each commit the '
            'rebase carried onto rebase_target_commit, or {"rebased_from": <original>, '
            '"dropped": "<reason>"} for a commit it did not carry',
        )
    own = _unique([copy for original, copy in rebased if copy == original])
    if own:
        raise CoordinatorError(
            f"commit_map maps previous-candidate commits as rebased copies of themselves: {own}",
            remedy="rebase the previous-candidate commits onto rebase_target_commit with "
            "git rebase --onto instead of merging it: a rebased copy is a new commit after the "
            "target, and rebased_from names its original",
        )
    mapped = [sha for sha, _ in planned] + [sha for _, sha in rebased]
    foreign = _unique([sha for sha in mapped if sha not in created])
    if foreign:
        raise CoordinatorError(
            f"commit_map names commits this dispatch did not create: {foreign}",
            remedy="map only the commits after rebase_target_commit up to commit_sha",
        )
    twice = _unique([sha for sha in mapped if mapped.count(sha) > 1])
    if twice:
        raise CoordinatorError(
            f"commit_map maps created commits more than once: {twice}",
            remedy="map each commit after rebase_target_commit exactly once: a rebased copy with "
            "rebased_from, or a new commit against one commit_plan entry",
        )
    unmapped = [sha for sha in created if sha not in mapped]
    if unmapped:
        raise CoordinatorError(
            f"commit_map must map each created commit once (unmapped: {unmapped})",
            remedy=REBASE_MAP_REMEDY,
        )
    entries = [plan_id for _, plan_id in planned]
    if any(plan_id not in plan_ids for plan_id in entries) or len(set(entries)) != len(
        entries
    ):
        raise CoordinatorError(
            "commit_map must map each new commit to one distinct immutable plan entry",
            remedy="map every new commit after the rebased copies to its own commit_plan entry; "
            "a rebased copy inherits the entry of its original and names none, and a "
            "developer-retry need not close every plan entry",
        )


def rebased_commits(
    report: JsonObject, resolve: Callable[[str], str]
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """The ``(original, copy)`` pairs and ``(original, reason)`` drops of a rebase developer-retry
    report's commit_map, which ``check_report`` has already accepted."""
    commit_map = report.get("commit_map")
    if not isinstance(commit_map, list):
        return [], []
    _, rebased, dropped = _rebase_entries(commit_map, resolve)
    return rebased, dropped


def _check_relation(
    pairs: list[tuple[str, str]], created: list[str], plan_ids: list[str]
) -> None:
    repeated = _unique(
        [
            f"{sha}/{plan_id}"
            for sha, plan_id in pairs
            if pairs.count((sha, plan_id)) > 1
        ]
    )
    if repeated:
        raise CoordinatorError(
            f"commit_map repeats the pairs {repeated}",
            remedy="report each commit_sha/plan_entry_id pair once",
        )
    unknown = _unique([plan_id for _, plan_id in pairs if plan_id not in plan_ids])
    if unknown:
        raise CoordinatorError(
            f"commit_map names unknown plan entries {unknown}",
            remedy=f"map commits only to this brief's commit_plan entries: {', '.join(plan_ids)}",
        )
    foreign = _unique([sha for sha, _ in pairs if sha not in created])
    if foreign:
        raise CoordinatorError(
            f"commit_map names commits this dispatch did not create: {foreign}",
            remedy="map only the commits after the dispatch base (snapshot_commit, or the rebase "
            "target for a rebase) up to commit_sha",
        )
    mapped = {sha for sha, _ in pairs}
    unmapped = [sha for sha in created if sha not in mapped]
    if unmapped:
        raise CoordinatorError(
            f"commit_map must map each created commit to at least one plan entry (unmapped: {unmapped})",
            remedy="add a commit_sha/plan_entry_id pair for every commit created after the dispatch base",
        )


def _check_coverage(
    coverage: object,
    count: int,
    created: list[str],
    resolve: Callable[[str], str],
    pairs: list[tuple[str, str]],
    covers: dict[object, list[int]],
) -> None:
    if not isinstance(coverage, list):
        raise CoordinatorError(
            "dod_coverage must be a list with one record per definition-of-done item",
            remedy=COVERAGE_REMEDY,
        )
    seen: list[int] = []
    for record in coverage:
        if not isinstance(record, dict) or set(record) not in (
            DOD_COVERED_FIELDS,
            DOD_NOT_COVERED_FIELDS,
        ):
            raise CoordinatorError(
                "dod_coverage records must be {dod_item, commits} or {dod_item, not_covered}",
                remedy=COVERAGE_REMEDY,
            )
        item = record["dod_item"]
        if not _is_item_number(item, count):
            raise CoordinatorError(
                f"dod_coverage names unknown definition-of-done item {item!r}; "
                f"this brief has items 1..{count}",
                remedy=COVERAGE_REMEDY,
            )
        if item in seen:
            raise CoordinatorError(
                f"dod_coverage records definition-of-done item {item} more than once",
                remedy=COVERAGE_REMEDY,
            )
        seen.append(item)
        if "not_covered" in record:
            if not _non_empty(record["not_covered"]):
                raise CoordinatorError(
                    f"dod_coverage item {item} is not_covered without a reason",
                    remedy="state in not_covered why the item is not covered, or list the commits that cover it",
                )
            continue
        commits = record["commits"]
        if (
            not isinstance(commits, list)
            or not commits
            or not all(isinstance(sha, str) for sha in commits)
        ):
            raise CoordinatorError(
                f"dod_coverage item {item} commits must be a non-empty list of commit SHAs",
                remedy=COVERAGE_REMEDY,
            )
        claimed = [
            _resolve_reported(resolve, sha, "dod_coverage[].commits") for sha in commits
        ]
        foreign = [sha for sha, full in zip(commits, claimed) if full not in created]
        if foreign:
            raise CoordinatorError(
                f"dod_coverage item {item} names commits this dispatch did not create: {foreign}",
                remedy="list only commits this dispatch created as covering commits",
            )
        expected = [plan_id for plan_id, items in covers.items() if item in items]
        if not set(claimed) & set(_covering_commits(item, pairs, covers)):
            entries = ", ".join(str(plan_id) for plan_id in expected)
            raise CoordinatorError(
                f"dod_coverage item {item} claims commits {commits}, but commit_map maps none "
                f"of them to a plan entry covering item {item} ({entries})",
                remedy=f"map one of the claimed commits to {entries} in commit_map, list the "
                f"commits commit_map maps to {entries}, or record item {item} as not_covered "
                "with a reason",
            )
    missing = [item for item in range(1, count + 1) if item not in seen]
    if missing:
        raise CoordinatorError(
            f"dod_coverage has no record for definition-of-done items {missing}",
            remedy=COVERAGE_REMEDY,
        )


def check_report(
    report: JsonObject,
    dispatch: JsonObject,
    created: list[str],
    resolve: Callable[[str], str],
    previous: list[str] | None = None,
) -> None:
    """Reject structural errors of a developer report against its brief's commit plan, and
    dod_coverage claims that its commit_map and the plan's ``covers`` contradict.

    ``created`` is the ordered list of commits the dispatch created (after ``snapshot_commit``, or
    after the rebase target for a rebase); ``resolve`` turns a reported SHA into its full form.
    ``previous`` lists the previous-candidate commits a developer-retry brief with
    ``rebase_target_commit`` rebased: those after the old base up to ``snapshot_commit``.
    """
    retry = is_developer_retry(dispatch)
    commit_map = _required_commit_map(report, retry)
    plan_ids = _plan_ids(dispatch)
    if retry and rebase_target(dispatch) is not None:
        _check_rebase_mapping(commit_map, created, previous or [], plan_ids, resolve)
        return
    pairs = _commit_map_pairs(commit_map)
    resolved = [
        (_resolve_reported(resolve, sha, "commit_map[].commit_sha"), plan_id)
        for sha, plan_id in pairs
    ]
    if retry:
        _check_retry_mapping(resolved, created, plan_ids)
        return
    _check_relation(resolved, created, plan_ids)
    relation = _relation(resolved, plan_ids)
    one_to_one = not any(relation.values())
    if not one_to_one and "dod_coverage" not in report:
        raise CoordinatorError(
            f"commit_map diverges from the commit plan ({_describe(relation)}) but the report "
            "has no dod_coverage",
            remedy=COVERAGE_REMEDY,
        )
    if "dod_coverage" in report:
        _check_coverage(
            report["dod_coverage"],
            len(dispatch.get("definition_of_done", [])),
            created,
            resolve,
            resolved,
            _plan_covers(dispatch),
        )
    if one_to_one:
        if "divergence_justification" in report:
            raise CoordinatorError(
                "divergence_justification is only for a commit_map that diverges from the commit plan",
                remedy="drop divergence_justification: every created commit closes exactly one "
                "plan entry and every entry is closed once",
            )
    elif not _non_empty(report.get("divergence_justification")):
        raise CoordinatorError(
            f"commit_map diverges from the commit plan ({_describe(relation)}) without a "
            "divergence_justification",
            remedy="set divergence_justification to what was merged, split or added and why",
        )


def _report_pairs(
    report: JsonObject, resolve: Callable[[str], str]
) -> list[tuple[str, str]]:
    commit_map = report.get("commit_map")
    if not isinstance(commit_map, list):
        return []
    return [(resolve(sha), plan_id) for sha, plan_id in _commit_map_pairs(commit_map)]


def divergence(
    report: JsonObject, dispatch: JsonObject, resolve: Callable[[str], str]
) -> JsonObject | None:
    """The divergence record of an initial or rebase developer report, or None when its
    commit_map is one-to-one with the plan (or the dispatch has no commit-plan relation)."""
    if not _relation_applies(dispatch):
        return None
    relation = _relation(_report_pairs(report, resolve), _plan_ids(dispatch))
    if not any(relation.values()):
        return None
    return {
        "developer_dispatch_id": dispatch["dispatch_id"],
        "justification": report.get("divergence_justification"),
        **relation,
    }


def coverage(
    report: JsonObject, dispatch: JsonObject, resolve: Callable[[str], str]
) -> tuple[list[JsonObject] | None, str | None]:
    """The definition-of-done coverage of a developer report and where it comes from.

    A report's own ``dod_coverage`` wins (``report``); a one-to-one report without it gets coverage
    derived from the plan entries' ``covers`` (``derived``). Not applicable: ``(None, None)``.
    """
    if not _relation_applies(dispatch):
        return None, None
    reported = report.get("dod_coverage")
    if isinstance(reported, list):
        return reported, "report"
    covers = _plan_covers(dispatch)
    pairs = _report_pairs(report, resolve)
    return [
        {
            "dod_item": item,
            "commits": _covering_commits(item, pairs, covers),
        }
        for item in range(1, len(dispatch.get("definition_of_done", [])) + 1)
    ], "derived"


def not_covered(report: JsonObject) -> list[JsonObject]:
    """The definition-of-done items a report declares not covered, with their reasons.

    Any such item keeps the report from being clean: it never auto-accepts, plain ``accept`` is
    refused, and it can be accepted only by ``override-warning`` with a note other than ``none``,
    or returned with ``retry``.
    """
    coverage = report.get("dod_coverage")
    if not isinstance(coverage, list):
        return []
    return [
        {"dod_item": record["dod_item"], "reason": record["not_covered"]}
        for record in coverage
        if isinstance(record, dict) and "not_covered" in record
    ]
