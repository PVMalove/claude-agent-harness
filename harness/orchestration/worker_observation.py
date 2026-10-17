"""Passive local worker evidence. No model calls, process control or lifecycle decisions."""

from __future__ import annotations

import socket
import os
import re
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from harness.orchestration.core.utils import JsonObject

PROTOCOL = "harness-worker-v1"
STARTUP_TIMEOUT_SECONDS = 120


def process_identity(pid: int) -> JsonObject | None:
    """Linux PID plus boot/start identity; other hosts remain explicitly unobserved."""
    if not sys.platform.startswith("linux") or isinstance(pid, bool) or pid < 1:
        return None
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {
            "pid": pid,
            "host": socket.gethostname(),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "start_time": fields[19],
            "pid_namespace": os.readlink(f"/proc/{pid}/ns/pid"),
        }
    except (OSError, IndexError, ValueError):
        return None


def observe_process(receipt: object, dispatch_id: object) -> JsonObject | None:
    """Only a bound, local process identity can prove termination. Reused PIDs stay unknown."""
    if not isinstance(receipt, Mapping) or receipt.get("protocol") != PROTOCOL:
        return None
    unknown = {
        "state": "unknown",
        "source": "local-process",
        "reason": "unverified process identity",
    }
    if receipt.get("dispatch_id") != dispatch_id or not receipt.get("launch_id"):
        return {**unknown, "reason": "launch receipt does not match dispatch"}
    identity = receipt.get("process")
    if not isinstance(identity, Mapping):
        return unknown
    pid = identity.get("pid")
    if (
        not sys.platform.startswith("linux")
        or identity.get("host") != socket.gethostname()
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid < 1
        or not isinstance(identity.get("start_time"), str)
        or not str(identity.get("start_time", "")).isdigit()
        or not isinstance(identity.get("boot_id"), str)
    ):
        return unknown
    try:
        if identity.get("pid_namespace") != os.readlink("/proc/self/ns/pid"):
            return {**unknown, "reason": "worker belongs to another PID namespace"}
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if boot != identity["boot_id"]:
            return {**unknown, "reason": "host rebooted since launch"}
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        except FileNotFoundError:
            return {
                "state": "stopped",
                "source": "local-process",
                "reason": "bound process no longer exists",
            }
        if fields[19] != identity["start_time"]:
            return {**unknown, "reason": "PID belongs to another process"}
        return {
            "state": "stopped" if fields[0] in {"Z", "X"} else "live",
            "source": "local-process",
            "reason": "bound process exited"
            if fields[0] in {"Z", "X"}
            else "bound process exists",
        }
    except (OSError, ValueError, IndexError):
        return {**unknown, "reason": "process observation is unavailable"}


def observe_worker(
    receipt: object, dispatch_id: object, *, repo: Path | None = None
) -> JsonObject | None:
    """Prefer a bound supervisor exit result; otherwise require local kernel evidence."""
    if (
        repo is not None
        and isinstance(receipt, dict)
        and receipt.get("dispatch_id") == dispatch_id
    ):
        terminal = read_terminal(repo, receipt)
        if terminal is not None:
            return {
                "state": "stopped",
                "source": "runtime-supervisor",
                "reason": "bound runtime exit was recorded",
                **terminal,
            }
    return observe_process(receipt, dispatch_id)


def redact_visible(text: str) -> str:
    """Scrub configured secret values as well as common credential assignments before storage."""
    from harness.orchestration.core.utils import _sanitise

    secret_key = re.compile(
        r"(?:token|secret|password|credential|api.?key|authorization)", re.I
    )
    for key, value in os.environ.items():
        if len(value) >= 4 and secret_key.search(key):
            text = text.replace(value, "<redacted>")
    text = re.sub(
        r"(?i)((?:[\w-]*(?:api[_-]?key|token|secret|password|authorization)[\w-]*)[\"']?\s*[:=]\s*[\"']?)([^\s,;\"'}]+)",
        r"\1<redacted>",
        text,
    )
    return _sanitise(text)[:8192]


