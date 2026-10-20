"""Built-in ``docker`` runtime_access: an approved external dispatch in its own container.

The executor runs only what the immutable brief approved: the resolved plan's resources at the same
absolute paths, the network the plan names, and the image digests that ``dispatch propose`` pinned.
It never builds or pulls an image and never approves anything on the human's behalf (ADR 0018).
Every engine command goes through ``run_engine``; nothing else here starts a process.

An empty ``network.hosts`` runs the worker with ``--network none``. A non-empty list runs it on an
``--internal`` network whose only peer is a per-dispatch egress-proxy sidecar (issue #706): the
sidecar is attached to the default bridge too and enforces the host allowlist for HTTP(S). The
sidecar and its network outlive the worker until the terminal teardown of issue #452 removes them
by their labels.

The engine is Docker or Podman, named by the plan and checked by one read-only ``<engine> info``
in ``observe``: rootful engines run the worker as the host ``uid:gid``, rootless Podman with
``--userns=keep-id`` and rootless Docker without a user flag (its container root is the host user),
so files keep the host owner. An engine that reports SELinux gets ``label=disable``; host files are
never relabeled. Remote Podman, Docker user namespace remapping, an info answer that does not match
the engine and an in-process transport each raise ``RuntimeAccessBlocked`` with its remedy.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import NoReturn, cast

from ..errors import INTERNAL_INVARIANT_REMEDY
from . import extensions
from .core.utils import JsonObject

EXTENSION_NAME = "docker"
# One local engine query or container creation; a stuck daemon is the worst expected case.
ENGINE_TIMEOUT_SECONDS = 30
# The worker's HOME is a tmpfs, so no host home directory is mounted and any image works.
CONTAINER_HOME = "/harness-home"
# A project adapter reads this marker to set its agent's flags for a container run (ADR 0018).
EXECUTOR_ENV = ("HARNESS_EXECUTOR", EXTENSION_NAME)
# The worker environment the executor sets itself; preflight and send block a plan name that
# would replace it.
FIXED_ENV = (
    ("HOME", CONTAINER_HOME),
    # Docker creates the parents of nested mount points as root; Git must still trust them.
    ("GIT_CONFIG_COUNT", "1"),
    ("GIT_CONFIG_KEY_0", "safe.directory"),
    ("GIT_CONFIG_VALUE_0", "*"),
    EXECUTOR_ENV,
)
RESERVED_ENV_NAMES = frozenset(name for name, _ in FIXED_ENV)
# The egress sidecar listens here; its allowlist reaches it in this variable.
PROXY_PORT = 3128
ALLOWED_HOSTS_ENV = "HARNESS_EGRESS_ALLOWED_HOSTS"
# With a host allowlist the executor points the worker at the sidecar through these names, so a
# plan may not set them; an offline plan still may.
PROXY_URL_ENV_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
PROXY_BYPASS_ENV_NAMES = ("NO_PROXY", "no_proxy")
PROXY_ENV_NAMES = frozenset(PROXY_URL_ENV_NAMES + PROXY_BYPASS_ENV_NAMES)
# Loopback stays inside the worker's own container, so it never needs the proxy.
PROXY_BYPASS = "localhost,127.0.0.1,::1"
# ``inherit`` asks for no particular mode; the container isolates the worker either way.
SUPPORTED_MODES = ("inherit", "sandbox")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
# Podman prints an image id without the ``sha256:`` prefix.
_BARE_IMAGE_ID = re.compile(r"[0-9a-f]{64}")
# The network a sidecar joins to reach the outside: the engine's default one.
DEFAULT_NETWORKS = {"docker": "bridge", "podman": "podman"}
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
# A comma, quote or line break would let a root inject options into its ``--mount`` value.
_UNSAFE_PATH = re.compile(r'[,"\n\r]')

# An approval binds the pinned digests, so a rebuilt image needs a new propose, never the old one.
CHANGED_IMAGE_REMEDY = (
    "restore the image with the pinned digest, or rebuild or pull the image, cancel this "
    "dispatch (dispatch cancel) and repeat dispatch propose: an approval is never reused "
    "for another image"
)

_Block = Callable[[str, str], None]


def run_engine(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run one container-engine command: the executor's only process boundary."""
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        check=False,
        timeout=ENGINE_TIMEOUT_SECONDS,
    )


