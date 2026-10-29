"""The shared recovery status and action API of the core, the CLI and the Harness console.

One structured contract (``recoveryStatus.v1``) says what keeps a batch from going on and what may
be done about it.  Every check names its diagnostics with a pointer, the redacted expected and
received values and a suggestion, so a console or a CI log shows them without parsing stderr:

* ``status`` is read-only: it takes no lock and writes nothing;
* ``reload`` re-reads and re-validates the current configuration and never starts a worker;
* ``fix`` previews or applies the configuration's safe normalization through the configuration
  transaction, and never resumes anything;
* ``skip`` hides one allowlisted advisory for one session; a fatal diagnostic is never skipped;
* ``abort`` records that the operator left recovery: it abandons no batch, stops no worker and
  cleans no worktree;
* ``resume`` (``recovery_commands``) re-checks the batch, releases its hold and reacts to the
  observed execution of its open dispatches.

Every action except ``skip`` records its durable intent before it runs, so a console that is
cancelled, reaches end of input or is interrupted shows the same status after a restart.  The hold
and the intent are recovery records of the runtime store (``recovery/hold/<batch_id>.json``,
``recovery/intent/<batch_id>.json``, ADR 0026): Python decides them, the Rust store writes them
through ``state.write``, and the file-ledger epoch keeps neither, so there every command refuses
with the ``harness-runtime migrate`` remedy.  Nothing
here reads standard input, waits for a terminal or imports a UI toolkit.  An exception that is not
a refusal is never shown as a configuration problem: it is an ``internal-error`` diagnostic with a
correlation ID.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from harness.configuration import Bundle, bundle_diff, read_config_bundle
from harness.configuration_store import ConfigurationStore, json_bytes
from harness.diagnostics import Diagnostic, pointer, safe_value
from harness.errors import INTERNAL_INVARIANT_REMEDY, HarnessError
from harness.orchestration import operational_hold
from harness.orchestration.config_boundary import inspect_configuration
from harness.orchestration.core.constants import (
    LIVE_DISPATCH_STATES,
    TERMINAL_BATCH_STATES,
)
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _canonical,
    _moment,
    _now,
    _silent_seconds,
)
from harness.orchestration.ledger.ledger_ops import (
    LedgerBusyError,
    _ledger_errors,
    _ledger_lock,
    _read_store_record,
    read_state,
    store_is_authority,
)
from harness.orchestration.ledger.lifecycle import LifecycleLedger
from harness.orchestration.operational_hold import (
    FILE_EPOCH_REMEDY,
    OperationalHoldError,
    sanitize_detail,
)
from harness.orchestration.runtime_client import RuntimeClientError

RECOVERY_CONTRACT = "recoveryStatus.v1"
INTENT_CONTRACT = "recoveryIntent.v1"
INTENT_DIRECTORY = "recovery/intent"
ACTIONS = ("fix", "reload", "resume", "skip", "abort")
# The reconciliation checks, in the order they run.
CHECKS = (
    "config",
    "store",
    "ledger",
    "batch",
    "lifecycle",
    "runtime",
    "snapshot",
    "context",
    "compatibility",
    "operation",
    "liveness",
    "attention",
    "approval",
)
CHECK_STATUSES = ("passed", "pending", "reconciled", "refused", "skipped")
DIAGNOSTIC_CODES = (
    "config-invalid",
    "config-advisory",
    "runtime-compatibility",
    "integrity-failure",
    "stale-evidence",
    "ambiguous-effect",
    "concurrency-conflict",
    "storage-unavailable",
    "live-worker",
    "attention-open",
    "approval-required",
    "approval-stale",
    "terminal-refusal",
    "internal-error",
)
OUTCOMES = ("done", "refused")
INTENT_STATUSES = ("pending", "done", "refused", "interrupted", "aborted")
# The closed allowlist of advisories ``skip`` may hide for one session.
ADVISORIES = ("config-normalized",)
# The diagnostic code of each error code of the runtime protocol (ADR 0021, section 3).
_RUNTIME_CODES = {
    "runtime_unavailable": "storage-unavailable",
    "store_busy": "storage-unavailable",
    "revision_conflict": "concurrency-conflict",
    "idempotency_conflict": "concurrency-conflict",
    "invariant_violation": "integrity-failure",
    "not_found": "integrity-failure",
    "protocol_unsupported": "runtime-compatibility",
    "internal": "internal-error",
}
# The features of the runtime hello that recovery reads and writes through, and the protocol
# that added the recovery records (minor 5).
RECOVERY_FEATURES = ("state.read", "state.write", "state.recovery")
RECOVERY_PROTOCOL = (1, 5)


@dataclass(frozen=True)
class RecoveryDiagnostic:
    """One finding of a check: what is wrong, where, and what would make it right."""

    code: str
    check: str
    message: str
    remedy: str | None = None
    location: str | None = None
    expected: object = None
    received: object = None
    suggestion: object = None
    severity: str = "error"
    advisory: str | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        if (
            self.code not in DIAGNOSTIC_CODES
            or self.check not in CHECKS
            or self.severity not in ("error", "warning")
            or (self.advisory is not None and self.advisory not in ADVISORIES)
            or (self.advisory is not None and self.severity != "warning")
        ):
            raise ValueError("unregistered recovery diagnostic")

    @property
    def id(self) -> str:
        """A stable identifier a console selects the diagnostic by."""
        key = json.dumps(
            [self.code, self.check, self.location, self.message, self.advisory]
        )
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    @property
    def fatal(self) -> bool:
        return self.severity == "error"

    def to_dict(self) -> JsonObject:
        return {
            "id": self.id,
            "code": self.code,
            "check": self.check,
            "message": sanitize_detail(self.message),
            "remedy": None if self.remedy is None else sanitize_detail(self.remedy),
            "pointer": self.location,
            "expected": safe_value(self.expected),
            "received_redacted": safe_value(self.received),
            "suggestion": safe_value(self.suggestion),
            "severity": self.severity,
            "fatal": self.fatal,
            "advisory": self.advisory,
            "correlation_id": self.correlation_id,
        }

    @classmethod
    def from_config(cls, item: Diagnostic) -> RecoveryDiagnostic:
        """A configuration diagnostic of the #682 boundary, kept with its pointer and values."""
        warning = item.severity == "warning"
        return cls(
            "config-advisory" if warning else "config-invalid",
            "config",
            f"{item.file}: {item.message}",
            remedy="run 'recovery fix' to preview the safe normalization"
            if item.recoverable
            else "edit .harness/orchestration.json at the pointer, then run 'recovery reload'",
            location=pointer(item.parts),
            expected=item.expected,
            received=item.received,
            suggestion=item.suggestion,
            severity=item.severity,
            advisory="config-normalized"
            if warning and item.code == "config-normalized"
            else None,
        )

    @classmethod
    def from_error(cls, check: str, exc: HarnessError) -> RecoveryDiagnostic:
        """A refusal of the core or the runtime, kept with its own remedy; never a
        configuration typo."""
        if isinstance(exc, RuntimeClientError):
            code = _RUNTIME_CODES.get(exc.code, "integrity-failure")
        elif isinstance(exc, RecoveryInternalError):
            code = "internal-error"
        elif isinstance(exc, OperationalHoldError) and exc.code in DIAGNOSTIC_CODES:
            code = exc.code
        elif isinstance(exc, LedgerBusyError):
            code = "storage-unavailable"
        else:
            code = "integrity-failure"
        return cls(code, check, exc.message, remedy=exc.remedy)


