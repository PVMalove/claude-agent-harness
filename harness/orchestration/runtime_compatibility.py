"""Compatibility of runtimes, adapters and extensions with the ``systemFields.v1`` contract.

Every party that reads or writes the coordinator's system fields declares the contract version it
speaks: a runtime assignment of the project config (``system_fields_contract``; an in-process
runtime runs this harness's own role contract, so without the field it speaks the harness's
version), an external runtime adapter through its ``contract`` verb, and a project-owned extension
through its ``system_fields_contract`` attribute.  The built-in ``none`` extension speaks the
harness's version.

``dispatch preflight``, ``dispatch create`` and ``dispatch send`` check the declarations before a
brief is written or a worker starts; ``report submit`` checks the worker's confirmation before a
report is accepted.  A missing, unsupported or mismatched declaration is a structured
``runtime-compatibility`` refusal with evidence; nothing is started, written or accepted on it.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from harness.diagnostics import pointer, safe_value
from harness.json_types import JsonObject
from harness.orchestration import extensions
from harness.orchestration.core.system_fields import CONTRACT_VERSION
from harness.orchestration.runtime_state_store import RuntimeStateError, sanitize_detail

CONTRACT_FIELD = extensions.CONTRACT_ATTRIBUTE
# The contracts this harness reads and writes; a declaration of any other one is unsupported.
SUPPORTED_CONTRACTS = (CONTRACT_VERSION,)
# The adapter verb that answers ``{"system_fields_contract": "<version>"}`` on stdout.
ADAPTER_VERB = "contract"
# The verb runs before any hand-off, under the ledger lock, so its answer is bounded.
ADAPTER_TIMEOUT_SECONDS = 30
# How one declaration compares with the contract the dispatch runs under.
STATUSES = ("compatible", "missing", "unsupported", "mismatch")
# Who declared it.
SUBJECTS = (
    "runtime-assignment",
    "external-adapter",
    "extension",
    "dispatch-brief",
    "worker-self-report",
    "completion-report",
)
REMEDY = (
    "declare system_fields_contract {expected!r} for every listed party: a runtime assignment in "
    ".harness/orchestration.json, an external adapter through its 'contract' verb, a project "
    "extension through its system_fields_contract attribute, a worker through 'dispatch "
    "self-report --system-fields-contract'; then repeat the command, nothing was started or "
    "accepted"
)


def status_of(declared: object, expected: str = CONTRACT_VERSION) -> str:
    """The status of one declared version against ``expected``; never a default."""
    if not isinstance(declared, str) or not declared.strip():
        return "missing"
    if declared not in SUPPORTED_CONTRACTS:
        return "unsupported"
    return "compatible" if declared == expected else "mismatch"


@dataclass(frozen=True)
class Declaration:
    """One party's declared contract version and where it came from."""

    subject: str
    name: str
    source: str
    declared: object
    expected: str = CONTRACT_VERSION
    location: tuple[str | int, ...] | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.subject not in SUBJECTS:
            raise ValueError(f"unknown runtime compatibility subject {self.subject!r}")

    @property
    def status(self) -> str:
        return status_of(self.declared, self.expected)

    def to_dict(self) -> JsonObject:
        return {
            "subject": self.subject,
            "name": sanitize_detail(self.name),
            "source": self.source,
            "status": self.status,
            "declared": safe_value(self.declared),
            "expected": self.expected,
            "pointer": None if self.location is None else pointer(self.location),
            "detail": None if self.detail is None else sanitize_detail(self.detail),
        }

    def describe(self) -> str:
        shown = (
            "nothing" if self.status == "missing" else repr(safe_value(self.declared))
        )
        return (
            f"{self.subject} {self.name} declares {shown}: {self.status}, "
            f"expected {self.expected!r}"
        )


class RuntimeCompatibilityError(RuntimeStateError):
    """A ``runtime-compatibility`` refusal; ``evidence`` lists every declaration checked."""

    def __init__(
        self, step: str, declarations: Sequence[Declaration], *, remedy: str
    ) -> None:
        failed = [item for item in declarations if item.status != "compatible"]
        detail = "; ".join(item.describe() for item in failed) or "no declaration"
        super().__init__("runtime-compatibility", step, detail, remedy=remedy)
        self.message = (
            f"runtime compatibility check at {step} failed: {sanitize_detail(detail)}; "
            "nothing was started or accepted"
        )
        self.args = (self.message,)
        self.declarations = tuple(declarations)
        self.evidence: JsonObject = evidence(declarations)
        self.diagnostics["evidence"] = self.evidence


