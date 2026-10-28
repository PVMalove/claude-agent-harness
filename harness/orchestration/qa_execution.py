"""Container QA through harness-runtime (ADR 0022, section 3; ADR 0025).

A QA dispatch whose approved brief pins ``qa_execution`` runs its command groups in containers.
The coordinator prepares the clean checkout on the host, so Git stays outside every container;
``qa.run`` of harness-runtime then runs one foreground container per command, and the coordinator
polls ``execution.observe`` until the run ends. The exit evidence of the runtime comes back as the
gate runner's ``GroupRun`` records: each stream is read from the runtime's file, checked against
its sha256 and sanitised, and a command whose ending the runtime classified keeps its real exit
code beside its failure domain.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import cast

from ..gate_runner.gate_runner import (
    STAGE_ENV_JUNIT,
    CleanRoomPolicy,
    CommandGroup,
    GateRunnerError,
    GroupContext,
    GroupRun,
    StageCommand,
    _approved_text,
    _junit_failures,
    _numbered,
    concise_evidence,
    format_command_log,
    sanitise,
)
from .contract import JsonObject
from .core.constants import STATE_REL, FailureDomain
from .workflow.delivery import execution_operation_id, observed_execution

# Endings that leave no report: the QA is repeated as a new attempt of the same dispatch, with a
# new operation id and without spending the developer's retry budget.
NO_REPORT_DOMAINS = frozenset(
    {
        FailureDomain.INFRASTRUCTURE,
        FailureDomain.ENVIRONMENT_PREPARATION,
        FailureDomain.CANCELLED,
    }
)
TERMINAL_STATES = frozenset({"exited", "lost", "unknown"})


def container_brief(dispatch: Mapping[str, object]) -> bool:
    """Whether a QA brief runs its commands in containers: it pins ``qa_execution``."""
    return "qa_execution" in dispatch


def next_attempt(status: Mapping[str, object]) -> int:
    """The attempt a new QA run of a dispatch takes: one after the recorded one."""
    attempt = status.get("attempt")
    return (attempt if isinstance(attempt, int) else 0) + 1


class RuntimeQaPolicy:
    """The execution policy of a container QA: the clean-room checkout on the host and the
    command groups in the runtime. ``domain`` is the failure domain of the run once it ended,
    ``None`` when every exit code is a command's own result."""

    def __init__(
        self, repo: Path, dispatch: JsonObject, attempt: int, fencing_token: object
    ) -> None:
        self.repo = repo
        self.dispatch = dispatch
        self.attempt = attempt
        self.fencing_token = str(fencing_token)
        self.settings = cast(JsonObject, dispatch["qa_execution"])
        self.clean_room = CleanRoomPolicy(repo, str(dispatch["candidate_commit"]))
        self.domain: FailureDomain | None = None

    def checkout(self) -> AbstractContextManager[Path]:
        return self.clean_room.checkout()

    def run_groups(
        self, groups: Sequence[CommandGroup], checkout: Path, run: GroupContext
    ) -> list[GroupRun]:
        from .runtime_client import RuntimeClient

        tmpdir = checkout.parent / "tmp"
        tmpdir.mkdir(exist_ok=True)
        ids = [_numbered(group.group, len(group.commands)) for group in groups]
        payload: JsonObject = {
            "dispatch_id": self.dispatch["dispatch_id"],
            "attempt": self.attempt,
            "run_root": str(checkout.parent),
            "workdir": str(checkout),
            "tmpdir": str(tmpdir),
            "groups": [
                {
                    "group": group.group,
                    "run_when": group.run_when,
                    "stop_on_failure": group.stop_on_failure,
                    "commands": [
                        {"stage_id": stage, "command": command}
                        for stage, command in zip(stages, group.commands, strict=True)
                    ],
                }
                for group, stages in zip(groups, ids, strict=True)
            ],
        }
        # The execution id of a QA run is its operation id; a refusal or a sidecar the engine
        # refused raises the runtime's typed error, and the lane records it as an attempt.
        execution_id = execution_operation_id(self.dispatch, self.attempt)
        RuntimeClient(self.repo).call(
            "qa.run",
            operation_id=execution_id,
            payload=payload,
            fencing_token=self.fencing_token,
        )
        execution = self._wait(execution_id)
        exit_ = execution.get("exit")
        found = {
            str(item["stage_id"]): item
            for item in _records(exit_.get("commands") if isinstance(exit_, dict) else None)
        }
        commands_dir = self.repo / STATE_REL / "executions" / execution_id
        runs = [
            self._group_run(group, stages, found, commands_dir / "commands", checkout, run)
            for group, stages in zip(groups, ids, strict=True)
        ]
        if execution.get("state") != "exited":
            self.domain = FailureDomain.INFRASTRUCTURE
        return runs

    def _wait(self, execution_id: str) -> JsonObject:
        """Poll the run until the runtime records its end."""
        interval = int(cast(int, self.settings["poll_interval_seconds"]))
        while True:
            execution = observed_execution(self.repo, str(self.dispatch["dispatch_id"]))
            if execution is None or execution.get("execution_id") != execution_id:
                raise GateRunnerError(
                    f"the runtime no longer observes the QA run {execution_id}",
                    remedy="inspect 'runtime status', then run qa run again: a new attempt",
                )
            if execution.get("state") in TERMINAL_STATES:
                return execution
            time.sleep(interval)

    def _group_run(
        self,
        group: CommandGroup,
        stages: list[str],
        found: Mapping[str, JsonObject],
        commands_dir: Path,
        checkout: Path,
        run: GroupContext,
    ) -> GroupRun:
        ran: list[StageCommand] = []
        unreached: list[str] = []
        checks: list[dict[str, str]] = []
        for stage, command in zip(stages, group.commands, strict=True):
            text = _approved_text(command)
            record = found.get(stage, {})
            domain = _domain(record.get("domain"))
            if domain is not None and self.domain is None:
                self.domain = domain
            code = record.get("exit_code")
            if record.get("result") != "ran" or not isinstance(code, int):
                unreached.append(text)
                continue
            stdout, stderr = (
                _stream(record.get(kind), commands_dir) for kind in ("stdout", "stderr")
            )
            combined = sanitise(stdout + ("\n" if stdout and stderr else "") + stderr)
            run.outputs.append(format_command_log(sanitise(text), code, combined))
            checks.append(
                {
                    "command": text,
                    "result": "pass" if code == 0 else "fail",
                    "evidence": f"exit {code}"
                    + (f"; {domain} ending" if domain else "")
                    + f"; {concise_evidence(combined)}",
                }
            )
            junit = record.get("junit")
            ran.append(
                StageCommand(
                    command=text,
                    executed=text,
                    exit_code=code,
                    output=combined,
                    stdout=sanitise(stdout),
                    stderr=sanitise(stderr),
                    duration_ms=int(cast(int, record.get("duration_ms", 0))),
                    failed_nodeids=_junit_failures({STAGE_ENV_JUNIT: str(junit)}, checkout)
                    if code != 0 and isinstance(junit, str)
                    else (),
                    not_run=domain is not None,
                    domain=None if domain is None else str(domain),
                )
            )
        skipped = not ran and all(found.get(stage, {}).get("result") != "ran" for stage in stages)
        return GroupRun(group, ran, unreached, checks, skipped)


def _domain(value: object) -> FailureDomain | None:
    try:
        return FailureDomain(value) if isinstance(value, str) else None
    except ValueError:
        return FailureDomain.UNKNOWN


def _stream(reference: object, commands_dir: Path) -> str:
    """The text of one stream file of the runtime, proven by its sha256 and its location."""
    if not isinstance(reference, dict):
        return ""
    path = Path(str(reference.get("path")))
    try:
        inside = path.resolve().is_relative_to(commands_dir.resolve())
        data = path.read_bytes() if inside else b""
    except OSError as exc:
        raise GateRunnerError(
            f"could not read the QA stream file {path}: {sanitise(str(exc))}",
            remedy="restore the runtime's state directory, then run qa run again: a new attempt",
        ) from exc
    if not inside or hashlib.sha256(data).hexdigest() != reference.get("sha256"):
        raise GateRunnerError(
            f"the QA stream file {path} is not the evidence the runtime recorded",
            remedy="the runtime's stream file changed or lies elsewhere; run qa run again",
        )
    return data.decode("utf-8", errors="replace")


def _records(value: object) -> Iterator[JsonObject]:
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict) and isinstance(item.get("stage_id"), str):
            yield item