@dataclass
class CheckResult:
    """The result of one reconciliation check."""

    check: str
    status: str = "passed"
    detail: str = ""
    diagnostics: list[RecoveryDiagnostic] = field(default_factory=list)
    evidence: JsonObject | None = None

    def add(self, diagnostic: RecoveryDiagnostic, *, status: str | None = None) -> None:
        self.diagnostics.append(diagnostic)
        if status is not None:
            self.set(status)

    def set(self, status: str) -> None:
        """Raise the status; a refusal is never lowered by a later finding."""
        order = {"passed": 0, "skipped": 0, "reconciled": 1, "pending": 2, "refused": 3}
        if order[status] >= order[self.status]:
            self.status = status

    def to_dict(self, hidden: frozenset[str] = frozenset()) -> JsonObject:
        return {
            "check": self.check,
            "status": self.status,
            "detail": sanitize_detail(self.detail) if self.detail else "",
            "evidence": self.evidence,
            "diagnostics": [
                item.to_dict() for item in self.diagnostics if item.id not in hidden
            ],
        }


@dataclass(frozen=True)
class RecoverySession:
    """What one console or CLI session chose to hide; never persisted, never a policy."""

    skipped: frozenset[str] = frozenset()


class RecoveryInterrupted(CoordinatorError):
    """The operator cancelled, reached end of input or interrupted an action; its intent says so."""


class RecoveryInternalError(CoordinatorError):
    """An exception that is not a refusal: an internal invariant, never a configuration typo.
    It keeps the failure code, the step and the diagnostics of the action."""

    def __init__(self, action: str, exc: BaseException, correlation_id: str) -> None:
        detail = sanitize_detail(f"{type(exc).__name__}: {exc}") or type(exc).__name__
        super().__init__(
            f"recovery {action} hit an internal invariant violation (correlation "
            f"{correlation_id}): {detail}; nothing was decided on it",
            remedy=f"{INTERNAL_INVARIANT_REMEDY}; correlation {correlation_id}",
        )
        self.code = "internal-error"
        self.step = f"recovery-{action}"
        self.correlation_id = correlation_id
        self.diagnostics: JsonObject = {
            "detail": detail,
            "correlation_id": correlation_id,
            "internal_invariant": True,
        }


# -- durable intent ------------------------------------------------------------------------------


def intent_relative(batch_id: str) -> str:
    """The path of the recovery intent of ``batch_id`` in the store."""
    return f"{INTENT_DIRECTORY}/{batch_id}.json"


def _write_intent(repo: Path, root: Path, record: JsonObject, *, own: str | None) -> None:
    """Write ``record`` as the intent of its batch in a store unit of its own.

    ``own`` is the correlation ID a finish expects to find: an intent another action wrote since
    is never overwritten by this one."""
    batch_id = str(record["batch_id"])
    ledger = LifecycleLedger(root)
    with _ledger_lock(ledger):
        stored, _ = _read_store_record(root, intent_relative(batch_id))
        if own is not None and (stored is None or stored.get("correlation_id") != own):
            return
        path = ledger.records_root() / intent_relative(batch_id)
        with _ledger_errors():
            if stored is None:
                ledger.write_immutable(path, record)
            else:
                ledger.replace(path, record)


@dataclass
class Intent:
    """The durable record of one action: written before it runs, finished after it."""

    repo: Path
    root: Path
    record: JsonObject

    def finish(self, status: str, outcome: str | None, detail: str | None) -> None:
        if status not in INTENT_STATUSES:
            raise ValueError(f"unknown recovery intent status {status!r}")
        self.record.update(
            {
                "status": status,
                "outcome": outcome,
                "detail": None if detail is None else sanitize_detail(detail),
                "updated_at": _now(),
            }
        )
        _write_intent(
            self.repo, self.root, self.record, own=str(self.record["correlation_id"])
        )


def runtime_hello(repo: Path) -> JsonObject:
    """What the runtime of ``repo`` answers in ``hello``: its protocol, store and features."""
    from harness.orchestration.runtime_client import RuntimeClient

    reply = RuntimeClient(repo).call(
        "status", operation_id=f"recovery.status-{uuid.uuid4().hex}", timeout_seconds=30.0
    )
    features = reply.hello.get("features")
    return {
        "protocol": reply.hello.get("protocol"),
        "server_version": reply.hello.get("server_version"),
        "store_id": reply.hello.get("store_id"),
        "features": list(features) if isinstance(features, list) else [],
    }


def runtime_problems(hello: Mapping[str, object]) -> list[str]:
    """Why the runtime of ``hello`` cannot keep the recovery records; empty when it can."""
    problems = []
    protocol = hello.get("protocol")
    major, minor = RECOVERY_PROTOCOL
    if not (
        isinstance(protocol, dict)
        and protocol.get("major") == major
        and isinstance(protocol.get("minor"), int)
        and protocol["minor"] >= minor
    ):
        problems.append(
            f"the runtime speaks protocol {protocol!r}, recovery needs {major}.{minor} or a "
            "later minor"
        )
    if not hello.get("store_id"):
        problems.append("the runtime serves no store")
    features = hello.get("features")
    missing = [
        name
        for name in RECOVERY_FEATURES
        if not isinstance(features, list) or name not in features
    ]
    if missing:
        problems.append(f"the runtime lacks the features {', '.join(missing)}")
    return problems


