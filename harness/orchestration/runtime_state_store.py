"""Durable runtime state store (``runtimeState.v1``), one record per batch.

The store keeps operational progress beside the lifecycle ledger, never inside it: the ledger
stays the owner of lifecycle and approvals, and nothing here writes a ledger record.  Not yet
wired into dispatch, intent or reconciliation (#684, #685).
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO, cast

from harness.configuration_schema import resolve_ref, value_problems
from harness.gate_runner.gate_runner import sanitise
from harness.json_types import JsonObject
from harness.storage import GIT_TIMEOUT_SECONDS, storage_root

from .core.constants import STATE_REL
from .core.runtime_state_contract import (
    RUNTIME_STATE_CONTRACT,
    RUNTIME_STATE_DIR,
    transition_problem,
)
from .core.system_fields import enum_problem, load_registry
from .core.utils import CoordinatorError, _canonical, _safe_id

SCHEMA_PATH = Path(__file__).with_name("runtime_state.v1.json")
SYSTEM_FIELD = "x-system-field"
DETAIL_LIMIT = 240
# An absolute POSIX or Windows path, not the tail of a relative one.
_ABSOLUTE_PATH = re.compile(r"(?<![\w.~-])(?:[A-Za-z]:[\\/]|[\\/])[^\s'\"]*")
_SETTLED_PHASES = frozenset({"ledger-committed", "committed"})
LOCK_TIMEOUT_SECONDS = 2.0
LOCK_RETRY_SECONDS = 0.05
_LOCK_BUSY = frozenset({errno.EACCES, errno.EAGAIN, errno.EDEADLK})
_DIRECTORY_FSYNC_UNSUPPORTED = frozenset(
    {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}
)
_STEP_REMEDIES = {
    "storage-unavailable": "make the runtime state directory readable and writable, then "
    "retry; nothing new was written",
    "state-persist-failed": "free space or repair the filesystem, then retry the write "
    "against the last durable revision",
}
# The platform flag the durability step reads; tests replace it to cover the Windows path.
WINDOWS = os.name == "nt"
# What a durable write cannot promise; a skipped step is reported, never hidden.
DURABILITY_LIMITS = MappingProxyType(
    {
        "windows": "no directory fsync: the replace is atomic, but write() reports "
        "directory_fsync 'unsupported'",
        "posix-filesystem": "a filesystem refusing directory fsync with EINVAL, ENOTSUP or "
        "EOPNOTSUPP makes write() report directory_fsync 'unsupported'",
        "macos": "os.fsync does not issue F_FULLFSYNC, so the drive cache may still hold "
        "the record",
        "network-filesystem": "the lock is an advisory flock or msvcrt lock; a network "
        "filesystem without reliable locking is not supported",
    }
)


@cache
def _schema() -> JsonObject:
    value: JsonObject = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return value


def _without_system_enums(node: object) -> object:
    """The schema with each registry-bound ``enum`` dropped: those values get the registry's
    exact-spelling diagnostic instead of the generic schema one."""
    if isinstance(node, dict):
        return {
            key: _without_system_enums(child)
            for key, child in node.items()
            if not (key == "enum" and SYSTEM_FIELD in node)
        }
    if isinstance(node, list):
        return [_without_system_enums(child) for child in node]
    return node


@cache
def _structure() -> JsonObject:
    return cast(JsonObject, _without_system_enums(_schema()))


@cache
def field_values(field_id: str) -> tuple[str, ...]:
    """The exact values of a systemFields.v1 field."""
    return tuple(load_registry().fields[field_id].values)


def sanitize_detail(text: str) -> str:
    """Secrets masked, absolute paths dropped and length bounded; a fixed point of itself."""
    cleaned = _ABSOLUTE_PATH.sub("<path>", sanitise(text)).strip()
    return cleaned[:DETAIL_LIMIT].strip()


def compatibility_problem(record: object) -> str | None:
    """A record of another or no contract is never read with defaults filled in."""
    if not isinstance(record, dict):
        return "runtime state record must be a JSON object"
    if record.get("contract") != RUNTIME_STATE_CONTRACT:
        return (
            f"runtime state contract must be {RUNTIME_STATE_CONTRACT!r}, "
            f"got {record.get('contract')!r}"
        )
    return None


def _enum_problems(value: object, node: object, path: str) -> list[str]:
    if not isinstance(node, dict):
        return []
    if "$ref" in node:
        node = resolve_ref(_schema(), node["$ref"])
    if SYSTEM_FIELD in node:
        problem = enum_problem(path, value, field_values(node[SYSTEM_FIELD]))
        return [problem] if problem else []
    if not isinstance(value, dict):
        return []
    problems: list[str] = []
    for key, child in node.get("properties", {}).items():
        if key in value:
            problems.extend(_enum_problems(value[key], child, f"{path}.{key}"))
    return problems


def _shape_problems(record: object) -> list[str]:
    """Structure, closed enums and the contract; the other checks index what these ensure."""
    if (problem := compatibility_problem(record)) is not None:
        return [problem]
    problems = value_problems(record, _structure(), _structure())
    problems += _enum_problems(record, _schema(), "record")
    return problems


def record_problems(record: object) -> list[str]:
    """Every reason one record is invalid on its own, without its predecessor; the store
    checks a record read from disk by the same rules a write is checked by."""
    if problems := _shape_problems(record):
        return problems
    return _content_problems(cast(JsonObject, record))


def _timestamp_problem(value: object, label: str) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return f"{label} must be an ISO-8601 timestamp"
    return None if parsed.tzinfo else f"{label} must include a timezone"


def _operation_problems(
    operation: JsonObject | None, previous: JsonObject | None
) -> list[str]:
    if operation is None:
        if previous is not None and previous["phase"] != "committed":
            return ["an operation that is not committed cannot be dropped"]
        return []
    problems: list[str] = []
    if previous is not None and operation["operation_id"] == previous["operation_id"]:
        for key in (
            "kind",
            "idempotency_key",
            "expected_before_sha256",
            "intended_after_sha256",
        ):
            if operation[key] != previous[key]:
                problems.append(f"operation {key} is immutable")
        for field_id, after, before in (
            ("runtime.operation_phase", operation["phase"], previous["phase"]),
            (
                "runtime.effect_state",
                operation["effect"]["state"],
                previous["effect"]["state"],
            ),
        ):
            if (problem := transition_problem(field_id, before, after)) is not None:
                problems.append(problem)
        return problems
    if previous is not None and previous["phase"] != "committed":
        problems.append(
            "a new operation cannot start before the previous one is committed"
        )
    if (
        operation["phase"] != "prepared"
        or operation["effect"]["state"] != "not-started"
    ):
        problems.append("a new operation must start as prepared and not-started")
    return problems


def _settled_problems(record: JsonObject) -> list[str]:
    """Legacy metadata or the ``unknown`` sentinel never stands in for a confirmed effect."""
    operation = record["operation"]
    if operation is None or operation["phase"] not in _SETTLED_PHASES:
        return []
    problems: list[str] = []
    if operation["effect"]["state"] != "confirmed":
        problems.append(f"phase {operation['phase']!r} requires a confirmed effect")
    if not record["ledger"]["generation"] or not record["ledger"]["record_hashes"]:
        problems.append(
            f"phase {operation['phase']!r} requires the ledger generation and record hashes"
        )
    return problems


def _content_problems(record: JsonObject) -> list[str]:
    """Timestamps, a sanitized failure and settled phases of a well-shaped record."""
    problems: list[str] = []
    for label, value in (
        ("updated_at", record["updated_at"]),
        (
            "operation.effect.observed_at",
            (record["operation"] or {}).get("effect", {}).get("observed_at"),
        ),
        (
            "last_confirmed_boundary.confirmed_at",
            (record["last_confirmed_boundary"] or {}).get("confirmed_at"),
        ),
    ):
        if (problem := _timestamp_problem(value, label)) is not None:
            problems.append(problem)
    failure = record["failure"]
    if (
        failure is not None
        and sanitize_detail(failure["message"]) != failure["message"]
    ):
        problems.append(
            "failure.message must be sanitized: no secrets, absolute paths or excess length"
        )
    return problems + _settled_problems(record)


def validate_record(record: object, previous: JsonObject | None) -> list[str]:
    """Every reason ``record`` may not follow ``previous`` (``None`` when creating); pure."""
    if problems := _shape_problems(record):
        return problems
    valid = cast(JsonObject, record)
    return _content_problems(valid) + transition_problems(valid, previous)


def transition_problems(record: JsonObject, previous: JsonObject | None) -> list[str]:
    """Every reason a record that is valid on its own may not follow ``previous``."""
    problems: list[str] = []
    if previous is None:
        if record["revision"] != 1:
            problems.append("a new runtime state record starts at revision 1")
        if record["state"] != "ready":
            problems.append("a new runtime state record starts in state 'ready'")
        return problems + _operation_problems(record["operation"], None)
    if record["identity"] != previous["identity"]:
        problems.append("identity is immutable")
    if record["revision"] != previous["revision"] + 1:
        problems.append(
            f"revision must be {previous['revision'] + 1}, got {record['revision']}"
        )
    if previous["state"] == "completed":
        problems.append("a completed runtime state record is immutable")
    elif (
        problem := transition_problem(
            "runtime.state", previous["state"], record["state"]
        )
    ) is not None:
        problems.append(problem)
    return problems + _operation_problems(record["operation"], previous["operation"])


class RuntimeStateError(CoordinatorError):
    """A runtime state request that stopped without persisting new progress."""

    progress_saved = False

    def __init__(
        self,
        code: str,
        step: str,
        detail: str,
        *,
        remedy: str,
        durable_revision: int | None = None,
        path: str | None = None,
        error_name: str | None = None,
        durability: str = "unchanged",
    ) -> None:
        if (
            problem := enum_problem(
                "runtime failure code", code, field_values("runtime.failure_code")
            )
        ) is not None:
            raise ValueError(problem)
        detail = sanitize_detail(detail)
        known = "unknown" if durable_revision is None else str(durable_revision)
        outcome = (
            "new progress is not confirmed durable"
            if durability == "unconfirmed"
            else "new progress was not persisted"
        )
        super().__init__(
            f"runtime state {step} stopped with {code}: {detail}; {outcome}, "
            f"the last durable revision is {known}",
            remedy=remedy,
        )
        self.code = code
        self.step = step
        self.durable_revision = durable_revision
        self.diagnostics: JsonObject = {
            "code": code,
            "step": step,
            "errno": error_name,
            "path": path,
            "detail": detail,
            "durability": durability,
        }


def _git_common_dir(checkout: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        return None
    common = Path(value)
    return (common if common.is_absolute() else checkout / common).resolve()


@dataclass(frozen=True)
class RuntimeStateLocation:
    """Where one state root's records live in the shared store, and the identity they carry.

    Every checkout of a repository resolves to its main checkout's storage root, so linked
    worktrees share one store; a non-default state root gets its own namespace there, named by
    the hash of its full path."""

    storage: Path
    repo_root: Path
    git_common_dir: Path
    state_root: Path
    namespace: str
    directory: Path

    @property
    def base(self) -> Path:
        return self.storage / RUNTIME_STATE_DIR

    def identity(self, batch_id: str) -> JsonObject:
        return {
            "repo_root": str(self.repo_root),
            "git_common_dir": str(self.git_common_dir),
            "state_root": str(self.state_root),
            "namespace": self.namespace,
            "batch_id": batch_id,
        }

    def relative(self, path: Path) -> str:
        """``path`` relative to the storage root, as diagnostics name it."""
        try:
            return path.relative_to(self.storage).as_posix()
        except ValueError:
            return path.name


def resolve_location(
    repo: Path, state_root: Path | None = None
) -> RuntimeStateLocation:
    """Bind ``repo`` and ``state_root`` (``None`` for the default) to the canonical store."""
    unavailable = RuntimeStateError(
        "storage-unavailable",
        "resolve-root",
        "the main checkout's shared storage cannot be located",
        remedy="run from a Git checkout of the repository whose main checkout exists",
    )
    try:
        storage = storage_root(repo, require_main_checkout=True)
    except ValueError:
        raise unavailable from None
    main = storage.parent
    common = _git_common_dir(main)
    if common is None:
        raise unavailable
    default = (main / STATE_REL).resolve()
    resolved = default if state_root is None else state_root.expanduser().resolve()
    base = storage / RUNTIME_STATE_DIR
    if resolved == default:
        namespace, directory = "default", base
    else:
        digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
        namespace, directory = f"alt-{digest}", base / "alternate" / digest
    return RuntimeStateLocation(storage, main, common, resolved, namespace, directory)


class RuntimeStateStore:
    """Read and write ``runtimeState.v1`` records of one state root."""

    def __init__(self, repo: Path, state_root: Path | None = None) -> None:
        self.location = resolve_location(repo, state_root)

    def _error(
        self,
        code: str,
        step: str,
        detail: str,
        remedy: str,
        durable_revision: int | None = None,
    ) -> RuntimeStateError:
        return RuntimeStateError(
            code, step, detail, remedy=remedy, durable_revision=durable_revision
        )

    def path(self, batch_id: str) -> Path:
        try:
            _safe_id(batch_id, "batch_id")
        except CoordinatorError:
            batch_id = ""
        if not batch_id.startswith("batch-"):
            raise self._error(
                "integrity-failure",
                "identity",
                "the batch ID is not a coordinator batch ID",
                "pass the coordinator-generated batch-... ID",
            )
        return self.location.directory / f"{batch_id}.json"

    def _refuse_links(self, *paths: Path) -> None:
        for path in paths:
            if path.is_symlink():
                raise self._error(
                    "integrity-failure",
                    "prepare",
                    f"{self.location.relative(path)} is a symbolic link",
                    "replace the link with a plain directory or file of the store",
                )

    def _prepare(self) -> None:
        """Create the store; its first directory ignores itself, so no checkout turns dirty
        even without a root ``.harness/`` rule."""
        base, directory = self.location.base, self.location.directory
        with self._storage_step("prepare", "storage-unavailable", directory):
            self._refuse_links(base, base / "alternate", directory)
            base.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(
                    base / ".gitignore", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644
                )
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(b"*\n")
            directory.mkdir(parents=True, exist_ok=True)
            self._refuse_links(base, base / "alternate", directory)

    def read(self, batch_id: str) -> JsonObject | None:
        """The current record, ``None`` when none was written; a record that is unreadable or
        bound to another identity stops instead of being repaired or defaulted."""
        path = self.path(batch_id)
        with self._storage_step("read", "storage-unavailable", path):
            self._refuse_links(path)
            try:
                raw = path.read_bytes()
            except FileNotFoundError:
                return None
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise self._error(
                "integrity-failure",
                "read",
                f"{self.location.relative(path)} is not valid JSON",
                "restore the record from a backup or remove it after review",
            ) from None
        if (problem := compatibility_problem(record)) is not None:
            raise self._error(
                "runtime-compatibility",
                "read",
                problem,
                f"read the record with a Harness that supports its contract; this one "
                f"reads only {RUNTIME_STATE_CONTRACT!r}",
            )
        # A record placed beside write() meets its rules too: a settled phase without a
        # confirmed effect is legacy metadata, never a success.
        if problems := record_problems(record):
            raise self._error(
                "integrity-failure",
                "read",
                "; ".join(problems),
                "restore the record from a backup or remove it after review",
            )
        if record["identity"] != self.location.identity(batch_id):
            raise self._error(
                "integrity-failure",
                "identity",
                f"{self.location.relative(path)} belongs to another repository, state root "
                "or batch",
                "open the store with the repository and state directory that wrote it",
            )
        return cast(JsonObject, record)

    @contextmanager
    def _locked(self, batch_id: str) -> Iterator[None]:
        """Hold the batch's operating-system lock, waiting at most ``LOCK_TIMEOUT_SECONDS``.

        The system drops the lock when its holder dies, so no stale lock is ever left behind."""
        path = self.location.directory / f"{batch_id}.lock"
        with self._storage_step("lock", "storage-unavailable", path):
            self._refuse_links(path)
            handle = open(path, "a+b", opener=_owner_only)
        with handle:
            with self._storage_step("lock", "storage-unavailable", path):
                deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
                while True:
                    try:
                        _guard_byte(handle, acquire=True)
                        break
                    except OSError as exc:
                        if exc.errno not in _LOCK_BUSY:
                            raise
                        if time.monotonic() >= deadline:
                            raise self._error(
                                "concurrency-conflict",
                                "lock",
                                f"another writer holds {self.location.relative(path)} "
                                f"longer than {LOCK_TIMEOUT_SECONDS} seconds",
                                "repeat the write once the other writer finishes",
                            ) from None
                        time.sleep(LOCK_RETRY_SECONDS)
            try:
                yield
            finally:
                with contextlib.suppress(OSError):
                    _guard_byte(handle, acquire=False)

    def write(self, record: JsonObject, *, expected_revision: int) -> WriteResult:
        """Persist ``record`` over ``expected_revision`` (0 when creating) or stop unchanged.

        A stale revision or a lock held past the timeout is a concurrency conflict; an invalid
        record or transition is refused before anything is written."""
        if (problem := compatibility_problem(record)) is not None:
            raise self._error(
                "runtime-compatibility",
                "validate",
                problem,
                f"write a {RUNTIME_STATE_CONTRACT!r} record",
            )
        if problems := record_problems(record):
            raise self._error(
                "integrity-failure",
                "validate",
                "; ".join(problems),
                "fix the record so it matches runtimeState.v1",
            )
        batch_id = record["identity"]["batch_id"]
        path = self.path(batch_id)
        if record["identity"] != self.location.identity(batch_id):
            raise self._error(
                "integrity-failure",
                "identity",
                "the record identity does not match this repository and state root",
                "take the identity from the store that writes the record",
            )
        self._prepare()
        with self._locked(batch_id):
            current = self.read(batch_id)
            revision = 0 if current is None else int(current["revision"])
            if revision != expected_revision:
                raise self._error(
                    "concurrency-conflict",
                    "revision",
                    f"expected revision {expected_revision}, the store holds {revision}",
                    "read the record again and retry against its current revision",
                    revision,
                )
            if problems := transition_problems(record, current):
                raise self._error(
                    "integrity-failure",
                    "validate",
                    "; ".join(problems),
                    "write a record that follows the current revision's transitions",
                    revision,
                )
            directory_fsync = self._replace(path, record, revision)
        return WriteResult(int(record["revision"]), directory_fsync)

    def _replace(self, path: Path, record: JsonObject, durable: int) -> str:
        """Write a flushed and fsynced temporary file, then replace the record atomically.

        A failure before the replace leaves revision ``durable`` byte for byte intact."""
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        failed = "state-persist-failed"
        try:
            with self._storage_step("write-temp", failed, temporary, durable):
                with open(temporary, "xb", opener=_owner_only) as stream:
                    stream.write(_canonical(record).encode("utf-8"))
                    stream.flush()
                    with self._storage_step("fsync-file", failed, temporary, durable):
                        os.fsync(stream.fileno())
            with self._storage_step("replace", failed, path, durable):
                _replace_atomically(temporary, path)
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink()
        # The replace already put the new revision on disk; only its durability is unknown.
        unconfirmed = (
            f"repair the filesystem, then read the record again: it already holds revision "
            f"{record['revision']}, so continue from the revision it holds instead of "
            f"retrying against revision {durable}"
        )
        with self._storage_step(
            "fsync-directory", failed, path.parent, durable, remedy=unconfirmed
        ):
            return _directory_durability(path.parent)

    @contextmanager
    def _storage_step(
        self,
        step: str,
        code: str,
        path: Path,
        durable_revision: int | None = None,
        *,
        remedy: str | None = None,
    ) -> Iterator[None]:
        """Turn an ``OSError`` of one storage step into a sanitized refusal; the raw error,
        its absolute paths and the record never reach the diagnostics.  ``remedy`` replaces
        the code's default one when the step knows better."""
        try:
            yield
        except OSError as exc:
            raise RuntimeStateError(
                code,
                step,
                exc.strerror or type(exc).__name__,
                remedy=remedy or _STEP_REMEDIES[code],
                durable_revision=durable_revision,
                path=self.location.relative(path),
                error_name=errno.errorcode.get(exc.errno) if exc.errno else None,
                durability="unconfirmed" if step == "fsync-directory" else "unchanged",
            ) from None