def _ask(argv: Sequence[str]) -> tuple[bool, str, bool]:
    """Run one engine command: whether it succeeded, its output or failure detail, and whether the
    engine answered at all. A CLI that could not start or did not answer in time never answered."""
    try:
        result = run_engine(argv)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc), False
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        return False, detail or f"exit code {result.returncode}", True
    return True, result.stdout.strip(), True


def _engine(argv: Sequence[str]) -> tuple[bool, str]:
    """Whether one engine command succeeded, with its output or the failure detail."""
    ok, detail, _ = _ask(argv)
    return ok, detail


@dataclass(frozen=True)
class ContainerObservation(extensions.RuntimeAccessObservation):
    """Worker evidence of the docker executor: the engine, its mode and the image digests."""

    engine: str = EXTENSION_NAME
    engine_mode: str = "not-probed"
    selinux: bool = False
    worker_image: str | None = None
    worker_image_id: str | None = None
    proxy_image: str | None = None
    proxy_image_id: str | None = None


def pins(observation: ContainerObservation) -> JsonObject:
    """The brief's ``runtime_images``: the image ids observed now, the only images a send runs."""
    return {"worker": observation.worker_image_id, "proxy": observation.proxy_image_id}


def valid_pins(images: object, container: Mapping[str, object]) -> bool:
    """Whether ``images`` pins exactly the images the plan's container component names."""
    return (
        isinstance(images, dict)
        and set(images) == {"worker", "proxy"}
        and isinstance(images["worker"], str)
        and _IMAGE_ID.fullmatch(images["worker"]) is not None
        and (
            isinstance(images["proxy"], str)
            and _IMAGE_ID.fullmatch(images["proxy"]) is not None
            if "proxy_image" in container
            else images["proxy"] is None
        )
    )


def container_name(dispatch_id: str) -> str:
    """The name of a dispatch's container: a failed launch removes exactly this object."""
    return f"harness-{dispatch_id}"


def proxy_name(dispatch_id: str) -> str:
    """The name of a dispatch's egress-proxy sidecar, also its DNS name on the internal network."""
    return f"{container_name(dispatch_id)}-proxy"


def network_name(dispatch_id: str) -> str:
    """The name of a dispatch's internal network."""
    return f"{container_name(dispatch_id)}-net"


def plan_root(plan: Mapping[str, object], resource: str) -> str:
    return str(
        next(
            i["path"]
            for i in cast(JsonObject, plan)["requirements"]
            if i["resource"] == resource
        )
    )


def _labels(brief: Mapping[str, object]) -> list[str]:
    """The labels of every object a dispatch creates: a teardown removes them all by filter."""
    data = cast(JsonObject, brief)
    plan = data["runtime_access"]
    argv: list[str] = []
    for entry in (
        f"harness.dispatch={data['dispatch_id']}",
        f"harness.repository={plan_root(plan, 'git_common')}",
        f"harness.plan_digest={plan['plan_digest']}",
    ):
        argv += ["--label", entry]
    return argv


def mounts(requirements: Sequence[Mapping[str, object]]) -> list[tuple[str, str]]:
    """One bind mount per plan root, read-write when any requirement writes it.

    Parents come before the roots nested in them, so a nested mount is never hidden.
    """
    access: dict[str, str] = {}
    for item in requirements:
        path, mode = str(item["path"]), str(item["access"])
        if access.get(path) != "write":
            access[path] = mode
    return sorted(
        access.items(), key=lambda pair: (len(PurePosixPath(pair[0]).parts), pair[0])
    )


def identity_argv(observation: ContainerObservation, *, worker: bool) -> list[str]:
    """The options that keep host file ownership for the observed engine mode, then SELinux.

    A rootful engine runs the host ``uid:gid``. Rootless Podman keeps the host id with
    ``--userns=keep-id``. Rootless Docker maps container root to the host user, so the worker pins
    ``--user 0:0`` instead of trusting the image's ``USER``; the sidecar keeps its own image user.
    An engine that reports SELinux gets ``label=disable``; host files are never relabeled.
    """
    if observation.engine_mode != "rootless":
        argv = ["--user", f"{os.getuid()}:{os.getgid()}"]
    elif observation.engine == "podman":
        argv = ["--userns=keep-id"]
    else:
        argv = ["--user", "0:0"] if worker else []
    if observation.selinux:
        argv += ["--security-opt", "label=disable"]
    return argv