def _require_recovery_store(repo: Path, root: Path, batch_id: str) -> None:
    """An action that writes an intent needs the runtime-store epoch, a runtime that keeps the
    recovery records and the batch's row in the store."""
    if not store_is_authority(root):
        raise CoordinatorError(
            "recovery runs on the runtime store; this repository is in the file-ledger epoch",
            remedy=FILE_EPOCH_REMEDY,
        )
    problems = runtime_problems(runtime_hello(repo))
    if problems:
        raise CoordinatorError(
            f"the runtime cannot keep the recovery records: {'; '.join(problems)}",
            remedy="update harness-runtime to the version of this harness and restart it "
            "with 'harness-runtime serve', then repeat the command",
        )
    with read_state(LifecycleLedger(root), repo):
        batch, _ = _read_store_record(root, f"batches/{batch_id}.json")
    if batch is None:
        raise CoordinatorError(
            f"the runtime store holds no batch {batch_id}",
            remedy="name a batch that 'coordinator.py batch status' lists",
        )


@contextmanager
def tracked(repo: Path, root: Path, batch_id: str, action: str) -> Iterator[Intent]:
    """Record the intent of ``action`` as pending, run it and record how it ended.

    A refusal ends ``refused``; cancel, end of input or an interrupt ends ``interrupted`` and is
    raised as a refusal; any other exception ends ``refused`` as an internal error with a
    correlation ID.  The caller finishes a normal run with its outcome.  Each write of the intent
    is a store unit of its own, so it is kept whatever the action does next."""
    if action not in ACTIONS:
        raise ValueError(f"unknown recovery action {action!r}")
    _require_recovery_store(repo, root, batch_id)
    moment = _now()
    intent = Intent(
        repo,
        root,
        {
            "contract": INTENT_CONTRACT,
            "batch_id": batch_id,
            "action": action,
            "status": "pending",
            "outcome": None,
            "detail": None,
            "requested_at": moment,
            "updated_at": moment,
            "correlation_id": str(uuid.uuid4()),
        },
    )
    _write_intent(repo, root, intent.record, own=None)
    try:
        yield intent
    except (KeyboardInterrupt, EOFError, asyncio.CancelledError) as exc:
        intent.finish("interrupted", None, type(exc).__name__)
        raise RecoveryInterrupted(
            f"recovery {action} was interrupted; its intent is recorded",
            remedy=f"run 'coordinator.py recovery status --batch {batch_id}' and repeat "
            f"'recovery {action}': every completed step is kept and nothing is repeated",
        ) from exc
    except HarnessError as exc:
        intent.finish("refused", "refused", exc.message)
        raise
    except Exception as exc:  # noqa: BLE001 -- recorded and raised as an internal invariant
        error = RecoveryInternalError(action, exc, intent.record["correlation_id"])
        intent.finish("refused", "refused", error.message)
        raise error from exc
    if intent.record["status"] == "pending":
        intent.finish("done", "done", None)


def read_intent(root: Path, batch_id: str) -> JsonObject | None:
    """The persisted intent a restarted console shows; an unreadable one is shown as such."""
    record, _ = _read_store_record(root, intent_relative(batch_id))
    if record is None:
        return None
    if record.get("contract") != INTENT_CONTRACT or record.get("batch_id") != batch_id:
        return {"contract": INTENT_CONTRACT, "unreadable": "integrity-failure"}
    return record


# -- the survey ----------------------------------------------------------------------------------


def open_entries(batch: JsonObject) -> list[JsonObject]:
    """The dispatches of the batch's active chain that are not settled yet."""
    from harness.orchestration.ledger.recovery_chain import active_dispatches
    from harness.orchestration.workflow.history import _settled

    return [entry for entry in active_dispatches(batch) if not _settled(entry)]


