"""Exercise job ownership and cursor replay through the authenticated transport."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from harness.console import jobs as job_module
from harness.console.jobs import JobService
from harness.console.process_tree import CommandCancelled
from harness.console.server import MAX_SUBSCRIPTIONS, MESSAGE_LIMIT, ConsoleServer
from tests.console.test_server import _connect

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection

DIGEST = "0123456789abcdef" * 4


def test_completed_jobs_append_client_and_result_to_command_journal(
    project: Path,
) -> None:
    async def scenario() -> None:
        fake = FakeExecutor()
        fake.finish.set()
        server = ConsoleServer(project, jobs=JobService(project, executor=fake))
        await server.start()
        try:
            async with _connect(server, project) as client:
                for origin in ("tui", "browser"):
                    reply = await start(client, command_id="diff", client=origin)
                    assert reply["ok"] is True
                    await read_until_done(client, reply["job"]["id"])
        finally:
            await server.close()

        journal = project / ".harness" / "console" / "commands.jsonl"
        records = [json.loads(line) for line in journal.read_text().splitlines()]
        assert [record["client"] for record in records] == ["tui", "browser"]
        assert all(record["timestamp"] for record in records)
        assert all(record["reversibility"] == "reversible" for record in records)
        assert all(record["returncode"] == 7 for record in records)
        assert all("diff" in record["argv"] for record in records)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "target",
    [
        "client_secret: private-value",
        "ordinary-worktree",
        "e",
        "worktree-remove",
        "  first\nsecond  ",
    ],
)
def test_journal_masks_all_user_values_and_preserves_command(
    project: Path, target: str
) -> None:
    async def scenario() -> None:
        fake = FakeExecutor()
        fake.finish.set()
        server = ConsoleServer(project, jobs=JobService(project, executor=fake))
        await server.start()
        try:
            async with _connect(server, project) as client:
                reply = await start(
                    client,
                    command_id="worktree-remove",
                    values={"worktree": target},
                    confirmed=True,
                )
                assert reply["ok"] is True, reply
                await read_until_done(client, reply["job"]["id"])
        finally:
            await server.close()
        journal = project / ".harness" / "console" / "commands.jsonl"
        [record] = [json.loads(line) for line in journal.read_text().splitlines()]
        assert record["command_id"] == "worktree-remove"
        assert record["argv"][-3:] == ["worktree-remove", str(project), "••••••"]
        assert fake.calls[0][0][-1] == target

    asyncio.run(scenario())


class FakeExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.calls: list[tuple[list[str], Path]] = []
        self.emit: Callable[[str, str], None] = lambda *_: None

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        on_output: Callable[[str, str], None],
        cancel_event: threading.Event,
    ) -> int:
        # This command ignores cancellation, like one that ends before the stop reaches it.
        del cancel_event
        self.calls.append((list(argv), cwd))
        self.emit = on_output
        on_output("stdout", "first\n")
        self.started.set()
        await self.finish.wait()
        on_output("stderr", "last\n")
        return 7


class CancellableExecutor:
    """An async executor whose command runs until the job's cancel event stops it."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        # Cleared by a test that needs the stop still in progress.
        self.stop = asyncio.Event()
        self.stop.set()

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        on_output: Callable[[str, str], None],
        cancel_event: threading.Event,
    ) -> int:
        del argv, cwd, on_output
        self.started.set()
        while not cancel_event.is_set():
            await asyncio.sleep(0.01)
        self.cancelled.set()
        await self.stop.wait()
        raise CommandCancelled


def request(kind: str, request_id: object = "request", **fields: object) -> str:
    return json.dumps({"id": request_id, "type": kind, **fields})


async def receive(client: ClientConnection):  # type: ignore[no-untyped-def]
    return json.loads(await asyncio.wait_for(client.recv(), 3))


async def read_until_done(  # type: ignore[no-untyped-def]
    client: ClientConnection, job_id: str, cursor: int = 0
):
    """The job once replay caught up, with every fragment read from `cursor`."""
    await client.send(request("job.subscribe", job_id=job_id, cursor=cursor))
    output: list[dict[str, str]] = []
    while True:
        reply = await receive(client)
        assert reply["ok"] is True, reply
        output.extend(reply["job"]["output"])
        if reply["job"]["done"]:
            return {**reply["job"], "output": output}