def create_argv(
    brief: Mapping[str, object],
    command: Sequence[str],
    observation: ContainerObservation,
) -> list[str]:
    """The exact ``<engine> create`` of the approved worker: plan roots only, no direct network.

    Without hosts the worker has no network. With hosts it joins only the dispatch's internal
    network and reaches the sidecar by name through the proxy variables. A plan's environment names
    become ``--env NAME`` without a value: the engine reads each value from its own inherited
    environment, so no value enters the argv.
    """
    data = cast(JsonObject, brief)
    plan = data["runtime_access"]
    dispatch_id = str(data["dispatch_id"])
    hosts = plan["network"]["hosts"]

    argv = [
        observation.engine,
        "create",
        # The default would pull a missing image; the harness never builds or pulls one.
        "--pull",
        "never",
        "--name",
        container_name(dispatch_id),
        "--network",
        network_name(dispatch_id) if hosts else "none",
        *identity_argv(observation, worker=True),
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        f"{CONTAINER_HOME}:rw,exec,nosuid,nodev,mode=1777",
    ]
    for pair in FIXED_ENV:
        argv += ["--env", "=".join(pair)]
    if hosts:
        url = f"http://{proxy_name(dispatch_id)}:{PROXY_PORT}"
        for name in PROXY_URL_ENV_NAMES:
            argv += ["--env", f"{name}={url}"]
        for name in PROXY_BYPASS_ENV_NAMES:
            argv += ["--env", f"{name}={PROXY_BYPASS}"]
    for name in plan.get("environment", []):
        argv += ["--env", name]
    argv += [*_labels(brief), "--workdir", plan_root(plan, "checkout")]
    for path, access in mounts(plan["requirements"]):
        argv += [
            "--mount",
            f"type=bind,source={path},target={path}"
            + ("" if access == "write" else ",readonly"),
        ]
    # The image is the pinned id, never the tag: a retag after approval cannot change it.
    argv += ["--entrypoint", command[0], data["runtime_images"]["worker"], *command[1:]]
    return argv


def network_argv(brief: Mapping[str, object], engine: str) -> list[str]:
    """The ``<engine> network create`` of the dispatch's internal network: no route out of it."""
    dispatch_id = str(brief["dispatch_id"])
    return [
        engine,
        "network",
        "create",
        "--internal",
        *_labels(brief),
        network_name(dispatch_id),
    ]


def proxy_argv(
    brief: Mapping[str, object], observation: ContainerObservation
) -> list[str]:
    """The ``<engine> create`` of the egress sidecar from its pinned digest, on the internal network.

    The allowlist is the plan's host list: validated DNS names, so it is not a secret and may
    appear in the argv. The sidecar needs no capability and writes nothing.
    """
    data = cast(JsonObject, brief)
    plan = data["runtime_access"]
    dispatch_id = str(data["dispatch_id"])
    return [
        observation.engine,
        "create",
        "--pull",
        "never",
        "--name",
        proxy_name(dispatch_id),
        "--network",
        network_name(dispatch_id),
        *identity_argv(observation, worker=False),
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--env",
        f"{ALLOWED_HOSTS_ENV}={','.join(plan['network']['hosts'])}",
        *_labels(brief),
        data["runtime_images"]["proxy"],
    ]