class Survey:
    """The read-only reconciliation checks of one batch; ``resume`` acts on their result."""

    def __init__(self, repo: Path, root: Path, batch_id: str) -> None:
        self.repo = repo
        self.root = root
        self.batch_id = batch_id
        self.checks: dict[str, CheckResult] = {
            name: CheckResult(name) for name in CHECKS
        }
        self.hello: JsonObject | None = None
        self.hold: operational_hold.Hold | None = None
        self.batch: JsonObject | None = None
        self.config: JsonObject | None = None
        self.config_document: JsonObject | None = None
        self.bundle: Bundle | None = None
        self.hold_reason: str | None = None

    # The checks run in CHECKS order; a check whose input is missing is skipped.
    def run(self) -> Survey:
        self._config()
        self._store()
        self._ledger()
        self._batch()
        if self.batch is None:
            for name in CHECKS[CHECKS.index("lifecycle") :]:
                if name != "operation":
                    self._skip(name, "the batch record is not readable")
            self._operation()
            return self
        self._lifecycle()
        self._runtime()
        self._snapshot()
        self._context()
        self._compatibility()
        self._operation()
        self._liveness()
        self._attention()
        self._approval()
        return self

    def _skip(self, name: str, detail: str) -> None:
        self.checks[name].status = "skipped"
        self.checks[name].detail = detail

    def _config(self) -> None:
        check = self.checks["config"]
        try:
            self.bundle = read_config_bundle(self.repo)
        except (ValueError, OSError) as exc:
            check.add(
                RecoveryDiagnostic(
                    "config-invalid",
                    "config",
                    f"the configuration cannot be read: {exc}",
                    remedy="repair .harness/orchestration.json, then run 'recovery reload'",
                ),
                status="refused",
            )
            return
        result = inspect_configuration(self.repo, self.bundle["orchestration.json"])
        for item in result.diagnostics:
            check.add(RecoveryDiagnostic.from_config(item))
        check.evidence = {
            "valid": result.valid,
            "source_sha256": result.source_sha256,
            "normalizer": result.to_dict()["normalizer"],
        }
        if not result.valid:
            check.set("refused")
            return
        self.config_document = result.document
        from harness.orchestration.core import config as core_config

        try:
            self.config = core_config._config(self.repo)
        except HarnessError as exc:
            check.add(RecoveryDiagnostic.from_error("config", exc), status="refused")

    def _store(self) -> None:
        """The runtime hello and the hold record of the batch: whether the store holds it."""
        check = self.checks["store"]
        try:
            self.hello = runtime_hello(self.repo)
            self.hold = operational_hold.current_hold(self.root, self.batch_id)
            _, revision = _read_store_record(
                self.root, operational_hold.hold_relative(self.batch_id)
            )
        except HarnessError as exc:
            check.add(RecoveryDiagnostic.from_error("store", exc), status="refused")
            return
        check.evidence = {
            "store_id": self.hello["store_id"],
            "hold_revision": revision,
            "hold": None if self.hold is None else self.hold.reason,
        }
        if self.hold is None:
            return
        held = self.hold
        diagnostic = RecoveryDiagnostic(
            "integrity-failure",
            "store",
            f"the runtime store holds the batch ({held.reason}): {held.message}",
            remedy=held.remedy,
            severity="error",
        )
        check.add(diagnostic, status="pending" if held.releasable else "refused")

    def _ledger(self) -> None:
        from harness.orchestration.ledger.ledger_ops import _ledger_errors

        check = self.checks["ledger"]
        try:
            with _ledger_errors():
                result = LifecycleLedger(self.root).validate()
        except HarnessError as exc:
            check.add(RecoveryDiagnostic.from_error("ledger", exc), status="refused")
            return
        check.evidence = {
            "version": result.get("version"),
            "generation": result.get("generation"),
        }

    def _batch(self) -> None:
        from harness.orchestration.ledger.ledger_ops import _load_batch
        from harness.orchestration.workflow.history import _validate_batch_integrity

        check = self.checks["batch"]
        try:
            batch = _load_batch(self.root, self.batch_id)
            _validate_batch_integrity(self.root, batch)
        except HarnessError as exc:
            check.add(RecoveryDiagnostic.from_error("batch", exc), status="refused")
            check.evidence = self._batch_row()
            if check.evidence["present"]:
                self.hold_reason = hold_reason_of(exc)
                check.evidence["hold_reason"] = self.hold_reason
            return
        self.batch = batch
        check.evidence = {
            "state": batch["state"],
            "next_action": batch.get("next_action"),
        }

    def _batch_row(self) -> JsonObject:
        """What the store holds for the batch, when the strict read refuses it."""
        try:
            row, revision = _read_store_record(self.root, f"batches/{self.batch_id}.json")
        except HarnessError as exc:
            return {"present": None, "error": sanitize_detail(exc.message)}
        if row is None:
            return {"present": False}
        state = row.get("state")
        return {
            "present": True,
            "revision": revision,
            "state": state if isinstance(state, str) else None,
        }

    def _lifecycle(self) -> None:
        batch = cast(JsonObject, self.batch)
        check = self.checks["lifecycle"]
        check.evidence = lifecycle_view(batch)
        if batch["state"] in TERMINAL_BATCH_STATES:
            check.add(
                RecoveryDiagnostic(
                    "terminal-refusal",
                    "lifecycle",
                    f"the batch is {batch['state']}: a terminal batch is never resumed",
                    remedy="create a new batch for further work; its history stays as it is",
                ),
                status="refused",
            )
        elif (route := stopped_route(batch)) is not None:
            check.evidence = {**check.evidence, "route": route}
            remedy = (
                "run 'recovery resume' with --approved-by, --approved-at and --note: it "
                "records the audited batch resume-stop"
                if route == "resume-stop"
                else "choose the gate to continue from with 'batch rewind --to <gate>' and a "
                "human approval; recovery never picks it"
            )
            check.add(
                RecoveryDiagnostic(
                    "approval-required",
                    "lifecycle",
                    f"the batch is {batch['state']}: its recorded stop is resumed only through "
                    f"the audited {route} with a human approval",
                    remedy=remedy,
                    severity="warning",
                ),
                status="pending",
            )

    def _runtime(self) -> None:
        """The harness runtime the batch's commands run on: the installed one, or the verified
        pinned snapshot they hand themselves to; recovery itself runs on the installed one."""
        from harness.orchestration.core import workspace
        from harness.orchestration.workflow.runtime_pin import _pinned_hash

        batch = cast(JsonObject, self.batch)
        check = self.checks["runtime"]
        pinned = _pinned_hash(batch)
        runs_on: str | None = "installed"
        try:
            workspace._validate_harness_runtime_snapshot(self.repo, batch)
        except HarnessError as exc:
            refusal, runs_on = exc, None
            try:
                if (
                    pinned is not None
                    and workspace._pinned_runtime_entry(self.root, pinned) is not None
                ):
                    runs_on = "pinned-snapshot"
            except HarnessError as snapshot:
                refusal = snapshot
            if runs_on is None:
                check.add(
                    RecoveryDiagnostic.from_error("runtime", refusal), status="refused"
                )
        check.evidence = {"pinned": pinned, "runs_on": runs_on}

    def _context(self) -> None:
        """Every open brief's Context Package verifies and still matches the batch's base and
        current developer candidate, by the rule the stale-evidence attention applies."""
        from harness.orchestration.ledger.ledger_ops import _load_context_package
        from harness.orchestration.workflow.history import (
            _current_developer_candidate,
            _effective_base,
            _pinned_package_stale,
            _validate_context_package,
        )

        batch = cast(JsonObject, self.batch)
        check = self.checks["context"]
        briefs = [
            (entry, dispatch)
            for entry, dispatch in self._briefs()
            if isinstance(dispatch.get("context_package_id"), str)
        ]
        evidence: list[JsonObject] = []
        try:
            current_base = _effective_base(batch)
            current_candidate = (
                _current_developer_candidate(self.repo, self.root, batch)
                if briefs
                else None
            )
            for entry, dispatch in briefs:
                dispatch_id = dispatch["dispatch_id"]
                location = f"/dispatches/{dispatch_id}/context_package_id"
                package = _load_context_package(
                    self.root, dispatch["context_package_id"]
                )
                _validate_context_package(self.root, batch, package)
                digest = hashlib.sha256(_canonical(package).encode("utf-8")).hexdigest()
                if digest != dispatch.get("context_package_sha256"):
                    check.add(
                        RecoveryDiagnostic(
                            "integrity-failure",
                            "context",
                            f"dispatch {dispatch_id}: its Context Package does not match the "
                            "hash its brief pinned",
                            remedy=INTERNAL_INVARIANT_REMEDY,
                            location=location,
                        ),
                        status="refused",
                    )
                    continue
                stale = entry.get("state") != "reported" and _pinned_package_stale(
                    self.repo, self.root, batch, dispatch
                )
                pinned = {
                    "base_commit": package["base_commit"],
                    "candidate_commit": package["candidate_commit"],
                }
                evidence.append(
                    {
                        "dispatch_id": dispatch_id,
                        "context_package_id": package["context_package_id"],
                        **pinned,
                        "stale": stale,
                    }
                )
                if stale:
                    check.add(
                        RecoveryDiagnostic(
                            "stale-evidence",
                            "context",
                            f"dispatch {dispatch_id} pins a Context Package older than the "
                            "batch's base or current developer candidate",
                            remedy="cancel the brief with dispatch cancel and propose a new "
                            "dispatch so a fresh Context Package is pinned; the brief is "
                            "never rewritten",
                            location=location,
                            expected={
                                "base_commit": current_base,
                                "candidate_commit": current_candidate,
                            },
                            received=pinned,
                        ),
                        status="refused",
                    )
        except HarnessError as exc:
            check.add(RecoveryDiagnostic.from_error("context", exc), status="refused")
            return
        check.evidence = {
            "current_base": current_base,
            "current_candidate": current_candidate,
            "briefs": evidence,
        }

    def _briefs(self) -> list[tuple[JsonObject, JsonObject]]:
        from harness.orchestration.ledger.ledger_ops import _load_dispatch

        batch = cast(JsonObject, self.batch)
        return [
            (entry, _load_dispatch(self.root, entry["dispatch_id"]))
            for entry in open_entries(batch)
        ]

    def _snapshot(self) -> None:
        from harness.orchestration.pinned_configuration import read_snapshot

        check = self.checks["snapshot"]
        pinned = []
        for _, dispatch in self._briefs():
            policy = dispatch.get("orchestration_policy")
            if not isinstance(policy, dict) or "configuration" not in policy:
                continue
            try:
                read_snapshot(dispatch)
            except HarnessError as exc:
                check.add(
                    RecoveryDiagnostic(
                        "integrity-failure",
                        "snapshot",
                        f"dispatch {dispatch['dispatch_id']}: {exc.message}",
                        remedy=f"{exc.remedy}; or cancel the unsent brief with dispatch "
                        "cancel and propose a new one",
                        location=f"/dispatches/{dispatch['dispatch_id']}/orchestration_policy"
                        "/configuration",
                    ),
                    status="refused",
                )
                continue
            pinned.append(dispatch["dispatch_id"])
        check.evidence = {"pinned": pinned}

    def _compatibility(self) -> None:
        """The runtime this coordinator talks to keeps the recovery records: the protocol, the
        store and the features of its hello."""
        check = self.checks["compatibility"]
        if self.hello is None:
            self._skip("compatibility", "the runtime hello is not readable")
            return
        check.evidence = dict(self.hello)
        problems = runtime_problems(self.hello)
        if problems:
            check.add(
                RecoveryDiagnostic(
                    "runtime-compatibility",
                    "compatibility",
                    f"the runtime cannot keep the recovery records: {'; '.join(problems)}",
                    remedy="update harness-runtime to the version of this harness and restart "
                    "it, then run 'recovery reload'",
                    expected={
                        "protocol": {
                            "major": RECOVERY_PROTOCOL[0],
                            "minor": RECOVERY_PROTOCOL[1],
                        },
                        "features": list(RECOVERY_FEATURES),
                    },
                    received={
                        "protocol": self.hello.get("protocol"),
                        "features": self.hello.get("features"),
                    },
                ),
                status="refused",
            )

    def _operation(self) -> None:
        """The observed execution of every open supervised dispatch: an ended one is the
        reaction resume applies, an unknown one an effect nothing proves."""
        from harness.orchestration.workflow import execution_reaction
        from harness.orchestration.workflow.delivery import observed_execution

        check = self.checks["operation"]
        if self.batch is None:
            return
        observed: list[JsonObject] = []
        try:
            for entry, dispatch in self._briefs():
                if not execution_reaction.supervised(self.root, dispatch):
                    continue
                execution = observed_execution(self.repo, entry["dispatch_id"])
                state = None if execution is None else execution.get("state")
                observed.append({"dispatch_id": entry["dispatch_id"], "execution": state})
                if state == "unknown":
                    check.add(
                        RecoveryDiagnostic(
                            "ambiguous-effect",
                            "operation",
                            f"the runtime does not know whether the worker of "
                            f"{entry['dispatch_id']} runs: resume refuses instead of starting "
                            "a second one",
                            remedy="stop the worker, or wait until the runtime observes it "
                            "again; a live or unproved worker is never started a second time",
                            severity="warning",
                        ),
                        status="pending",
                    )
                elif state is not None and (
                    state not in execution_reaction.UNSETTLED_EXECUTION_STATES
                ):
                    check.detail = (
                        "an ended execution: resume applies the reaction of the observed exit"
                    )
                    check.set("pending")
        except HarnessError as exc:
            check.add(RecoveryDiagnostic.from_error("operation", exc), status="refused")
        check.evidence = {"executions": observed}

    def _liveness(self) -> None:
        from harness.orchestration.ledger.ledger_ops import _load_dispatch_status

        check = self.checks["liveness"]
        live = []
        for entry, _ in self._briefs():
            status = _load_dispatch_status(self.root, entry["dispatch_id"])
            if status.get("state") not in LIVE_DISPATCH_STATES:
                continue
            silent = _silent_seconds(status)
            live.append({"dispatch_id": entry["dispatch_id"], "silent_seconds": silent})
            check.add(
                RecoveryDiagnostic(
                    "live-worker",
                    "liveness",
                    f"the worker of {entry['dispatch_id']} may still run (silent {silent}s): "
                    "resume starts no worker and creates no dispatch for it",
                    remedy="wait for its report, or stop it and run dispatch cancel "
                    "--runtime-stopped with an approval",
                    severity="warning",
                )
            )
        check.evidence = {"live": live}

    def _attention(self) -> None:
        batch = cast(JsonObject, self.batch)
        check = self.checks["attention"]
        if batch.get("needs_attention") is True:
            check.add(
                RecoveryDiagnostic(
                    "attention-open",
                    "attention",
                    f"the batch needs human attention ({batch.get('attention_reason')}): "
                    "recovery never resolves it",
                    remedy=f"{batch.get('recommended_human_action')}; resolve it with "
                    f"'batch attention resolve --batch {self.batch_id}' before 'recovery "
                    "resume'",
                ),
                status="refused",
            )
        check.evidence = {
            "needs_attention": bool(batch.get("needs_attention", False)),
            "attention_reason": batch.get("attention_reason"),
        }

    def _approval(self) -> None:
        check = self.checks["approval"]
        for entry, dispatch in self._briefs():
            if entry.get("state") != "approved":
                continue
            policy = dispatch.get("orchestration_policy")
            ttl = (self.config or {}).get("approval_ttl_seconds")
            if ttl is None and isinstance(policy, dict):
                ttl = policy.get("approval_ttl_seconds")
            approval = dispatch.get("coordinator_approval")
            approved_at = (
                approval.get("approved_at") if isinstance(approval, dict) else None
            )
            if isinstance(ttl, int) and isinstance(approved_at, str):
                age = (
                    datetime.now(UTC) - _moment(approved_at, "approved-at")
                ).total_seconds()
                if age > ttl:
                    check.add(
                        RecoveryDiagnostic(
                            "approval-stale",
                            "approval",
                            f"the approval of the unsent {dispatch['dispatch_id']} is "
                            f"{int(age)}s old and approval_ttl_seconds is {ttl}",
                            remedy="cancel the unsent brief with dispatch cancel and approve "
                            "a new proposal with its current transition digest; an expired "
                            "approval is never reused",
                        ),
                        status="refused",
                    )
            snapshot = policy.get("configuration") if isinstance(policy, dict) else None
            if (
                self.config is not None
                and isinstance(snapshot, dict)
                and snapshot.get("config") != dict(self.config)
            ):
                check.add(
                    RecoveryDiagnostic(
                        "approval-required",
                        "approval",
                        f"the configuration changed since {dispatch['dispatch_id']} was "
                        "approved: the brief keeps its approved snapshot",
                        remedy="the next transition needs an approval with the digest "
                        "'dispatch propose' prints under the current configuration",
                        severity="warning",
                    )
                )

    # -- the document ------------------------------------------------------------------------

    @property
    def resumable(self) -> bool:
        return all(check.status != "refused" for check in self.checks.values())

    def diagnostics(self) -> list[RecoveryDiagnostic]:
        return [item for name in CHECKS for item in self.checks[name].diagnostics]

    def document(self, session: RecoverySession | None = None) -> JsonObject:
        hidden = (session or RecoverySession()).skipped
        visible = [item for item in self.diagnostics() if item.id not in hidden]
        return {
            "contract": RECOVERY_CONTRACT,
            "batch_id": self.batch_id,
            "observed_at": _now(),
            "resumable": self.resumable,
            "lifecycle": None if self.batch is None else lifecycle_view(self.batch),
            "hold": None if self.hold is None else self.hold.to_dict(),
            "checks": [self.checks[name].to_dict(hidden) for name in CHECKS],
            "diagnostics": [item.to_dict() for item in visible],
            "skipped_advisories": sorted(hidden),
            "actions": self.actions(visible),
            "intent": read_intent(self.root, self.batch_id),
            "next": self.next_step(),
        }

    def fix_available(self) -> bool:
        """Whether the authored configuration has a safe normalization to write; a project
        without one gets none created."""
        authored = None if self.bundle is None else self.bundle["orchestration.json"]
        if authored is None or self.config_document is None:
            return False
        try:
            document: object = json.loads(authored)
        except ValueError:
            return False
        return document != self.config_document

    def actions(self, visible: list[RecoveryDiagnostic]) -> list[JsonObject]:
        advisories = [item for item in visible if item.advisory is not None]
        fix = self.fix_available()
        return [
            {
                "action": "fix",
                "available": fix,
                "reason": "the configuration has a safe normalization to apply"
                if fix
                else "no safe normalization: an error needs an authored edit",
            },
            {
                "action": "reload",
                "available": True,
                "reason": "re-read and validate the current configuration",
            },
            {
                "action": "resume",
                "available": self.resumable,
                "reason": "every check passed or resume reconciles it"
                if self.resumable
                else "a check refuses: resolve its diagnostics first",
            },
            {
                "action": "skip",
                "available": bool(advisories),
                "reason": "hide an allowlisted advisory for this session"
                if advisories
                else "no allowlisted advisory to hide",
            },
            {
                "action": "abort",
                "available": True,
                "reason": "leave recovery; nothing is abandoned, stopped or cleaned",
            },
        ]

    def next_step(self) -> JsonObject | None:
        if self.resumable:
            return {
                "command": f"recovery resume --batch {self.batch_id}",
                "reason": "every check passed or is reconciled by resume",
            }
        if self.checks["config"].status == "refused":
            if self.fix_available():
                command = "config validate --fix --dry-run"
            else:
                command = f"recovery reload --batch {self.batch_id}"
            return {
                "command": command,
                "reason": "the current configuration is invalid; edit it, then reload",
            }
        for name in CHECKS:
            for item in self.checks[name].diagnostics:
                if self.checks[name].status == "refused" and item.fatal:
                    return {"command": None, "reason": item.remedy or item.message}
        return None


