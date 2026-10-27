"""External adapters receive readable approved briefs in both ledger epochs."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from harness.orchestration.ledger import ledger_ops
from tests.orchestration.runtime_store_support import (
    ServingRuntime,
    coordinator_with,
    migrate,
)
from tests.orchestration import test_recovery as recovery_support


@pytest.mark.skipif(sys.platform == "win32", reason="the runtime uses a Unix socket")
@pytest.mark.parametrize(
    "cut_over", [False, True], ids=["file-ledger", "runtime-store"]
)
@pytest.mark.parametrize(
    "conflicting_copy", [False, True], ids=["new-copy", "conflicting-copy"]
)
def test_external_adapter_gets_a_durable_copy_of_the_approved_brief(
    tmp_path: Path, cut_over: bool, conflicting_copy: bool
) -> None:
    fixture = recovery_support.RecoveryTests()
    fixture.setUp()
    runtime = None
    try:
        fixture._use_external_developer()
        config_path = fixture.repo / ".harness/orchestration.json"
        config = json.loads(config_path.read_text())
        config["assignment_plans"]["architect"]["transport"] = "external"
        config_path.write_text(json.dumps(config))
        batch = fixture._create_batch()
        created = fixture._dispatch(batch["batch_id"], "architect")["brief"]
        approved = ledger_ops._load_dispatch(fixture.state_dir, created["dispatch_id"])
        if cut_over:
            result = migrate(fixture.repo)
            assert result.returncode == 0, result.stderr
            runtime = ServingRuntime(fixture.repo)
        exported = (
            fixture.repo
            / ".harness/.sandboxes/scratch/briefs"
            / f"{approved['dispatch_id']}.json"
        )
        if conflicting_copy:
            exported.parent.mkdir(parents=True, exist_ok=True)
            exported.write_text('{"unapproved":true}\n')
        marker = tmp_path / "received.json"
        adapter = tmp_path / "adapter.py"
        adapter.write_text(
            "import json, sys\nfrom pathlib import Path\n"
            "path=Path(sys.argv[sys.argv.index('--brief')+1])\n"
            "brief=json.loads(path.read_text())\n"
            f"Path({str(marker)!r}).write_text(json.dumps("
            "{'path':str(path),'brief':brief,'cwd':str(Path.cwd())}))\n"
            "print(json.dumps({'worker_started':True,'launch_id':'fake-launch'}))\n"
        )

        result = coordinator_with(
            fixture.repo,
            runtime,
            "dispatch",
            "send",
            "--dispatch",
            approved["dispatch_id"],
            "--worktree",
            str(fixture.worktree),
            "--adapter",
            str(adapter),
        )

        if conflicting_copy and cut_over:
            assert result.returncode == 2, result.stderr
            assert "exported worker brief differs" in result.stderr
            assert exported.read_text() == '{"unapproved":true}\n'
            assert not marker.exists()
        else:
            assert result.returncode == 0, result.stderr
            received = json.loads(marker.read_text())
            assert received["brief"] == approved
            assert received["cwd"] == str(fixture.worktree.resolve())
            path = Path(received["path"])
            assert path.is_absolute() and path.is_file()
            assert json.loads(path.read_text()) == approved
            assert json.loads(result.stdout)["brief"] == str(path)
            if cut_over:
                assert path.stat().st_mode & 0o777 == 0o400
        validated = coordinator_with(
            fixture.repo, runtime, "ledger", "validate", "--full"
        )
        assert validated.returncode == 0, validated.stderr
    finally:
        if runtime is not None:
            runtime.stop()
        fixture.tearDown()
