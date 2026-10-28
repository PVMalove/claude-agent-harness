"""Server-owned catalog commands and cursor-based WebSocket output.

Requests use ``job.start(command_id, values?, confirmed?, confirmation?, plan_digest?)``,
``job.cancel(job_id, confirmed?)``, ``job.get/job.subscribe(job_id, cursor=0)`` and ``job.list``.
Replies carry a ``job`` snapshot, or the ``jobs`` summaries for ``job.list``. The cursor counts
output fragments, starting at zero. Each snapshot contains the next bounded batch and its new
cursor; ``done`` means execution and replay ended. Subscriptions own no execution task. A job keeps
a bounded output, and ``truncated`` marks the cut. Jobs live until server exit, except that only
the newest finished ones are kept.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import threading
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, NamedTuple, Protocol, cast

from .catalog import HARNESS_COMMANDS, MEMORY_COMMANDS, CatalogEntry, process_argv
from .coordinator_catalog import COORDINATOR_COMMANDS, CoordinatorCommand
from .runner import capturing_runner
from .snapshots import (
    MAX_ID_CHARS,
    PROTOCOL_VERSION,
    Rejected,
    encode,
    error_reply,
    parse_envelope,
)

_FRAGMENT_CHARS = 4096
# A character takes at most six bytes in an encoded reply (a control character as `\u00XX`),
# so a batch stays well below the server's 64 KiB message limit.
_BATCH_CHARS = 8192
_BATCH_EVENTS = 8
_OUTPUT_LIMIT_CHARS = 4 * 1024 * 1024
_KEPT_FINISHED = 32
# Each running job holds an event-loop worker thread; snapshot rebuilds need one as well.
_RUNNING_LIMIT = 4
# The TUI deletes only the cleanup set its preview approved (`--plan-digest`); a job demands the same.
_PLAN_COMMANDS = frozenset({"cleanup-apply", "cleanup-hard-apply"})
_PLAN_DIGEST = re.compile(r"[0-9a-f]{64}")

_FIELDS = {
    "job.start": {
        "id",
        "type",
        "command_id",
        "values",
        "confirmed",
        "confirmation",
        "plan_digest",
    },
    "job.get": {"id", "type", "job_id", "cursor"},
    "job.subscribe": {"id", "type", "job_id", "cursor"},
    "job.cancel": {"id", "type", "job_id", "confirmed"},
    "job.list": {"id", "type"},
}
_ERRORS = {
    "unknown_command": "Unknown catalog command.",
    "confirmation_required": "The command needs an explicit confirmation.",
    "plan_required": "Preview the cleanup plan and send its plan_digest.",
    "unavailable_command": "The command is unavailable in this project.",
    "start_failed": "The command could not be prepared.",
    "busy": "Too many running jobs.",
    "not_running": "The job is not running.",
    "unknown_job": "Unknown job.",
    "invalid_cursor": "Cursor is outside the job output.",
}

Phase = Literal["running", "completed", "failed", "cancelled"]


class JobExecutor(Protocol):
    """Emit on the event loop, and finish all output callbacks before returning an exit code."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        on_output: Callable[[str, str], None],
    ) -> Awaitable[int]: ...


async def _default_executor(
    argv: Sequence[str],
    *,
    cwd: Path,
    on_output: Callable[[str, str], None],
    cancel_event: threading.Event | None = None,
) -> int:
    loop = asyncio.get_running_loop()

    def emit(channel: str, text: str) -> None:
        loop.call_soon_threadsafe(on_output, channel, text)

    # Keep injected test runners and embedders compatible with the pre-cancellation signature.
    supports_cancellation = _runner_supports_cancellation(cancel_event)
    if supports_cancellation:
        assert cancel_event is not None
        result = await asyncio.to_thread(
            capturing_runner,
            argv,
            cwd=cwd,
            on_output=emit,
            cancel_event=cancel_event,
        )
    else:
        result = await asyncio.to_thread(capturing_runner, argv, cwd=cwd, on_output=emit)
    return result.returncode


def _runner_supports_cancellation(cancel_event: threading.Event | None) -> bool:
    """Allow tests and embedders that inject the pre-cancellation runner signature."""
    if cancel_event is None:
        return False
    parameters = inspect.signature(capturing_runner).parameters
    return "cancel_event" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


class JobRejected(Exception):
    """A job request refused after its envelope was read; the message defaults to the code's."""

    def __init__(
        self, code: str, message: str | None = None, *, job_id: str | None = None
    ) -> None:
        super().__init__(code)
        self.code, self.message = code, message or _ERRORS.get(code)
        self.job_id = job_id


class _Fragment(NamedTuple):
    channel: str
    text: str