_UNKNOWN_VALUE = re.compile(r"\binvalid \w+ value '")
_MISSING_VALUE = re.compile(
    r"\binvalid \w+ value None\b|\bmissing\b|\bincomplete\b|\brequires?\b"
)


def hold_reason_of(exc: HarnessError) -> str:
    """The hold reason of a strict-read refusal of the batch record: a value the registry does
    not list, a value that is absent, or a record that does not read at all."""
    if _UNKNOWN_VALUE.search(exc.message):
        return "unknown-value"
    if _MISSING_VALUE.search(exc.message):
        return "missing-value"
    return "unreadable-record"


def stopped_route(batch: JsonObject) -> str | None:
    from harness.orchestration.workflow import recovery

    return recovery.stopped_route(batch)


def lifecycle_view(batch: JsonObject) -> JsonObject:
    """The lifecycle facts recovery preserves and shows; it never changes them itself."""
    from harness.orchestration.workflow.recovery import STOPPED_STATES

    return {
        "state": batch["state"],
        "next_action": batch.get("next_action"),
        "terminal": batch["state"] in TERMINAL_BATCH_STATES,
        "stopped": batch["state"] in STOPPED_STATES,
        "needs_attention": bool(batch.get("needs_attention", False)),
        "attention_reason": batch.get("attention_reason"),
        "auto_stop": batch.get("auto_stop") is not None,
        "manual_recovery": bool(batch.get("manual_recovery", False)),
    }


