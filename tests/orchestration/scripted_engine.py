"""A scripted container engine CLI for seam tests of the runtime (ADR 0022, section 4).

``ScriptedEngine.install`` writes an executable named after the engine (``podman`` or ``docker``)
into a directory that a test puts first on the ``PATH`` of ``harness-runtime serve``. Every call
runs this module as a script: it records its argv and answers from a JSON state file, locked
per call, with the engine's own wording. Containers and networks are created, started, stopped
and removed by the calls that make them, so a test scripts a worker's exit instead of answers.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    # The script runs outside the package too: only the type checker reads this import.
    from harness.json_types import JsonObject as State

STATE_ENV = "HARNESS_SCRIPTED_ENGINE_STATE"
JUNIT_ENV = "HARNESS_STAGE_JUNIT_XML"
WORKER_ID = "0123456789abcdef" * 4
PROXY_ID = "89abcdef01234567" * 4
NETWORK_ID = "fedcba9876543210" * 4


def podman_info(*, rootless: bool = False, selinux: bool = False) -> State:
    """The part of ``podman info`` the runtime reads."""
    return {
        "host": {
            "hostname": "podman-host",
            "serviceIsRemote": False,
            "security": {"rootless": rootless, "selinuxEnabled": selinux},
        },
        "version": {"Version": "5.4.0"},
    }


class ScriptedEngine:
    """The test side: the state file, its objects and the recorded calls."""

    def __init__(self, directory: Path, binary: str = "podman") -> None:
        self.bin = directory / "bin"
        self.bin.mkdir(parents=True, exist_ok=True)
        self.state_file = directory / f"{binary}.json"
        self.binary = binary
        self._write(
            {"binary": binary, "info": podman_info(), "images": {}, "objects": {},
             "failures": {}, "calls": [], "runs": {}}
        )
        wrapper = self.bin / binary
        wrapper.write_text(
            f"#!/bin/sh\n{STATE_ENV}='{self.state_file}' exec '{sys.executable}' "
            f"'{Path(__file__).resolve()}' \"$@\"\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

    def path(self) -> str:
        """A ``PATH`` whose first directory holds this engine and no other."""
        return f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}"

    def _write(self, state: State) -> None:
        self.state_file.write_text(json.dumps(state), encoding="utf-8")

    def update(self, change: Callable[[State], None]) -> None:
        with _locked(self.state_file) as state:
            change(state)

    def state(self) -> State:
        return cast("State", json.loads(self.state_file.read_text(encoding="utf-8")))

    def calls(self) -> list[list[str]]:
        return list(self.state()["calls"])

    def removals(self) -> list[list[str]]:
        return [c for c in self.calls() if c[1] == "rm" or c[1:3] == ["network", "rm"]]

    def script(
        self, stage_id: str, *, stdout: str = "", stderr: str = "", exit_code: int = 0,
        hang: bool = False, junit: str = "",
    ) -> None:
        """What the foreground ``run`` of the QA command ``stage_id`` prints and exits with; a
        hanging one runs until a stop or a kill of its container ends it. ``junit`` is the JUnit
        report the command writes where ``HARNESS_STAGE_JUNIT_XML`` of its ``run`` points."""
        def add(state: State) -> None:
            state["runs"][stage_id] = {
                "stdout": stdout, "stderr": stderr, "exit_code": exit_code, "hang": hang,
                "junit": junit,
            }
        self.update(add)

    def runs(self) -> list[list[str]]:
        return [call for call in self.calls() if call[1] == "run"]

    def add_container(self, container_id: str, name: str, labels: dict[str, str], status: str) -> None:
        def add(state: State) -> None:
            state["objects"][container_id] = {
                "kind": "container", "id": container_id, "name": name, "labels": labels,
                "status": status, "exit_code": 0,
            }
        self.update(add)

    def add_network(self, network_id: str, name: str, labels: dict[str, str]) -> None:
        def add(state: State) -> None:
            state["objects"][network_id] = {
                "kind": "network", "id": network_id, "name": name, "labels": labels,
            }
        self.update(add)


class _locked:
    """The state file, exclusively locked, written back on exit."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> State:
        self.handle = self.path.open("r+", encoding="utf-8")
        fcntl.flock(self.handle, fcntl.LOCK_EX)
        self.state = cast("State", json.load(self.handle))
        return self.state

    def __exit__(self, *_: object) -> None:
        self.handle.seek(0)
        self.handle.truncate()
        json.dump(self.state, self.handle)
        self.handle.close()


def _labels(argv: list[str]) -> dict[str, str]:
    return dict(
        argv[i + 1].split("=", 1) for i, part in enumerate(argv) if part == "--label"
    )


def _find(state: State, kind: str | None, wanted: str) -> State | None:
    return next(
        (
            item
            for item in state["objects"].values()
            if (kind is None or item["kind"] == kind) and wanted in (item["id"], item["name"])
        ),
        None,
    )


def _describe(item: State) -> State:
    if item["kind"] == "network":
        return {"Id": item["id"], "Name": item["name"], "Labels": item["labels"]}
    exited = item["status"] == "exited"
    return {
        "Id": item["id"],
        "Name": "/" + item["name"],
        "State": {
            "Status": item["status"],
            "ExitCode": item["exit_code"] if exited else 0,
            "FinishedAt": "2026-10-28T10:00:00Z" if exited else "0001-01-01T00:00:00Z",
            "OOMKilled": False,
        },
        "Config": {"Labels": item["labels"]},
    }


