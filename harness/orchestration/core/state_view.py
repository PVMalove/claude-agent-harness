"""The read view of the runtime store (ADR 0020, sections 1 and 3).

After the cutover the operational state lives in ``state/runtime.db``, which only the Rust runtime
opens. The coordinator reads it through ``runtime_client`` (``state.read``) and renders the same
text as before, because the runtime answers every record in the form the legacy ledger file had.

A *view* stands in for the file ledger while one command reads. It names a virtual records root,
``<state>/runtime.db``, and the code that builds a record path under the records root keeps doing
so: ``_read_object``, ``_glob`` and ``_exists`` answer a path below that root from the view and
never touch the disk. A read that was not routed through the view fails loudly: a path below
``runtime.db`` is not a directory, so the file system refuses it, and no read silently falls back
to the file ledger.

This module is a leaf. It imports nothing of the coordinator, so ``core.utils`` and the ledger may
depend on it; the runtime client is passed in as ``fetch``.
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

# One ``state.read`` request: the collections and the paths it asks for, and the revision of the
# store the view already saw. The answer holds the records by path, and the revision.
type Fetch = Callable[
    [Sequence[str], Sequence[str], int | None], tuple[dict[str, JsonObject], int | None]
]


class StateView:
    """The records of the store as one read sees them, fetched when first needed."""

    def __init__(self, root: Path, fetch: Fetch) -> None:
        self.root = root
        self.virtual_root = root / VIRTUAL_ROOT_NAME
        self._fetch = fetch
        self._records: dict[str, JsonObject] = {}
        self._collections: set[str] = set()
        self._asked: set[str] = set()
        self.revision: int | None = None

    def relative(self, path: Path | str) -> str | None:
        """The ``/`` path of ``path`` below the virtual root, or ``None`` when it is not below it."""
        try:
            parts = Path(path).relative_to(self.virtual_root).parts
        except ValueError:
            return None
        return "/".join(parts)

    def _load(self, collections: Sequence[str], paths: Sequence[str]) -> None:
        records, revision = self._fetch(collections, paths, self.revision)
        if revision is not None:
            self.revision = revision
        self._records.update(records)

    def _collection(self, name: str) -> None:
        if name not in self._collections:
            self._load([name], [])
            self._collections.add(name)

    def read(self, relative: str) -> JsonObject | None:
        """The record at ``relative``, or ``None`` when the store holds none."""
        head = relative.split("/", 1)[0]
        if head not in self._collections and relative not in self._asked:
            self._load([], [relative])
            self._asked.add(relative)
        return self._records.get(relative)

    def names(self, relative_dir: str) -> list[str]:
        """The names of the records that are direct children of ``relative_dir``."""
        head = relative_dir.split("/", 1)[0]
        self._collection(head)
        prefix = f"{relative_dir}/" if relative_dir else ""
        return sorted(
            path[len(prefix) :]
            for path in self._records
            if path.startswith(prefix) and "/" not in path[len(prefix) :]
        )

    def is_dir(self, relative: str) -> bool:
        if relative == "":
            return True
        head = relative.split("/", 1)[0]
        if "/" not in relative:
            return head in RECORD_DIRECTORIES
        self._collection(head)
        return any(path.startswith(f"{relative}/") for path in self._records)

    def is_file(self, relative: str) -> bool:
        return relative != "" and self.read(relative) is not None


_VIEW: ContextVar[StateView | None] = ContextVar("runtime_store_view", default=None)


def current_view() -> StateView | None:
    """The view bound to the running read, or ``None`` when the file ledger answers."""
    return _VIEW.get()


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