class _Launch:
    """A staged launch: it records every engine object it attempts and removes them all on failure.

    An object is recorded before its creating command runs, because a command that failed may
    still have left it behind.
    """

    def __init__(self, engine: str) -> None:
        self._engine = engine
        self._objects: list[tuple[str, str]] = []

    def run(
        self,
        label: str,
        argv: list[str],
        *,
        creates: tuple[str, str] | None = None,
        container_id: bool = False,
    ) -> str:
        """Run one stage and return its output; a failed stage rolls back the whole launch."""
        if creates is not None:
            self._objects.append(creates)
        ok, output = _engine(argv)
        if not ok:
            self._fail(f"{label} failed: {output}")
        if container_id and not _CONTAINER_ID.fullmatch(output):
            self._fail(f"{label} returned no container id: {output!r}")
        return output

    def _fail(self, failure: str) -> NoReturn:
        # Removal is best effort, newest object first: its results only change the evidence,
        # never the refusal.
        results = []
        for kind, name in reversed(self._objects):
            removed, detail = _engine(
                [self._engine, "rm", "--force", name]
                if kind == "container"
                else [self._engine, "network", "rm", name]
            )
            results.append(
                f"{kind} {name} was removed"
                if removed
                else f"removing {kind} {name} failed: {detail}"
            )
        facts: JsonObject = {
            "objects": [f"{kind} {name}" for kind, name in self._objects]
        }
        containers = [name for kind, name in self._objects if kind == "container"]
        if containers:
            facts["container"] = containers[-1]
        raise extensions.RuntimeAccessBlocked(
            "; ".join([failure, *results]),
            remedy="fix the engine error, for example a missing plan root or an adapter outside "
            "the plan's mounts, and send the dispatch again: it stays approved",
            facts=facts,
        )


def _launch(
    brief: Mapping[str, object],
    command: Sequence[str],
    observation: ContainerObservation,
) -> str:
    """Create and start the dispatch's containers detached; any failure removes what it created.

    The sidecar comes first and only when the plan has hosts: its internal network, then the
    sidecar attached to it, then the default bridge, then its start. The worker is last.
    """
    data = cast(JsonObject, brief)
    dispatch_id = str(data["dispatch_id"])
    engine = observation.engine
    launch = _Launch(engine)
    if data["runtime_access"]["network"]["hosts"]:
        launch.run(
            f"{engine} network create",
            network_argv(brief, engine),
            creates=("network", network_name(dispatch_id)),
        )
        proxy_id = launch.run(
            f"{engine} create (egress proxy)",
            proxy_argv(brief, observation),
            creates=("container", proxy_name(dispatch_id)),
            container_id=True,
        )
        launch.run(
            f"{engine} network connect",
            [engine, "network", "connect", DEFAULT_NETWORKS[engine], proxy_id],
        )
        launch.run(f"{engine} start (egress proxy)", [engine, "start", proxy_id])
    container_id = launch.run(
        f"{engine} create",
        create_argv(brief, command, observation),
        creates=("container", container_name(dispatch_id)),
        container_id=True,
    )
    launch.run(f"{engine} start", [engine, "start", container_id])
    return container_id


# The engine's own wording for an object it does not know (docker and podman): "No such container:
# x", "No such network: x", "network x not found". A bare "no such" is not enough: a daemon that
# cannot be reached says "... connect: no such file or directory".
_NO_SUCH = re.compile(
    r"\bno such (?:container|network|object)\b|\b(?:container|network) \S+ not found\b",
    re.IGNORECASE,
)
_RUNNING_STATUSES = frozenset({"running", "paused", "restarting"})
_STOPPED_STATUSES = frozenset({"exited", "dead"})


def _gone(detail: str, answered: bool) -> bool:
    """Whether the engine itself said the object does not exist, as opposed to any other failure.

    A failure to run the engine CLI (``No such file or directory: 'docker'``) is not an answer, so
    its text never counts, however it is worded. Neither is the CLI's failure to reach the daemon:
    its text may say "no such file or directory" for the socket, but it names no missing object.
    """
    return answered and _NO_SUCH.search(detail) is not None