@dataclass
class _Job:
    id: str
    command_id: str
    output: list[_Fragment] = field(default_factory=list)
    output_chars: int = 0
    truncated: bool = False
    phase: Phase = "running"
    returncode: int | None = None
    error: str = ""
    cancel_requested: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event)
    task: asyncio.Task[None] | None = None
    # Replaced on every change, so a reader waits on the event it saw before it read.
    next_change: asyncio.Event = field(default_factory=asyncio.Event)

    def append(self, channel: str, text: str) -> None:
        """Store output in bounded fragments until the limit, then only mark the cut."""
        for offset in range(0, len(text), _FRAGMENT_CHARS):
            part = text[offset : offset + _FRAGMENT_CHARS]
            if self.truncated or self.output_chars + len(part) > _OUTPUT_LIMIT_CHARS:
                self.truncated = True
                break
            self.output.append(_Fragment(channel, part))
            self.output_chars += len(part)
        if text:
            self.notify()

    def notify(self) -> None:
        previous, self.next_change = self.next_change, asyncio.Event()
        previous.set()

    def summary(self) -> dict[str, object]:
        return {
            "id": self.id,
            "command_id": self.command_id,
            "phase": self.phase,
            "cursor": len(self.output),
            "done": self.phase != "running",
            "truncated": self.truncated,
            "returncode": self.returncode,
            "error": self.error,
        }

    def snapshot(self, cursor: object) -> dict[str, object]:
        """The summary with the next bounded batch after `cursor`; done once replay caught up."""
        if type(cursor) is not int or not 0 <= cursor <= len(self.output):
            raise JobRejected("invalid_cursor")
        batch: list[dict[str, str]] = []
        size = 0
        for fragment in self.output[cursor : cursor + _BATCH_EVENTS]:
            if size + len(fragment.text) > _BATCH_CHARS:
                break
            batch.append(fragment._asdict())
            size += len(fragment.text)
        next_cursor = cursor + len(batch)
        return {
            **self.summary(),
            "output": batch,
            "cursor": next_cursor,
            "done": self.phase != "running" and next_cursor == len(self.output),
        }


def job_request_type(raw: str) -> str | None:
    """The type of a `job.*` request; malformed and other requests stay with the snapshot protocol."""
    try:
        _, _, kind = parse_envelope(raw)
    except Rejected:
        return None
    return kind if kind.startswith("job.") else None


