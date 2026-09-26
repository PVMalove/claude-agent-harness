"""Проверка допустимости delta-review: сценарий clean-room из `scripts/test_clean_room.py`."""

import json
import subprocess
import sys
from types import SimpleNamespace

from scripts.clean_room.support import (
    capture,
)


def run(ctx: SimpleNamespace) -> None:
    """Проверка допустимости delta-review.

    Читает из контекста: `coordinator_path`, `test_root`.
    """
    coordinator_path = ctx.coordinator_path
    test_root = ctx.test_root
    # Delta-review eligibility is a pure function of two commits and a prior review's severities:
    # a test-only fix diff that matches no risk trigger is eligible; anything else falls back to a
    # full independent review.  Probed directly, mirroring the shared contract policy probe above.
    probe_repo = test_root / "delta-probe-repo"
    probe_repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=probe_repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=probe_repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Clean Room"], cwd=probe_repo, check=True
    )
    (probe_repo / "app.py").write_text("print('base')\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=probe_repo, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "chore: baseline"], cwd=probe_repo, check=True
    )
    (probe_repo / "services").mkdir()
    (probe_repo / "services" / "handler.py").write_text(
        "def handle():\n    return 'ok'\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=probe_repo, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "feat: add handler"], cwd=probe_repo, check=True
    )
    probe_prior_sha = capture(
        ["git", "-C", str(probe_repo), "rev-parse", "HEAD"]
    ).strip()

    subprocess.run(
        ["git", "checkout", "-q", "-b", "eligible"], cwd=probe_repo, check=True
    )
    (probe_repo / "tests").mkdir(exist_ok=True)
    (probe_repo / "tests" / "test_handler.py").write_text(
        "def test_handle():\n    assert True\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=probe_repo, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "test: cover handler edge case"],
        cwd=probe_repo,
        check=True,
    )
    probe_eligible_sha = capture(
        ["git", "-C", str(probe_repo), "rev-parse", "HEAD"]
    ).strip()

    subprocess.run(
        ["git", "checkout", "-q", "-b", "nontest", probe_prior_sha],
        cwd=probe_repo,
        check=True,
    )
    (probe_repo / "services" / "handler.py").write_text(
        "def handle():\n    return 'fixed'\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=probe_repo, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "fix: adjust handler return value"],
        cwd=probe_repo,
        check=True,
    )
    probe_nontest_sha = capture(
        ["git", "-C", str(probe_repo), "rev-parse", "HEAD"]
    ).strip()

    subprocess.run(
        ["git", "checkout", "-q", "-b", "trigger", probe_prior_sha],
        cwd=probe_repo,
        check=True,
    )
    (probe_repo / "tests").mkdir(exist_ok=True)
    (probe_repo / "tests" / "test_handler_retry.py").write_text(
        "def test_retry():\n    assert True\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=probe_repo, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "test: cover concurrency-retry timing"],
        cwd=probe_repo,
        check=True,
    )
    probe_trigger_sha = capture(
        ["git", "-C", str(probe_repo), "rev-parse", "HEAD"]
    ).strip()

    known_triggers = [
        "api-public-contract",
        "schema-change",
        "data-migration",
        "outbox",
        "queues",
        "message-schema-routing",
        "transactions",
        "authorization-security",
        "concurrency-retry",
        "retry-dlq",
    ]
    delta_probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib.util, sys, json\n"
                "from pathlib import Path\n"
                "spec = importlib.util.spec_from_file_location('coordinator', sys.argv[1])\n"
                "coordinator = importlib.util.module_from_spec(spec)\n"
                "sys.modules[spec.name] = coordinator\n"
                "spec.loader.exec_module(coordinator)\n"
                # Executing coordinator.py aliases the installed `.harness/` tree as the
                # `harness` package, so its own subpackages import by name from here on.
                "from harness.orchestration.workflow import dispatch\n"
                "repo = Path(sys.argv[2])\n"
                "known = json.loads(sys.argv[3])\n"
                "prior_sha, eligible_sha, nontest_sha, trigger_sha = sys.argv[4:8]\n"
                "def severities(standards, spec_value):\n"
                "    return {'candidate_commit': 'x', 'scope': [],\n"
                "            'standards': {'severity': standards, 'findings': [], 'risks': 'r', 'blockers': 'b'},\n"
                "            'spec': {'severity': spec_value, 'findings': [], 'risks': 'r', 'blockers': 'b'}}\n"
                "prior = {'candidate_commit': prior_sha}\n"
                "axis = dispatch._delta_review_eligibility(\n"
                "    repo, {}, known, prior, {'review': severities('clean', 'warning')}, eligible_sha,\n"
                ")\n"
                "if axis != 'spec': raise SystemExit(f'unexpected delta-review axis: {axis}')\n"
                "for bad_sha, label in ((nontest_sha, 'non-test'), (trigger_sha, 'risk-trigger')):\n"
                "    try:\n"
                "        dispatch._delta_review_eligibility(\n"
                "            repo, {}, known, prior, {'review': severities('clean', 'warning')}, bad_sha,\n"
                "        )\n"
                "    except coordinator.CoordinatorError:\n"
                "        pass\n"
                "    else:\n"
                "        raise SystemExit(f'accepted an ineligible {label} delta-review')\n"
                "axis = dispatch._delta_review_eligibility(\n"
                "    repo, {}, known, prior, {'review': severities('clean', 'blocker')}, eligible_sha,\n"
                ")\n"
                "if axis != 'spec': raise SystemExit(f'unexpected blocker delta-review axis: {axis}')\n"
                "try:\n"
                "    dispatch._delta_review_eligibility(\n"
                "        repo, {}, known, prior, {'review': severities('warning', 'warning')}, eligible_sha,\n"
                "    )\n"
                "except coordinator.CoordinatorError:\n"
                "    pass\n"
                "else:\n"
                "    raise SystemExit('accepted a delta-review without inherited Standards=Clean')\n"
            ),
            str(coordinator_path),
            str(probe_repo),
            json.dumps(known_triggers),
            probe_prior_sha,
            probe_eligible_sha,
            probe_nontest_sha,
            probe_trigger_sha,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if delta_probe.returncode != 0:
        sys.exit(
            "delta-review eligibility probe failed: "
            + (delta_probe.stderr or delta_probe.stdout)
        )

    print("delta-review eligibility probe passed")