def inspect_worker(launch_id: str) -> JsonObject:
    """What the engine says about one worker container: the only proof that it stopped.

    ``state`` is ``running``, ``exited``, ``missing`` (the engine has no such container) or
    ``unknown`` (an engine failure, an unreadable answer or a state that proves nothing). Only
    ``exited`` and ``missing`` are stop proofs. The labels are returned so that the caller can bind
    the answer to the dispatch and repository it expects.
    """
    result: JsonObject = {
        "state": "unknown",
        "exit_code": None,
        "finished_at": None,
        "oom_killed": None,
        "labels": None,
    }
    ok, output, answered = _ask(
        ["docker", "container", "inspect", "--format", "{{json .}}", launch_id]
    )
    if not ok:
        return {**result, "state": "missing" if _gone(output, answered) else "unknown"}
    try:
        info = json.loads(output)
        engine_state = info["State"]
        status = engine_state["Status"]
        labels = info["Config"]["Labels"] or {}
    except (ValueError, KeyError, TypeError):
        return result
    if not isinstance(status, str) or not isinstance(labels, dict):
        return result
    result["labels"] = labels
    if status in _RUNNING_STATUSES:
        return {**result, "state": "running"}
    if status in _STOPPED_STATUSES:
        exit_code = engine_state.get("ExitCode")
        return {
            **result,
            "state": "exited",
            "exit_code": exit_code if isinstance(exit_code, int) else None,
            "finished_at": engine_state.get("FinishedAt"),
            "oom_killed": engine_state.get("OOMKilled"),
        }
    return result


def _listed_ids(argv: list[str], filters: list[str]) -> list[str] | None:
    """The ids an engine ``ps`` or ``network ls`` lists for the label filters; ``None`` on failure."""
    ok, output = _engine(
        [*argv, "--quiet", "--no-trunc", *[a for f in filters for a in ("--filter", f)]]
    )
    return output.split() if ok else None


def _labelled(
    kind: str,
    command: list[str],
    ids: list[str],
    repository: str,
    dispatch_id: str | None,
) -> list[JsonObject] | None:
    """The inspected objects whose own labels name this repository (and dispatch), or ``None``.

    The engine filter only narrows the search: every object is checked again against its own
    labels, so an object of another repository is never returned.
    """
    if not ids:
        return []
    ok, output = _engine([*command, *ids])
    try:
        inspected = json.loads(output) if ok else None
    except ValueError:
        inspected = None
    if not isinstance(inspected, list):
        return None
    found: list[JsonObject] = []
    for item in inspected:
        if not isinstance(item, dict):
            continue
        container = kind == "container"
        labels = (
            (item.get("Config") or {}).get("Labels")
            if container
            else item.get("Labels")
        )
        if not isinstance(labels, dict):
            continue
        dispatch = labels.get("harness.dispatch")
        name = str(item.get("Name", "")).removeprefix("/")
        role = {
            container_name(str(dispatch)): "worker",
            proxy_name(str(dispatch)): "proxy",
            network_name(str(dispatch)): "network",
        }.get(name)
        if (
            labels.get("harness.repository") != repository
            or not isinstance(dispatch, str)
            or (dispatch_id is not None and dispatch != dispatch_id)
            or role is None
            or (role == "network") == container
        ):
            continue
        found.append(
            {
                "kind": kind,
                "id": str(item.get("Id", "")),
                "name": name,
                "dispatch": dispatch,
                "role": role,
                "state": str(item["State"]["Status"]) if container else "present",
            }
        )
    return found


def engine_objects(
    repository: str, dispatch_id: str | None = None
) -> list[JsonObject] | None:
    """The containers and networks the harness created for ``repository`` (one dispatch if named).

    ``None`` means the engine could not answer, which is never the same as "nothing is there".
    """
    filters = [f"label=harness.repository={repository}"]
    if dispatch_id is not None:
        filters.append(f"label=harness.dispatch={dispatch_id}")
    containers = _listed_ids(["docker", "ps", "--all"], filters)
    networks = _listed_ids(["docker", "network", "ls"], filters)
    if containers is None or networks is None:
        return None
    found_containers = _labelled(
        "container",
        ["docker", "container", "inspect"],
        containers,
        repository,
        dispatch_id,
    )
    found_networks = _labelled(
        "network", ["docker", "network", "inspect"], networks, repository, dispatch_id
    )
    if found_containers is None or found_networks is None:
        return None
    return [*found_containers, *found_networks]


_ROLE_ORDER = {"worker": 0, "proxy": 1, "network": 2}


def _remove(argv: list[str], result: JsonObject, label: str) -> bool:
    """Run one removal; an object that is already gone counts as removed. Whether it is now gone."""
    ok, detail, answered = _ask(argv)
    if ok:
        result["removed"].append(label)
    elif _gone(detail, answered):
        result["missing"].append(label)
    else:
        result["failed"].append({"object": label, "reason": detail})
        return False
    return True


