"""Built-in ``docker`` runtime_access: an approved external dispatch in its own rootful container.

The executor runs only what the immutable brief approved: the resolved plan's resources at the same
absolute paths, no network, and the image digests that ``dispatch propose`` pinned. It never builds
or pulls an image and never approves anything on the human's behalf (ADR 0018). Every engine
command goes through ``run_engine``; nothing else here starts a process.

Rootful Docker with an empty ``network.hosts`` is the supported scope. Podman, rootless Docker, a
host allowlist and an in-process transport each raise ``RuntimeAccessBlocked`` with its remedy.
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
from typing import cast

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
# ``inherit`` asks for no particular mode; the container isolates the worker either way.
SUPPORTED_MODES = ("inherit", "sandbox")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
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


class DockerExecutor:
    """Run the approved worker in one rootful Docker container without network."""

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

        if transport != "external":
            block(
                f"the docker executor launches only an external dispatch, not {transport}",
                "set the role's assignment_plans transport to external, then repeat dispatch "
                "preflight and propose",
            )
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
        if hosts:
            block(
                f"network hosts {', '.join(hosts)} need an egress proxy that the docker "
                "executor does not provide",
                "set the role's access_policy network.hosts to []",
            )
        if data["mode"] not in SUPPORTED_MODES:
            block(
                f"mode {data['mode']} cannot run in a container",
                "set the role's access_policy mode to sandbox or inherit",
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
            hosts=(),
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
