"""Coordinator как runtime-нейтральная граница: сценарий clean-room из `scripts/test_clean_room.py`."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace


def run(ctx: SimpleNamespace) -> None:
    """Coordinator как runtime-нейтральная граница.

    Читает из контекста: `orchestration_project`, `orchestration_root`, `test_root`.
    Передаёт дальше: `coordinator_path`, `coordinator_run`, `fake_adapter`, `fake_adapter_log`,
    `lifecycle_records`, `state_root`, `sync_origin_base`.
    """
    orchestration_project = ctx.orchestration_project
    orchestration_root = ctx.orchestration_root
    test_root = ctx.test_root
    # The coordinator is the runtime-neutral public seam: it owns the batch and report
    # records, and passes only an explicitly approved immutable brief to a transport adapter.
    coordinator_path = orchestration_root / "coordinator.py"
    if not coordinator_path.is_file():
        sys.exit("backend-orchestration coordinator CLI missing")

    trigger_probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib.util, sys\n"
                "from pathlib import Path\n"
                "spec = importlib.util.spec_from_file_location('coordinator', sys.argv[1])\n"
                "module = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(module)\n"
                # Executing coordinator.py aliases the installed `.harness/` tree as the
                # `harness` package, so its own subpackages import by name from here on.
                "from harness.orchestration.workflow import risk\n"
                "known = risk._risk_triggers(Path(sys.argv[2]))\n"
                "matched = set(risk._matching_triggers('queue consumer changed; transaction boundary changed', known))\n"
                "required = {'queues', 'transactions'}\n"
                "if not required <= matched:\n"
                "    raise SystemExit('singular risk evidence was not matched')\n"
            ),
            str(coordinator_path),
            str(orchestration_project),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if trigger_probe.returncode != 0:
        sys.exit(
            trigger_probe.stderr
            or trigger_probe.stdout
            or "risk trigger morphology probe failed"
        )

    state_root = orchestration_project / ".harness" / "orchestration" / "state"
    fake_adapter = test_root / "fake-adapter.py"
    fake_adapter_log = test_root / "fake-adapter-log.json"
    fake_adapter.write_text(
        """import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
Path(os.environ["FAKE_ADAPTER_LOG"]).write_text(json.dumps(args), encoding="utf-8")
if "--brief" not in args:
    raise SystemExit("missing approved brief")
brief = json.loads(Path(args[args.index("--brief") + 1]).read_text(encoding="utf-8"))
if not brief.get("coordinator_approval"):
    raise SystemExit("adapter received an unapproved brief")
print(json.dumps({"accepted": True, "dispatch_id": brief["dispatch_id"]}))
""",
        encoding="utf-8",
    )

    def coordinator_run(*arguments, env=None):
        coordinator_env = dict(os.environ, FAKE_ADAPTER_LOG=str(fake_adapter_log))
        if env:
            coordinator_env.update(env)
        # New batches are subject to the same bounded-scope preflight as production use.  The
        # lifecycle fixture is intentionally small, so give each synthetic ticket a compact
        # declared envelope unless a scenario explicitly supplies one of its own.
        arguments = list(arguments)
        if (
            "batch" in arguments
            and "create" in arguments
            and "--expected-file" not in arguments
        ):
            arguments.extend(
                [
                    "--expected-file",
                    "backend/service.py",
                    "--expected-service",
                    "backend",
                    "--expected-changed-lines",
                    "10",
                ]
            )

        def run_once(command_arguments):
            return subprocess.run(
                [
                    sys.executable,
                    str(coordinator_path),
                    "--repo",
                    str(orchestration_project),
                    *command_arguments,
                ],
                capture_output=True,
                text=True,
                env=coordinator_env,
                check=False,
            )

        # An explicit approval is bound to the transition it was shown: mirror the operator, who runs
        # `dispatch propose` with the same arguments and approves the digest it prints.
        creates = [
            i
            for i, item in enumerate(arguments[:-1])
            if item == "dispatch" and arguments[i + 1] == "create"
        ]
        if (
            creates
            and "--approved-by" in arguments
            and "--transition-digest" not in arguments
        ):
            proposal_arguments = list(arguments)
            proposal_arguments[creates[0] + 1] = "propose"
            for flag in ("--approved-by", "--approved-at"):
                if flag in proposal_arguments:
                    position = proposal_arguments.index(flag)
                    del proposal_arguments[position : position + 2]
            proposal = run_once(proposal_arguments)
            if proposal.returncode != 0:
                return proposal  # the same refusal a create would give, before anything is approved
            arguments.extend(
                [
                    "--transition-digest",
                    json.loads(proposal.stdout)["transition_digest"],
                ]
            )
        return run_once(arguments)

    def sync_origin_base(ref: str = "main") -> None:
        """Push local HEAD to origin's tracked ref so `batch create`'s mandatory fetch sees a
        current, matching tip - mirroring a freshly forked issue branch in real usage. `-f` because
        the dedicated stale-base scenario deliberately diverges origin from this checkout's history."""
        subprocess.run(
            ["git", "push", "-q", "-f", "origin", f"HEAD:refs/heads/{ref}"],
            cwd=orchestration_project,
            check=True,
        )

    def lifecycle_records(state: Path) -> Path:
        """Resolve the public ledger selector instead of reaching into a layout generation."""
        pointer = state / "ledger.json"
        if not pointer.is_file():
            return state
        return (
            state
            / "generations"
            / json.loads(pointer.read_text(encoding="utf-8"))["generation"]
        )
    ctx.coordinator_path = coordinator_path
    ctx.coordinator_run = coordinator_run
    ctx.fake_adapter = fake_adapter
    ctx.fake_adapter_log = fake_adapter_log
    ctx.lifecycle_records = lifecycle_records
    ctx.state_root = state_root
    ctx.sync_origin_base = sync_origin_base
