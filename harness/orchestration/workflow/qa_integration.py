"""The QA lane's CLI surface.

`qa_lane.py` owns the lane itself and deliberately never imports the coordinator; it receives the
slice it needs as its `ops` argument.  These four handlers are that hand-off point, and nothing
else.  The coordinator facade is imported inside each call: it is the module that carries the whole
`CoordinatorOps` surface, and importing it at module scope would close an import cycle.
"""

from __future__ import annotations

import argparse
from typing import cast

from harness.orchestration import qa_lane
from harness.orchestration.core.utils import JsonObject


def _ops() -> qa_lane.CoordinatorOps:
    from harness.orchestration import coordinator

    return cast(qa_lane.CoordinatorOps, coordinator)


def qa_evidence(args: argparse.Namespace) -> JsonObject:
    return qa_lane.qa_evidence(args, _ops())


def run_qa(args: argparse.Namespace) -> JsonObject:
    """Run the repository-scoped QA lane without coupling it to CLI wiring."""
    return qa_lane.run(args, _ops())


def qa_status(args: argparse.Namespace) -> JsonObject:
    return qa_lane.status(args, _ops())


def clear_qa_lease(args: argparse.Namespace) -> JsonObject:
    return qa_lane.clear_stale_lease(args, _ops())
