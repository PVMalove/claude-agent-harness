"""Переносимый контракт политики и brief: сценарий clean-room из `scripts/test_clean_room.py`."""

import json
import os
import subprocess
import sys
from types import SimpleNamespace


def run(ctx: SimpleNamespace) -> None:
    """Переносимый контракт политики и brief.

    Читает из контекста: `adapter_config`, `adapter_path`, `approved_brief`, `contract_path`,
    `fake_orca`, `fake_orca_log`, `orchestration_config`, `orchestration_project`,
    `orchestration_root`, `test_root`.
    Передаёт дальше: `adapter_run`.
    """
    adapter_config = ctx.adapter_config
    adapter_path = ctx.adapter_path
    approved_brief = ctx.approved_brief
    contract_path = ctx.contract_path
    fake_orca = ctx.fake_orca
    fake_orca_log = ctx.fake_orca_log
    orchestration_config = ctx.orchestration_config
    orchestration_project = ctx.orchestration_project
    orchestration_root = ctx.orchestration_root
    test_root = ctx.test_root
    # The portable policy contract is the shared public seam.  Model, zone and capability
    # mistakes must be rejected before a coordinator or runtime adapter can interpret them.
    contract_probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import copy, importlib, importlib.util, json, sys\n"
                "from pathlib import Path\n"
                "harness_root = Path(sys.argv[1]).parent.parent\n"
                "sys.path.insert(0, str(harness_root.parent))\n"
                "spec = importlib.util.spec_from_file_location(\n"
                "  'harness', harness_root / '__init__.py', submodule_search_locations=[str(harness_root)])\n"
                "package = importlib.util.module_from_spec(spec)\n"
                "sys.modules['harness'] = package\n"
                "spec.loader.exec_module(package)\n"
                "contract = importlib.import_module('harness.orchestration.contract')\n"
                "config = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))\n"
                "role = contract.load_role_manifest(Path(sys.argv[3]))\n"
                "for mutate in (\n"
                "  lambda value: value['assignment_plans']['developer']['runtimes']['codex'].__setitem__('model', 'invalid model'),\n"
                "  lambda value: value['assignment_plans']['developer'].__setitem__('zone', 'missing-zone'),\n"
                "  lambda value: value['provider_profiles']['backend-default'].__setitem__('capabilities', ['independent-verification']),\n"
                "):\n"
                "  invalid = copy.deepcopy(config); mutate(invalid)\n"
                "  try: contract.resolve_assignment(invalid, role, 'developer', 'backend', 'codex')\n"
                "  except contract.ContractError: pass\n"
                "  else: raise SystemExit('contract accepted invalid assignment policy')\n"
            ),
            str(contract_path),
            str(orchestration_config),
            str(orchestration_root / "roles" / "developer.md"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if contract_probe.returncode != 0:
        sys.exit(
            contract_probe.stderr
            or contract_probe.stdout
            or "shared contract policy probe failed"
        )
    brief_path = test_root / "approved-dispatch.json"
    brief_path.write_text(json.dumps(approved_brief, indent=2) + "\n", encoding="utf-8")

    def adapter_run(path, *, active=False, repo=orchestration_project):
        env = dict(
            os.environ, FAKE_ORCA_LOG=str(fake_orca_log), FAKE_ORCA_FAIL_AGENT="codex"
        )
        if active:
            env["FAKE_ORCA_ACTIVE"] = "1"
        return subprocess.run(
            [
                sys.executable,
                str(adapter_path),
                "dispatch",
                "--repo",
                str(repo),
                "--brief",
                str(path),
                "--run",
                "fake-run",
                "--orca-bin",
                str(fake_orca),
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    unapproved_brief = json.loads(json.dumps(approved_brief))
    del unapproved_brief["coordinator_approval"]
    unapproved_path = test_root / "unapproved-dispatch.json"
    unapproved_path.write_text(json.dumps(unapproved_brief), encoding="utf-8")
    if adapter_run(unapproved_path).returncode == 0:
        sys.exit("Orca adapter accepted a dispatch without coordinator approval")
    if fake_orca_log.exists():
        sys.exit(
            "Orca adapter invoked the runtime before rejecting an unapproved dispatch"
        )

    invalid_zone_brief = json.loads(json.dumps(approved_brief))
    invalid_zone_brief["zone"] = "unknown-zone"
    invalid_zone_path = test_root / "invalid-zone-dispatch.json"
    invalid_zone_path.write_text(json.dumps(invalid_zone_brief), encoding="utf-8")
    if adapter_run(invalid_zone_path).returncode == 0:
        sys.exit("Orca adapter accepted a dispatch outside its declared zone")
    if fake_orca_log.exists():
        sys.exit("Orca adapter invoked the runtime before rejecting an invalid zone")

    read_only_brief = json.loads(json.dumps(approved_brief))
    read_only_brief.update(
        {"role": "code-review", "access": "write", "write_paths": ["services/**"]}
    )
    read_only_path = test_root / "read-only-write-dispatch.json"
    read_only_path.write_text(json.dumps(read_only_brief), encoding="utf-8")
    if adapter_run(read_only_path).returncode == 0:
        sys.exit("Orca adapter gave a read-only role write access")
    if fake_orca_log.exists():
        sys.exit(
            "Orca adapter invoked the runtime before rejecting a role-mode mismatch"
        )

    first_dispatch = adapter_run(brief_path)
    if first_dispatch.returncode != 0:
        sys.exit("Orca adapter rejected an approved dispatch: " + first_dispatch.stderr)
    records_dir = orchestration_project / ".harness" / "orca-dispatches"
    records = sorted(records_dir.glob("*.json"))
    if len(records) != 1:
        sys.exit("Orca adapter did not create one immutable dispatch record")
    first_record_path = records[0]
    first_record_bytes = first_record_path.read_bytes()
    first_record = json.loads(first_record_bytes)
    if first_record["brief"] != approved_brief:
        sys.exit("Orca adapter did not preserve the approved dispatch brief verbatim")
    if first_record["resolved"] != {
        "profile": "backend-fallback",
        "agent": "codex-fallback",
        "model": "project-developer-model",
        "effort": "high",
    }:
        sys.exit(
            "Orca adapter did not record the role-configured model, effort and fallback resolution"
        )
    if first_record["terminal_outcome"] != "ready":
        sys.exit("Orca adapter did not record the Orca terminal outcome")
    fake_calls = [
        json.loads(line)
        for line in fake_orca_log.read_text(encoding="utf-8").splitlines()
    ]
    worker_calls = [call for call in fake_calls if call[1] == "worker-start"]
    if len(worker_calls) != 2:
        sys.exit("Orca adapter did not try the configured fallback exactly once")
    for call in worker_calls:
        if call[call.index("--worktree") + 1] != "new-top-level":
            sys.exit("Orca adapter did not request an isolated Orca worktree")
        if call[call.index("--base-branch") + 1] != approved_brief["branch"]:
            sys.exit("Orca adapter did not dispatch from the approved issue branch")
    if (
        worker_calls[0][worker_calls[0].index("--model") + 1]
        != "project-developer-model"
    ):
        sys.exit("Orca adapter did not pass the developer's configured model")
    if (
        worker_calls[1][worker_calls[1].index("--model") + 1]
        != "project-developer-model"
    ):
        sys.exit(
            "Orca adapter did not preserve the role-configured model through fallback"
        )
    for call in worker_calls:
        if call[call.index("--effort") + 1] != "high":
            sys.exit("Orca adapter did not pass the developer's configured effort")

    retry_dispatch = adapter_run(brief_path)
    if retry_dispatch.returncode != 0:
        sys.exit("Orca adapter rejected a valid retry: " + retry_dispatch.stderr)
    records = sorted(records_dir.glob("*.json"))
    if len(records) != 2 or first_record_path.read_bytes() != first_record_bytes:
        sys.exit("Orca adapter retry changed an immutable prior dispatch record")
    if (
        len(
            {
                json.loads(path.read_text(encoding="utf-8"))["dispatch_id"]
                for path in records
            }
        )
        != 2
    ):
        sys.exit("Orca adapter retry reused a dispatch ID")

    adapter_config["concurrency_budget"] = 1
    orchestration_config.write_text(
        json.dumps(adapter_config, indent=2) + "\n", encoding="utf-8"
    )
    calls_before_budget_rejection = len(
        fake_orca_log.read_text(encoding="utf-8").splitlines()
    )
    if adapter_run(brief_path, active=True).returncode == 0:
        sys.exit("Orca adapter exceeded the project concurrency budget")
    budget_calls = [
        json.loads(line)
        for line in fake_orca_log.read_text(encoding="utf-8").splitlines()[
            calls_before_budget_rejection:
        ]
    ]
    if [call[1] for call in budget_calls] != ["worker-list"]:
        sys.exit("Orca adapter created a task after concurrency-budget rejection")
    ctx.adapter_run = adapter_run
