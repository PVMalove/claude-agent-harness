#!/usr/bin/env python3
"""Runtime-neutral coordinator for the backend-orchestration capability: the CLI entry point.

The coordinator owns batch, approval, dispatch and completion-report state.  A runtime adapter
is an explicitly selected transport: it receives a dispatch only after this CLI has persisted the
approved immutable brief.

This module is the facade over that lifecycle, and holds no logic of its own: it parses arguments,
routes `args.action` to a handler in `workflow/`, turns a `HarnessError` into an exit code, and
prints the result as JSON.  The lifecycle itself lives in `core/` (constants, configuration, git,
the workspace), `ledger/` (persistence) and `workflow/` (one module per stage).
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from typing import cast

# `harness/bin/harness`'s package_files() copies this file verbatim into target projects as
# `.harness/orchestration/coordinator.py` -- a different directory name than the source tree's
# `harness/`. Alias `harness` to whichever of the two this file actually lives under so
# `from harness...` resolves the same way in both places. See docs/adr/0018.
_HARNESS_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _HARNESS_ROOT.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if _HARNESS_ROOT.name != "harness":
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "harness", _HARNESS_ROOT / "__init__.py", submodule_search_locations=[str(_HARNESS_ROOT)]
    )
    assert _spec is not None and _spec.loader is not None
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules["harness"] = _pkg
    _spec.loader.exec_module(_pkg)

from harness.errors import HarnessError, print_and_exit
from harness.orchestration.coordinator_cli import build_parser
from harness.orchestration.core import constants

# -- CLI handlers -------------------------------------------------------------------------------
# One name per subcommand `coordinator_cli.build_parser` wires up; this module is the `handlers`
# module it resolves them on.
from harness.orchestration.ledger.ledger_admin import (
    clean_ledger as clean_ledger, ledger_status as ledger_status, migrate_ledger as migrate_ledger,
    reset_ledger as reset_ledger,
)
from harness.orchestration.workflow.attention import (
    attention_check as attention_check, attention_resolve as attention_resolve,
    record_context_pressure as record_context_pressure,
)
from harness.orchestration.workflow.batch import (
    abandon_batch as abandon_batch, approve_batch as approve_batch, create_batch as create_batch,
    list_batches as list_batches, mark_batch_not_required as mark_batch_not_required,
    preflight_batch as preflight_batch,
)
from harness.orchestration.workflow.context_package import (
    register_context_package as register_context_package,
)
from harness.orchestration.workflow.decisions import (
    decide_batch as decide_batch, decision_packet as decision_packet,
)
from harness.orchestration.workflow.dispatch import (
    cancel_dispatch as cancel_dispatch, create_dispatch as create_dispatch,
    dispatch_status as dispatch_status, preflight_dispatch as preflight_dispatch,
    publish_dispatch as publish_dispatch, send_dispatch as send_dispatch, wait_dispatch as wait_dispatch,
)
from harness.orchestration.workflow.qa_integration import (
    clear_qa_lease as clear_qa_lease, qa_evidence as qa_evidence, qa_status as qa_status, run_qa as run_qa,
)
from harness.orchestration.workflow.reports import (
    checkpoint_dispatch as checkpoint_dispatch, heartbeat_dispatch as heartbeat_dispatch,
    rate_limited_dispatch as rate_limited_dispatch, record_telemetry as record_telemetry,
    resume_dispatch as resume_dispatch, self_report_dispatch as self_report_dispatch,
    submit_report as submit_report,
)
from harness.orchestration.workflow.risk import assess_risk as assess_risk

# -- CoordinatorOps -----------------------------------------------------------------------------
# `qa_lane.py` deliberately never imports the coordinator; it receives the slice it needs as its
# `ops` argument, and this facade is the module that carries that whole slice. The names below
# exist here for no other reason -- see qa_lane.CoordinatorOps for the protocol they satisfy.
from harness.orchestration.core.constants import (
    QA_LEASE_FIELDS as QA_LEASE_FIELDS, QA_QUEUE_FIELDS as QA_QUEUE_FIELDS, STATE_REL as STATE_REL,
)
from harness.orchestration.core.utils import (
    CoordinatorError as CoordinatorError, _moment as _moment, _non_empty as _non_empty, _now as _now,
    _read_object as _read_object, _repo as _repo, _safe_id as _safe_id,
)
from harness.orchestration.core.git_utils import _candidate_commit as _candidate_commit
from harness.orchestration.core.config import _config as _config, _role as _role
from harness.orchestration.ledger.ledger_ops import (
    _load_batch as _load_batch, _load_dispatch as _load_dispatch,
    _load_dispatch_status as _load_dispatch_status,
)
from harness.orchestration.workflow.approval import _approval as _approval
from harness.orchestration.workflow.history import (
    _accepted_qa_for_candidate as _accepted_qa_for_candidate,
    _batch_for_ticket_branch as _batch_for_ticket_branch,
    _validate_batch_integrity as _validate_batch_integrity, _validate_dispatch as _validate_dispatch,
)
from harness.orchestration.workflow.reports import (
    _persist_report as _persist_report, _validate_report as _validate_report,
)


def parser() -> argparse.ArgumentParser:
    # The CLI's two module arguments are the two halves this facade routes between: the command
    # handlers it exposes, and the fixed default vocabulary they validate against.
    return build_parser(sys.modules[__name__], constants)


def main() -> int:
    # Git Bash on Windows can inherit a legacy Windows code page while displaying UTF-8.  Emit
    # UTF-8 independently of that inherited setting so JSON evidence is never merely *shown* as
    # corrupted and mistaken for a damaged state record.
    for stream in (sys.stdout, sys.stderr):
        try:
            cast(io.TextIOWrapper, stream).reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    args = parser().parse_args()
    try:
        output = args.handler(args)
    except HarnessError as exc:
        return print_and_exit(exc)
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
