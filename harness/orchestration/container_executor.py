"""Built-in ``docker`` runtime_access: an approved external dispatch in its own container.

The executor checks what the immutable brief approved: the resolved plan's resources at the same
absolute paths, the network the plan names, and the image digests that ``dispatch propose``
pinned. It never builds or pulls an image and never approves anything on the human's behalf
(ADR 0025).

Python builds no engine argv and starts no engine process (ADR 0022). ``observe`` asks
``harness-runtime`` (``container.observe``) for the engine, its mode, SELinux and the image ids;
the runtime refuses a plan root that exposes an engine socket or its own directory, and names a
missing environment variable, remote Podman, Docker user namespace remapping and an info answer
that does not match the engine as blockers. The runtime launches the worker of a supervised
dispatch itself (``execution.launch``), observes it, stops it at its deadline, saves its logs and
removes its objects with the engine of its execution.

The executor runs only on the runtime store epoch and only on Linux: on the file ledger, on
another platform and for an in-process transport ``observe`` raises ``RuntimeAccessBlocked``
with its remedy.
"""

from __future__ import annotations

import re
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from ..errors import INTERNAL_INVARIANT_REMEDY
from . import extensions
from .core.utils import JsonObject
from .ledger.lifecycle import store_is_authority
from .runtime_client import RuntimeClient, RuntimeClientError

EXTENSION_NAME = "docker"
# The coordinator's own QA operation (ADR 0022, section 3): ``qa run`` hands the approved commands
# to harness-runtime, which runs each in its own container. No other coordinator operation, Git
# and publish included, is proven by a container.
QA_OPERATION_TRANSPORT = "qa-operation"
# The worker environment the runtime sets itself (its home, the Git safe.directory override and
# the executor marker of ADR 0025); preflight and send block a plan name that would replace it.
RESERVED_ENV_NAMES = frozenset(
    {
        "HOME",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "HARNESS_EXECUTOR",
    }
)
# With a host allowlist the runtime points the worker at the sidecar through these names, so a
# plan may not set them; an offline plan still may.
PROXY_URL_ENV_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
PROXY_BYPASS_ENV_NAMES = ("NO_PROXY", "no_proxy")
PROXY_ENV_NAMES = frozenset(PROXY_URL_ENV_NAMES + PROXY_BYPASS_ENV_NAMES)
# ``inherit`` asks for no particular mode; the container isolates the worker either way.
SUPPORTED_MODES = ("inherit", "sandbox")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
# One ``container.observe``: an engine info and up to two image inspects, each bounded by the
# runtime's own engine timeout.
OBSERVE_TIMEOUT_SECONDS = 120.0

# An approval binds the pinned digests, so a rebuilt image needs a new propose, never the old one.
CHANGED_IMAGE_REMEDY = (
    "restore the image with the pinned digest, or rebuild or pull the image, cancel this "
    "dispatch (dispatch cancel) and repeat dispatch propose: an approval is never reused "
    "for another image"
)

_Block = Callable[[str, str], None]


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


def _storage(plan: JsonObject) -> Path:
    """The plan's shared storage: the ``.harness`` of the main checkout."""
    return Path(
        str(
            next(
                item["path"]
                for item in plan["requirements"]
                if item["resource"] == "shared_storage"
            )
        )
    )


def _plan_support(data: JsonObject, transport: str, block: _Block) -> None:
    """Block every plan property the executor cannot run; it reads only the plan."""
    if transport not in ("external", QA_OPERATION_TRANSPORT):
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


def _epoch_support(data: JsonObject, block: _Block) -> bool:
    """Whether the runtime may be asked: it launches the worker only on Linux and only on the
    runtime store epoch, so the file ledger never gets a container (ADR 0022)."""
    if sys.platform != "linux":
        block(
            f"the docker executor runs only on Linux, not on {sys.platform}",
            "run the coordinator and harness-runtime on Linux; the executor has no Windows "
            "or macOS launch",
        )
        return False
    storage = _storage(data)
    if not store_is_authority(storage / "orchestration" / "state"):
        block(
            "the docker executor runs only on the runtime store epoch, and this repository "
            "still uses the file ledger",
            f"cut the ledger over with `harness-runtime migrate --repo {storage.parent}`, start "
            f"`harness-runtime serve --repo {storage.parent}`, then repeat dispatch preflight",
        )
        return False
    return True


def _observed(data: JsonObject, block: _Block) -> JsonObject:
    """The engine facts ``container.observe`` answers; a refusal of the runtime is a blocker."""
    try:
        reply = RuntimeClient(_storage(data).parent).call(
            "container.observe",
            operation_id=f"container.observe-{uuid.uuid4().hex}",
            payload={"plan": data},
            timeout_seconds=OBSERVE_TIMEOUT_SECONDS,
        )
    except RuntimeClientError as exc:
        block(
            f"harness-runtime did not observe the container plan [{exc.code}]: {exc.message}",
            exc.remedy,
        )
        return {}
    result = reply.result
    blockers = result.get("blockers")
    if not isinstance(blockers, list) or not all(
        isinstance(item, dict) for item in blockers
    ):
        block(
            "harness-runtime answered container.observe without its blockers",
            "run the harness-runtime of this harness version and repeat dispatch preflight",
        )
        return {}
    for item in blockers:
        block(str(item.get("reason")), str(item.get("remedy")))
    return {
        "engine_mode": str(result.get("engine_mode")),
        "selinux": result.get("selinux") is True,
        "environment_id": str(result.get("environment_id") or ""),
        "worker_image_id": result.get("worker_image_id"),
        "proxy_image_id": result.get("proxy_image_id"),
    }


class DockerExecutor:
    """Check the approved worker of one Docker or Podman container, offline or behind its sidecar;
    ``harness-runtime`` launches it."""

    def observe(
        self, plan: Mapping[str, object], transport: str
    ) -> ContainerObservation:
        """Check the plan, then ask harness-runtime about the engine and the images, read-only,
        and report every blocker."""
        data = cast(JsonObject, plan)
        container = data.get("container")
        engine = str(container["engine"]) if container else EXTENSION_NAME
        worker_image = str(container["worker_image"]) if container else None
        proxy_image = container.get("proxy_image") if container else None
        blockers: list[dict[str, str]] = []

        def block(reason: str, remedy: str) -> None:
            blockers.append({"reason": reason, "remedy": remedy})

        _plan_support(data, transport, block)
        observed: JsonObject = {}
        if container and _epoch_support(data, block):
            observed = _observed(data, block)
        engine_mode = str(observed.get("engine_mode", "not-probed"))
        selinux = observed.get("selinux") is True
        environment_id = str(observed.get("environment_id", ""))
        worker_id = observed.get("worker_image_id")
        proxy_id = observed.get("proxy_image_id")
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
            worker_image_id=str(worker_id) if worker_id else None,
            proxy_image=proxy_image,
            proxy_image_id=str(proxy_id) if proxy_id else None,
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
