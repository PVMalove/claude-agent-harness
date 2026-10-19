"""Built-in ``docker`` runtime_access: an approved external dispatch in its own rootful container.

The executor runs only what the immutable brief approved: the resolved plan's resources at the same
absolute paths, the network the plan names, and the image digests that ``dispatch propose`` pinned.
It never builds or pulls an image and never approves anything on the human's behalf (ADR 0018).
Every engine command goes through ``run_engine``; nothing else here starts a process.

An empty ``network.hosts`` runs the worker with ``--network none``. A non-empty list runs it on an
``--internal`` network whose only peer is a per-dispatch egress-proxy sidecar (issue #706): the
sidecar is attached to the default bridge too and enforces the host allowlist for HTTP(S). The
sidecar and its network outlive the worker until the terminal teardown of issue #452 removes them
by their labels.

Rootful Docker is the supported scope. Podman, rootless Docker and an in-process transport each
raise ``RuntimeAccessBlocked`` with its remedy.
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


def _engine(argv: Sequence[str]) -> tuple[bool, str]:
    """Whether one engine command succeeded, with its output or the failure detail."""
    try:
        result = run_engine(argv)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        return False, detail or f"exit code {result.returncode}"
    return True, result.stdout.strip()


@dataclass(frozen=True)
class ContainerObservation(extensions.RuntimeAccessObservation):
    """Worker evidence of the docker executor: the engine, its mode and the image digests."""

    engine: str = EXTENSION_NAME
    engine_mode: str = "not-probed"
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


def _plan_root(plan: Mapping[str, object], resource: str) -> str:
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
        f"harness.repository={_plan_root(plan, 'git_common')}",
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


def create_argv(
    brief: Mapping[str, object], command: Sequence[str], user: str
) -> list[str]:
    """The exact ``docker create`` of the approved worker: plan roots only, no direct network.

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
        "docker",
        "create",
        # The default would pull a missing image; the harness never builds or pulls one.
        "--pull",
        "never",
        "--name",
        container_name(dispatch_id),
        "--network",
        network_name(dispatch_id) if hosts else "none",
        "--user",
        user,
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
    argv += [*_labels(brief), "--workdir", _plan_root(plan, "checkout")]
    for path, access in mounts(plan["requirements"]):
        argv += [
            "--mount",
            f"type=bind,source={path},target={path}"
            + ("" if access == "write" else ",readonly"),
        ]
    # The image is the pinned id, never the tag: a retag after approval cannot change it.
    argv += ["--entrypoint", command[0], data["runtime_images"]["worker"], *command[1:]]
    return argv


def network_argv(brief: Mapping[str, object]) -> list[str]:
    """The ``docker network create`` of the dispatch's internal network: no route out of it."""
    dispatch_id = str(brief["dispatch_id"])
    return [
        "docker",
        "network",
        "create",
        "--internal",
        *_labels(brief),
        network_name(dispatch_id),
    ]