async def start(connection: ClientConnection, **fields: object):  # type: ignore[no-untyped-def]
    await connection.send(request("job.start", **fields))
    return await receive(connection)


def _read_pid_pair(path: Path) -> tuple[int, int]:
    """Wait for the public test command to publish its parent and child process ids."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            parent, child = (int(value) for value in path.read_text().split())
            return parent, child
        except (OSError, ValueError):
            time.sleep(0.01)
    raise AssertionError(f"process ids were not written to {path}")


def _pid_exists(pid: int) -> bool:
    """Check process existence without treating a permission error as process death."""
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return any(
            token == str(pid)
            for line in result.stdout.splitlines()
            for token in line.split()
        )
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_pid(pid: int) -> None:
    """Best-effort test cleanup for a command left alive after a failed assertion."""
    if pid <= 1 or pid == os.getpid() or not _pid_exists(pid):
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        os.kill(pid, signal.SIGKILL)


def _wait_for_exit(pid: int) -> None:
    """Wait briefly for a cancelled process tree to disappear, or fail with its pid."""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return
        time.sleep(0.01)
    raise AssertionError(f"cancelled process {pid} is still alive")


@pytest.mark.parametrize("abort", [False, True], ids=["close", "abort"])
def test_catalog_job_stream_survives_disconnect_and_replays_from_cursor(
    project: Path, abort: bool
) -> None:
    async def scenario() -> None:
        fake = FakeExecutor()
        server = ConsoleServer(project, jobs=JobService(project, executor=fake))
        await server.start()
        try:
            async with _connect(server, project) as client:
                reply = await start(client, command_id="diff")
                assert reply["ok"] is True, reply
                job_id = reply["job"]["id"]
                assert isinstance(job_id, str) and job_id
                await asyncio.wait_for(fake.started.wait(), 3)
                assert fake.calls[0][1] == project
                assert fake.calls[0][0][-2:] == ["diff", str(project)]
                await client.send(request("job.subscribe", job_id=job_id, cursor=0))
                first = (await receive(client))["job"]
                assert first["output"] == [{"channel": "stdout", "text": "first\n"}]
                assert first["cursor"] == 1
                # Output written while the subscription waits arrives without another request.
                fake.emit("stdout", "live\n")
                live = (await receive(client))["job"]
                assert live["output"] == [{"channel": "stdout", "text": "live\n"}]
                cursor = live["cursor"]
                assert cursor == 2
                if abort:
                    client.transport.abort()

            # The old subscription ends. Execution still belongs to the server.
            assert not fake.finish.is_set()
            fake.emit("stdout", "offline\n")
            fake.finish.set()
            async with _connect(server, project) as client:
                job = await read_until_done(client, job_id, cursor)
                assert job["output"] == [
                    {"channel": "stdout", "text": "offline\n"},
                    {"channel": "stderr", "text": "last\n"},
                ]
                assert job["returncode"] == 7
                await client.send(request("job.get", job_id=job_id, cursor=4))
                completed = (await receive(client))["job"]
                assert completed["output"] == []
                assert completed["returncode"] == 7
                assert completed["done"] is True
                assert len(fake.calls) == 1
        finally:
            fake.finish.set()
            await server.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fields,code",
    [
        ({"command_id": "no-such-command"}, "unknown_command"),
        ({"command_id": "init"}, "confirmation_required"),
        ({"command_id": "diff", "argv": ["arbitrary"]}, "bad_request"),
        ({"command_id": "diff", "values": {"repo": "/other"}}, "bad_request"),
        ({"command_id": "diff", "values": []}, "bad_request"),
        ({"command_id": "diff", "confirmed": "yes"}, "bad_request"),
        ({"command_id": "diff", "plan_digest": DIGEST}, "bad_request"),
        ({"command_id": "cleanup-apply", "confirmed": True}, "plan_required"),
        (
            {"command_id": "cleanup-apply", "confirmed": True, "plan_digest": "abc"},
            "plan_required",
        ),
        ({"command_id": "batch-list", "values": {"state": "bogus"}}, "bad_request"),
        ({"command_id": "batch-create"}, "bad_request"),
        (
            {
                "command_id": "batch-create",
                "values": {
                    "ticket": "1",
                    "branch": "b",
                    "worktree": "w",
                    "definition_of_done": "done",
                    "prohibited_change": "none",
                    "required_gate": "review\nbogus",
                },
            },
            "bad_request",
        ),
        ({"command_id": "batch-list", "values": {"nope": "x"}}, "bad_request"),
    ],
)
def test_job_admission_rejects_invalid_catalog_requests(
    project: Path, fields: dict[str, object], code: str
) -> None:
    (project / ".harness/orchestration").mkdir(parents=True)
    (project / ".harness/orchestration/coordinator.py").write_text("")

    async def scenario() -> None:
        fake = FakeExecutor()
        server = ConsoleServer(project, jobs=JobService(project, executor=fake))
        await server.start()
        try:
            async with _connect(server, project) as client:
                reply = await start(client, **fields)
                assert reply["ok"] is False
                assert reply["error"]["code"] == code
                assert fake.calls == []
        finally:
            fake.finish.set()
            await server.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kind,fields,code",
    [
        ("job.nope", {}, "unknown_type"),
        ("job.get", {"job_id": "missing"}, "unknown_job"),
        ("job.get", {"job_id": ""}, "bad_request"),
        ("job.list", {"cursor": 0}, "bad_request"),
    ],
)
def test_job_lookup_rejects_invalid_requests(
    project: Path, kind: str, fields: dict[str, object], code: str
) -> None:
    async def scenario() -> None:
        server = ConsoleServer(
            project, jobs=JobService(project, executor=FakeExecutor())
        )
        await server.start()
        try:
            async with _connect(server, project) as client:
                await client.send(request(kind, **fields))
                reply = await receive(client)
                assert (reply["id"], reply["type"], reply["ok"]) == (
                    "request",
                    kind,
                    False,
                )
                assert reply["error"]["code"] == code
        finally:
            await server.close()

    asyncio.run(scenario())


def test_coordinator_job_passes_declared_fields_only_when_the_coordinator_exists(
    project: Path,
) -> None:
    async def scenario() -> None:
        fake = FakeExecutor()
        fake.finish.set()
        server = ConsoleServer(project, jobs=JobService(project, executor=fake))
        await server.start()
        try:
            async with _connect(server, project) as client:
                values = {"state": "active", "open": "1"}
                reply = await start(client, command_id="batch-list", values=values)
                assert reply["error"]["code"] == "unavailable_command"
                (project / ".harness/orchestration").mkdir(parents=True)
                (project / ".harness/orchestration/coordinator.py").write_text("")
                reply = await start(client, command_id="batch-list", values=values)
                assert reply["ok"] is True, reply
                assert (await read_until_done(client, reply["job"]["id"]))["done"]
        finally:
            await server.close()
        argv = fake.calls[0][0]
        assert argv[-5:] == ["batch", "list", "--state", "active", "--open"]
        assert ".harness/orchestration/coordinator.py" in argv
        assert len(fake.calls) == 1

    asyncio.run(scenario())


def test_cleanup_job_deletes_only_the_previewed_plan(project: Path) -> None:
    async def scenario() -> None:
        fake = FakeExecutor()
        server = ConsoleServer(project, jobs=JobService(project, executor=fake))
        await server.start()
        try:
            async with _connect(server, project) as client:
                reply = await start(
                    client,
                    command_id="cleanup-hard-apply",
                    confirmed=True,
                    confirmation="HARD",
                    plan_digest=DIGEST,
                )
                assert reply["ok"] is True, reply
                await asyncio.wait_for(fake.started.wait(), 3)
                assert fake.calls[0][0][-2:] == ["--plan-digest", DIGEST]
        finally:
            fake.finish.set()
            await server.close()

    asyncio.run(scenario())


def test_job_list_shows_running_and_finished_jobs_to_a_new_client(
    project: Path,
) -> None:
    async def scenario() -> None:
        fake = FakeExecutor()
        server = ConsoleServer(project, jobs=JobService(project, executor=fake))
        await server.start()
        try:
            async with _connect(server, project) as client:
                job_id = (await start(client, command_id="diff"))["job"]["id"]
                await asyncio.wait_for(fake.started.wait(), 3)
            async with _connect(server, project) as client:
                await client.send(request("job.list"))
                [running] = (await receive(client))["jobs"]
                assert (running["id"], running["command_id"]) == (job_id, "diff")
                assert (running["phase"], running["done"]) == ("running", False)
                fake.finish.set()
                await read_until_done(client, job_id)
                await client.send(request("job.list"))
                [finished] = (await receive(client))["jobs"]
                assert (finished["phase"], finished["returncode"]) == ("completed", 7)
                assert finished["cursor"] == 2
        finally:
            fake.finish.set()
            await server.close()

    asyncio.run(scenario())


def test_running_job_limit_refuses_a_start_but_not_reads(project: Path) -> None:
    async def scenario() -> None:
        fake = FakeExecutor()
        server = ConsoleServer(project, jobs=JobService(project, executor=fake))
        await server.start()
        try:
            async with _connect(server, project) as client:
                ids = [
                    (await start(client, command_id="diff"))["job"]["id"]
                    for _ in range(job_module._RUNNING_LIMIT)
                ]
                refused = await start(client, command_id="diff")
                assert refused["error"]["code"] == "busy"
                # No `job_id`: the limit refused it, not a running mutation.
                assert "job_id" not in refused["error"]
                mutation = await start(client, command_id="registry")
                assert mutation["error"]["code"] == "busy"
                assert "job_id" not in mutation["error"]
                await client.send(request("job.get", job_id=ids[0]))
                assert (await receive(client))["ok"] is True
                assert len(fake.calls) == job_module._RUNNING_LIMIT
        finally:
            fake.finish.set()
            await server.close()

    asyncio.run(scenario())


def test_mutating_jobs_are_exclusive_but_reads_run_beside_one(
    project: Path,
) -> None:
    async def scenario() -> None:
        fake = FakeExecutor()
        server = ConsoleServer(project, jobs=JobService(project, executor=fake))
        await server.start()
        try:
            async with _connect(server, project) as client:
                # Both commands are reversible in the confirmation UX, but each writes project
                # state and must therefore share the mutation slot.
                first = await start(client, command_id="registry")
                assert first["ok"] is True, first
                active_id = first["job"]["id"]
                await asyncio.wait_for(fake.started.wait(), 3)

                refused = await start(client, command_id="health-fix")
                assert refused["error"]["code"] == "busy"
                assert refused["error"]["job_id"] == active_id
                assert active_id in refused["error"]["message"]

                read = await start(client, command_id="diff")
                assert read["ok"] is True, read
                await asyncio.sleep(0)
                assert len(fake.calls) == 2
                fake.finish.set()
                await read_until_done(client, active_id)
                await read_until_done(client, read["job"]["id"])
        finally:
            fake.finish.set()
            await server.close()

    asyncio.run(scenario())


def test_job_cancel_requires_confirmation_then_frees_the_mutation_slot(
    project: Path,
) -> None:
    async def scenario() -> None:
        fake = CancellableExecutor()
        server = ConsoleServer(project, jobs=JobService(project, executor=fake))
        await server.start()
        try:
            async with _connect(server, project) as client:
                started = await start(client, command_id="registry")
                assert started["ok"] is True, started
                job_id = started["job"]["id"]
                await asyncio.wait_for(fake.started.wait(), 3)

                await client.send(request("job.cancel", job_id=job_id))
                refused = await receive(client)
                assert refused["error"]["code"] == "confirmation_required"
                assert not fake.cancelled.is_set()

                await client.send(request("job.cancel", job_id=job_id, confirmed=True))
                cancelled = await receive(client)
                assert cancelled["ok"] is True, cancelled
                assert cancelled["job"]["id"] == job_id
                assert cancelled["job"]["phase"] == "cancelled"
                assert cancelled["job"]["done"] is True
                assert cancelled["job"]["returncode"] is None
                assert fake.cancelled.is_set()

                replacement = await start(client, command_id="health-fix")
                assert replacement["ok"] is True, replacement
                await client.send(
                    request(
                        "job.cancel", job_id=replacement["job"]["id"], confirmed=True
                    )
                )
                assert (await receive(client))["job"]["phase"] == "cancelled"
        finally:
            await server.close()

    asyncio.run(scenario())


def test_cancelling_a_read_leaves_the_mutation_running(project: Path) -> None:
    async def scenario() -> None:
        fake = CancellableExecutor()
        server = ConsoleServer(project, jobs=JobService(project, executor=fake))
        await server.start()
        try:
            async with _connect(server, project) as client:
                mutation = (await start(client, command_id="registry"))["job"]["id"]
                read = (await start(client, command_id="diff"))["job"]["id"]

                await client.send(request("job.cancel", job_id=read))
                refused = await receive(client)
                # Only a mutation warns that its changes stay: a read made none.
                assert (
                    refused["error"]["message"]
                    == job_module._ERRORS["confirmation_required"]
                )
                await client.send(request("job.cancel", job_id=read, confirmed=True))
                assert (await receive(client))["job"]["phase"] == "cancelled"

                await client.send(request("job.get", job_id=mutation))
                assert (await receive(client))["job"]["phase"] == "running"
                busy = await start(client, command_id="health-fix")
                assert busy["error"]["job_id"] == mutation
                await client.send(
                    request("job.cancel", job_id=mutation, confirmed=True)
                )
                assert (await receive(client))["job"]["phase"] == "cancelled"
        finally:
            await server.close()

    asyncio.run(scenario())


def test_job_cancel_refuses_unknown_and_finished_jobs(project: Path) -> None:
    async def scenario() -> None:
        fake = FakeExecutor()
        server = ConsoleServer(project, jobs=JobService(project, executor=fake))
        await server.start()
        try:
            async with _connect(server, project) as client:
                await client.send(
                    request("job.cancel", job_id="f" * 32, confirmed=True)
                )
                assert (await receive(client))["error"]["code"] == "unknown_job"

                job_id = (await start(client, command_id="registry"))["job"]["id"]
                fake.finish.set()
                assert (await read_until_done(client, job_id))["phase"] == "completed"
                await client.send(request("job.cancel", job_id=job_id, confirmed=True))
                assert (await receive(client))["error"]["code"] == "not_running"
        finally:
            fake.finish.set()
            await server.close()

    asyncio.run(scenario())


def test_two_cancels_of_one_job_both_answer_with_the_cancelled_job(
    project: Path,
) -> None:
    async def scenario() -> None:
        fake = CancellableExecutor()
        server = ConsoleServer(project, jobs=JobService(project, executor=fake))
        await server.start()
        try:
            async with _connect(server, project) as client:
                job_id = (await start(client, command_id="registry"))["job"]["id"]
                await asyncio.wait_for(fake.started.wait(), 3)
                for request_id in ("first", "second"):
                    await client.send(
                        request("job.cancel", request_id, job_id=job_id, confirmed=True)
                    )
                replies = {}
                for _ in range(2):
                    reply = await receive(client)
                    replies[reply["id"]] = reply
                assert set(replies) == {"first", "second"}
                assert all(
                    reply["ok"] is True and reply["job"]["phase"] == "cancelled"
                    for reply in replies.values()
                )
        finally:
            await server.close()

    asyncio.run(scenario())


def test_cancel_completes_after_the_requesting_client_disconnects(
    project: Path,
) -> None:
    async def scenario() -> None:
        fake = CancellableExecutor()
        fake.stop.clear()
        server = ConsoleServer(project, jobs=JobService(project, executor=fake))
        await server.start()
        try:
            async with _connect(server, project) as client:
                job_id = (await start(client, command_id="registry"))["job"]["id"]
                await asyncio.wait_for(fake.started.wait(), 3)
                await client.send(request("job.cancel", job_id=job_id, confirmed=True))
                # The requesting client leaves while `job.cancel` still waits for the stop.
                await asyncio.wait_for(fake.cancelled.wait(), 3)
            fake.stop.set()
            async with _connect(server, project) as client:
                assert (await read_until_done(client, job_id))["phase"] == "cancelled"
        finally:
            fake.stop.set()
            await server.close()

    asyncio.run(scenario())


def test_mutation_slot_frees_when_the_job_completes(project: Path) -> None:
    async def scenario() -> None:
        fake = FakeExecutor()
        server = ConsoleServer(project, jobs=JobService(project, executor=fake))
        await server.start()
        try:
            async with _connect(server, project) as client:
                job_id = (await start(client, command_id="registry"))["job"]["id"]
                fake.finish.set()
                assert (await read_until_done(client, job_id))["phase"] == "completed"
                assert (await start(client, command_id="health-fix"))["ok"] is True
        finally:
            fake.finish.set()
            await server.close()

    asyncio.run(scenario())


def test_ledger_reads_run_beside_a_mutation_while_release_lock_is_busy(
    project: Path,
) -> None:
    async def scenario() -> None:
        fake = FakeExecutor()
        (project / ".harness/orchestration").mkdir(parents=True)
        (project / ".harness/orchestration/coordinator.py").write_text("")
        server = ConsoleServer(project, jobs=JobService(project, executor=fake))
        await server.start()
        try:
            async with _connect(server, project) as client:
                mutation = (await start(client, command_id="registry"))["job"]["id"]
                for read in ("ledger-lock-status", "dispatch-status"):
                    assert (await start(client, command_id=read))["ok"] is True, read

                release = await start(
                    client, command_id="ledger-release-lock", confirmed=True
                )
                assert release["error"]["code"] == "busy"
                assert release["error"]["job_id"] == mutation
        finally:
            fake.finish.set()
            await server.close()

    asyncio.run(scenario())


def test_cancel_of_a_command_that_outlives_the_stop_keeps_its_own_result(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A command that ends on its own is reported with its exit code, not as cancelled."""
    monkeypatch.setattr(job_module, "_CANCEL_WAIT_SECONDS", 0.05)

    async def scenario() -> None:
        fake = FakeExecutor()
        server = ConsoleServer(project, jobs=JobService(project, executor=fake))
        await server.start()
        try:
            async with _connect(server, project) as client:
                job_id = (await start(client, command_id="registry"))["job"]["id"]
                await asyncio.wait_for(fake.started.wait(), 3)
                await client.send(request("job.cancel", job_id=job_id))
                warned = await receive(client)
                assert warned["error"]["code"] == "confirmation_required"
                assert "not rolled back" in warned["error"]["message"]

                await client.send(request("job.cancel", job_id=job_id, confirmed=True))
                answered = await receive(client)
                assert answered["ok"] is True and answered["job"]["phase"] == "running"

                fake.finish.set()
                job = await read_until_done(client, job_id)
                assert job["phase"] == "completed" and job["returncode"] == 7
        finally:
            fake.finish.set()
            await server.close()

    asyncio.run(scenario())


