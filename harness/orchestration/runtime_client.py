"""Python bridge to the Rust ``harness-runtime`` process (ADR 0019, ADR 0021).

The coordinator asks the runtime over a Unix domain socket and never writes state itself when the
runtime is unavailable: an unreachable runtime is the typed error ``runtime_unavailable``, not a
fallback. A request that loses its transport is repeated with the same ``operation_id`` until its
``request_deadline``, so a lost answer never becomes a second intent.

The closed vocabularies of the protocol live here next to the client. The ``systemFields.v1``
registry holds the same values, and ``native/harness-runtime/crates/contracts`` holds them on the
Rust side; a drift check in each language fails CI on any difference. Nothing here uses a
Unix-only API at import time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import struct
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ..errors import HarnessError
from .core.utils import JsonObject, _repo

# ADR 0021, section 3: the error codes the runtime answers with, then the client-side code for a
# runtime that does not answer.
RUNTIME_ERROR_CODES = (
    "protocol_unsupported",
    "unauthorized_peer",
    "malformed_request",
    "unknown_operation",
    "approval_mismatch",
    "revision_conflict",
    "store_busy",
    "lease_held",
    "fencing_mismatch",
    "idempotency_conflict",
    "not_found",
    "invariant_violation",
    "deadline_exceeded",
    "engine_unavailable",
    "policy_denied",
    "internal",
    "runtime_unavailable",
)
# The failure-domain taxonomy of ADR 0019.
FAILURE_DOMAINS = (
    "environment_preparation",
    "infrastructure",
    "policy",
    "tooling",
    "code_test",
    "timeout",
    "cancelled",
    "unknown",
)

# The protocol version this client speaks; the runtime accepts a common major (ADR 0021).
PROTOCOL_VERSION = (1, 0)
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_SOCKET_PATH_BYTES = 100
_FRAME_HEADER = struct.Struct(">I")
# ADR 0021, section 4: the delay between two attempts of one request.
BACKOFF_START_SECONDS = 0.1
BACKOFF_CAP_SECONDS = 5.0
DEFAULT_TIMEOUT_SECONDS = 30.0
# Without XDG_RUNTIME_DIR the runtime directory sits in the state root of the repository.
_STATE_RUNTIME_DIR = Path(".harness/orchestration/state/runtime")
_NO_RUNTIME_REMEDY = (
    "start the runtime with `harness-runtime serve --repo <repo>`; the coordinator never writes "
    "state without it"
)


class RuntimeClientError(HarnessError):
    """The runtime refused a request, or did not answer it.

    ``code`` is a member of ``RUNTIME_ERROR_CODES``. The first line of the remedy starts with
    ``[<code>]``, so a script can recognise the code without a change of the CLI error format.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        remedy: str,
        domain: str = "tooling",
        retryable: bool = False,
        facts: JsonObject | None = None,
    ) -> None:
        """Keep the protocol error fields beside the message and the remedy."""
        super().__init__(message, remedy=f"[{code}] {remedy}")
        self.code = code
        self.domain = domain
        self.retryable = retryable
        self.facts: JsonObject = facts or {}


class _TransportFailure(Exception):
    """A connection that could not carry the request; the request is repeated."""


@dataclass(frozen=True)
class RuntimePaths:
    """Where one repository's runtime keeps its socket and its token."""

    repo_id: str
    directory: Path
    socket: Path
    token: Path


@dataclass(frozen=True)
class RuntimeReply:
    """The answer to one request: what ``hello`` reported, and the result of the operation."""

    hello: JsonObject
    result: JsonObject
    revision: int | None


def _not_a_repository(repo_root: Path) -> RuntimeClientError:
    return RuntimeClientError(
        "runtime_unavailable",
        f"{repo_root} is not the root of a git working tree, so no runtime address exists for it",
        remedy="pass the root of the repository with --repo",
        domain="environment_preparation",
    )


