"""A local transport for native permission requests; never batch or dispatch approval."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from harness.storage import storage_path
from .core.constants import LIVE_DISPATCH_STATES
from .core.utils import CoordinatorError, JsonObject, _canonical, _sanitise
from .worker_observation import PROTOCOL, redact_visible


def _filename(request_id: str) -> str:
    return hashlib.sha256(request_id.encode()).hexdigest() + ".json"


def _root(repo: Path, dispatch: JsonObject, receipt: JsonObject) -> Path:
    log = Path(receipt.get("log_path", "")).resolve()
    if (
        receipt.get("protocol") != PROTOCOL
        or receipt.get("dispatch_id") != dispatch["dispatch_id"]
        or not log.is_relative_to(storage_path(repo, "logs", "workers").resolve())
        or log.parent.name != receipt.get("launch_id")
    ):
        raise CoordinatorError(
            "native permission channel does not match this worker",
            remedy="reconnect to the same bound runtime session",
        )
    return log.parent / "control"


def stage_request(
    root: Path, dispatch: JsonObject, launch_id: str, request: JsonObject
) -> None:
    """Called only by the runtime supervisor on a native can_use_tool control request."""
    request_id = request.get("request_id")
    if not isinstance(request_id, str) or not request_id or len(request_id) > 256:
        raise CoordinatorError(
            "invalid native request ID", remedy="inspect runtime protocol compatibility"
        )
    value = {
        "dispatch_id": dispatch["dispatch_id"],
        "launch_id": launch_id,
        "request_id": request_id,
        "request": {
            "subtype": "can_use_tool",
            "tool_name": redact_visible(
                str(request["request"].get("tool_name", "unknown"))
            )[:100],
            "input_preview": redact_visible(
                json.dumps(request["request"].get("input", {}), ensure_ascii=False)
            ),
            "input_sha256": hashlib.sha256(
                _canonical(request["request"].get("input", {})).encode()
            ).hexdigest(),
        },
    }
    path = root / "pending" / _filename(request_id)
    raw = _canonical(value).encode()
    if path.exists():
        if path.read_bytes() != raw:
            raise CoordinatorError(
                "native request ID was reused with different input",
                remedy="deny the ambiguous request and inspect the runtime",
            )
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)


def pending_requests(
    repo: Path, dispatch: JsonObject, status: JsonObject
) -> list[JsonObject]:
    receipt = status.get("adapter_receipt")
    if not isinstance(receipt, dict):
        return []
    try:
        root = _root(repo, dispatch, receipt)
    except CoordinatorError:
        return []
    requests = []
    for path in sorted((root / "pending").glob("*.json")):
        try:
            raw = path.read_bytes()
            if len(raw) > 1048576:
                continue
            value = json.loads(raw)
            request = value["request"]
            if (
                value["dispatch_id"] != dispatch["dispatch_id"]
                or value["launch_id"] != receipt["launch_id"]
                or request.get("subtype") != "can_use_tool"
            ):
                continue
            requests.append(
                {
                    "request_id": value["request_id"],
                    "request_sha256": hashlib.sha256(raw).hexdigest(),
                    "tool_name": _sanitise(str(request.get("tool_name", "unknown")))[
                        :100
                    ],
                    "input_preview": redact_visible(
                        str(request.get("input_preview", ""))
                    ),
                }
            )
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return requests[:20]


def respond(
    repo: Path,
    dispatch: JsonObject,
    status: JsonObject,
    request_id: str,
    expected_sha256: str,
    *,
    allow: bool,
) -> JsonObject:
    """Send one explicit operator choice to the native host; scope/settings stay unchanged."""
    if status.get("state") not in LIVE_DISPATCH_STATES:
        raise CoordinatorError(
            "the worker is no longer awaiting a native permission",
            remedy="refresh the same dispatch; do not replay stale permissions",
        )
    receipt = status.get("adapter_receipt")
    if not isinstance(receipt, dict):
        raise CoordinatorError(
            "worker has no native permission channel",
            remedy="use its runtime's permission interface",
        )
    root = _root(repo, dispatch, receipt)
    path = root / "pending" / _filename(request_id)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CoordinatorError(
            "native permission request is no longer pending",
            remedy="refresh the worker view",
        ) from exc
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise CoordinatorError(
            "native permission request changed",
            remedy="review the fresh request before making a new choice",
        )
    value = json.loads(raw)
    request = value["request"]
    if (
        value["dispatch_id"] != dispatch["dispatch_id"]
        or value["launch_id"] != receipt["launch_id"]
        or value["request_id"] != request_id
    ):
        raise CoordinatorError(
            "native permission request binding differs",
            remedy="reconnect to the selected dispatch",
        )
    if allow and request.get("tool_name") not in dispatch.get("allowed_tools", []):
        raise CoordinatorError(
            "native request is outside the approved tool policy",
            remedy="deny this request; a broader task needs a new proposal and approval",
        )
    response = (
        {"behavior": "allow"}
        if allow
        else {
            "behavior": "deny",
            "message": "The operator denied this native permission request.",
        }
    )
    payload = {
        "dispatch_id": dispatch["dispatch_id"],
        "launch_id": receipt["launch_id"],
        "request_id": request_id,
        "request_sha256": expected_sha256,
        "response": {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": response,
            },
        },
    }
    target = root / "responses" / _filename(request_id)
    encoded = _canonical(payload).encode()
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if target.read_bytes() != encoded:
            raise CoordinatorError(
                "a different native permission choice is already queued",
                remedy="wait for the runtime response; do not overwrite the first choice",
            ) from None
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
    return {"request_id": request_id, "state": "queued", "allow_once": allow}