def remove_objects(objects: Sequence[Mapping[str, object]]) -> JsonObject:
    """Remove the given engine objects dispatch by dispatch: worker, then sidecar, then network.

    The worker goes by a plain ``docker rm``: the engine itself refuses a running container, so no
    race can stop a live worker. The sidecar is force-removed only once its dispatch's worker is
    gone. A network is removed only after the containers on it, and an object that is already
    absent is reported as ``missing``, which makes a repeated run safe.
    """
    result: JsonObject = {"removed": [], "missing": [], "failed": []}
    by_dispatch: dict[str, list[Mapping[str, object]]] = {}
    for item in objects:
        by_dispatch.setdefault(str(item["dispatch"]), []).append(item)
    for group in by_dispatch.values():
        gone = True
        for item in sorted(group, key=lambda entry: _ROLE_ORDER[str(entry["role"])]):
            label = f"{item['kind']} {item['name']}"
            if not gone:
                result["failed"].append(
                    {
                        "object": label,
                        "reason": "an earlier object of this dispatch is still present",
                    }
                )
                continue
            identity = str(item["id"])
            argv = {
                "worker": ["docker", "rm", identity],
                "proxy": ["docker", "rm", "--force", identity],
                "network": ["docker", "network", "rm", identity],
            }[str(item["role"])]
            gone = _remove(argv, result, label)
    return result


def _overlaps_home(path: str) -> bool:
    home = PurePosixPath(CONTAINER_HOME)
    root = PurePosixPath(path)
    return root == home or home in root.parents or root in home.parents


def _docker_facts(info: object) -> tuple[str, bool, str] | None:
    """The mode, SELinux flag and daemon identity in a Docker answer; ``None`` if it is not one."""
    if (
        not isinstance(info, dict)
        or not isinstance(info.get("SecurityOptions"), list)
        or not isinstance(info.get("ServerVersion"), str)
        or not info["ServerVersion"]
        or not isinstance(info.get("ID"), str)
        or not info["ID"]
    ):
        return None
    options = {
        option.split(",")[0]
        for option in info["SecurityOptions"]
        if isinstance(option, str)
    }
    mode = (
        "rootless"
        if "name=rootless" in options
        else "userns-remap"
        if "name=userns" in options
        else "rootful"
    )
    return mode, "name=selinux" in options, f"docker:{info['ID']}"


def _podman_facts(info: object) -> tuple[str, bool, str] | None:
    """The mode, SELinux flag and host identity in a Podman answer; ``None`` if it is not one."""
    host = info.get("host") if isinstance(info, dict) else None
    security = host.get("security") if isinstance(host, dict) else None
    version = info.get("version") if isinstance(info, dict) else None
    if (
        not isinstance(host, dict)
        or not isinstance(host.get("hostname"), str)
        or not host["hostname"]
        or not isinstance(security, dict)
        or not isinstance(security.get("rootless"), bool)
        or not isinstance(security.get("selinuxEnabled"), bool)
        or not isinstance(version, dict)
        or not isinstance(version.get("Version"), str)
        or not version["Version"]
    ):
        return None
    mode = (
        "remote"
        if host.get("serviceIsRemote") is True
        else "rootless"
        if security["rootless"]
        else "rootful"
    )
    return mode, security["selinuxEnabled"], f"podman:{host['hostname']}"


# A mode the executor cannot run, with the blocker it raises before any worker starts.
_UNSUPPORTED_MODES = {
    "userns-remap": (
        "Docker user namespace remapping is not supported by the container executor",
        "run the coordinator against a Docker daemon without userns-remap, or against rootless "
        "Docker or Podman",
    ),
    "remote": (
        "remote Podman is not supported by the container executor",
        "use a local Podman: the plan's mounts are paths of the coordinator's own host",
    ),
}


