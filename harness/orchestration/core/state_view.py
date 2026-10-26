"""The view of the runtime store (ADR 0020, sections 1 to 3).

After the cutover the operational state lives in ``state/runtime.db``, which only the Rust runtime
opens. The coordinator reads it through ``runtime_client`` (``state.read``) and renders the same
text as before, because the runtime answers every record in the form the legacy ledger file had.

A *view* stands in for the file ledger while one command reads. It names a virtual records root,
``<state>/runtime.db``, and the code that builds a record path under the records root keeps doing
so: ``_read_object``, ``_glob`` and ``_exists`` answer a path below that root from the view and
never touch the disk. A read that was not routed through the view fails loudly: a path below
``runtime.db`` is not a directory, so the file system refuses it, and no read silently falls back
to the file ledger.

A command that changes state does so through a *store unit* on the view. The ledger primitives
stage their writes on it: the view answers the command's own reads from what it staged, nothing
reaches the runtime until the command ends cleanly, and then one ``state.write`` per batch sends
every staged mutation with the revision the command read (ADR 0020, section 3). A command that
raises sends nothing.

This module is a leaf. It imports nothing of the coordinator, so ``core.utils`` and the ledger may
depend on it; the runtime client is passed in as ``fetch`` and ``commit``.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from harness.json_types import JsonObject

VIRTUAL_ROOT_NAME = "runtime.db"
# The record directories of a generation; each is a collection of ``state.read``.
RECORD_DIRECTORIES = frozenset(
    {
        "batches",
        "plans",
        "dispatches",
        "dispatch-status",
        "risk-assessments",
        "context-packages",
        "checkpoints",
        "reports",
        "qa-lane",
        "qa-artifacts",
        "audit",
    }
)

# The collections of records a command may read one by one. After ``BULK_AFTER`` point reads in
# one of them the view asks for the whole collection once: a call costs milliseconds, and a status
# of hundreds of dispatches would otherwise make hundreds of calls.
BULK_COLLECTIONS = frozenset(
    {
        "batches",
        "plans",
        "dispatches",
        "dispatch-status",
        "risk-assessments",
        "context-packages",
        "checkpoints",
    }
)
BULK_AFTER = 3


class AnswerTooLarge(Exception):
    """The runtime refused an answer that would not fit one frame."""


# One ``state.read`` request: the collections and the paths it asks for, and the revision of the
# store the view already saw. The answer holds the records by path, the revision of the store and
# the revision of every row that has one.
type Fetch = Callable[
    [Sequence[str], Sequence[str], int | None],
    tuple[dict[str, JsonObject], int | None, dict[str, int]],
]

# One ``state.write`` request: the batch of the mutations, the revision of its batch row that the
# command read (0 for a batch that the mutations create) and the mutations. The answer is the
# revision of every row the request created or changed.
type Commit = Callable[[str | None, int | None, list[JsonObject]], dict[str, int]]


class StateView:
    """The records of the store as one command sees them, fetched when first needed."""

    def __init__(self, root: Path, fetch: Fetch) -> None:
        self.root = root
        self.virtual_root = root / VIRTUAL_ROOT_NAME
        self._fetch = fetch
        self._records: dict[str, JsonObject] = {}
        self._collections: set[str] = set()
        self._asked: set[str] = set()
        self._point_reads: dict[str, int] = {}
        self._too_large: set[str] = set()
        self.revision: int | None = None
        # The revision of every row the view has read: the value a write of the row expects.
        self.revisions: dict[str, int] = {}
        # What the open store unit staged: a record, or ``None`` for a row it deleted; the paths
        # of staged text artifacts, which the view knows exist but does not read back.
        self._staged: dict[str, JsonObject | None] = {}
        self._staged_text: set[str] = set()
        self.unit: StoreUnit | None = None

    def relative(self, path: Path | str) -> str | None:
        """The ``/`` path of ``path`` below the virtual root, or ``None`` when it is not below it."""
        try:
            parts = Path(path).relative_to(self.virtual_root).parts
        except ValueError:
            return None
        return "/".join(parts)

    def refresh(self) -> None:
        """Forget everything the view read, so that the next read asks the runtime again.

        A command that waits or runs for long takes a fresh snapshot for each block of its own
        (``_ledger_lock``, ``read_state``), as the file ledger did at every lock: a view that
        kept its first answer would show a poll the state of the command's start. Call it only
        while no store unit is open."""
        self._records.clear()
        self._collections.clear()
        self._asked.clear()
        self._point_reads.clear()
        self.revisions.clear()
        self.revision = None

    def _load(self, collections: Sequence[str], paths: Sequence[str]) -> None:
        records, revision, revisions = self._fetch(collections, paths, self.revision)
        if revision is not None:
            self.revision = revision
        self._records.update(records)
        self.revisions.update(revisions)

    def _collection(self, name: str) -> None:
        if name not in self._collections:
            self._load([name], [])
            self._collections.add(name)

    def read(self, relative: str) -> JsonObject | None:
        """The record at ``relative``, or ``None`` when the store holds none."""
        if relative in self._staged:
            return self._staged[relative]
        head = relative.split("/", 1)[0]
        if head not in self._collections and relative not in self._asked:
            count = self._point_reads.get(head, 0) + 1
            self._point_reads[head] = count
            if (
                head in BULK_COLLECTIONS
                and head not in self._too_large
                and count > BULK_AFTER
            ):
                try:
                    self._collection(head)
                except AnswerTooLarge:
                    self._too_large.add(head)
            if head not in self._collections:
                self._load([], [relative])
                self._asked.add(relative)
        return self._records.get(relative)

    def _paths(self) -> set[str]:
        """The paths of the records the command can see: the fetched ones with its own staging."""
        paths = {path for path in self._records if path not in self._staged}
        paths.update(path for path, record in self._staged.items() if record is not None)
        paths.update(self._staged_text)
        return paths

    def names(self, relative_dir: str) -> list[str]:
        """The names of the records that are direct children of ``relative_dir``."""
        head = relative_dir.split("/", 1)[0]
        self._collection(head)
        prefix = f"{relative_dir}/" if relative_dir else ""
        return sorted(
            path[len(prefix) :]
            for path in self._paths()
            if path.startswith(prefix) and "/" not in path[len(prefix) :]
        )

    def is_dir(self, relative: str) -> bool:
        if relative == "":
            return True
        head = relative.split("/", 1)[0]
        if "/" not in relative:
            return head in RECORD_DIRECTORIES
        self._collection(head)
        return any(path.startswith(f"{relative}/") for path in self._paths())

    def is_file(self, relative: str) -> bool:
        if relative in self._staged_text:
            return True
        return relative != "" and self.read(relative) is not None

    def begin_unit(self, commit: Commit) -> StoreUnit:
        """Open the store unit of the command that runs on this view."""
        self.unit = StoreUnit(self, commit)
        return self.unit

    def _settle(self) -> None:
        """Take the staged writes into the view after the runtime committed them."""
        for path, record in self._staged.items():
            if record is None:
                self._records.pop(path, None)
            else:
                self._records[path] = record
            self._asked.add(path)
        self._staged.clear()
        self._staged_text.clear()
        # The commit moved the store on; the next read asks without a stale revision.
        self.revision = None


class StoreUnit:
    """The writes of one command, staged on its view until the command ends cleanly."""

    def __init__(self, view: StateView, commit: Commit) -> None:
        self.view = view
        self._commit = commit
        # Each mutation with the batch it belongs to (``None`` for a row of no batch).
        self._mutations: list[tuple[str | None, JsonObject]] = []
        self._created: set[str] = set()
        self._touched: set[str] = set()

    def _batch_of(self, relative: str, record: JsonObject | None) -> str | None:
        directory, _, name = relative.partition("/")
        stem = name.removesuffix(".json")
        if directory in {"batches", "plans"}:
            return stem
        if directory == "dispatches":
            batch = record.get("batch_id") if record else None
            return batch if isinstance(batch, str) else None
        if directory == "dispatch-status":
            dispatch = self.view.read(f"dispatches/{stem}.json")
            batch = dispatch.get("batch_id") if dispatch else None
            return batch if isinstance(batch, str) else None
        return None

    def create(
        self, relative: str, record: JsonObject, content: str, *, artifact: bool = False
    ) -> None:
        """Stage a record that does not exist yet (``artifact``: the immutable-artifact audit)."""
        mutation: JsonObject = {
            "op": "create_artifact" if artifact else "create",
            "path": relative,
            "content": content,
        }
        self._mutations.append((self._batch_of(relative, record), mutation))
        self._created.add(relative)
        self.view._staged[relative] = record

    def create_text(self, relative: str, text: str) -> None:
        """Stage a text artifact; the view knows it exists but never reads it back."""
        mutation: JsonObject = {
            "op": "create_artifact",
            "path": relative,
            "content": text,
        }
        self._mutations.append((None, mutation))
        self.view._staged_text.add(relative)

    def _expect(self, mutation: JsonObject, relative: str) -> None:
        """The first touch of a row that exists carries the revision the command read."""
        if relative not in self._created and relative not in self._touched:
            revision = self.view.revisions.get(relative)
            if revision is not None:
                mutation["expected_revision"] = revision
        self._touched.add(relative)

    def replace(
        self,
        relative: str,
        record: JsonObject,
        content: str,
        *,
        decision: JsonObject | None = None,
    ) -> None:
        """Stage the new content of a record that exists."""
        mutation: JsonObject = {"op": "replace", "path": relative, "content": content}
        if decision is not None:
            mutation["decision"] = decision
        self._expect(mutation, relative)
        self._mutations.append((self._batch_of(relative, record), mutation))
        self.view._staged[relative] = record

    def delete(self, relative: str, *, reason: str) -> None:
        """Stage the deletion of a record. A record that this command created never existed."""
        if relative in self._created:
            self._mutations = [
                (batch, mutation)
                for batch, mutation in self._mutations
                if mutation["path"] != relative
            ]
            self._created.discard(relative)
            self.view._staged.pop(relative, None)
            return
        mutation: JsonObject = {"op": "delete", "path": relative, "reason": reason}
        self._expect(mutation, relative)
        self._mutations.append((self._batch_of(relative, None), mutation))
        self.view._staged[relative] = None

    def abort(self) -> None:
        """Drop the staged writes: a command that raised changed nothing."""
        self.view._staged.clear()
        self.view._staged_text.clear()
        self.view.unit = None
        self._mutations = []

    def _segments(self) -> list[tuple[str | None, list[JsonObject]]]:
        """The mutations cut where the batch changes: the runtime writes one batch per request."""
        segments: list[tuple[str | None, list[JsonObject]]] = []
        for batch, mutation in self._mutations:
            if not segments or (
                batch is not None and segments[-1][0] not in (None, batch)
            ):
                segments.append((batch, [mutation]))
                continue
            if segments[-1][0] is None:
                segments[-1] = (batch, segments[-1][1])
            segments[-1][1].append(mutation)
        return segments

    def _guard(self, batch: str | None, mutations: list[JsonObject]) -> int | None:
        """The revision of the batch row that the command read: 0 where the request creates it."""
        if batch is None:
            return None
        path = f"batches/{batch}.json"
        if any(m["op"] == "create" and m["path"] == path for m in mutations):
            return 0
        return self.view.revisions.get(path)

    def _carry_revisions(self, mutations: list[JsonObject]) -> None:
        """The first touch of a row in a request carries the revision the view holds for it.

        ``_expect`` marks a row as touched once for the whole unit, so a row that two requests of
        one flush write (the batches go A, B, A) reaches the second request without a revision,
        and the runtime refuses it. By then the first request has returned the new revision."""
        seen: set[str] = set()
        for mutation in mutations:
            path = str(mutation["path"])
            if (
                path not in seen
                and mutation["op"] in {"replace", "delete"}
                and "expected_revision" not in mutation
            ):
                revision = self.view.revisions.get(path)
                if revision is not None:
                    mutation["expected_revision"] = revision
            seen.add(path)

    def persist(self) -> None:
        """Send the staged mutations now, one ``state.write`` for each batch, and keep the unit open.

        A block that writes evidence and then raises on purpose (a refusal that records its
        attempt) calls this before the raise: the file ledger kept such a write, and a unit that
        aborts would drop it."""
        for batch, mutations in self._segments():
            self._carry_revisions(mutations)
            revisions = self._commit(batch, self._guard(batch, mutations), mutations)
            for mutation in mutations:
                if mutation["op"] == "delete":
                    self.view.revisions.pop(str(mutation["path"]), None)
            self.view.revisions.update(revisions)
        self.view._settle()
        self._mutations = []
        self._created.clear()
        self._touched.clear()

    def flush(self) -> None:
        """Send the staged mutations, one ``state.write`` for each batch, and settle the view."""
        try:
            self.persist()
        finally:
            self.view.unit = None
            self._mutations = []


_VIEW: ContextVar[StateView | None] = ContextVar("runtime_store_view", default=None)


def current_view() -> StateView | None:
    """The view bound to the running command, or ``None`` when the file ledger answers."""
    return _VIEW.get()


def current_unit() -> StoreUnit | None:
    """The store unit of the running command, or ``None`` outside a command that may write."""
    view = _VIEW.get()
    return None if view is None else view.unit


def view_for(path: Path | str) -> tuple[StateView, str] | None:
    """The bound view and the path relative to its virtual root, when ``path`` is below it."""
    view = _VIEW.get()
    if view is None:
        return None
    relative = view.relative(path)
    return None if relative is None else (view, relative)


@contextmanager
def bind(view: StateView) -> Iterator[StateView]:
    """Make ``view`` answer the reads of the code that runs in the block."""
    token = _VIEW.set(view)
    try:
        yield view
    finally:
        _VIEW.reset(token)


def glob(directory: Path, pattern: str) -> list[Path]:
    """``sorted(directory.glob(pattern))`` for a flat pattern, from the view below its root."""
    found = view_for(directory)
    if found is None:
        return sorted(directory.glob(pattern))
    view, relative = found
    return [
        directory / name
        for name in view.names(relative)
        if fnmatch.fnmatchcase(name, pattern)
    ]


def exists(path: Path) -> bool:
    found = view_for(path)
    if found is None:
        return path.exists()
    view, relative = found
    return view.is_dir(relative) or view.is_file(relative)


def is_file(path: Path) -> bool:
    found = view_for(path)
    return path.is_file() if found is None else found[0].is_file(found[1])


def is_dir(path: Path) -> bool:
    found = view_for(path)
    return path.is_dir() if found is None else found[0].is_dir(found[1])