class JobService:
    """One server's job admission, command execution, bounded output and independent readers.

    The mutation slot is deliberately owned by this server instance. ``ConsoleServer`` acquires
    the project's ``Discovery`` lock before exposing a ``JobService``, so two externally reachable
    services for one project cannot run concurrently.
    """

    def __init__(
        self, repo: Path, *, executor: JobExecutor = _default_executor
    ) -> None:
        self.repo = repo.resolve()
        self._executor = executor
        entries: tuple[CatalogEntry | CoordinatorCommand, ...] = (
            *HARNESS_COMMANDS,
            *MEMORY_COMMANDS,
            *COORDINATOR_COMMANDS,
        )
        self._catalog = {entry.key: entry for entry in entries}
        self._jobs: dict[str, _Job] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._active_mutation: _Job | None = None

    @property
    def active_count(self) -> int:
        return sum(job.phase == "running" for job in self._jobs.values())

    def _start(self, body: Mapping[str, object]) -> _Job:
        command_id = body.get("command_id")
        if not isinstance(command_id, str):
            raise JobRejected("bad_request")
        entry = self._catalog.get(command_id)
        if entry is None:
            raise JobRejected("unknown_command")
        values = body.get("values", {})
        if not isinstance(values, dict) or any(
            not isinstance(name, str) or not isinstance(value, str)
            for name, value in values.items()
        ):
            raise JobRejected("bad_request")
        filled = cast(dict[str, str], values)
        confirmed = body.get("confirmed", False)
        if not entry.accepts(filled) or not isinstance(confirmed, bool):
            raise JobRejected("bad_request")
        typed, dev_environment = (
            (entry.typed_confirmation, entry.dev_environment)
            if isinstance(entry, CatalogEntry)
            else (None, False)
        )
        if (entry.needs_confirmation and not confirmed) or (
            typed is not None and body.get("confirmation") != typed
        ):
            raise JobRejected("confirmation_required")
        digest = body.get("plan_digest")
        if command_id not in _PLAN_COMMANDS and "plan_digest" in body:
            raise JobRejected("bad_request")
        if command_id in _PLAN_COMMANDS and not (
            isinstance(digest, str) and _PLAN_DIGEST.fullmatch(digest)
        ):
            raise JobRejected("plan_required")
        try:
            available = entry.available(self.repo)
            argv = process_argv(
                entry.cli_argv(self.repo, filled),
                self.repo,
                dev_environment=dev_environment,
            )
        except Exception as exc:
            raise JobRejected("start_failed", type(exc).__name__) from None
        if not available:
            raise JobRejected("unavailable_command")
        mutating = entry.mutates
        # The slot is released in the same step that ends its job, so a holder is still running.
        active = self._active_mutation
        if mutating and active is not None:
            raise JobRejected(
                "busy", f"Busy: {active.id} ({active.command_id})", job_id=active.id
            )
        if self.active_count >= _RUNNING_LIMIT:
            raise JobRejected("busy")
        if isinstance(digest, str):
            argv += ["--plan-digest", digest]
        job = _Job(uuid.uuid4().hex, command_id)
        self._jobs[job.id] = job
        task = asyncio.create_task(self._execute(job, argv))
        job.task = task
        if mutating:
            self._active_mutation = job
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _execute(self, job: _Job, argv: list[str]) -> None:
        try:
            if self._executor is _default_executor:
                job.returncode = await self._executor(
                    argv,
                    cwd=self.repo,
                    on_output=job.append,
                    cancel_event=job.cancel_event,
                )
            else:
                job.returncode = await self._executor(
                    argv, cwd=self.repo, on_output=job.append
                )
            if job.cancel_requested:
                job.returncode = None
                job.phase, job.error = "cancelled", "cancelled"
            else:
                job.phase = "completed"
        except asyncio.CancelledError:
            job.returncode = None
            job.phase, job.error = (
                ("cancelled", "cancelled")
                if job.cancel_requested
                else ("failed", "CancelledError")
            )
        except Exception as exc:
            if job.cancel_requested:
                job.returncode = None
                job.phase, job.error = "cancelled", "cancelled"
            else:
                job.phase, job.error = "failed", type(exc).__name__
        finally:
            if self._active_mutation is job:
                self._active_mutation = None
            job.notify()
            finished = [
                key for key, kept in self._jobs.items() if kept.phase != "running"
            ]
            for key in finished[:-_KEPT_FINISHED]:
                del self._jobs[key]

    async def _cancel(self, body: Mapping[str, object]) -> _Job:
        """Confirm and stop one running job, waiting until its process/task has ended."""
        job = self._requested_job(body)
        confirmed = body.get("confirmed", False)
        if not isinstance(confirmed, bool):
            raise JobRejected("bad_request")
        if not confirmed:
            raise JobRejected("confirmation_required")
        if job.phase != "running" or job.task is None:
            raise JobRejected("not_running")
        job.cancel_requested = True
        job.cancel_event.set()
        if self._executor is _default_executor and _runner_supports_cancellation(
            job.cancel_event
        ):
            # The default executor observes the event and terminates its child process. A shield
            # keeps a client disconnect from cancelling the server-owned execution task.
            await asyncio.shield(job.task)
        else:
            # Test and embedders may provide a plain async executor. Task cancellation is the
            # only cancellation contract available to such an executor.
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
        return job

    def _requested_job(self, body: Mapping[str, object]) -> _Job:
        job_id = body.get("job_id")
        if not isinstance(job_id, str) or not job_id or len(job_id) > MAX_ID_CHARS:
            raise JobRejected("bad_request")
        if job_id not in self._jobs:
            raise JobRejected("unknown_job")
        return self._jobs[job_id]

    async def replies(self, raw: str) -> AsyncGenerator[str, None]:
        """Reply once to start/get/list, or stream successive non-overlapping subscription
        batches. Never raises: every failure is an error reply."""
        try:
            body, request_id, kind = parse_envelope(raw)
        except Rejected as rejected:
            yield error_reply(rejected.code, rejected.id, rejected.type)
            return
        echoed = kind[:MAX_ID_CHARS]
        envelope = {"v": PROTOCOL_VERSION, "id": request_id, "type": echoed}
        try:
            if kind not in _FIELDS:
                raise JobRejected("unknown_type")
            if set(body) - _FIELDS[kind]:
                raise JobRejected("bad_request")
            if kind == "job.list":
                jobs = [job.summary() for job in self._jobs.values()]
                yield encode({**envelope, "ok": True, "jobs": jobs})
                return
            if kind == "job.start":
                job = self._start(body)
            elif kind == "job.cancel":
                job = await self._cancel(body)
            else:
                job = self._requested_job(body)
            cursor = body.get("cursor", 0)
            while True:
                next_change = job.next_change
                snapshot = job.snapshot(cursor)
                yield encode({**envelope, "ok": True, "job": snapshot})
                if kind != "job.subscribe" or snapshot["done"]:
                    return
                cursor = snapshot["cursor"]
                if cursor == len(job.output):
                    await next_change.wait()
                else:
                    await asyncio.sleep(0)
        except JobRejected as rejected:
            yield error_reply(
                rejected.code, request_id, echoed, rejected.message, rejected.job_id
            )

    async def close(self) -> None:
        """An explicit server shutdown waits for its executions, so no command is abandoned
        half-done; subscription cancellation never does."""
        await asyncio.gather(*(asyncio.shield(task) for task in tuple(self._tasks)))