@dataclass(frozen=True)
class WriteResult:
    """The revision now on disk and whether its directory entry was flushed."""

    revision: int
    directory_fsync: str


def _guard_byte(handle: BinaryIO, *, acquire: bool) -> None:
    """Take without waiting, or drop, the operating-system lock on a file's first byte.

    The store's own seam, so it never reaches into ledger internals; it locks only the
    store's ``.lock`` files, which nothing else locks."""
    handle.seek(0)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(
            handle.fileno(), msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK, 1
        )
    else:
        import fcntl

        fcntl.flock(
            handle.fileno(),
            (fcntl.LOCK_EX | fcntl.LOCK_NB) if acquire else fcntl.LOCK_UN,
        )


def _replace_atomically(source: Path, target: Path) -> None:
    """``os.replace``, retrying the brief sharing conflicts a Windows reader causes."""
    for attempt in range(5):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 4:
                raise
            time.sleep(0.02 * (attempt + 1))


def _owner_only(path: str, flags: int) -> int:
    """Open a store file readable by its owner only; ``open`` closes it if wrapping fails."""
    return os.open(path, flags, 0o600)


def _fsync_directory(directory: Path) -> None:
    """Flush a directory entry to disk; the seam the fault tests replace."""
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _directory_durability(directory: Path) -> str:
    """``done``, or ``unsupported`` where ``DURABILITY_LIMITS`` says the step cannot run."""
    if WINDOWS:
        return "unsupported"
    try:
        _fsync_directory(directory)
    except OSError as exc:
        if exc.errno in _DIRECTORY_FSYNC_UNSUPPORTED:
            return "unsupported"
        raise
    return "done"
