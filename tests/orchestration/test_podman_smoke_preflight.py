"""Which hosts may run the rootless Podman smoke (#811; ADR 0022 §6).

``host_problem`` reads a ``podman info --format json`` answer and ``/sys/fs/selinux/enforce``. It
needs no engine, so this module runs in every default pytest run, while the live smoke it guards
runs only with ``-m podman_smoke``. A host without cgroup delegation of cpu, memory and pids is a
blocker of the smoke, not a reason to pass it.
"""

from __future__ import annotations

import copy

from harness.json_types import JsonObject
from tests.orchestration.test_podman_smoke import host_problem

FIT: JsonObject = {
    "host": {
        "hostname": "fedora",
        "serviceIsRemote": False,
        "cgroupVersion": "v2",
        "cgroupControllers": ["cpu", "io", "memory", "pids"],
        "security": {"rootless": True, "selinuxEnabled": True},
    },
    "version": {"Version": "5.0.0"},
}


def _host(**fields: object) -> JsonObject:
    info = copy.deepcopy(FIT)
    info["host"].update(fields)
    return info


def _security(**fields: object) -> JsonObject:
    info = copy.deepcopy(FIT)
    info["host"]["security"].update(fields)
    return info


def test_a_rootless_enforcing_host_with_delegated_controllers_fits() -> None:
    assert host_problem(FIT, "1") is None


def test_a_remote_service_does_not_fit() -> None:
    assert "remote" in str(host_problem(_host(serviceIsRemote=True), "1"))


def test_a_rootful_engine_does_not_fit() -> None:
    assert "rootless" in str(host_problem(_security(rootless=False), "1"))


def test_a_host_without_selinux_does_not_fit() -> None:
    assert "SELinux" in str(host_problem(_security(selinuxEnabled=False), "1"))


def test_a_permissive_selinux_does_not_fit() -> None:
    assert "enforcing" in str(host_problem(FIT, "0"))


def test_cgroup_v1_does_not_fit() -> None:
    assert "cgroup v2" in str(host_problem(_host(cgroupVersion="v1"), "1"))


def test_a_missing_delegated_controller_is_a_blocker() -> None:
    problem = str(host_problem(_host(cgroupControllers=["memory", "pids"]), "1"))

    assert "cgroup delegation" in problem
    assert "cpu" in problem
    assert "memory" not in problem


def test_no_controller_list_means_no_delegation() -> None:
    info = copy.deepcopy(FIT)
    del info["host"]["cgroupControllers"]

    assert "lacks cpu, memory, pids" in str(host_problem(info, "1"))