def git_common_dir(repo_root: Path) -> Path:
    """The absolute common git directory of a working tree root.

    ``.git`` itself, or, for a linked worktree, the directory its ``commondir`` file names.
    """
    dot_git = repo_root / ".git"
    if dot_git.is_dir():
        return dot_git.resolve()
    if dot_git.is_file():
        pointer = dot_git.read_text(encoding="utf-8").strip()
        if pointer.startswith("gitdir:"):
            git_dir = (repo_root / pointer.removeprefix("gitdir:").strip()).resolve()
            commondir = git_dir / "commondir"
            if commondir.is_file():
                return (git_dir / commondir.read_text(encoding="utf-8").strip()).resolve()
            return git_dir
    raise _not_a_repository(repo_root)


def repo_id(common_git_dir: Path) -> str:
    """The first 16 hex digits of the SHA-256 of the path of the common git directory."""
    return hashlib.sha256(os.fsencode(common_git_dir)).hexdigest()[:16]


def runtime_paths(
    repo_root: Path, environ: Mapping[str, str] | None = None
) -> RuntimePaths:
    """The socket and the token of a repository's runtime (ADR 0021, section 2).

    The runtime directory is ``$XDG_RUNTIME_DIR/harness``, or ``<repo>/.harness/orchestration/
    state/runtime`` when the variable is unset, empty or not absolute.
    """
    root = repo_root.resolve()
    identity = repo_id(git_common_dir(root))
    xdg = (os.environ if environ is None else environ).get("XDG_RUNTIME_DIR", "")
    directory = (
        Path(xdg) / "harness" if xdg and Path(xdg).is_absolute() else root / _STATE_RUNTIME_DIR
    )
    sock = directory / f"{identity}.sock"
    if len(os.fsencode(sock)) > MAX_SOCKET_PATH_BYTES:
        raise RuntimeClientError(
            "runtime_unavailable",
            f"the socket path {sock} is longer than {MAX_SOCKET_PATH_BYTES} bytes",
            remedy="set XDG_RUNTIME_DIR to a shorter absolute path and restart the runtime",
            domain="environment_preparation",
        )
    return RuntimePaths(
        repo_id=identity,
        directory=directory,
        socket=sock,
        token=directory / f"{identity}.token",
    )


def _read_exactly(connection: socket.socket, count: int) -> bytes:
    chunks: list[bytes] = []
    while count:
        chunk = connection.recv(count)
        if not chunk:
            raise _TransportFailure("the runtime closed the connection")
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)