def _engine_mode(engine: str, block: _Block) -> tuple[str, bool, str]:
    """The engine mode, SELinux flag and identity from ``<engine> info``; it only reads state."""
    ok, output = _engine([engine, "info", "--format", "{{json .}}"])
    if not ok:
        block(
            f"the {engine} engine is unavailable: {output}",
            f"start the {engine} engine and let the coordinator reach the {engine} CLI, "
            "then repeat dispatch preflight",
        )
        return "unavailable", False, ""
    try:
        info = json.loads(output)
    except ValueError:
        info = None
    facts = _docker_facts(info) if engine == "docker" else _podman_facts(info)
    if facts is None:
        block(
            f"the {engine} engine mode cannot be determined from {engine} info",
            f"use a matching engine: the {engine} CLI must answer as {engine}; a "
            "docker-compatible shim or another engine is not supported",
        )
        return "unknown", False, ""
    mode, selinux, environment_id = facts
    if mode in _UNSUPPORTED_MODES:
        block(*_UNSUPPORTED_MODES[mode])
    return mode, selinux, environment_id


def _image_id(engine: str, tag: str, block: _Block) -> str | None:
    """The local image id of ``tag``; a missing image is a blocker, never a build or a pull."""
    ok, output = _engine([engine, "image", "inspect", "--format", "{{.Id}}", tag])
    if ok and _BARE_IMAGE_ID.fullmatch(output):
        output = f"sha256:{output}"
    if ok and _IMAGE_ID.fullmatch(output):
        return output
    block(
        f"image {tag} is not present in the {engine} engine; the harness never builds or "
        "pulls images",
        f"build or pull {tag} yourself, then repeat dispatch preflight and propose",
    )
    return None


def _plan_support(data: JsonObject, transport: str, block: _Block) -> None:
    """Block every plan property the executor cannot run; it reads only the plan and the host."""
    if transport != "external":
        block(
            f"the docker executor launches only an external dispatch, not {transport}",
            "set the role's assignment_plans transport to external, then repeat dispatch "
            "preflight and propose",
        )
    container = data.get("container")
    if not container:
        block(
            "the access plan has no container component",
            "set access_policy.defaults.container.worker_image, then repeat dispatch "
            "preflight and propose",
        )
    hosts = data["network"]["hosts"]
    if hosts and not (container and container.get("proxy_image")):
        block(
            f"network hosts {', '.join(hosts)} need an egress proxy, but the container "
            "component names no proxy_image",
            "build the proxy image from .harness/container/Dockerfile.proxy and set the "
            "role's access_policy container.proxy_image to its tag, or set network.hosts to []",
        )
    if data["mode"] not in SUPPORTED_MODES:
        block(
            f"mode {data['mode']} cannot run in a container",
            "set the role's access_policy mode to sandbox or inherit",
        )
    names = data.get("environment", [])
    taken = RESERVED_ENV_NAMES | PROXY_ENV_NAMES if hosts else RESERVED_ENV_NAMES
    reserved = [name for name in names if name in taken]
    if reserved:
        block(
            f"the docker executor sets these environment names itself: {', '.join(reserved)}",
            "remove these names from the access_policy environment",
        )
    # Only presence is read: a value never enters a blocker, its remedy or the evidence.
    missing = [name for name in names if name not in os.environ]
    if missing:
        block(
            f"the host environment has no variable named: {', '.join(missing)}",
            f"set each of {', '.join(missing)} in the coordinator's environment, then "
            "repeat dispatch preflight",
        )
    for path in sorted({item["path"] for item in data["requirements"]}):
        if _UNSAFE_PATH.search(path) or _overlaps_home(path):
            block(
                f"plan root {path!r} cannot be mounted safely",
                "move the root to a path without commas, quotes or line breaks outside "
                f"{CONTAINER_HOME}",
            )
    if not hasattr(os, "getuid"):
        block(
            "the host has no POSIX user identity for the container user",
            "run the coordinator on a POSIX host",
        )


