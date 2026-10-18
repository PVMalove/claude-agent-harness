"""Scripted app-server for the runtime-probe tests: JSON lines on stdio, behaviour set by --mode.

Modes: ``ready`` completes the READY turn; ``capacity`` fails it with ``serverOverloaded`` after
``thread/start``; ``mismatch`` starts the thread on another model; ``missing-cwd`` omits the thread's
``cwd``; ``silent`` never finishes the turn; ``exit`` quits before answering.
"""

from __future__ import annotations

import json
import sys

MODE = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else "ready"


def _send(message: dict[str, object]) -> None:
    print(json.dumps(message), flush=True)


def main() -> None:
    if MODE == "exit":
        sys.stderr.write("not logged in\n")
        raise SystemExit(1)
    for line in sys.stdin:
        message = json.loads(line)
        method, request_id = message.get("method"), message.get("id")
        params = message.get("params") or {}
        if method == "initialize":
            _send({"id": request_id, "result": {"userAgent": "fake"}})
        elif method == "model/list":
            _send(
                {
                    "id": request_id,
                    "result": {"data": [{"id": "sonnet", "model": "sonnet"}]},
                }
            )
        elif method == "thread/start":
            result = {
                "thread": {"id": "thread-1"},
                "model": "opus" if MODE == "mismatch" else params.get("model"),
                "modelProvider": "fake",
                "cwd": params.get("cwd"),
                "reasoningEffort": (params.get("config") or {}).get(
                    "model_reasoning_effort"
                ),
            }
            if MODE == "missing-cwd":
                del result["cwd"]
            _send({"id": request_id, "result": result})
        elif method == "turn/start":
            _send(
                {
                    "id": request_id,
                    "result": {"turn": {"id": "turn-1", "status": "inProgress"}},
                }
            )
            if MODE == "capacity":
                _send(
                    {
                        "method": "error",
                        "params": {
                            "error": {
                                "message": "Selected model is at capacity",
                                "codexErrorInfo": "serverOverloaded",
                            },
                            "willRetry": False,
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                        },
                    }
                )
            elif MODE != "silent":
                _send(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turn": {"id": "turn-1", "status": "completed"},
                        },
                    }
                )


if __name__ == "__main__":
    main()
