"""The human approval a coordinator decision carries.

An approval is explicit and current: it names who approved and when, and a project may require it
to be typed on the controlling terminal rather than merely passed on the command line.  Every
lifecycle transition that needs a human goes through here.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from contextlib import ExitStack
from datetime import UTC, datetime

from harness.errors import INTERNAL_INVARIANT_REMEDY
from harness.orchestration.core import config as core_config
from harness.orchestration.core.config import (
    _approval_ttl,
    _human_approval_gate,
    _reject_sensitive,
)
from harness.orchestration.core.constants import (
    APPROVAL_CLOCK_SKEW_SECONDS,
    AUTO_DECISION_KINDS,
    AUTO_EVIDENCE_FIELDS,
)
from harness.orchestration.core.utils import (
    CoordinatorError,
    JsonObject,
    _canonical,
    _moment,
    _non_empty,
    _repo,
)

AUTO_POLICY = "auto"
AUTO_APPROVER = f"policy:{AUTO_POLICY}"


def auto_configured(config: JsonObject, batch: JsonObject) -> bool:
    """Whether both the project config and the batch plan chose ``approval_policy: auto``.

    A batch planned under another policy keeps it, and a project that left ``auto`` stops
    approving by policy, so both must agree (issue #643).
    """
    return (
        config.get("approval_policy") == AUTO_POLICY
        and batch.get("approval_policy") == AUTO_POLICY
    )


def auto_active(config: JsonObject, batch: JsonObject) -> bool:
    """Whether ``auto`` approves this batch's next step: configured and not stopped.

    A recorded ``auto_stop`` is irreversible for the batch: every later step needs a human.
    """
    return auto_configured(config, batch) and "auto_stop" not in batch


def sealed(body: JsonObject) -> JsonObject:
    """``body`` with the ``record_sha256`` of its canonical form, as every hashed record has."""
    return {
        **body,
        "record_sha256": hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest(),
    }


def record_auto(
    batch: JsonObject,
    *,
    kind: str,
    dispatch_id: str | None,
    rationale: str,
    evidence: JsonObject,
    moment: str,
) -> JsonObject:
    """Append one hashed ``policy:auto`` approval to ``batch.auto_decisions`` and return it.

    Only the in-memory batch changes; the caller persists it in the same ledger write as the
    approval it records.
    """
    if kind not in AUTO_DECISION_KINDS or set(evidence) != AUTO_EVIDENCE_FIELDS[kind]:
        raise CoordinatorError(
            f"auto decision {kind!r} has an invalid evidence shape",
            remedy=INTERNAL_INVARIANT_REMEDY,
        )
    records = batch.setdefault("auto_decisions", [])
    record = sealed(
        {
            "sequence": len(records) + 1,
            "kind": kind,
            "dispatch_id": dispatch_id,
            "approved_by": AUTO_APPROVER,
            "approved_at": moment,
            "rationale": rationale,
            "evidence": evidence,
        }
    )
    _reject_sensitive(record, "auto decision")
    records.append(record)
    return record


def _confirm_on_terminal(
    approved_by: str, transition_digest: str | None = None
) -> None:
    """Take the approval from the controlling terminal instead of from the calling session.

    `--approved-by` and `--approved-at` are only claims: a coordinator session holding a shell can
    type them itself, which is exactly how a gate gets advanced without the human ever seeing the
    decision packet. A line read from the real terminal cannot be produced by a non-interactive
    tool call, so under this gate the approval is the operator's or it does not happen.
    """
    bound = f" (transition {transition_digest[:12]})" if transition_digest else ""
    prompt = f"Type 'approve' to record this decision as {approved_by}{bound}: "
    reader, writer = (
        ("CONIN$", "CONOUT$") if os.name == "nt" else ("/dev/tty", "/dev/tty")
    )
    # The stack closes the input handle as well when the output handle cannot be opened.
    with ExitStack() as handles:
        try:
            stream = handles.enter_context(open(reader, "r", encoding="utf-8"))
            sink = handles.enter_context(open(writer, "w", encoding="utf-8"))
        except OSError as exc:
            raise CoordinatorError(
                "human_approval_gate is 'tty': this decision must be confirmed by a human on the "
                "terminal, and this session has none. Show the decision packet and have the "
                "operator run the same command in their own terminal.",
                remedy="show the decision packet and have a human operator run this same command in their own terminal",
            ) from exc
        sink.write(prompt)
        sink.flush()
        answer = stream.readline().strip().lower()
    if answer != "approve":
        raise CoordinatorError(
            "human approval was not confirmed on the terminal",
            remedy="run this same decision command in a real interactive terminal so it can prompt for confirmation",
        )


def _require_current_approval(approved_at: str, config: JsonObject) -> None:
    """Fail closed on an approval outside its window; nothing here ever re-asks or re-uses one."""
    ttl = _approval_ttl(config)
    if ttl is None:
        return
    age = (datetime.now(UTC) - _moment(approved_at, "approved-at")).total_seconds()
    if age > ttl:
        raise CoordinatorError(
            f"approval expired: approved-at is {int(age)}s old and approval_ttl_seconds is {ttl}",
            remedy="obtain a fresh human approval for the current transition; an expired approval is never reused",
        )
    if age < -APPROVAL_CLOCK_SKEW_SECONDS:
        raise CoordinatorError(
            "approval is dated in the future",
            remedy="pass the real time of the approval in --approved-at",
        )


def _approval(
    args: argparse.Namespace, transition_digest: str | None = None
) -> JsonObject:
    approved_by = getattr(args, "approved_by", None)
    approved_at = getattr(args, "approved_at", None)
    if not _non_empty(approved_by) or not _non_empty(approved_at):
        raise CoordinatorError(
            "explicit coordinator approval requires approved-by and approved-at",
            remedy="pass --approved-by and --approved-at for this decision",
        )
    result = {"approved_by": approved_by.strip(), "approved_at": approved_at.strip()}
    _reject_sensitive(result, "coordinator approval")
    try:
        config = core_config._config(_repo(args))
    except CoordinatorError:
        config = {}
    _require_current_approval(result["approved_at"], config)
    if _human_approval_gate(config) == "tty":
        _confirm_on_terminal(result["approved_by"], transition_digest)
    return result