# -- actions -------------------------------------------------------------------------------------


def _require_store_epoch(root: Path) -> None:
    if not store_is_authority(root):
        raise CoordinatorError(
            "recovery runs on the runtime store; this repository is in the file-ledger epoch",
            remedy=FILE_EPOCH_REMEDY,
        )


def status(
    repo: Path, root: Path, batch_id: str, *, session: RecoverySession | None = None
) -> JsonObject:
    """The read-only recovery status of ``batch_id``: it takes no lock and writes nothing."""
    _require_store_epoch(root)
    with read_state(LifecycleLedger(root), repo):
        return Survey(repo, root, batch_id).run().document(session)


def reload(repo: Path, root: Path, batch_id: str) -> JsonObject:
    """Re-read and validate the current configuration and the runtime hello; never starts a
    worker."""
    with tracked(repo, root, batch_id, "reload") as intent:
        survey = Survey(repo, root, batch_id)
        survey._config()
        check = survey.checks["config"]
        hello = runtime_hello(repo)
        problems = runtime_problems(hello)
        refused = check.status == "refused" or bool(problems)
        outcome = "refused" if refused else "done"
        intent.finish(outcome, outcome, "; ".join(problems) or None)
        return {
            "contract": RECOVERY_CONTRACT,
            "action": "reload",
            "batch_id": batch_id,
            "outcome": outcome,
            "config": check.evidence,
            "diagnostics": [item.to_dict() for item in check.diagnostics],
            "runtime": {**hello, "compatible": not problems, "problems": problems},
            "worker_started": False,
            "next": {"command": f"recovery resume --batch {batch_id}"}
            if outcome == "done"
            else {"command": "config validate --fix --dry-run"}
            if survey.fix_available()
            else None,
        }