def visible_events(record: object) -> list[JsonObject]:
    """Allowlisted stream-json text/tool names only; never thinking, inputs or raw JSON."""
    if not isinstance(record, dict):
        return []

    def row(kind: str, text: object) -> list[JsonObject]:
        return (
            [{"kind": kind, "text": redact_visible(text)}]
            if isinstance(text, str) and text
            else []
        )

    kind = record.get("type")
    if kind == "harness-event" and record.get("kind") in {
        "output",
        "tool",
        "tool-result",
        "result",
        "error",
        "system",
        "permission",
        "stderr",
    }:
        return row(record["kind"], record.get("text"))
    if kind in {"assistant", "user"}:
        message = record.get("message", {})
        content = message.get("content", []) if isinstance(message, dict) else []
        events: list[JsonObject] = []
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    events.extend(row("output", block.get("text")))
                elif block.get("type") == "tool_use":
                    events.extend(row("tool", block.get("name")))
                elif block.get("type") == "tool_result" and isinstance(
                    block.get("content"), str
                ):
                    events.extend(row("tool-result", block["content"]))
        return events
    if kind == "result":
        return row(
            "error" if record.get("is_error") else "result", record.get("result")
        )
    if kind == "system" and record.get("subtype") == "init":
        return row("system", "Runtime session connected")
    if (
        kind == "control_request"
        and isinstance(record.get("request"), dict)
        and record["request"].get("subtype") == "can_use_tool"
    ):
        return row(
            "permission",
            "Native permission required: "
            + str(record["request"].get("tool_name", "unknown")),
        )
    if kind == "permission_request":
        return row(
            "permission",
            "Native runtime permission request; respond through the runtime",
        )
    # Partial text duplicates the final assistant message. In particular, thinking deltas and
    # provider metadata are not a second channel to display hidden reasoning or credentials.
    return []


def read_events(repo: Path, receipt: object, offset: int = 0) -> JsonObject:
    """Bounded incremental local log reader; never interprets output as executable commands."""
    import json
    from harness.storage import storage_path

    empty: JsonObject = {
        "events": [],
        "next_offset": offset,
        "stream_state": "unavailable",
        "reset": False,
    }
    if not isinstance(receipt, dict) or not isinstance(receipt.get("log_path"), str):
        return {**empty, "stream_reason": "adapter supplied no structured event log"}
    path = Path(receipt["log_path"]).resolve()
    allowed = storage_path(repo, "logs").resolve()
    if not path.is_relative_to(allowed):
        return {**empty, "stream_reason": "event log is outside shared logs storage"}
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            start = offset if offset <= size else 0
            reset = offset > size
            if size - start > 65536:
                start = size - 65536
                reset = True
            stream.seek(start)
            if start and reset:
                stream.readline(
                    65536
                )  # never parse the middle of an oversized/rotated record
            raw = stream.read(65536)
            complete = raw.rfind(b"\n") + 1
            next_offset = stream.tell() - len(raw) + complete
        events: list[JsonObject] = []
        for line in raw[:complete].splitlines():
            try:
                events.extend(visible_events(json.loads(line)))
            except (ValueError, UnicodeDecodeError):
                continue  # incomplete/foreign output is not trusted text to expose
        return {
            "events": events[-200:],
            "next_offset": next_offset,
            "stream_state": "connected" if size else "waiting",
            "reset": reset,
            "log_path": str(path),
            "last_event_age_seconds": max(0, int(time.time() - path.stat().st_mtime))
            if size
            else None,
        }
    except OSError:
        return {
            **empty,
            "stream_reason": "event stream is missing or unreadable; worker outcome is independent",
        }


def read_terminal(repo: Path, receipt: object) -> JsonObject | None:
    """A supervisor's bound exit result is separate from a completion report or its acceptance."""
    import json
    from harness.storage import storage_path

    if (
        not isinstance(receipt, dict)
        or receipt.get("protocol") != PROTOCOL
        or not isinstance(receipt.get("terminal_path"), str)
    ):
        return None
    terminal = Path(receipt["terminal_path"]).resolve()
    log = Path(receipt.get("log_path", "")).resolve()
    if (
        not terminal.is_relative_to(storage_path(repo, "logs", "workers").resolve())
        or terminal.parent != log.parent
        or terminal.name != "terminal.json"
        or terminal.parent.name != receipt.get("launch_id")
    ):
        return None
    try:
        with terminal.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            return None
        record = json.loads(raw)
        code = record.get("exit_code")
        if (
            record.get("dispatch_id") != receipt.get("dispatch_id")
            or record.get("launch_id") != receipt.get("launch_id")
            or not isinstance(code, int)
            or isinstance(code, bool)
        ):
            return None
        return {"exit_code": code, "source": "runtime-supervisor"}
    except (OSError, ValueError, TypeError, AttributeError):
        return None