def test_public_job_cancel_terminates_the_full_process_tree(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = project / "cancel-tree.pids"
    script = (
        "import os, pathlib, subprocess, sys, time; "
        "path = pathlib.Path(sys.argv[1]); "
        "child = subprocess.Popen([sys.executable, '-u', '-c', "
        "'import time; time.sleep(60)']); "
        "path.write_text(f'{os.getpid()} {child.pid}'); "
        "print('ready', flush=True); child.wait()"
    )

    def process_tree_command(
        _argv: Sequence[str], _repo: Path, *, dev_environment: bool = False
    ) -> list[str]:
        del _argv, _repo, dev_environment
        return [sys.executable, "-u", "-c", script, str(pid_file)]

    monkeypatch.setattr(job_module, "process_argv", process_tree_command)

    async def scenario() -> None:
        server = ConsoleServer(project)
        parent_pid: int | None = None
        child_pid: int | None = None
        await server.start()
        try:
            async with _connect(server, project) as client:
                started = await start(client, command_id="registry")
                assert started["ok"] is True, started
                job_id = started["job"]["id"]
                parent_pid, child_pid = await asyncio.to_thread(
                    _read_pid_pair, pid_file
                )
                assert parent_pid is not None and child_pid is not None
                assert _pid_exists(parent_pid)
                assert _pid_exists(child_pid)

                await client.send(request("job.cancel", job_id=job_id, confirmed=True))
                cancelled = await receive(client)
                assert cancelled["ok"] is True, cancelled
                assert cancelled["job"]["phase"] == "cancelled"
                assert cancelled["job"]["done"] is True
                await asyncio.to_thread(_wait_for_exit, child_pid)
        finally:
            if parent_pid is not None:
                await asyncio.to_thread(_kill_pid, parent_pid)
            if child_pid is not None:
                await asyncio.to_thread(_kill_pid, child_pid)
            await server.close()

    asyncio.run(scenario())


def test_waiting_subscriptions_leave_request_slots_free(project: Path) -> None:
    async def scenario() -> None:
        fake = FakeExecutor()
        server = ConsoleServer(project, jobs=JobService(project, executor=fake))
        await server.start()
        try:
            async with _connect(server, project) as client:
                job_id = (await start(client, command_id="diff"))["job"]["id"]
                await asyncio.wait_for(fake.started.wait(), 3)
                for index in range(MAX_SUBSCRIPTIONS):
                    await client.send(request("job.subscribe", index, job_id=job_id))
                    assert (await receive(client))["id"] == index
                await client.send(request("job.subscribe", "extra", job_id=job_id))
                refused = await receive(client)
                assert (refused["id"], refused["error"]["code"]) == ("extra", "busy")
                await client.send(request("job.get", "read", job_id=job_id))
                answered = await receive(client)
                assert (answered["id"], answered["ok"]) == ("read", True)
        finally:
            fake.finish.set()
            await server.close()

    asyncio.run(scenario())


def test_failed_execution_reports_only_the_failure_type(project: Path) -> None:
    async def failing(
        argv: Sequence[str],
        *,
        cwd: Path,
        on_output: Callable[[str, str], None],
        cancel_event: threading.Event,
    ) -> int:
        on_output("stderr", "boom\n")
        raise RuntimeError("private detail")

    async def scenario() -> None:
        server = ConsoleServer(project, jobs=JobService(project, executor=failing))
        await server.start()
        try:
            async with _connect(server, project) as client:
                job_id = (await start(client, command_id="diff"))["job"]["id"]
                job = await read_until_done(client, job_id)
                assert (job["phase"], job["error"]) == ("failed", "RuntimeError")
                assert job["returncode"] is None
                assert job["output"] == [{"channel": "stderr", "text": "boom\n"}]
        finally:
            await server.close()

    asyncio.run(scenario())


def test_unpreparable_command_is_an_error_reply_on_a_live_connection(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> list[str]:
        raise OSError("no interpreter")

    monkeypatch.setattr(job_module, "process_argv", broken)

    async def scenario() -> None:
        fake = FakeExecutor()
        server = ConsoleServer(project, jobs=JobService(project, executor=fake))
        await server.start()
        try:
            async with _connect(server, project) as client:
                reply = await start(client, command_id="diff")
                assert reply["error"] == {"code": "start_failed", "message": "OSError"}
                await client.send(request("job.list"))
                assert (await receive(client))["jobs"] == []
                assert fake.calls == []
        finally:
            await server.close()

    asyncio.run(scenario())


def test_output_beyond_the_job_limit_is_cut_and_marked(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(job_module, "_OUTPUT_LIMIT_CHARS", 10)

    async def scenario() -> None:
        fake = FakeExecutor()
        server = ConsoleServer(project, jobs=JobService(project, executor=fake))
        await server.start()
        try:
            async with _connect(server, project) as client:
                job_id = (await start(client, command_id="diff"))["job"]["id"]
                await asyncio.wait_for(fake.started.wait(), 3)
                fake.emit("stdout", "0123456789")
                fake.emit("stdout", "x")
                fake.finish.set()
                job = await read_until_done(client, job_id)
                assert job["output"] == [{"channel": "stdout", "text": "first\n"}]
                assert job["truncated"] is True
                assert job["returncode"] == 7
        finally:
            fake.finish.set()
            await server.close()

    asyncio.run(scenario())


def test_only_the_newest_finished_jobs_are_kept(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(job_module, "_KEPT_FINISHED", 1)

    async def scenario() -> None:
        fake = FakeExecutor()
        fake.finish.set()
        server = ConsoleServer(project, jobs=JobService(project, executor=fake))
        await server.start()
        try:
            async with _connect(server, project) as client:
                first = (await start(client, command_id="diff"))["job"]["id"]
                await read_until_done(client, first)
                second = (await start(client, command_id="diff"))["job"]["id"]
                await read_until_done(client, second)
                await client.send(request("job.get", job_id=first))
                assert (await receive(client))["error"]["code"] == "unknown_job"
                await client.send(request("job.get", job_id=second))
                assert (await receive(client))["job"]["returncode"] == 7
        finally:
            await server.close()

    asyncio.run(scenario())


def test_running_job_prevents_idle_exit_without_clients(project: Path) -> None:
    async def scenario() -> None:
        fake = FakeExecutor()
        server = ConsoleServer(
            project, jobs=JobService(project, executor=fake), idle_grace_seconds=0.1
        )
        await server.start()
        try:
            async with _connect(server, project) as client:
                assert (await start(client, command_id="diff"))["ok"] is True
                await asyncio.wait_for(fake.started.wait(), 3)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(server.wait_closed()), 0.35)
            fake.finish.set()
            await asyncio.wait_for(server.wait_closed(), 3)
            assert len(fake.calls) == 1
        finally:
            fake.finish.set()
            await server.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("cursor", [-1, True, 99, "1", 1.5])
def test_invalid_cursor_does_not_advance_another_reader(
    project: Path, cursor: object
) -> None:
    async def scenario() -> None:
        fake = FakeExecutor()
        server = ConsoleServer(project, jobs=JobService(project, executor=fake))
        await server.start()
        try:
            async with _connect(server, project) as client:
                job_id = (await start(client, command_id="diff"))["job"]["id"]
                await asyncio.wait_for(fake.started.wait(), 3)
                await client.send(request("job.get", job_id=job_id, cursor=cursor))
                bad = await receive(client)
                assert bad["error"]["code"] == "invalid_cursor"
                await client.send(request("job.get", job_id=job_id, cursor=0))
                good = (await receive(client))["job"]
                assert good["cursor"] == 1
                assert good["output"] == [{"channel": "stdout", "text": "first\n"}]
        finally:
            fake.finish.set()
            await server.close()

    asyncio.run(scenario())


def test_large_completed_output_is_replayed_to_independent_clients(
    project: Path,
) -> None:
    async def scenario() -> None:
        fake = FakeExecutor()
        server = ConsoleServer(project, jobs=JobService(project, executor=fake))
        await server.start()
        try:
            async with _connect(server, project) as creator:
                job_id = (await start(creator, command_id="diff"))["job"]["id"]
                await asyncio.wait_for(fake.started.wait(), 3)
                # Escaped control characters and four-byte characters are the widest on the wire.
                text = "\x01ё🙂" * 20000
                fake.emit("stdout", text)
                fake.finish.set()

                async def replay() -> str:
                    async with _connect(server, project) as client:
                        await client.send(request("job.subscribe", job_id=job_id))
                        output: list[str] = []
                        cursor = 0
                        while True:
                            raw = await asyncio.wait_for(client.recv(), 5)
                            assert len(raw.encode()) <= MESSAGE_LIMIT
                            job = json.loads(raw)["job"]
                            assert job["cursor"] == cursor + len(job["output"])
                            cursor = job["cursor"]
                            output.extend(item["text"] for item in job["output"])
                            if job["done"]:
                                assert job["returncode"] == 7
                                return "".join(output)

                first, second = await asyncio.gather(replay(), replay())
                assert first == "first\n" + text + "last\n"
                assert second == "first\n" + text + "last\n"
                assert len(fake.calls) == 1
        finally:
            fake.finish.set()
            await server.close()

    asyncio.run(scenario())


def test_default_job_executor_uses_the_existing_capturing_runner(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def capture(
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        on_output: Callable[[str, str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> subprocess.CompletedProcess[str]:
        assert cwd == project
        assert on_output is not None
        # The job's own event, so `job.cancel` reaches the runner that owns the process.
        assert isinstance(cancel_event, threading.Event)
        calls.append(list(argv))
        on_output("stdout", "from runner\n")
        return subprocess.CompletedProcess(list(argv), 0, "from runner\n", "")

    monkeypatch.setattr(job_module, "capturing_runner", capture)

    async def scenario() -> None:
        server = ConsoleServer(project)
        await server.start()
        try:
            async with _connect(server, project) as client:
                job_id = (await start(client, command_id="diff"))["job"]["id"]
                job = await read_until_done(client, job_id)
                assert job["returncode"] == 0
                assert job["output"] == [{"channel": "stdout", "text": "from runner\n"}]
                assert len(calls) == 1
        finally:
            await server.close()

    asyncio.run(scenario())


def test_typed_catalog_confirmation_is_preserved(project: Path) -> None:
    async def scenario() -> None:
        fake = FakeExecutor()
        server = ConsoleServer(project, jobs=JobService(project, executor=fake))
        await server.start()
        try:
            async with _connect(server, project) as client:
                reply = await start(
                    client, command_id="uninstall-apply", confirmed=True
                )
                assert reply["error"]["code"] == "confirmation_required"
                assert fake.calls == []
                reply = await start(
                    client,
                    command_id="uninstall-apply",
                    confirmed=True,
                    confirmation="UNINSTALL",
                )
                assert reply["ok"] is True
                await asyncio.wait_for(fake.started.wait(), 3)
                assert "uninstall" in fake.calls[0][0]
        finally:
            fake.finish.set()
            await server.close()

    asyncio.run(scenario())
