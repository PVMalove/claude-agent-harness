"""Bounded runtime probes that diagnose a launch before or after a worker failed to start (#679).

``probe_adapter`` checks a project adapter's dispatch flags against its own ``dispatch --help``.
``probe_app_server`` runs the app-server READY probe of #552 against one immutable brief.  Neither
hands a brief to a worker, and neither result ever replaces the worker's model self-report.
"""

from __future__ import annotations

import json
import queue
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO, cast

from harness.json_types import JsonObject

# A help call is far shorter than a worker session.
ADAPTER_PROBE_TIMEOUT_SECONDS = 15
# The READY turn of the app-server probe may queue behind provider capacity.
APP_SERVER_PROBE_TIMEOUT_SECONDS = 120
# Seconds the probe gives an app-server to exit before it is killed.
_SHUTDOWN_SECONDS = 5
_OUTPUT_LIMIT = 1200
# Credentials in a probed command or its output stay out of evidence.
_SECRET = re.compile(
    r"((?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret|token)\s*[=:]\s*)\S+",
    re.IGNORECASE,
)
# App-server ``codexErrorInfo`` kinds that name an infrastructure cause.
_APP_SERVER_ERROR_CATEGORIES = {
    "serverOverloaded": "capacity",
    "rateLimitExceeded": "rate-limit",
    "usageLimitExceeded": "rate-limit",
}
_READY_PROMPT = "Reply with the single word READY. Do not use tools."


def _redact(text: str) -> str:
    return _SECRET.sub(r"\1<redacted>", text[:_OUTPUT_LIMIT])


def probe_adapter(
    command: Sequence[str],
    adapter_args: Sequence[str],
    *,
    cwd: Path,
    timeout: int = ADAPTER_PROBE_TIMEOUT_SECONDS,
) -> JsonObject:
    """Check every option in ``adapter_args`` against the adapter's ``dispatch --help`` output.

    The probe passes no repository and no brief, so it cannot start a worker.  ``unsupported``
    names the options the help text lacks.  A help call that fails or times out confirms nothing:
    its status is ``unknown``, ``timeout`` or ``unavailable`` with a ``limitation`` to show.
    """
    probe = [*command, "dispatch", "--help"]
    evidence: JsonObject = {
        "operation": "adapter-help",
        "command": [_redact(part) for part in probe],
        "worker_started": False,
    }
    try:
        result = subprocess.run(
            probe,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            **evidence,
            "status": "timeout",
            "limitation": f"adapter dispatch --help gave no answer within {timeout}s",
        }
    except OSError as exc:
        return {**evidence, "status": "unavailable", "limitation": _redact(str(exc))}
    if result.returncode != 0:
        return {
            **evidence,
            "status": "unknown",
            "exit_code": result.returncode,
            "limitation": _redact(result.stderr or result.stdout)
            or f"adapter dispatch --help exited with {result.returncode}",
        }
    help_text = result.stdout + result.stderr
    if not all(flag in help_text for flag in ("--repo", "--brief")):
        # Output that does not describe the dispatch interface says nothing about its flags.
        return {
            **evidence,
            "status": "unknown",
            "limitation": "adapter dispatch --help does not describe the dispatch interface, "
            "so its flags cannot be checked",
        }
    unsupported = sorted(
        {
            flag
            for flag in (argument.split("=", 1)[0] for argument in adapter_args)
            if flag.startswith("-")
            and not re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", help_text)
        }
    )
    return {
        **evidence,
        "status": "unsupported" if unsupported else "supported",
        "unsupported_arguments": unsupported,
    }


def validate_runtime_evidence(
    expected: Mapping[str, object], observed: Mapping[str, object]
) -> JsonObject:
    """Compare runtime-observed fields with the values the immutable brief resolved, fail-closed.

    ``expected`` maps a field to its brief value; a ``None`` value is not compared.  The result is
    launch evidence only: it never satisfies the worker's mandatory model self-report.
    """
    missing: list[str] = []
    mismatched: list[str] = []
    for field, want in expected.items():
        if want is None:
            continue
        got = observed.get(field)
        if not isinstance(got, str) or not got.strip():
            missing.append(field)
        elif (
            Path(got).resolve() != Path(str(want)).resolve()
            if field == "cwd"
            else got != want
        ):
            mismatched.append(field)
    status = "mismatch" if mismatched else "missing" if missing else "verified"
    return {
        "status": status,
        "match": status == "verified",
        "missing": missing,
        "mismatched": mismatched,
        "model_self_report_required": True,
    }


class _ProbeTimeout(Exception):
    pass


class _ProbeFailure(Exception):
    def __init__(self, failure: JsonObject) -> None:
        super().__init__(failure.get("error"))
        self.failure = failure


def _failure(error: object) -> JsonObject:
    """Turn a JSON-RPC or turn error into runtime-failure fields for the shared classifier."""
    if not isinstance(error, Mapping):
        return {"error": _redact(str(error))}
    failure: JsonObject = {"error": _redact(str(error.get("message", "")))}
    info = error.get("codexErrorInfo")
    if isinstance(info, Mapping) and info:
        kind, detail = next(iter(info.items()))
        if isinstance(detail, Mapping) and isinstance(
            detail.get("httpStatusCode"), int
        ):
            failure["status_code"] = detail["httpStatusCode"]
        info = kind
    if isinstance(info, str):
        failure["runtime_error"] = info
        category = _APP_SERVER_ERROR_CATEGORIES.get(info)
        if category is not None:
            failure["category"] = category
    return failure