def _exchange(connection: socket.socket, request: JsonObject) -> JsonObject:
    """Write one frame and read one frame."""
    body = json.dumps(request, ensure_ascii=False).encode("utf-8")
    connection.sendall(_FRAME_HEADER.pack(len(body)) + body)
    (length,) = _FRAME_HEADER.unpack(_read_exactly(connection, _FRAME_HEADER.size))
    if length > MAX_FRAME_BYTES:
        raise RuntimeClientError(
            "internal",
            f"the runtime announced a frame of {length} bytes",
            remedy="update harness and harness-runtime to the same release",
        )
    try:
        answer = json.loads(_read_exactly(connection, length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeClientError(
            "internal",
            "the runtime sent a response that is not JSON",
            remedy="update harness and harness-runtime to the same release",
        ) from exc
    if not isinstance(answer, dict):
        raise RuntimeClientError(
            "internal",
            "the runtime sent a response that is not a JSON object",
            remedy="update harness and harness-runtime to the same release",
        )
    return cast(JsonObject, answer)


def _typed_error(response: Mapping[str, object]) -> RuntimeClientError:
    """The runtime's ``error`` object as a typed error; unknown fields are ignored."""
    raw = response.get("error")
    error = cast(JsonObject, raw) if isinstance(raw, dict) else {}
    code = error.get("code")
    domain = error.get("domain")
    facts = error.get("facts")
    message = str(error.get("message") or "the runtime refused the request")
    remedy = str(error.get("remedy") or "see the runtime log")
    if code not in RUNTIME_ERROR_CODES:
        message = f"{message} (the runtime sent the unknown error code {code!r})"
        code = "internal"
    return RuntimeClientError(
        str(code),
        message,
        remedy=remedy,
        domain=str(domain) if domain in FAILURE_DOMAINS else "unknown",
        retryable=error.get("retryable") is True,
        facts=cast(JsonObject, facts) if isinstance(facts, dict) else None,
    )


class RuntimeClient:
    """Asks ``harness-runtime`` for one operation, repeating it while the transport fails."""

    def __init__(
        self,
        repo_root: Path,
        *,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Resolve the runtime address of ``repo_root``; the clock and sleep are replaceable."""
        self.repo_root = repo_root.resolve()
        self.paths = runtime_paths(self.repo_root, environ)
        self._clock = clock
        self._sleep = sleep

    def call(
        self,
        operation: str,
        *,
        operation_id: str,
        payload: JsonObject | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> RuntimeReply:
        """Send one request until it is answered or its ``request_deadline`` passes.

        Every attempt carries the same ``operation_id`` and the same ``request_deadline``. Only a
        lost transport is repeated, with a delay from 100 ms to 5 s; an error the runtime answered
        with is raised as it is.
        """
        deadline = self._clock() + timeout_seconds
        request: JsonObject = {
            "protocol_version": {
                "major": PROTOCOL_VERSION[0],
                "minor": PROTOCOL_VERSION[1],
            },
            "operation_id": operation_id,
            "operation": operation,
            "request_deadline": int(deadline * 1000),
        }
        if payload is not None:
            request["payload"] = payload
        delay = BACKOFF_START_SECONDS
        while True:
            try:
                return self._attempt(request, deadline)
            except _TransportFailure as failure:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise self._unavailable(str(failure)) from failure
                self._sleep(min(delay, remaining))
                delay = min(delay * 2, BACKOFF_CAP_SECONDS)

    def _unavailable(self, reason: str) -> RuntimeClientError:
        return RuntimeClientError(
            "runtime_unavailable",
            f"harness-runtime does not answer on {self.paths.socket}: {reason}",
            remedy=_NO_RUNTIME_REMEDY,
            domain="infrastructure",
            retryable=True,
        )

    def _attempt(self, request: JsonObject, deadline: float) -> RuntimeReply:
        """One connection: ``hello`` with the token, then the request."""
        if not hasattr(socket, "AF_UNIX"):
            raise RuntimeClientError(
                "runtime_unavailable",
                "this platform has no Unix domain sockets",
                remedy="run the coordinator on Linux or macOS",
                domain="environment_preparation",
            )
        try:
            token = self.paths.token.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise _TransportFailure(f"cannot read the token file: {exc}") from exc
        if not token:
            raise _TransportFailure("the token file is empty")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(max(deadline - self._clock(), 0.001))
            connection.connect(str(self.paths.socket))
            greeting = _exchange(connection, self._hello(request, token))
            if greeting.get("status") != "ok":
                raise _typed_error(greeting)
            answer = _exchange(connection, request)
        except OSError as exc:
            raise _TransportFailure(str(exc)) from exc
        finally:
            connection.close()
        if answer.get("status") != "ok":
            raise _typed_error(answer)
        hello = greeting.get("result")
        result = answer.get("result")
        revision = answer.get("revision")
        return RuntimeReply(
            hello=cast(JsonObject, hello) if isinstance(hello, dict) else {},
            result=cast(JsonObject, result) if isinstance(result, dict) else {},
            revision=revision if isinstance(revision, int) else None,
        )

    def _hello(self, request: JsonObject, token: str) -> JsonObject:
        major, minor = PROTOCOL_VERSION
        version = {"major": major, "minor": minor}
        return {
            "protocol_version": version,
            "operation_id": f"{request['operation_id']}.hello",
            "operation": "hello",
            "request_deadline": request["request_deadline"],
            "payload": {
                "protocol": {"min": version, "max": version},
                "client_version": "harness-coordinator",
                "repo_root": str(self.repo_root),
                "token": token,
            },
        }


def runtime_status(args: argparse.Namespace) -> JsonObject:
    """``runtime status``: the state of the repository's runtime, read-only."""
    client = RuntimeClient(_repo(args))
    reply = client.call(
        "status",
        operation_id=f"status-{client.paths.repo_id}",
        timeout_seconds=float(args.timeout),
    )
    protocol = reply.hello.get("protocol")
    return {
        "socket": str(client.paths.socket),
        "protocol": protocol,
        "server_version": reply.hello.get("server_version"),
        "store_id": reply.hello.get("store_id"),
        "features": reply.hello.get("features"),
        "runtime": reply.result,
    }
