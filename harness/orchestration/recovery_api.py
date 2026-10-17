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
* ``resume`` (``recovery_commands``) reconciles the batch and releases its hold.

Every action except ``skip`` records its durable intent before it runs, so a console that is
cancelled, reaches end of input or is interrupted shows the same status after a restart.  Nothing
here reads standard input, waits for a terminal or imports a UI toolkit.  An exception that is not
a refusal is never shown as a configuration problem: it is an ``internal-error`` diagnostic with a
correlation ID.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
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
from harness.orchestration import extensions, operational_hold, runtime_compatibility
from harness.orchestration.config_boundary import inspect_configuration
from harness.orchestration.core.constants import (
    LIVE_DISPATCH_STATES,
    TERMINAL_BATCH_STATES,
)
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _moment,
    _now,
    _silent_seconds,
)
from harness.orchestration.ledger.lifecycle import LifecycleLedger
from harness.orchestration.runtime_reconcile import settle
from harness.orchestration.runtime_state_store import (
    RuntimeStateError,
    RuntimeStateStore,
    sanitize_detail,
)

RECOVERY_CONTRACT = "recoveryStatus.v1"
INTENT_CONTRACT = "recoveryIntent.v1"
ACTIONS = ("fix", "reload", "resume", "skip", "abort")
# The reconciliation checks, in the order they run.
CHECKS = (
    "config",
    "store",
    "ledger",
    "batch",
    "lifecycle",
    "snapshot",
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
ADVISORIES = ("config-normalized", "unconfirmed-contract")
# The runtime failure codes a refusal keeps as its diagnostic code.
_FAILURE_CODES = {
    "runtime-compatibility": "runtime-compatibility",
    "ambiguous-effect": "ambiguous-effect",
    "concurrency-conflict": "concurrency-conflict",
    "storage-unavailable": "storage-unavailable",
    "state-persist-failed": "storage-unavailable",
    "internal-error": "internal-error",
    "config-invalid": "config-invalid",
}


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
        """A refusal of the core, kept with its own remedy; never a configuration typo."""
        code = (
            _FAILURE_CODES.get(exc.code, "integrity-failure")
            if isinstance(exc, RuntimeStateError)
            else "integrity-failure"
        )
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


class RecoveryInternalError(RuntimeStateError):
    """An exception that is not a refusal: an internal invariant, never a configuration typo."""

    def __init__(self, action: str, exc: BaseException, correlation_id: str) -> None:
        detail = sanitize_detail(f"{type(exc).__name__}: {exc}") or type(exc).__name__
        super().__init__(
            "internal-error",
            f"recovery-{action}",
            detail,
            remedy=f"{INTERNAL_INVARIANT_REMEDY}; correlation {correlation_id}",
        )
        self.message = (
            f"recovery {action} hit an internal invariant violation (correlation "
            f"{correlation_id}): {detail}; nothing was decided on it"
        )
        self.args = (self.message,)
        self.correlation_id = correlation_id
        self.diagnostics["correlation_id"] = correlation_id
        self.diagnostics["internal_invariant"] = True


# -- durable intent ------------------------------------------------------------------------------


@dataclass
class Intent:
    """The durable record of one action: written before it runs, finished after it."""

    store: RuntimeStateStore
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
        self.store.write_recovery_intent(self.record["batch_id"], self.record)


@contextmanager
def tracked(store: RuntimeStateStore, batch_id: str, action: str) -> Iterator[Intent]:
    """Record the intent of ``action`` as pending, run it and record how it ended.

    A refusal ends ``refused``; cancel, end of input or an interrupt ends ``interrupted`` and is
    raised as a refusal; any other exception ends ``refused`` as an internal error with a
    correlation ID.  The caller finishes a normal run with its outcome."""
    if action not in ACTIONS:
        raise ValueError(f"unknown recovery action {action!r}")
    moment = _now()
    intent = Intent(
        store,
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
    store.write_recovery_intent(batch_id, intent.record)
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


def read_intent(store: RuntimeStateStore, batch_id: str) -> JsonObject | None:
    """The persisted intent a restarted console shows; an unreadable one is shown as such."""
    try:
        return store.read_recovery_intent(batch_id)
    except RuntimeStateError as exc:
        return {"contract": INTENT_CONTRACT, "unreadable": exc.code}


# -- the survey ----------------------------------------------------------------------------------


def open_entries(batch: JsonObject) -> list[JsonObject]:
    """The dispatches of the batch's active chain that are not settled yet."""
    from harness.orchestration.ledger.recovery_chain import active_dispatches
    from harness.orchestration.workflow.history import _settled

    return [entry for entry in active_dispatches(batch) if not _settled(entry)]


def config_declarations(config: Mapping[str, object]) -> JsonObject:
    """The compatibility of every runtime assignment and selected extension of ``config``."""
    declarations: list[runtime_compatibility.Declaration] = []
    plans = config.get("assignment_plans")
    for role, plan in (plans if isinstance(plans, dict) else {}).items():
        if not isinstance(plan, dict):
            continue
        runtimes = plan.get("runtimes")
        transport = str(plan.get("transport", "in-process"))
        for runtime in runtimes if isinstance(runtimes, dict) else {}:
            declaration = runtime_compatibility.assignment_declaration(
                config, str(role), str(runtime), transport
            )
            if declaration is not None:
                declarations.append(declaration)
    declarations += runtime_compatibility.extension_declarations(
        extensions.selected(config)
    )
    return runtime_compatibility.evidence(declarations)


class Survey:
    """The read-only reconciliation checks of one batch; ``resume`` acts on their result."""

    def __init__(self, repo: Path, root: Path, batch_id: str) -> None:
        self.repo = repo
        self.root = root
        self.batch_id = batch_id
        self.checks: dict[str, CheckResult] = {
            name: CheckResult(name) for name in CHECKS
        }
        self.store: RuntimeStateStore | None = None
        self.record: JsonObject | None = None
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
        self._snapshot()
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
        check = self.checks["store"]
        try:
            self.store = RuntimeStateStore(self.repo, self.root)
            self.record = self.store.read(self.batch_id)
        except RuntimeStateError as exc:
            check.add(RecoveryDiagnostic.from_error("store", exc), status="refused")
            if self.store is not None:
                self.hold = operational_hold.current_hold(self.store, self.batch_id)
            return
        self.hold = operational_hold.current_hold(self.store, self.batch_id)
        record = self.record
        if record is not None:
            operation = record["operation"]
            check.evidence = {
                "state": record["state"],
                "revision": record["revision"],
                "failure": record["failure"],
                "operation": None
                if operation is None
                else {
                    "kind": operation["kind"],
                    "phase": operation["phase"],
                    "effect": operation["effect"]["state"],
                },
            }
        if self.hold is None:
            return
        held = self.hold
        diagnostic = RecoveryDiagnostic(
            "runtime-compatibility"
            if held.reason == "runtime-compatibility"
            else "integrity-failure",
            "store",
            f"the runtime state store holds the batch ({held.reason}): {held.message}",
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
        from harness.orchestration.ledger.ledger_admin import batch_inspection
        from harness.orchestration.ledger.ledger_ops import (
            LedgerUnmigratedError,
            _load_batch,
        )
        from harness.orchestration.workflow.history import _validate_batch_integrity

        check = self.checks["batch"]
        try:
            batch = _load_batch(self.root, self.batch_id)
            _validate_batch_integrity(self.root, batch)
        except HarnessError as exc:
            self.hold_reason = (
                "unknown-value"
                if isinstance(exc, LedgerUnmigratedError)
                else "unreadable-record"
            )
            check.add(RecoveryDiagnostic.from_error("batch", exc), status="refused")
            check.evidence = batch_inspection(self.repo, self.root, self.batch_id)
            return
        self.batch = batch
        check.evidence = {
            "state": batch["state"],
            "next_action": batch.get("next_action"),
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
        check = self.checks["compatibility"]
        results: JsonObject = {}
        for _, dispatch in self._briefs():
            if self.checks["snapshot"].status == "refused":
                break
            try:
                results[dispatch["dispatch_id"]] = runtime_compatibility.require_brief(
                    self.config or {}, dispatch, step="recovery"
                )
            except runtime_compatibility.RuntimeCompatibilityError as exc:
                results[dispatch["dispatch_id"]] = exc.evidence
                check.add(
                    RecoveryDiagnostic(
                        "runtime-compatibility",
                        "compatibility",
                        exc.message,
                        remedy=exc.remedy,
                        expected=runtime_compatibility.dispatch_contract(dispatch),
                        received=[
                            item.declared
                            for item in exc.declarations
                            if item.status != "compatible"
                        ],
                    ),
                    status="refused",
                )
            except HarnessError as exc:
                check.add(
                    RecoveryDiagnostic.from_error("compatibility", exc),
                    status="refused",
                )
        if self.config is not None:
            current = config_declarations(self.config)
            results["current_config"] = current
            if not current["compatible"]:
                check.add(
                    RecoveryDiagnostic(
                        "runtime-compatibility",
                        "compatibility",
                        "the current configuration declares an incompatible runtime or "
                        "extension for the next transition",
                        remedy="declare a supported system_fields_contract, then run "
                        "'recovery reload'",
                        expected=list(runtime_compatibility.SUPPORTED_CONTRACTS),
                    ),
                    status="refused",
                )
        check.evidence = results

    def _operation(self) -> None:
        check = self.checks["operation"]
        record = self.record
        operation = None if record is None else record["operation"]
        if operation is None or operation["phase"] == "committed":
            return
        check.evidence = {
            "kind": operation["kind"],
            "phase": operation["phase"],
            "effect": operation["effect"]["state"],
        }
        check.detail = (
            "an unfinished operation: resume reconciles it from its durable intent and never "
            "repeats a confirmed step or a hand-off"
        )
        if operation["effect"]["state"] == "unknown":
            check.add(
                RecoveryDiagnostic(
                    "ambiguous-effect",
                    "operation",
                    f"the {operation['kind']} may have started a worker: resume asks its "
                    "adapter or provider and stops when nothing proves the outcome",
                    remedy="stop the worker, or let its adapter answer observe; a live or "
                    "unproved worker is never started a second time",
                    severity="warning",
                ),
            )
        check.set("pending")

    def _liveness(self) -> None:
        from harness.orchestration.ledger.ledger_ops import _load_dispatch_status

        check = self.checks["liveness"]
        live = []
        for entry, dispatch in self._briefs():
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
            if "contract_self_report" not in status and status.get("model_self_report"):
                check.add(
                    RecoveryDiagnostic(
                        "runtime-compatibility",
                        "liveness",
                        f"the worker of {entry['dispatch_id']} did not confirm its "
                        "systemFields contract",
                        remedy="the next session confirms it with dispatch self-report "
                        "--system-fields-contract",
                        expected=runtime_compatibility.dispatch_contract(dispatch),
                        severity="warning",
                        advisory="unconfirmed-contract",
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
                    "'batch attention resolve'",
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
            "intent": None
            if self.store is None
            else read_intent(self.store, self.batch_id),
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


def _store(repo: Path, root: Path, batch_id: str) -> RuntimeStateStore:
    store = RuntimeStateStore(repo, root)
    store.path(batch_id)
    return store


def status(
    repo: Path, root: Path, batch_id: str, *, session: RecoverySession | None = None
) -> JsonObject:
    """The read-only recovery status of ``batch_id``: it takes no lock and writes nothing."""
    return Survey(repo, root, batch_id).run().document(session)


def reload(repo: Path, root: Path, batch_id: str) -> JsonObject:
    """Re-read and validate the current configuration; never starts a worker."""
    with tracked(_store(repo, root, batch_id), batch_id, "reload") as intent:
        survey = Survey(repo, root, batch_id)
        survey._config()
        check = survey.checks["config"]
        compatibility = (
            None if survey.config is None else config_declarations(survey.config)
        )
        refused = check.status == "refused" or not (
            compatibility is None or compatibility["compatible"]
        )
        outcome = "refused" if refused else "done"
        intent.finish(outcome, outcome, None)
        return {
            "contract": RECOVERY_CONTRACT,
            "action": "reload",
            "batch_id": batch_id,
            "outcome": outcome,
            "config": check.evidence,
            "diagnostics": [item.to_dict() for item in check.diagnostics],
            "runtime_compatibility": compatibility,
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
    with tracked(_store(repo, root, batch_id), batch_id, "fix") as intent:
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
    store = _store(repo, root, batch_id)
    with tracked(store, batch_id, "abort") as intent:
        intent.finish("aborted", "done", "the operator left recovery")
    survey = Survey(repo, root, batch_id)
    survey._store()
    survey._batch()
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
        "intent": read_intent(store, batch_id),
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
    """Reconcile ``batch_id`` and release its hold, then continue a recorded stop through the
    audited ``batch resume-stop`` when ``approval`` names the human, the time and the note.

    Under the ledger lock every check of ``status`` runs first.  A refused check changes
    nothing, except that a batch the ledger cannot read is held in the store.  Otherwise the
    hold is released and ``settle`` finishes an unfinished operation from its durable intent: a
    confirmed step is never repeated, and an ambiguous effect or a live worker stops instead of
    starting a second one.  No approval, attention, budget, auto stop or terminal refusal is
    changed, no brief is rewritten and no dispatch is created: the next transition needs its own
    approval with the current digest."""
    from harness.orchestration.ledger.ledger_ops import _ledger_lock

    store = _store(repo, root, batch_id)
    run = _Run()
    with tracked(store, batch_id, "resume") as intent:
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
                _hold_unreadable(store, survey, run, refusals[0])
            else:
                _reconcile(repo, root, ledger, store, survey, run)
        if not run.refused and survey.batch is not None:
            _continue_stop(repo, root, batch_id, survey.batch, approval, run)
        outcome = "refused" if run.refused else "done"
        intent.finish(
            outcome, outcome, None if not run.refused else run.refused[0].message
        )
    final = Survey(repo, root, batch_id).run()
    return {
        "contract": RECOVERY_CONTRACT,
        "action": "resume",
        "batch_id": batch_id,
        "outcome": outcome,
        "steps": run.steps,
        "refusals": [item.to_dict() for item in run.refused],
        "dispatch_created": False,
        "worker_started": False,
        "status": final.document(),
        "next": _next_transition(root, final)
        if outcome == "done"
        else final.next_step(),
    }


def _hold_unreadable(
    store: RuntimeStateStore,
    survey: Survey,
    run: _Run,
    refusal: RecoveryDiagnostic,
) -> None:
    """Hold a batch whose ledger record does not read, through the store alone: no lifecycle
    record is written for it, and every command that would act on it stops on the hold.  A
    store that refused is not written either.  A readable batch is not held: the commands that
    repair it (dispatch cancel, attention resolve) keep working."""
    if (
        survey.batch is not None
        or survey.hold_reason is None
        or survey.checks["store"].status == "refused"
    ):
        return
    held = operational_hold.hold(
        store, survey.batch_id, survey.hold_reason, refusal.message
    )
    if held is not None:
        run.step(
            "hold",
            f"the batch is held ({held.reason}) until it reads and verifies again",
            hold=held.to_dict(),
        )


def _reconcile(
    repo: Path,
    root: Path,
    ledger: LifecycleLedger,
    store: RuntimeStateStore,
    survey: Survey,
    run: _Run,
) -> None:
    """Release the hold, then finish an unfinished operation; the caller holds the lock."""
    held = survey.hold
    if held is not None:
        operational_hold.release(store, survey.batch_id)
        run.step("release", f"the {held.reason} hold is released", hold=held.to_dict())
    try:
        reconciled = settle(repo, root, ledger, survey.batch_id)
    except RuntimeStateError as exc:
        run.refused.append(RecoveryDiagnostic.from_error("operation", exc))
        return
    if reconciled:
        run.step(
            "reconcile",
            "the unfinished operation was finished from its durable intent",
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