def fix(repo: Path, root: Path, batch_id: str, *, apply: bool = False) -> JsonObject:
    """Preview, or with ``apply`` write through the configuration transaction, the safe
    normalization of the current configuration; nothing is resumed."""
    with tracked(repo, root, batch_id, "fix") as intent:
        survey = Survey(repo, root, batch_id)
        survey._config()
        check = survey.checks["config"]
        result: JsonObject = {
            "contract": RECOVERY_CONTRACT,
            "action": "fix",
            "batch_id": batch_id,
            "diagnostics": [item.to_dict() for item in check.diagnostics],
            "diff": None,
            "applied": False,
            "backup_version": None,
            "resumed": False,
            "next": {"command": f"recovery reload --batch {batch_id}"},
        }
        document = survey.config_document
        if not survey.fix_available() or survey.bundle is None or document is None:
            result["outcome"] = "refused" if check.status == "refused" else "done"
            intent.finish(result["outcome"], result["outcome"], "nothing to normalize")
            return result
        changes: Bundle = {"orchestration.json": json_bytes(document)}
        result["diff"] = bundle_diff(
            {"orchestration.json": survey.bundle["orchestration.json"]},
            changes,
            "current",
            "canonical",
        )
        if apply:
            try:
                backup = ConfigurationStore(repo).apply(changes, expected=survey.bundle)
            except (ValueError, OSError) as exc:
                result["diagnostics"].append(
                    RecoveryDiagnostic(
                        "config-invalid",
                        "config",
                        f"the configuration transaction refused the write: {exc}",
                        remedy="run 'recovery reload' and preview the fix again",
                    ).to_dict()
                )
                result["outcome"] = "refused"
                intent.finish("refused", "refused", str(exc))
                return result
            result.update({"applied": True, "backup_version": backup.id})
        result["outcome"] = "done"
        intent.finish("done", "done", "applied" if apply else "previewed")
        return result


def skip(
    session: RecoverySession, document: JsonObject, diagnostic_id: str
) -> RecoverySession:
    """Hide one allowlisted advisory of ``document`` for this session only; a fatal or
    unlisted diagnostic is refused, and nothing is written."""
    found = next(
        (
            item
            for item in cast(list[JsonObject], document.get("diagnostics", []))
            if item.get("id") == diagnostic_id
        ),
        None,
    )
    if found is None:
        raise CoordinatorError(
            f"no diagnostic {diagnostic_id} in this recovery status",
            remedy="select a diagnostic the current status lists",
        )
    if found.get("fatal") is not False or found.get("advisory") not in ADVISORIES:
        raise CoordinatorError(
            f"diagnostic {diagnostic_id} ({found.get('code')}) is not a skippable advisory",
            remedy="only an allowlisted advisory is hidden; schema, enum, integrity, "
            "compatibility, evidence and approval diagnostics are resolved, never skipped",
        )
    return RecoverySession(session.skipped | {diagnostic_id})


def abort(repo: Path, root: Path, batch_id: str) -> JsonObject:
    """Record that the operator left recovery and flush that intent; the batch, its workers
    and its worktree stay exactly as they are."""
    with tracked(repo, root, batch_id, "abort") as intent:
        intent.finish("aborted", "done", "the operator left recovery")
    survey = Survey(repo, root, batch_id)
    with read_state(LifecycleLedger(root), repo):
        survey._store()
        survey._batch()
        intent_record = read_intent(root, batch_id)
    return {
        "contract": RECOVERY_CONTRACT,
        "action": "abort",
        "batch_id": batch_id,
        "outcome": "done",
        "lifecycle": None if survey.batch is None else lifecycle_view(survey.batch),
        "hold": None if survey.hold is None else survey.hold.to_dict(),
        "abandoned": False,
        "worker_stopped": False,
        "worktree_cleaned": False,
        "intent": intent_record,
    }


# -- resume --------------------------------------------------------------------------------------


@dataclass
class _Run:
    """What one resume did, step by step; every step is safe to find again on a repeat."""

    steps: list[JsonObject] = field(default_factory=list)
    refused: list[RecoveryDiagnostic] = field(default_factory=list)

    def step(self, name: str, detail: str, **evidence: object) -> None:
        self.steps.append({"step": name, "detail": detail, **evidence})


def resume(
    repo: Path,
    root: Path,
    batch_id: str,
    *,
    approval: Mapping[str, str] | None = None,
) -> JsonObject:
    """Re-check ``batch_id``, release its hold and react to the observed execution of its open
    dispatches, then continue a recorded stop through the audited ``batch resume-stop`` when
    ``approval`` names the human, the time and the note.

    In one store unit every check of ``status`` runs first.  A refused check changes nothing,
    except that a batch whose record the strict read refuses is held in the store.  Otherwise
    the hold is released and the coordinator reacts to every open supervised dispatch as the
    runtime observes it: an ended execution gets its reaction, an unknown one is refused, and
    a live worker is never started a second time.  No approval, attention, budget, auto stop or
    terminal refusal is changed, no brief is rewritten and no dispatch is created: the next
    transition needs its own approval with the current digest."""
    run = _Run()
    with tracked(repo, root, batch_id, "resume") as intent:
        ledger = LifecycleLedger(root)
        with _ledger_lock(ledger):
            survey = Survey(repo, root, batch_id).run()
            refusals = [
                item
                for name in CHECKS
                if survey.checks[name].status == "refused"
                for item in survey.checks[name].diagnostics
                if item.fatal
            ]
            if refusals:
                run.refused.extend(refusals)
                _hold_unreadable(root, survey, run, refusals[0])
            else:
                _reconcile(repo, root, survey, run)
        if not run.refused and survey.batch is not None:
            _continue_stop(repo, root, batch_id, survey.batch, approval, run)
        outcome = "refused" if run.refused else "done"
        intent.finish(
            outcome, outcome, None if not run.refused else run.refused[0].message
        )
    with read_state(LifecycleLedger(root), repo):
        final = Survey(repo, root, batch_id).run()
        document = final.document()
        following = (
            _next_transition(root, final) if outcome == "done" else final.next_step()
        )
    return {
        "contract": RECOVERY_CONTRACT,
        "action": "resume",
        "batch_id": batch_id,
        "outcome": outcome,
        "steps": run.steps,
        "refusals": [item.to_dict() for item in run.refused],
        "dispatch_created": False,
        "worker_started": False,
        "status": document,
        "next": following,
    }