def evidence(declarations: Sequence[Declaration]) -> JsonObject:
    """The structured result of a check: compatible only when every declaration is."""
    items = [item.to_dict() for item in declarations]
    return {
        "contract": CONTRACT_VERSION,
        "supported": list(SUPPORTED_CONTRACTS),
        "compatible": all(item["status"] == "compatible" for item in items),
        "declarations": items,
    }


def require_compatible(step: str, declarations: Sequence[Declaration]) -> JsonObject:
    """The evidence of ``declarations`` when every one is compatible; else the refusal."""
    if all(item.status == "compatible" for item in declarations):
        return evidence(declarations)
    expected = next(
        (item.expected for item in declarations if item.status != "compatible"),
        CONTRACT_VERSION,
    )
    raise RuntimeCompatibilityError(
        step, declarations, remedy=REMEDY.format(expected=expected)
    )


def pinned_contract(dispatch: Mapping[str, object]) -> str | None:
    """The contract the brief was approved under (its pinned configuration snapshot), or
    ``None`` for a brief written before snapshots were pinned."""
    policy = dispatch.get("orchestration_policy")
    configuration = policy.get("configuration") if isinstance(policy, dict) else None
    value = configuration.get("contract") if isinstance(configuration, dict) else None
    return value if isinstance(value, str) else None


def dispatch_contract(dispatch: Mapping[str, object]) -> str:
    """The version every party of the brief must speak: its pinned one when this harness
    supports it, else this harness's own (the brief's declaration then fails on its own)."""
    pinned = pinned_contract(dispatch)
    if pinned is not None and pinned in SUPPORTED_CONTRACTS:
        return pinned
    return CONTRACT_VERSION


def brief_declaration(dispatch: Mapping[str, object]) -> Declaration:
    """The brief's own pinned contract; a legacy brief without a snapshot runs on this harness's."""
    pinned = pinned_contract(dispatch)
    return Declaration(
        "dispatch-brief",
        str(dispatch.get("dispatch_id")),
        "pinned-configuration" if pinned is not None else "built-in",
        pinned if pinned is not None else CONTRACT_VERSION,
        location=("orchestration_policy", "configuration", "contract"),
    )


def assignment_declaration(
    config: Mapping[str, object],
    role: str,
    runtime: str,
    transport: str,
    *,
    expected: str = CONTRACT_VERSION,
) -> Declaration | None:
    """The runtime assignment's declaration, or ``None`` when an external assignment states none:
    then its adapter declares at send.  An in-process runtime without the field runs this
    harness's role contract."""
    plans = config.get("assignment_plans")
    plan = plans.get(role) if isinstance(plans, dict) else None
    runtimes = plan.get("runtimes") if isinstance(plan, dict) else None
    assignment = runtimes.get(runtime) if isinstance(runtimes, dict) else None
    location = ("assignment_plans", role, "runtimes", runtime, CONTRACT_FIELD)
    name = f"{role}/{runtime}"
    if isinstance(assignment, dict) and CONTRACT_FIELD in assignment:
        return Declaration(
            "runtime-assignment",
            name,
            "config",
            assignment[CONTRACT_FIELD],
            expected,
            location,
        )
    if transport == "external":
        return None
    return Declaration(
        "runtime-assignment", name, "built-in", CONTRACT_VERSION, expected, location
    )


def require_assignment(
    config: Mapping[str, object],
    role: str,
    runtime: str,
    transport: str,
    *,
    step: str,
) -> JsonObject:
    """The evidence that a dispatch of ``role`` on ``runtime`` would run on compatible parties:
    its runtime assignment and the selected extensions.  An external runtime's adapter declares
    at ``dispatch send``, the only step that names it; ``checked_at_send`` says so."""
    assignment = assignment_declaration(config, role, runtime, transport)
    declarations = [assignment] if assignment is not None else []
    declarations += extension_declarations(extensions.selected(config))
    result = require_compatible(step, declarations)
    result["checked_at_send"] = ["external-adapter"] if transport == "external" else []
    return result


def extension_declarations(
    names: Mapping[str, str], *, expected: str = CONTRACT_VERSION
) -> list[Declaration]:
    """The declaration of every selected project extension; ``none`` is the harness's own."""
    declarations = []
    for kind in extensions.EXTENSION_KINDS:
        name = names.get(kind, extensions.DEFAULT_EXTENSION)
        if name == extensions.DEFAULT_EXTENSION:
            continue
        detail = None
        try:
            declared = getattr(
                extensions.implementation(kind, name), CONTRACT_FIELD, None
            )
        except extensions.ExtensionError as exc:
            declared, detail = None, exc.message
        declarations.append(
            Declaration(
                "extension",
                f"{kind}={name}",
                "extension",
                declared,
                expected,
                ("extensions", kind),
                detail,
            )
        )
    return declarations