def proxy_argv(brief: Mapping[str, object], user: str) -> list[str]:
    """The ``docker create`` of the egress sidecar from its pinned digest, on the internal network.

    The allowlist is the plan's host list: validated DNS names, so it is not a secret and may
    appear in the argv. The sidecar needs no capability and writes nothing.
    """
    data = cast(JsonObject, brief)
    plan = data["runtime_access"]
    dispatch_id = str(data["dispatch_id"])
    return [
        "docker",
        "create",
        "--pull",
        "never",
        "--name",
        proxy_name(dispatch_id),
        "--network",
        network_name(dispatch_id),
        "--user",
        user,
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

    def __init__(self) -> None:
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
                ["docker", "rm", "--force", name]
                if kind == "container"
                else ["docker", "network", "rm", name]
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


def _launch(brief: Mapping[str, object], command: Sequence[str], user: str) -> str:
    """Create and start the dispatch's containers detached; any failure removes what it created.

    The sidecar comes first and only when the plan has hosts: its internal network, then the
    sidecar attached to it, then the default bridge, then its start. The worker is last.
    """
    data = cast(JsonObject, brief)
    dispatch_id = str(data["dispatch_id"])
    launch = _Launch()
    if data["runtime_access"]["network"]["hosts"]:
        launch.run(
            "docker network create",
            network_argv(brief),
            creates=("network", network_name(dispatch_id)),
        )
        proxy_id = launch.run(
            "docker create (egress proxy)",
            proxy_argv(brief, user),
            creates=("container", proxy_name(dispatch_id)),
            container_id=True,
        )
        launch.run(
            "docker network connect",
            ["docker", "network", "connect", "bridge", proxy_id],
        )
        launch.run("docker start (egress proxy)", ["docker", "start", proxy_id])
    container_id = launch.run(
        "docker create",
        create_argv(brief, command, user),
        creates=("container", container_name(dispatch_id)),
        container_id=True,
    )
    launch.run("docker start", ["docker", "start", container_id])
    return container_id


def _overlaps_home(path: str) -> bool:
    home = PurePosixPath(CONTAINER_HOME)
    root = PurePosixPath(path)
    return root == home or home in root.parents or root in home.parents


def _engine_mode(block: _Block) -> tuple[str, str]:
    """The engine mode and daemon identity from ``docker info``; it only reads engine state."""
    ok, output = _engine(["docker", "info", "--format", "{{json .}}"])
    if not ok:
        block(
            f"the docker engine is unavailable: {output}",
            "start the rootful Docker daemon and let the coordinator reach the docker CLI, "
            "then repeat dispatch preflight",
        )
        return "unavailable", ""
    try:
        info = json.loads(output)
    except ValueError:
        info = None
    if (
        not isinstance(info, dict)
        or not isinstance(info.get("SecurityOptions"), list)
        or not isinstance(info.get("ServerVersion"), str)
        or not info["ServerVersion"]
        or not isinstance(info.get("ID"), str)
        or not info["ID"]
    ):
        block(
            "the docker engine mode cannot be determined from docker info",
            "use a rootful Docker Engine; Podman and docker-compatible shims are not supported",
        )
        return "unknown", ""
    if any(
        isinstance(option, str) and option.split(",")[0] == "name=rootless"
        for option in info["SecurityOptions"]
    ):
        block(
            "rootless Docker is not supported by the docker executor",
            "run the coordinator against a rootful Docker daemon",
        )
        return "rootless", f"docker:{info['ID']}"
    return "rootful", f"docker:{info['ID']}"


def _image_id(tag: str, block: _Block) -> str | None:
    """The local image id of ``tag``; a missing image is a blocker, never a build or a pull."""
    ok, output = _engine(["docker", "image", "inspect", "--format", "{{.Id}}", tag])
    if ok and _IMAGE_ID.fullmatch(output):
        return output
    block(
        f"image {tag} is not present in the docker engine; the harness never builds or "
        "pulls images",
        f"build or pull {tag} yourself, then repeat dispatch preflight and propose",
    )
    return None


def _plan_support(data: JsonObject, transport: str, engine: str, block: _Block) -> None:
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
    elif engine != EXTENSION_NAME:
        block(
            f"container engine {engine} is not supported; the docker executor runs "
            "rootful Docker only",
            "set the access_policy container engine to docker or omit it",
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
    """Run the approved worker in one rootful Docker container, offline or behind its sidecar."""

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

        _plan_support(data, transport, engine, block)
        engine_mode, environment_id = "not-probed", ""
        worker_id = proxy_id = None
        if container and engine == EXTENSION_NAME:
            engine_mode, environment_id = _engine_mode(block)
            if engine_mode != "unavailable" and worker_image is not None:
                worker_id = _image_id(worker_image, block)
                proxy_id = _image_id(proxy_image, block) if proxy_image else None
        facts: dict[str, object] = {
            "engine": engine,
            "engine_mode": engine_mode,
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
        container_id = _launch(brief, command, f"{os.getuid()}:{os.getgid()}")
        return replace(
            observation,
            applied=True,
            handed_off=True,
            dispatch_id=dispatch_id,
            launch_id=container_id,
            observed_at=datetime.now(UTC).isoformat(),
        )