def _answer(state: State, argv: list[str]) -> tuple[int, str, str]:
    verb = argv[1]
    sub = argv[2] if len(argv) > 2 else ""
    kind = f"{verb}-{sub}" if verb in ("network", "container", "image") else verb
    failure = state["failures"].get(kind)
    if isinstance(failure, str):
        return 1, "", failure
    if verb == "info":
        return 0, json.dumps(state["info"]) + "\n", ""
    if kind == "image-inspect":
        image = state["images"].get(argv[-1])
        if not isinstance(image, str):
            return 1, "", f"Error: No such image: {argv[-1]}"
        return 0, image + "\n", ""
    if verb in ("ps", "network") and (verb == "ps" or sub == "ls"):
        wanted = [
            argv[i + 1].removeprefix("label=").split("=", 1)
            for i, part in enumerate(argv)
            if part == "--filter"
        ]
        listed_kind = "container" if verb == "ps" else "network"
        ids = [
            item["id"]
            for item in state["objects"].values()
            if item["kind"] == listed_kind
            and all(item["labels"].get(key) == value for key, value in wanted)
        ]
        return 0, "".join(f"{i}\n" for i in ids), ""
    if kind in ("container-inspect", "network-inspect") or verb == "inspect":
        formatted = "--format" in argv
        ids = argv[argv.index("inspect") + 1 :]
        if formatted:
            ids = ids[2:]
        object_kind: str | None = (
            None if verb == "inspect" else ("network" if verb == "network" else "container")
        )
        found = [_find(state, object_kind, wanted) for wanted in ids]
        if any(item is None for item in found):
            missing = ids[found.index(None)]
            return 1, "", f"Error: no such object: {missing}"
        described = [_describe(item) for item in found if item is not None]
        if verb == "inspect":
            return 0, described[0]["Id"] + "\n", ""
        return 0, json.dumps(described[0] if formatted else described) + "\n", ""
    if verb == "create" or kind == "network-create":
        name = argv[argv.index("--name") + 1] if verb == "create" else argv[-1]
        object_id = NETWORK_ID if verb == "network" else (PROXY_ID if name.endswith("-proxy") else WORKER_ID)
        state["objects"][object_id] = {
            "kind": "network" if verb == "network" else "container", "id": object_id,
            "name": name, "labels": _labels(argv), "status": "created", "exit_code": 0,
        }
        return 0, object_id + "\n", ""
    if verb in ("start", "stop", "kill"):
        item = _find(state, "container", argv[-1])
        if item is None:
            return 1, "", f"Error: no such container {argv[-1]}"
        item["status"] = "running" if verb == "start" else "exited"
        item["signalled"] = verb
        return 0, argv[-1] + "\n", ""
    if kind == "network-connect":
        return 0, "", ""
    if verb == "logs":
        return 0, "worker stdout\n", "worker stderr\n"
    if verb == "rm" or kind == "network-rm":
        item = _find(state, "network" if verb == "network" else "container", argv[-1])
        if item is None:
            return 1, "", f"Error: no such object: {argv[-1]}"
        if item.get("status") == "running" and "--force" not in argv:
            return 1, "", f"cannot remove container {item['name']}: container is running"
        del state["objects"][item["id"]]
        return 0, argv[-1] + "\n", ""
    return 1, "", f"the scripted engine has no answer for {argv}"


def _run(path: Path, argv: list[str]) -> int:
    """A foreground ``run --rm`` of a QA command: its scripted streams and exit code, or, for a
    hanging one, the wait outside the lock until a stop (143) or a kill (137) of its name."""
    name = argv[argv.index("--name") + 1]
    with _locked(path) as state:
        argv[0] = state["binary"]
        state["calls"].append(argv)
        script: State = next(
            (value for key, value in state["runs"].items() if name.endswith(f"-{key}")), {}
        )
        state["objects"][name] = {
            "kind": "container", "id": name, "name": name, "labels": _labels(argv),
            "status": "running", "exit_code": 0,
        }
    junit = next(
        (part.split("=", 1)[1] for part in argv if part.startswith(f"{JUNIT_ENV}=")), None
    )
    if junit is not None and script.get("junit"):
        # The run root is bind-mounted at its own path: the report lands on the host as is.
        Path(junit).write_text(script["junit"], encoding="utf-8")
    sys.stdout.write(script.get("stdout", ""))
    sys.stderr.write(script.get("stderr", ""))
    sys.stdout.flush()
    sys.stderr.flush()
    code = int(script.get("exit_code", 0))
    while script.get("hang"):
        with _locked(path) as state:
            item = state["objects"].get(name, {"status": "exited", "signalled": "kill"})
        if item["status"] == "exited":
            code = 143 if item.get("signalled") == "stop" else 137
            break
        time.sleep(0.05)
    with _locked(path) as state:
        state["objects"].pop(name, None)
    return code


def main() -> int:
    argv = [Path(sys.argv[0]).name, *sys.argv[1:]]
    if argv[1:2] == ["run"]:
        return _run(Path(os.environ[STATE_ENV]), argv)
    with _locked(Path(os.environ[STATE_ENV])) as state:
        argv[0] = state["binary"]
        state["calls"].append(argv)
        code, stdout, stderr = _answer(state, argv)
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
