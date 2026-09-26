"""Публичный CLI Orca-адаптера: сценарий clean-room из `scripts/test_clean_room.py`."""

import json
import sys
from types import SimpleNamespace


def run(ctx: SimpleNamespace) -> None:
    """Публичный CLI Orca-адаптера.

    Читает из контекста: `orchestration_config`, `orchestration_root`, `test_root`,
    `valid_orchestration`.
    Передаёт дальше: `adapter_config`, `adapter_path`, `approved_brief`, `contract_path`,
    `fake_orca`, `fake_orca_log`.
    """
    orchestration_config = ctx.orchestration_config
    orchestration_root = ctx.orchestration_root
    test_root = ctx.test_root
    valid_orchestration = ctx.valid_orchestration
    # The Orca adapter is an opt-in managed resource. Its public CLI must reject an
    # unapproved brief before it ever invokes the runtime boundary.
    adapter_path = orchestration_root / "orca_adapter.py"
    if not adapter_path.is_file():
        sys.exit("backend-orchestration Orca adapter missing")
    contract_path = orchestration_root / "contract.py"
    if not contract_path.is_file():
        sys.exit("backend-orchestration shared contract module missing")

    fake_orca = test_root / "fake-orca.py"
    fake_orca_log = test_root / "fake-orca-log.jsonl"
    fake_orca.write_text(
        """import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
log_path = Path(os.environ["FAKE_ORCA_LOG"])
with log_path.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")
if args[:2] != ["orchestration", args[1] if len(args) > 1 else ""]:
    raise SystemExit("expected an Orca orchestration command")
command = args[1]
if command == "worker-list":
    workers = [{"id": "existing", "state": "working"}] if os.environ.get("FAKE_ORCA_ACTIVE") else []
    print(json.dumps({"ok": True, "result": {"workers": workers}}))
elif command == "task-create":
    print(json.dumps({"ok": True, "result": {"task": {"id": "fake-task"}}}))
elif command == "worker-start":
    model = args[args.index("--model") + 1]
    agent = args[args.index("--agent") + 1]
    if agent == os.environ.get("FAKE_ORCA_FAIL_AGENT"):
        print(json.dumps({"ok": False, "error": {"code": "agent_unavailable"}}))
        raise SystemExit(1)
    print(json.dumps({"ok": True, "result": {"worker": {"id": "fake-worker"}}}))
else:
    raise SystemExit("unexpected command: " + command)
""",
        encoding="utf-8",
    )

    adapter_config = json.loads(json.dumps(valid_orchestration))
    adapter_config["provider_profiles"]["backend-default"]["fallback"] = [
        "backend-fallback"
    ]
    adapter_config["provider_profiles"]["backend-fallback"] = {
        "capabilities": adapter_config["provider_profiles"]["backend-default"][
            "capabilities"
        ],
        "agent": "codex-fallback",
        "fallback": [],
        "known_limitations": ["project-defined limitations"],
    }
    orchestration_config.write_text(
        json.dumps(adapter_config, indent=2) + "\n", encoding="utf-8"
    )

    approved_brief = {
        "ticket": "#900",
        "role": "developer",
        "access": "write",
        "zone": "backend",
        "write_paths": ["services/**"],
        "branch": "feature/issue-900-approved-dispatch",
        "worktree": "isolated-orca-worktree",
        "resolved_runtime": "codex",
        "definition_of_done": ["produce the requested backend change"],
        "prohibited_changes": ["no merge or production operations"],
        "verification_commands": [f"{sys.executable} qa_baseline.py"],
        "required_gates": ["code review"],
        "dependencies": ["approved project config"],
        "coordinator_approval": {
            "approved_by": "project coordinator",
            "approved_at": "2026-09-09T12:00:00Z",
        },
    }
    ctx.adapter_config = adapter_config
    ctx.adapter_path = adapter_path
    ctx.approved_brief = approved_brief
    ctx.contract_path = contract_path
    ctx.fake_orca = fake_orca
    ctx.fake_orca_log = fake_orca_log