def _hold_unreadable(
    root: Path,
    survey: Survey,
    run: _Run,
    refusal: RecoveryDiagnostic,
) -> None:
    """Hold a batch whose record the strict read refuses, through its recovery record alone: no
    lifecycle record is written for it, and every command that would act on it stops on the
    hold.  A store that refused is not written either.  A readable batch is not held: the
    commands that repair it (dispatch cancel, attention resolve) keep working."""
    if (
        survey.batch is not None
        or survey.hold_reason is None
        or survey.checks["store"].status == "refused"
    ):
        return
    held = operational_hold.hold(root, survey.batch_id, survey.hold_reason, refusal.message)
    if held is not None:
        run.step(
            "hold",
            f"the batch is held ({held.reason}) until it reads and verifies again",
            hold=held.to_dict(),
        )


def _reconcile(repo: Path, root: Path, survey: Survey, run: _Run) -> None:
    """Release the hold, then react to the observed execution of every open supervised
    dispatch; the caller's store unit writes both."""
    from harness.orchestration.workflow import execution_reaction

    held = survey.hold
    if held is not None:
        operational_hold.release(root, survey.batch_id)
        run.step("release", f"the {held.reason} hold is released", hold=held.to_dict())
    for entry, dispatch in survey._briefs():
        if not execution_reaction.supervised(root, dispatch):
            continue
        try:
            execution = execution_reaction.observe(repo, root, entry["dispatch_id"])
        except HarnessError as exc:
            run.refused.append(RecoveryDiagnostic.from_error("operation", exc))
            return
        state = None if execution is None else execution.get("state")
        if state == "unknown":
            run.refused.append(
                RecoveryDiagnostic(
                    "ambiguous-effect",
                    "operation",
                    f"the runtime does not know whether the worker of {entry['dispatch_id']} "
                    "runs: nothing proves the outcome of its hand-off",
                    remedy="stop the worker, or wait until the runtime observes it again; a "
                    "live or unproved worker is never started a second time",
                )
            )
            return
        if state is not None and state not in execution_reaction.UNSETTLED_EXECUTION_STATES:
            run.step(
                "reconcile",
                f"the {state} execution of {entry['dispatch_id']} got its reaction",
                dispatch_id=entry["dispatch_id"],
                execution=state,
            )


def _continue_stop(
    repo: Path,
    root: Path,
    batch_id: str,
    batch: JsonObject,
    approval: Mapping[str, str] | None,
    run: _Run,
) -> None:
    """A recorded stop continues only through the audited ``batch resume-stop``."""
    import argparse

    from harness.orchestration.workflow.recovery import resume_stopped_batch

    route = stopped_route(batch)
    if route is None:
        return
    if route == "rewind" or approval is None:
        run.refused.append(
            RecoveryDiagnostic(
                "approval-required",
                "lifecycle",
                f"the {batch['state']} batch continues only through the audited {route}",
                remedy="repeat 'recovery resume' with --approved-by, --approved-at and --note"
                if route == "resume-stop"
                else "choose the gate with 'batch rewind --to <gate>' and a human approval",
            )
        )
        return
    try:
        result = resume_stopped_batch(
            argparse.Namespace(
                repo=str(repo),
                state_dir=str(root),
                batch=batch_id,
                approved_by=approval.get("approved_by"),
                approved_at=approval.get("approved_at"),
                note=approval.get("note"),
            )
        )
    except HarnessError as exc:
        run.refused.append(RecoveryDiagnostic.from_error("lifecycle", exc))
        return
    event = result.get("event")
    run.step(
        "resume-stop",
        "the recorded stop continues through the audited resume-stop event",
        event_sequence=event.get("sequence") if isinstance(event, dict) else None,
        state=result.get("state"),
    )


def _next_transition(root: Path, survey: Survey) -> JsonObject | None:
    """The command that continues the batch; a new transition needs its own approval."""
    from harness.orchestration.core.constants import NEXT_ACTION_DISPATCH_ROLE
    from harness.orchestration.ledger.ledger_ops import _load_dispatch_status

    batch = survey.batch
    if batch is None:
        return None
    for entry in open_entries(batch):
        dispatch_id = entry["dispatch_id"]
        state = _load_dispatch_status(root, dispatch_id).get("state")
        if entry.get("state") == "approved" and state == "approved":
            command = (
                f"qa run --dispatch {dispatch_id}"
                if entry.get("role") == "qa"
                else f"dispatch send --dispatch {dispatch_id}"
            )
            return {"command": command, "reason": "the approved brief was never sent"}
        if entry.get("state") == "reported":
            return {
                "command": f"batch decision-packet --batch {batch['batch_id']}",
                "reason": "a completion report awaits its decision",
            }
        if state in LIVE_DISPATCH_STATES:
            return {
                "command": f"dispatch wait --dispatch {dispatch_id}",
                "reason": "a worker may still run; no second one is started",
            }
    action = str(batch.get("next_action"))
    # A recovered stage names its role itself (``developer`` after a resume-stop).
    role = NEXT_ACTION_DISPATCH_ROLE.get(
        action, action if action in {"developer", "architect"} else None
    )
    if role is None or batch.get("state") in TERMINAL_BATCH_STATES:
        return None
    purpose = " --purpose publish" if action == "publish" else ""
    return {
        "command": f"dispatch propose --batch {batch['batch_id']} --role {role}{purpose}",
        "reason": "the next transition needs an approval with its current digest",
    }