class _AppServer:
    """One app-server process spoken to over JSON lines on stdio, bounded by a single deadline."""

    def __init__(self, command: Sequence[str], cwd: Path, deadline: float) -> None:
        self.deadline = deadline
        self.stderr = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        self.process = subprocess.Popen(
            list(command),
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.messages: queue.Queue[JsonObject | None] = queue.Queue()
        self.next_id = 0
        threading.Thread(
            target=self._read, args=(cast(IO[str], self.process.stdout),), daemon=True
        ).start()

    def _read(self, stream: IO[str]) -> None:
        for line in stream:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict):
                self.messages.put(message)
        self.messages.put(None)

    def _send(self, message: JsonObject) -> None:
        stdin = cast(IO[str], self.process.stdin)
        try:
            stdin.write(json.dumps(message) + "\n")
            stdin.flush()
        except OSError as exc:
            raise _ProbeFailure(
                {"error": f"app-server closed its input: {exc}"}
            ) from exc

    def receive(self) -> JsonObject:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _ProbeTimeout
        try:
            message = self.messages.get(timeout=remaining)
        except queue.Empty as exc:
            raise _ProbeTimeout from exc
        if message is None:
            self.stderr.seek(0)
            raise _ProbeFailure(
                {
                    "error": "app-server exited before it answered: "
                    + _redact(self.stderr.read())
                }
            )
        return message

    def notify(self, method: str) -> None:
        self._send({"method": method})

    def request(self, method: str, params: JsonObject) -> JsonObject:
        self.next_id += 1
        request_id = self.next_id
        self._send({"id": request_id, "method": method, "params": params})
        while True:
            message = self.receive()
            if message.get("id") != request_id or "method" in message:
                continue
            if "error" in message:
                raise _ProbeFailure(_failure(message["error"]))
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    def turn(self) -> JsonObject:
        """Wait for the turn to complete; a non-retried ``error`` notification also ends it."""
        while True:
            message = self.receive()
            params = message.get("params")
            params = params if isinstance(params, dict) else {}
            if message.get("method") == "turn/completed":
                turn = params.get("turn")
                return turn if isinstance(turn, dict) else {}
            if message.get("method") == "error" and params.get("willRetry") is not True:
                return {"status": "failed", "error": params.get("error")}

    def close(self) -> None:
        try:
            cast(IO[str], self.process.stdin).close()
        except OSError:
            pass
        self.process.terminate()
        try:
            self.process.wait(timeout=_SHUTDOWN_SECONDS)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.stderr.close()


def probe_app_server(
    command: Sequence[str],
    dispatch: Mapping[str, object],
    *,
    cwd: Path,
    timeout: int = APP_SERVER_PROBE_TIMEOUT_SECONDS,
) -> JsonObject:
    """Run the app-server READY probe of #552 for one immutable brief without starting a worker.

    The probe sends ``initialize``, ``initialized``, ``model/list``, ``thread/start`` and
    ``turn/start`` on an ephemeral read-only thread with approvals disabled, then waits for
    ``turn/completed`` or a non-retried ``error``.  ``ready`` needs the brief's model in the list,
    ``thread/start`` fields that match the brief and a completed turn.  A capacity, rate-limit or
    service error after ``thread/start`` is a classified failure, never READY.  The process is
    always terminated within the deadline plus a short shutdown grace.
    """
    from harness.orchestration.workflow import failure_classifier

    model = dispatch.get("resolved_model")
    effort = dispatch.get("resolved_effort")
    evidence: JsonObject = {
        "operation": "app-server-probe",
        "command": [_redact(part) for part in command],
        "worker_started": False,
        "model_self_report_required": True,
        "ready": False,
    }
    try:
        server = _AppServer(command, cwd, time.monotonic() + timeout)
    except OSError as exc:
        return {**evidence, "status": "unavailable", "limitation": _redact(str(exc))}
    try:
        server.request(
            "initialize",
            {"clientInfo": {"name": "harness-runtime-probe", "version": "1"}},
        )
        server.notify("initialized")
        listed = server.request("model/list", {}).get("data")
        evidence["model_listed"] = isinstance(listed, list) and any(
            isinstance(entry, dict) and model in (entry.get("model"), entry.get("id"))
            for entry in listed
        )
        params: JsonObject = {
            "model": model,
            "cwd": str(cwd),
            "approvalPolicy": "never",
            "sandbox": "read-only",
            "ephemeral": True,
        }
        if effort is not None:
            params["config"] = {"model_reasoning_effort": effort}
        started = server.request("thread/start", params)
        observed = {
            field: started.get(field)
            for field in ("model", "modelProvider", "cwd", "reasoningEffort")
        }
        evidence["thread_start"] = observed
        evidence["runtime_evidence"] = validate_runtime_evidence(
            {"model": model, "cwd": str(cwd), "reasoningEffort": effort}, observed
        )
        thread = started.get("thread")
        server.request(
            "turn/start",
            {
                "threadId": thread.get("id") if isinstance(thread, dict) else None,
                "input": [{"type": "text", "text": _READY_PROMPT}],
            },
        )
        turn = server.turn()
        evidence["turn_status"] = turn.get("status")
        if turn.get("status") != "completed":
            raise _ProbeFailure(
                _failure(turn.get("error") or "the READY turn did not complete")
            )
    except _ProbeTimeout:
        return {
            **evidence,
            "status": "timeout",
            "limitation": f"the app-server probe did not finish within {timeout}s",
        }
    except _ProbeFailure as exc:
        return {
            **evidence,
            "status": "failed",
            "failure": exc.failure,
            "classification": failure_classifier.classify_runtime_failure(exc.failure),
        }
    finally:
        server.close()
    ready = evidence["model_listed"] is True and evidence["runtime_evidence"]["match"]
    return {**evidence, "status": "ready" if ready else "not-ready", "ready": ready}
