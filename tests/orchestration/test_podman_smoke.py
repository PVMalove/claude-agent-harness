"""Real rootless Podman smoke of the container executor on Fedora with SELinux enforcing (#811;
ADR 0022 §6, ADR 0025).

The coordinator CLI runs as its own process against the real ``harness-runtime``, real Git
worktrees, the real store and the real rootless ``podman`` on the host ``PATH``. The worker and the
QA commands run in containers of the images the podman-smoke workflow builds from
``harness/container/Dockerfile.sandbox`` (a project worker image ``FROM`` it) and
``harness/container/Dockerfile.proxy``; the worker, the egress and the QA checks are those of
``test_docker_smoke.py``. Only the self-hosted runner with the label ``fedora-selinux-podman`` runs
that workflow, because a GitHub-hosted runner gives no Fedora with SELinux.

The ``podman_smoke`` marker keeps this module out of every default pytest run: pyproject.toml
deselects it through ``addopts``, so even a node id of this module runs only together with
``-m podman_smoke``. A host that is not rootless Podman with SELinux enforcing, cgroup v2 and the
cpu, memory and pids controllers delegated to the user, or that lacks the smoke images, skips every
test with its reason; ``HARNESS_REQUIRE_PODMAN_SMOKE=1`` turns that skip into a failure. Missing
cgroup delegation is therefore a blocker of the smoke, never a silent pass.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from harness.json_types import JsonObject
from tests.orchestration.runtime_store_support import Supervised
from tests.orchestration.test_docker_smoke import (
    ADAPTER,
    ALLOWED_HOST,
    ENGINE_TIMEOUT_SECONDS,
    EVIDENCE,
    LABEL_DISPATCH,
    PROXY_TAG,
    WORKER_TAG,
    _logs,
)
from tests.orchestration.test_qa_container import ContainerQa, _stage_evidence

pytestmark = [
    pytest.mark.podman_smoke,
    pytest.mark.skipif(
        not sys.platform.startswith("linux"),
        reason="the podman executor smoke runs only on Linux",
    ),
]

REQUIRE_ENV = "HARNESS_REQUIRE_PODMAN_SMOKE"
SELINUX_ENFORCE = Path("/sys/fs/selinux/enforce")
SELINUX_XATTR = "security.selinux"
# The cgroup v2 controllers a container limit needs: --cpus, --memory and --pids-limit.
DELEGATED_CONTROLLERS = frozenset({"cpu", "memory", "pids"})
POLICY: JsonObject = {
    "defaults": {
        "mode": "sandbox",
        "container": {
            "engine": "podman",
            "worker_image": WORKER_TAG,
            "proxy_image": PROXY_TAG,
        },
        "network": {"hosts": [ALLOWED_HOST]},
    }
}


def host_problem(info: JsonObject, enforce: str) -> str | None:
    """Why the host of ``podman info`` cannot run the smoke, or ``None`` when it can.

    ``enforce`` is the text of ``/sys/fs/selinux/enforce``. The fields are the ones
    ``podman_facts`` of the runtime reads, plus the cgroup version and delegated controllers.
    """
    host = info.get("host")
    if not isinstance(host, dict):
        return "podman info has no host section"
    if host.get("serviceIsRemote") is True:
        return "the smoke needs a local Podman service, and this one is remote"
    security = host.get("security")
    security = security if isinstance(security, dict) else {}
    if security.get("rootless") is not True:
        return "the smoke verifies rootless Podman only, and this engine is rootful"
    if security.get("selinuxEnabled") is not True or enforce != "1":
        return "the smoke needs SELinux in enforcing mode"
    if host.get("cgroupVersion") != "v2":
        return "the smoke needs cgroup v2"
    controllers = host.get("cgroupControllers")
    delegated = set(controllers) if isinstance(controllers, list) else set()
    missing = sorted(DELEGATED_CONTROLLERS - delegated)
    if missing:
        return (
            f"cgroup delegation to the user lacks {', '.join(missing)}: "
            "delegate them in systemd user@.service (Delegate=)"
        )
    return None


def _podman(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["podman", *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=ENGINE_TIMEOUT_SECONDS,
    )


def _engine_problem() -> str | None:
    """Why this host cannot run the smoke, or ``None`` when it can."""
    if shutil.which("podman") is None:
        return "no podman CLI on PATH"
    try:
        info = _podman("info", "--format", "json")
        if info.returncode != 0:
            return f"podman info failed: {info.stderr.strip()[:300]}"
        enforce = (
            SELINUX_ENFORCE.read_text().strip() if SELINUX_ENFORCE.is_file() else ""
        )
        problem = host_problem(json.loads(info.stdout), enforce)
        if problem is not None:
            return problem
        for tag in (WORKER_TAG, PROXY_TAG):
            if _podman("image", "inspect", "--format", "{{.Id}}", tag).returncode:
                return f"the smoke image {tag} is missing; build it as the podman-smoke workflow does"
    except subprocess.TimeoutExpired:
        return f"podman did not answer within {ENGINE_TIMEOUT_SECONDS} seconds"
    return None


def require_engine() -> None:
    """Skip without a usable host, or fail when ``HARNESS_REQUIRE_PODMAN_SMOKE=1``."""
    problem = _engine_problem()
    if problem is None:
        return
    if os.environ.get(REQUIRE_ENV) == "1":
        pytest.fail(f"{REQUIRE_ENV}=1, but {problem}")
    pytest.skip(problem)


def labelled_objects(dispatch_id: str) -> tuple[list[str], list[str]]:
    """The containers and the networks that carry the label of ``dispatch_id``."""
    selector = f"label={LABEL_DISPATCH}={dispatch_id}"
    containers = _podman("ps", "--all", "--quiet", "--filter", selector)
    networks = _podman("network", "ls", "--quiet", "--filter", selector)
    assert containers.returncode == 0, containers.stderr
    assert networks.returncode == 0, networks.stderr
    return containers.stdout.split(), networks.stdout.split()


def wait_removed(
    dispatch_id: str, timeout: float = 60.0
) -> tuple[list[str], list[str]]:
    """What still carries the dispatch label once the runtime had ``timeout`` seconds to remove it."""
    deadline = time.monotonic() + timeout
    while True:
        left = labelled_objects(dispatch_id)
        if left == ([], []) or time.monotonic() > deadline:
            return left
        time.sleep(1)


@pytest.fixture
def labelled() -> Iterator[list[str]]:
    """Dispatch ids whose labelled containers and networks are removed after the test, so a failed
    smoke leaves nothing on the engine."""
    dispatches: list[str] = []
    yield dispatches
    for dispatch_id in dispatches:
        containers, networks = labelled_objects(dispatch_id)
        if containers:
            _podman("rm", "--force", *containers)
        if networks:
            _podman("network", "rm", *networks)


def _cgroup_files(*limits: str) -> list[str]:
    """What a rootless container started with ``limits`` reads from its own cgroup files."""
    files = (
        "/sys/fs/cgroup/memory.max",
        "/sys/fs/cgroup/pids.max",
        "/sys/fs/cgroup/cpu.max",
    )
    run = _podman(
        "run",
        "--rm",
        "--pull",
        "never",
        "--userns=keep-id",
        "--security-opt",
        "label=disable",
        *limits,
        WORKER_TAG,
        "cat",
        *files,
    )
    assert run.returncode == 0, run.stderr
    return run.stdout.splitlines()


# The container plan sets no resource limit yet (ADR 0022, «Решения реализации #811»), so the
# delegation is proven by the engine itself and not through the runtime.
def test_rootless_containers_get_the_delegated_cgroup_limits() -> None:
    require_engine()

    limited = _cgroup_files("--memory", "128m", "--pids-limit", "64", "--cpus", "0.5")
    unlimited = _cgroup_files()

    assert limited == ["134217728", "64", "50000 100000"]
    # Without the flags the same files hold no limit, so the values above come from the flags.
    assert unlimited[:2] == ["max", "max"]


def test_a_worker_reaches_only_its_allowed_host_and_writes_as_the_host_user(
    tmp_path: Path, labelled: list[str]
) -> None:
    require_engine()
    repo = Supervised(tmp_path / "worker", access_policy=POLICY, create=False)
    try:
        repo.create()
        labelled.append(repo.dispatch)
        # The artifacts root is mounted read-write at the same path, so the entrypoint is there.
        adapter = repo.repo / ".harness/.sandboxes/scratch/smoke-adapter.sh"
        adapter.write_text(ADAPTER, encoding="utf-8")
        adapter.chmod(0o755)
        mounted = (adapter, repo.repo / ".git")
        labels = [os.getxattr(path, SELINUX_XATTR) for path in mounted]

        sent = repo.run(
            "dispatch", "send", "--dispatch", repo.dispatch, "--adapter", str(adapter)
        )

        assert sent.returncode == 0, sent.stderr
        execution = json.loads(sent.stdout)["execution"]
        assert (execution["kind"], execution["engine"]) == ("container", "podman")
        ended = repo.wait_execution("exited", timeout=300)
        exit_record = ended.get("exit")
        assert isinstance(exit_record, dict), ended
        assert exit_record.get("exit_code") == 0, _logs(ended)
        evidence_file = adapter.parent / EVIDENCE
        assert evidence_file.is_file(), _logs(ended)
        evidence = json.loads(evidence_file.read_text(encoding="utf-8"))
        # --userns=keep-id maps the container user to the host user.
        stat = evidence_file.stat()
        host = (os.getuid(), os.getgid())
        assert (stat.st_uid, stat.st_gid) == host, evidence
        assert (evidence["uid"], evidence["gid"]) == tuple(str(item) for item in host)
        # ADR 0025 keeps HARNESS_EXECUTOR=docker for every engine.
        assert evidence["executor"] == "docker"
        assert evidence["proxy"].startswith(f"http://harness-{repo.dispatch}-proxy:")
        # label=disable leaves the SELinux labels of the host files as they were: no :z/:Z relabel.
        after = [os.getxattr(path, SELINUX_XATTR) for path in mounted]
        assert after == labels
        assert not any(b"container_file_t" in label for label in after), after
        # The allowed host answers through the sidecar over HTTPS and HTTP.
        allowed_https = evidence["allowed_https"]
        assert (allowed_https["exit"], allowed_https["connect"]) == (0, "200"), evidence
        assert 200 <= int(allowed_https["http"]) < 400, evidence
        assert evidence["allowed_http"]["exit"] == 0, evidence
        assert 200 <= int(evidence["allowed_http"]["http"]) < 400, evidence
        # Another host gets the sidecar's 403, for a tunnel and for a plain request.
        denied_https = evidence["denied_https"]
        assert denied_https["exit"] != 0, evidence
        assert "403" in (denied_https["connect"], denied_https["http"]), evidence
        denied_http = evidence["denied_http"]
        assert (denied_http["exit"], denied_http["http"]) == (0, "403"), evidence
        # Past the proxy the internal network has no route out.
        direct = evidence["direct"]
        assert direct["exit"] != 0 and direct["http"] == "000", evidence
        assert wait_removed(repo.dispatch) == ([], [])
    finally:
        repo.close()


def test_a_container_qa_run_reports_the_real_exit_code_of_a_failing_gate(
    tmp_path: Path, labelled: list[str]
) -> None:
    require_engine()
    identity = f"{os.getuid()}:{os.getgid()}"
    gate = [f'test "$(id -u):$(id -g)" = "{identity}"', "exit 3"]
    repo = ContainerQa(
        tmp_path / "qa",
        engine="podman",
        worker_tag=WORKER_TAG,
        proxy_tag=PROXY_TAG,
        gate=gate,
        hosts=[ALLOWED_HOST],
        scripted_engine=False,
    )
    try:
        labelled.append(repo.dispatch)

        code, result, err = repo.qa_run()

        assert code == 0 and result is not None, err
        report = repo.report()
        assert report["outcome"] == "failed", report
        assert [(c["command"], c["result"]) for c in report["checks_run"]] == [
            (gate[0], "pass"),
            (gate[1], "fail"),
        ]
        records = repo.execution()["exit"]["commands"]
        assert [(r["stage_id"], r["result"], r["exit_code"]) for r in records] == [
            ("gate-1", "ran", 0),
            ("gate-2", "ran", 3),
        ]
        stages = _stage_evidence(repo, report)["stages"]
        assert [(s["stage_id"], s["result"], s["exit_code"]) for s in stages] == [
            ("gate-1", "pass", 0),
            ("gate-2", "fail", 3),
        ]
        assert wait_removed(repo.dispatch) == ([], [])
    finally:
        repo.close()
