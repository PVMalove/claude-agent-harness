"""The QA lane in the runtime-store epoch: the lease that the runtime owns (ADR 0020, section 7;
ADR 0021, section 5).

The runtime grants, renews, releases, reaps and clears the one QA lease with typed operations of
``state.write`` (``lease_acquire``, ``lease_heartbeat``, ``lease_release``, ``lease_reap``,
``lease_clear``). It records the identity of the client process that acquires the lease, checks
the fencing token of every later operation and decides alone whether an owner is dead. This module
only stages those operations on the command's store unit, reads the lease from the command's view
and turns the runtime's refusals into coordinator errors. It never decides liveness, and it never
writes the lease with the generic mutations of the file lane.

The file-ledger epoch keeps the lane of ``qa_lane.py`` unchanged.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from harness.json_types import JsonObject
from harness.orchestration.core import state_view
from harness.orchestration.core.constants import QaOwnerKind
from harness.orchestration.core.utils import CoordinatorError
from harness.orchestration.ledger.lifecycle import LifecycleLedger, store_is_authority

LEASE_PATH = "qa-lane/lease.json"


def active(ledger: LifecycleLedger) -> bool:
    """Whether the runtime store owns the lane of ``ledger``."""
    return store_is_authority(ledger.root)


def _owner_document(kind: str, identity: str) -> JsonObject:
    return (
        {"dispatch_id": identity}
        if kind == QaOwnerKind.DISPATCH
        else {"owner_kind": kind, "owner_id": identity}
    )


def owner_of(lease: JsonObject) -> tuple[str, str]:
    """The owner kind and id of a lease in either shape."""
    return (
        str(lease.get("owner_kind", QaOwnerKind.DISPATCH)),
        str(lease.get("owner_id", lease.get("dispatch_id"))),
    )


def normalize(record: JsonObject) -> JsonObject:
    """The lease as the coordinator shows it: the fields of the file lane (the owner, ``host``,
    ``pid``, ``acquired_at``, ``expires_at``) and, from the runtime, ``heartbeat_at``,
    ``fencing_token`` and ``identity``. A lease of schema version 2 has no identity, and its
    fencing token is 0."""
    identity = record.get("identity")
    identity = identity if isinstance(identity, dict) else None
    acquired_at = record.get("acquired_at")
    token = record.get("fencing_token")
    return {
        **_owner_document(*owner_of(record)),
        "host": record.get("host", identity.get("host") if identity else None),
        "pid": record.get("pid", identity.get("pid") if identity else None),
        "acquired_at": acquired_at,
        "expires_at": record.get("expires_at"),
        "heartbeat_at": record.get("heartbeat_at", acquired_at),
        "fencing_token": token if isinstance(token, int) else 0,
        "identity": identity,
    }


def expired(lease: JsonObject) -> bool:
    """Whether the expiry of ``lease`` has passed: its owner missed its heartbeat
    (``stale_heartbeat``). An expiry that is no time has passed."""
    value = lease.get("expires_at")
    try:
        moment = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        moment = None
    return moment is None or moment.tzinfo is None or moment <= datetime.now(UTC)


def summary(lease: JsonObject | None) -> JsonObject | None:
    """The ``qa_lane`` object of ``dispatch status``: who holds the lane and whether its owner
    missed its heartbeat, or ``None`` for a free lane."""
    if lease is None:
        return None
    kind, identity = owner_of(lease)
    return {
        "owner_kind": kind,
        "owner_id": identity,
        "fencing_token": lease.get("fencing_token"),
        "acquired_at": lease.get("acquired_at"),
        "expires_at": lease.get("expires_at"),
        "heartbeat_at": lease.get("heartbeat_at", lease.get("acquired_at")),
        "stale_heartbeat": expired(lease),
    }


def _named(lease: JsonObject) -> JsonObject:
    kind, identity = owner_of(lease)
    token = lease.get("fencing_token")
    return {
        "owner_kind": kind,
        "owner_id": identity,
        "fencing_token": token if isinstance(token, int) else 0,
    }


def _unit(ledger: LifecycleLedger) -> state_view.StoreUnit:
    unit = state_view.current_unit()
    if unit is None or unit.view.root != ledger.root:
        raise CoordinatorError(
            "a QA lease operation of the runtime store runs outside a command's store unit",
            remedy="file a bug report: the QA lane must change the store inside _ledger_lock",
        )
    return unit


def acquire(
    ledger: LifecycleLedger, owner_id: str, *, owner_kind: str, lease_seconds: int
) -> JsonObject:
    """Stage ``lease_acquire`` and return the lease the runtime grants at the flush.

    The returned dict is filled in place when the command's store unit commits: the owner, the
    expiry, the fencing token and the identity the runtime recorded. Until then it holds only the
    owner, and ``release`` of it cancels the staged acquire."""
    lease: JsonObject = _owner_document(owner_kind, owner_id)

    def granted(answer: JsonObject | None) -> None:
        if answer is not None:
            lease.clear()
            lease.update(normalize(answer))

    _unit(ledger).lease(
        {
            "op": "lease_acquire",
            "path": LEASE_PATH,
            "lease": {
                "owner_kind": owner_kind,
                "owner_id": owner_id,
                "ttl_seconds": lease_seconds,
            },
        },
        on_answer=granted,
    )
    return lease


def release(ledger: LifecycleLedger, lease: JsonObject, *, reason: str) -> None:
    """Stage ``lease_release`` of exactly ``lease``; the runtime refuses another owner or token
    with ``fencing_mismatch``. A lease this command only staged is cancelled instead."""
    unit = _unit(ledger)
    if "fencing_token" not in lease:
        unit.cancel_lease(*owner_of(lease))
        return
    unit.lease(
        {
            "op": "lease_release",
            "path": LEASE_PATH,
            "lease": _named(lease),
            "reason": reason,
        }
    )


def clear(
    ledger: LifecycleLedger, lease: JsonObject, *, approval: JsonObject, reason: str
) -> None:
    """Stage ``lease_clear``: the operator override of a lease, behind a human approval."""
    _unit(ledger).lease(
        {
            "op": "lease_clear",
            "path": LEASE_PATH,
            "lease": _named(lease),
            "decision": approval,
            "reason": reason,
        }
    )


def reap(ledger: LifecycleLedger, lease: JsonObject, *, reason: str) -> None:
    """Stage ``lease_reap`` of the expired ``lease``: the runtime clears it only when its owner is
    provably dead, and answers ``lease_held`` otherwise."""
    _unit(ledger).lease(
        {
            "op": "lease_reap",
            "path": LEASE_PATH,
            "lease": _named(lease),
            "reason": reason,
        }
    )


def held_error(exc: Exception, facts: JsonObject) -> CoordinatorError:
    """The coordinator error of a ``lease_held`` refusal, with ``stale_heartbeat`` and the verdict."""
    owner = facts.get("owner")
    stale = facts.get("stale_heartbeat") is True
    verdict = facts.get("verdict")
    holder = (
        f"{owner.get('owner_kind')} {owner.get('owner_id')}"
        if isinstance(owner, dict)
        else "another owner"
    )
    return CoordinatorError(
        f"the QA lane is held by {holder} until {facts.get('expires_at')}"
        + (f" ({verdict})" if verdict else "")
        + f": {exc}",
        remedy=f"[lease_held] stale_heartbeat={str(stale).lower()}"
        + (f" verdict={verdict}" if verdict else "")
        + "; a lease is released by its owner, and one whose owner is provably dead "
        "(owner-dead) is reaped by the next 'qa run' after it expires; a live owner is never "
        "released. If the owner is known to be gone but cannot be proven dead, an operator "
        "clears the lease with 'qa clear-stale-lease' and an approval",
    )


@contextmanager
def refusals() -> Iterator[None]:
    """Turn a ``lease_held`` refusal of the runtime into a coordinator error that carries
    ``stale_heartbeat`` and the verdict."""
    from harness.orchestration.runtime_client import RuntimeClientError

    try:
        yield
    except RuntimeClientError as exc:
        if exc.code == "lease_held":
            raise held_error(exc, exc.facts) from exc
        raise


class Beat:
    """The heartbeat of one lease: ``lost`` once the runtime answered ``fencing_mismatch``,
    because an operator cleared the lease or another owner holds the lane."""

    def __init__(self) -> None:
        self.lost = False
        self.beats = 0


def _beat(repo: Path, lease: JsonObject, ttl_seconds: int, beat: Beat) -> bool:
    """Send one ``lease_heartbeat``; whether the heartbeat goes on."""
    from harness.orchestration.runtime_client import RuntimeClient, RuntimeClientError

    try:
        RuntimeClient(repo).call(
            "state.write",
            operation_id=f"lease-heartbeat-{uuid.uuid4().hex}",
            payload={
                "mutations": [
                    {
                        "op": "lease_heartbeat",
                        "path": LEASE_PATH,
                        "lease": {**_named(lease), "ttl_seconds": ttl_seconds},
                    }
                ]
            },
            timeout_seconds=max(1.0, ttl_seconds / 3),
        )
    except RuntimeClientError as exc:
        if exc.code == "fencing_mismatch":
            beat.lost = True
            return False
        # A busy store or a lost transport is repeated at the next beat.
        return True
    beat.beats += 1
    return True


@contextmanager
def heartbeat(
    ledger: LifecycleLedger, lease: JsonObject, ttl_seconds: int
) -> Iterator[Beat]:
    """Renew ``lease`` while the block runs (the QA gate): a background thread with its own
    runtime client sends ``lease_heartbeat`` every third of ``ttl_seconds`` and stops when the
    block ends. ``fencing_mismatch`` stops it and marks the lease lost. A lease of the file lane,
    which has no fencing token, gets no heartbeat."""
    beat = Beat()
    if not active(ledger) or not isinstance(lease.get("fencing_token"), int):
        yield beat
        return
    repo = ledger.repository()
    stop = threading.Event()

    def renew() -> None:
        while not stop.wait(ttl_seconds / 3):
            if not _beat(repo, lease, ttl_seconds, beat):
                return

    thread = threading.Thread(target=renew, name="qa-lease-heartbeat", daemon=True)
    thread.start()
    try:
        yield beat
    finally:
        stop.set()
        thread.join(timeout=max(1.0, ttl_seconds / 3) + 5)


def reap_expired(
    ledger: LifecycleLedger,
    lock: Callable[[LifecycleLedger], AbstractContextManager[None]],
    read: Callable[[LifecycleLedger], JsonObject | None],
    recover: Callable[[JsonObject], None],
) -> None:
    """Before a QA admission: reap an expired lease whose owner is provably dead.

    One request of its own (``lock`` is the command's ``_ledger_lock``): ``lease_reap`` of the
    expired lease the snapshot shows, and ``recover``, the policy of ``qa clear-stale-lease`` for
    the owner (its queue entry, its QA dispatch back to ``approved`` and a ``qa-lane/recoveries``
    record). The runtime commits it only for ``owner-dead``; another verdict is ``lease_held`` with
    ``stale_heartbeat``, and the request changes nothing. A lease another command reaped first is
    no error."""
    from harness.orchestration.runtime_client import RuntimeClientError

    if not active(ledger):
        return
    try:
        with refusals(), lock(ledger):
            lease = read(ledger)
            if lease is None or not expired(lease):
                return
            reap(ledger, lease, reason="reap the QA lease of a dead owner")
            recover(lease)
    except RuntimeClientError as exc:
        if exc.code != "fencing_mismatch":
            raise