class DockerExecutor:
    """Run the approved worker in one Docker or Podman container, offline or behind its sidecar."""

    def observe(
        self, plan: Mapping[str, object], transport: str
    ) -> ContainerObservation:
        """Probe the engine and images the plan names, read-only, and report every blocker."""
        data = cast(JsonObject, plan)
        container = data.get("container")
        engine = str(container["engine"]) if container else EXTENSION_NAME
        worker_image = str(container["worker_image"]) if container else None
        proxy_image = container.get("proxy_image") if container else None
        blockers: list[dict[str, str]] = []

        def block(reason: str, remedy: str) -> None:
            blockers.append({"reason": reason, "remedy": remedy})

        _plan_support(data, transport, block)
        engine_mode, selinux, environment_id = "not-probed", False, ""
        worker_id = proxy_id = None
        if container:
            engine_mode, selinux, environment_id = _engine_mode(engine, block)
            if engine_mode != "unavailable" and worker_image is not None:
                worker_id = _image_id(engine, worker_image, block)
                proxy_id = (
                    _image_id(engine, proxy_image, block) if proxy_image else None
                )
        facts: dict[str, object] = {
            "engine": engine,
            "engine_mode": engine_mode,
            "selinux": selinux,
            "worker_image": worker_image,
            "worker_image_id": worker_id,
            "proxy_image": proxy_image,
            "proxy_image_id": proxy_id,
            "blockers": blockers,
        }
        if blockers:
            remedies = list(dict.fromkeys(item["remedy"] for item in blockers))
            raise extensions.RuntimeAccessBlocked(
                "; ".join(item["reason"] for item in blockers),
                remedy="; ".join(remedies),
                facts=facts,
            )
        return ContainerObservation(
            plan_digest=str(data["plan_digest"]),
            transport=transport,
            supported_modes=SUPPORTED_MODES,
            effective_mode="sandbox",
            mechanism="native-apply",
            environment_id=environment_id,
            launch_id=extensions.LAUNCH_AT_HANDOFF,
            source="native-runtime",
            observed_at=datetime.now(UTC).isoformat(),
            hosts=tuple(data["network"]["hosts"]),
            filesystem=tuple(
                (item["path"], item["access"]) for item in data["requirements"]
            ),
            engine=engine,
            engine_mode=engine_mode,
            selinux=selinux,
            worker_image=worker_image,
            worker_image_id=worker_id,
            proxy_image=proxy_image,
            proxy_image_id=proxy_id,
        )

    def apply(
        self,
        brief: Mapping[str, object],
        observation: extensions.RuntimeAccessObservation,
    ) -> ContainerObservation:
        """Confirm that the images observed now are the digests the approval pinned."""
        if not isinstance(observation, ContainerObservation):
            raise extensions.RuntimeAccessBlocked(
                "the docker executor cannot apply another provider's evidence",
                remedy=INTERNAL_INVARIANT_REMEDY,
                facts={"provider": type(observation).__name__},
            )
        pinned = brief.get("runtime_images")
        before = pinned if isinstance(pinned, dict) else {}
        current = pins(observation)
        changed = [
            f"image {tag} changed after dispatch propose: pinned {before.get(key)}, "
            f"now {current[key]}"
            for key, tag in (
                ("worker", observation.worker_image),
                ("proxy", observation.proxy_image),
            )
            if before.get(key) != current[key]
        ]
        if changed:
            raise extensions.RuntimeAccessBlocked(
                "; ".join(changed),
                remedy=CHANGED_IMAGE_REMEDY,
                facts={"pinned": pinned, "current": current},
            )
        return replace(observation, applied=True)

    def handoff(
        self,
        brief: Mapping[str, object],
        observation: extensions.RuntimeAccessObservation,
        command: tuple[str, ...] | None,
    ) -> ContainerObservation:
        """Start the approved adapter command detached in the worker's own container.

        With hosts the worker's internal network and egress sidecar come up first. The receipt
        names the worker's container id as its ``launch_id``. Nothing waits for the worker, and
        nothing builds, pulls or runs an image attached.
        """
        if not isinstance(observation, ContainerObservation):
            raise extensions.RuntimeAccessBlocked(
                "the docker executor cannot launch with another provider's evidence",
                remedy=INTERNAL_INVARIANT_REMEDY,
                facts={"provider": type(observation).__name__},
            )
        if not command:
            raise extensions.RuntimeAccessBlocked(
                "the docker executor wraps the external runtime adapter and received no "
                "adapter command",
                remedy="send the external dispatch with --adapter",
                facts={"engine": observation.engine},
            )
        dispatch_id = str(brief["dispatch_id"])
        container_id = _launch(brief, command, observation)
        return replace(
            observation,
            applied=True,
            handed_off=True,
            dispatch_id=dispatch_id,
            launch_id=container_id,
            observed_at=datetime.now(UTC).isoformat(),
        )