def adapter_declaration(
    command: Sequence[str],
    adapter_args: Sequence[str],
    *,
    repo: Path,
    brief: Path,
    cwd: Path,
    expected: str = CONTRACT_VERSION,
) -> Declaration | None:
    """Ask an external adapter which contract it speaks; ``None`` when the adapter cannot be
    started at all, which the hand-off reports as an unavailable runtime.  Any other failure,
    a timeout or an answer that is not exactly ``{"system_fields_contract": <text>}`` declares
    nothing."""
    name = Path(command[-1]).name
    try:
        result = subprocess.run(
            [
                *command,
                ADAPTER_VERB,
                *adapter_args,
                "--repo",
                str(repo),
                "--brief",
                str(brief),
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=ADAPTER_TIMEOUT_SECONDS,
        )
    except OSError:
        return None
    except subprocess.TimeoutExpired:
        return Declaration(
            "external-adapter",
            name,
            "adapter",
            None,
            expected,
            detail=f"the '{ADAPTER_VERB}' verb did not answer in "
            f"{ADAPTER_TIMEOUT_SECONDS} seconds",
        )
    declared: object = None
    detail = None
    if result.returncode != 0:
        detail = f"the '{ADAPTER_VERB}' verb exited {result.returncode}"
    else:
        try:
            answer = json.loads(result.stdout)
        except ValueError:
            answer = None
        if isinstance(answer, dict) and set(answer) == {CONTRACT_FIELD}:
            declared = answer[CONTRACT_FIELD]
        else:
            detail = f"the '{ADAPTER_VERB}' verb answered no {CONTRACT_FIELD} object"
    return Declaration(
        "external-adapter", name, "adapter", declared, expected, detail=detail
    )


def dispatch_declarations(
    config: Mapping[str, object], dispatch: Mapping[str, object]
) -> list[Declaration]:
    """The declarations an approved brief runs on: its pinned contract, its runtime assignment
    in the configuration it was approved under, and the extensions frozen into it."""
    brief = brief_declaration(dispatch)
    expected = dispatch_contract(dispatch)
    declarations = [brief]
    assignment = assignment_declaration(
        config,
        str(dispatch.get("role")),
        str(dispatch.get("resolved_runtime")),
        str(dispatch.get("resolved_transport", "in-process")),
        expected=expected,
    )
    if assignment is not None:
        declarations.append(assignment)
    policy = dispatch.get("orchestration_policy")
    frozen = policy.get("extensions") if isinstance(policy, dict) else None
    names = (
        {str(kind): str(name) for kind, name in frozen.items()}
        if isinstance(frozen, dict)
        else extensions.selected(config)
    )
    declarations.extend(extension_declarations(names, expected=expected))
    return declarations


# The dispatch-status field that keeps the worker's optional confirmation as evidence.
SELF_REPORT_FIELD = "contract_self_report"


def self_report_declaration(
    dispatch: Mapping[str, object], declared: object
) -> Declaration:
    """The worker's confirmation in ``dispatch self-report`` against the brief's contract."""
    return Declaration(
        "worker-self-report",
        str(dispatch.get("dispatch_id")),
        "self-report",
        declared,
        dispatch_contract(dispatch),
    )


def self_report_evidence(declaration: Declaration, moment: str) -> JsonObject:
    """What the dispatch status keeps of a confirmation: bounded and redacted."""
    return {
        "reported": safe_value(declaration.declared),
        "expected": declaration.expected,
        "status": declaration.status,
        "reported_at": moment,
    }


def report_declarations(
    config: Mapping[str, object],
    dispatch: Mapping[str, object],
    status: Mapping[str, object],
    report: Mapping[str, object],
) -> list[Declaration]:
    """Every declaration a completion report is accepted on: the brief's, the worker's
    confirmation when its self-report gave one, and the report's own optional field.  A worker
    started before the confirmation existed confirms nothing, and its report is judged on the
    brief's declarations alone."""
    declarations = dispatch_declarations(config, dispatch)
    expected = dispatch_contract(dispatch)
    confirmation = status.get(SELF_REPORT_FIELD)
    if isinstance(confirmation, dict):
        declarations.append(
            self_report_declaration(dispatch, confirmation.get("reported"))
        )
    if CONTRACT_FIELD in report:
        declarations.append(
            Declaration(
                "completion-report",
                str(dispatch.get("dispatch_id")),
                "report",
                report[CONTRACT_FIELD],
                expected,
                (CONTRACT_FIELD,),
            )
        )
    return declarations
