"""The human approval a coordinator decision carries.

An approval is explicit and current: it names who approved and when, and a project may require it
to be typed on the controlling terminal rather than merely passed on the command line.  Every
lifecycle transition that needs a human goes through here.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from datetime import datetime, timedelta, timezone
from harness.orchestration.core.constants import (
    APPROVAL_CLOCK_SKEW_SECONDS,
)
from harness.orchestration.core.utils import (
    CoordinatorError, JsonObject, _moment, _non_empty, _repo,
)
from harness.orchestration.core import config as core_config
from harness.orchestration.core.config import (
    _approval_ttl, _human_approval_gate, _reject_sensitive,
)


def _confirm_on_terminal(approved_by: str, transition_digest: str | None = None) -> None:
    """Take the approval from the controlling terminal instead of from the calling session.

    `--approved-by` and `--approved-at` are only claims: a coordinator session holding a shell can
    type them itself, which is exactly how a gate gets advanced without the human ever seeing the
    decision packet. A line read from the real terminal cannot be produced by a non-interactive
    tool call, so under this gate the approval is the operator's or it does not happen.
    """
    bound = f" (transition {transition_digest[:12]})" if transition_digest else ""
    prompt = f"Type 'approve' to record this decision as {approved_by}{bound}: "
    try:
        if os.name == "nt":
            stream = open("CONIN$", "r", encoding="utf-8")  # noqa: SIM115 - closed below
            sink = open("CONOUT$", "w", encoding="utf-8")  # noqa: SIM115 - closed below
        else:
            stream = open("/dev/tty", "r", encoding="utf-8")  # noqa: SIM115 - closed below
            sink = open("/dev/tty", "w", encoding="utf-8")  # noqa: SIM115 - closed below
    except OSError as exc:
        raise CoordinatorError(
            "human_approval_gate is 'tty': this decision must be confirmed by a human on the "
            "terminal, and this session has none. Show the decision packet and have the operator "
            "run the same command in their own terminal.",
            remedy="show the decision packet and have a human operator run this same command in their own terminal",
        ) from exc
    try:
        sink.write(prompt)
        sink.flush()
        answer = stream.readline().strip().lower()
    finally:
        stream.close()
        sink.close()
    if answer != "approve":
        raise CoordinatorError("human approval was not confirmed on the terminal", remedy="run this same decision command in a real interactive terminal so it can prompt for confirmation")


def _require_current_approval(approved_at: str, config: JsonObject) -> None:
    """Fail closed on an approval outside its window; nothing here ever re-asks or re-uses one."""
    ttl = _approval_ttl(config)
    if ttl is None:
        return
    age = (datetime.now(timezone.utc) - _moment(approved_at, "approved-at")).total_seconds()
    if age > ttl:
        raise CoordinatorError(
            f"approval expired: approved-at is {int(age)}s old and approval_ttl_seconds is {ttl}",
            remedy="obtain a fresh human approval for the current transition; an expired approval is never reused",
        )
    if age < -APPROVAL_CLOCK_SKEW_SECONDS:
        raise CoordinatorError(
            "approval is dated in the future", remedy="pass the real time of the approval in --approved-at",
        )


def _approval(args: argparse.Namespace, transition_digest: str | None = None) -> JsonObject:
    approved_by = getattr(args, "approved_by", None)
    approved_at = getattr(args, "approved_at", None)
    if not _non_empty(approved_by) or not _non_empty(approved_at):
        raise CoordinatorError("explicit coordinator approval requires approved-by and approved-at", remedy="pass --approved-by and --approved-at for this decision")
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
