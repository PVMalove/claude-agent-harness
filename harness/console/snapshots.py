"""Typed console facts for clients that cannot import the TUI: standard library only.

`build_home_snapshot` is the one mapping from the dashboard data and the Home context to JSON-safe
values; the Home screen of the TUI shows the same facts. Nothing here imports `rich`, `textual`,
`h11` or `websockets`."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from .data import DashboardData, collect_dashboard
from .repo_map import RepoMapView, cached_map, head_commit
from .repo_map_settings import MemoryStatus, memory_status

SCHEMA_VERSION = 1
HOME_TTL_SECONDS = 15.0

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 4096
MAX_ID_CHARS = 64
HOME_REQUEST = "home.snapshot"

PROBLEMS_LIMIT = 100
SOURCE_TYPES_LIMIT = 32
MESSAGE_BYTES = 300
ID_BYTES = 100

_TIER = re.compile(r"tier=(\w+)")


@dataclass(frozen=True)
class HomeContext:
    """What the context cards show beside the health facts: the Repo Map cached for HEAD and the
    state of project memory. Either is None when the console cannot read it."""

    repo_map: RepoMapView | None = None
    memory: MemoryStatus | None = None


def collect_context(repo: Path) -> HomeContext:
    """Read the Repo Map cached for the project's HEAD and the memory status; builds nothing."""
    head = head_commit(repo)
    return HomeContext(
        repo_map=cached_map(repo, head) if head else None, memory=memory_status(repo)
    )


@dataclass(frozen=True)
class HealthCounters:
    ok: int
    warn: int
    fail: int
    skipped: int


@dataclass(frozen=True)
class Problem:
    status: str
    id: str
    message: str


@dataclass(frozen=True)
class HeadMap:
    """The Repo Map cached for HEAD."""

    origin: str
    commit: str
    files: int
    edges: int
    estimated_tokens: int


@dataclass(frozen=True)
class RepoMapCard:
    tier: str | None
    message: str
    head_map: HeadMap | None  # None: there is no map for HEAD


@dataclass(frozen=True)
class MemoryState:
    """The `memory.index` health check."""

    status: str
    message: str


@dataclass(frozen=True)
class MemoryPolicy:
    enabled: bool
    active: bool
    source_types: tuple[str, ...]
    indexed: int | None
    problem: str
    inactive: str
    top_k: int | None
    max_tokens: int | None


@dataclass(frozen=True)
class MemoryCard:
    state: MemoryState | None
    policy: MemoryPolicy | None


@dataclass(frozen=True)
class HomeSnapshot:
    version: int
    health: HealthCounters
    problems: tuple[Problem, ...]
    problems_total: int
    harness_version: str
    drift: str
    active_batches: int | None  # None: orchestration is not connected
    repo_map: RepoMapCard
    memory: MemoryCard

    def to_payload(self) -> dict[str, object]:
        return asdict(self)


_SHORT_ESCAPES = frozenset('"\\\b\f\n\r\t')
# json.loads accepts "\ud800"; the str cannot be encoded as UTF-8, so a reply carries it escaped.
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _wire_cost(char: str) -> int:
    """Bytes `char` takes inside a JSON string: a quote, a backslash or a short escape is two,
    any other control character and a lone surrogate (sent as `\\udXXX`) is six, everything
    else is its UTF-8 length."""
    if char in _SHORT_ESCAPES:
        return 2
    if char < " " or _LONE_SURROGATE.match(char):
        return 6
    return len(char.encode("utf-8"))


def _clip(text: str, limit: int) -> str:
    """The longest prefix of `text` that takes at most `limit` bytes as a JSON string."""
    used = 0
    for index, char in enumerate(text[:limit]):
        used += _wire_cost(char)
        if used > limit:
            return text[:index]
    return text[:limit]


def _repo_map_card(data: DashboardData, view: RepoMapView | None) -> RepoMapCard:
    found = _TIER.search(data.repo_map_tier)
    tier = found.group(1) if found else (view.tier if view is not None else None)
    head_map = (
        HeadMap(
            origin=_clip(view.origin, ID_BYTES),
            commit=_clip(view.commit, ID_BYTES),
            files=len(view.files),
            edges=len(view.edges),
            estimated_tokens=view.estimated_tokens,
        )
        if view is not None
        else None
    )
    return RepoMapCard(tier, _clip(data.repo_map_tier, MESSAGE_BYTES), head_map)


def _memory_card(data: DashboardData, status: MemoryStatus | None) -> MemoryCard:
    check = data.memory_index
    return MemoryCard(
        state=MemoryState(
            _clip(check.status, ID_BYTES), _clip(check.message, MESSAGE_BYTES)
        )
        if check is not None
        else None,
        policy=MemoryPolicy(
            enabled=status.enabled,
            active=status.active,
            source_types=tuple(
                _clip(name, ID_BYTES) for name in status.source_types[:SOURCE_TYPES_LIMIT]
            ),
            indexed=status.indexed,
            problem=_clip(status.problem, MESSAGE_BYTES),
            inactive=_clip(status.inactive, MESSAGE_BYTES),
            top_k=status.top_k,
            max_tokens=status.max_tokens,
        )
        if status is not None
        else None,
    )


def build_home_snapshot(data: DashboardData, context: HomeContext) -> HomeSnapshot:
    """Map the dashboard data and the Home context to JSON-safe values. Problems are capped so
    that any reply stays below the message limit; `problems_total` keeps the real count."""
    return HomeSnapshot(
        version=SCHEMA_VERSION,
        health=HealthCounters(data.ok, data.warn, data.fail, data.skipped),
        problems=tuple(
            Problem(
                _clip(status, ID_BYTES),
                _clip(check_id, ID_BYTES),
                _clip(message, MESSAGE_BYTES),
            )
            for status, check_id, message in data.problems[:PROBLEMS_LIMIT]
        ),
        problems_total=len(data.problems),
        harness_version=_clip(data.harness_version, ID_BYTES),
        drift=_clip(data.drift_state, ID_BYTES),
        active_batches=data.active_batches,
        repo_map=_repo_map_card(data, context.repo_map),
        memory=_memory_card(data, context.memory),
    )


def collect_home(
    repo: Path,
    *,
    dashboard: Callable[[Path], DashboardData] = collect_dashboard,
    context: Callable[[Path], HomeContext] = collect_context,
) -> HomeSnapshot:
    """Run the offline checks and read the context cards; blocks until both are done."""
    return build_home_snapshot(dashboard(repo), context(repo))


@dataclass(frozen=True)
class Served:
    """A snapshot as served: whether it came from the cache and how old it is."""

    snapshot: HomeSnapshot
    cached: bool
    age_seconds: float


class SnapshotService:
    """Serves the Home snapshot from a short cache so that the expensive checks (health, drift)
    do not rerun on every request. One rebuild runs at a time and never on the event loop."""

    def __init__(
        self,
        repo: Path,
        *,
        ttl: float = HOME_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        collect: Callable[[Path], HomeSnapshot] = collect_home,
    ) -> None:
        self._repo = repo
        self._ttl = ttl
        self._clock = clock
        self._collect = collect
        self._building: asyncio.Task[HomeSnapshot] | None = None
        self._snapshot: HomeSnapshot | None = None
        self._stamp = 0.0

    def _fresh(self) -> Served | None:
        if self._snapshot is None:
            return None
        age = self._clock() - self._stamp
        return Served(self._snapshot, True, age) if age < self._ttl else None

    async def _rebuild(self) -> HomeSnapshot:
        """The collect, as a task of its own: it records its result and ends the rebuild even
        when every request that waited for it was cancelled."""
        try:
            snapshot = await asyncio.to_thread(self._collect, self._repo)
            self._snapshot, self._stamp = snapshot, self._clock()
            return snapshot
        finally:
            self._building = None

    async def home(self, *, force: bool = False) -> Served:
        """The cached snapshot while it is younger than the TTL, else a rebuilt one. `force`
        always rebuilds. A failed collect raises and leaves the cache as it was. A request that
        is cancelled while it waits does not cancel the collect: the next one waits for it."""
        while True:
            # A rebuild that finished while this request waited serves it too.
            if not force and (served := self._fresh()) is not None:
                return served
            if self._building is None:
                break
            await asyncio.wait({self._building})
        task = self._building = asyncio.create_task(self._rebuild())
        # Nobody may be left to read the outcome of a collect whose requester went away.
        task.add_done_callback(lambda done: done.cancelled() or done.exception())
        await asyncio.wait({task})
        return Served(task.result(), False, 0.0)


_ERRORS = {
    "bad_json": "Request is not valid JSON.",
    "bad_request": "Request has a missing or invalid field.",
    "unknown_type": "Unknown request type.",
    "too_large": "Request is too large.",
    "busy": "Too many unfinished requests.",
    "collect_failed": "Snapshot collection failed.",
}


@dataclass(frozen=True)
class _Request:
    id: str | int | None
    type: str
    force: bool


class Rejected(Exception):
    """A request that cannot be served, with whatever of its id and type could be trusted."""

    def __init__(
        self, code: str, id: str | int | None = None, type: str | None = None
    ) -> None:
        super().__init__(code)
        self.code, self.id, self.type = code, id, type


def parse_envelope(raw: str) -> tuple[dict[str, object], str | int | None, str]:
    """The request object with its id and type; `Rejected` carries what of them could be trusted."""
    if len(raw.encode("utf-8", errors="replace")) > MAX_REQUEST_BYTES:
        raise Rejected("too_large")
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        raise Rejected("bad_json") from None
    if not isinstance(body, dict):
        raise Rejected("bad_request")
    request_id = body.get("id")
    if "id" in body and not (
        (isinstance(request_id, str) and len(request_id) <= MAX_ID_CHARS)
        or (isinstance(request_id, int) and not isinstance(request_id, bool))
    ):
        raise Rejected("bad_request")
    kind = body.get("type")
    if not isinstance(kind, str):
        raise Rejected("bad_request", request_id)
    return body, request_id, kind


def _parse(raw: str) -> _Request:
    body, request_id, kind = parse_envelope(raw)
    if kind != HOME_REQUEST:
        raise Rejected("unknown_type", request_id, kind[:MAX_ID_CHARS])
    force = body.get("force", False)
    if not isinstance(force, bool):
        raise Rejected("bad_request", request_id, kind)
    return _Request(request_id, kind, force)


def encode(reply: dict[str, object]) -> str:
    """The reply as text that encodes as UTF-8 whatever it echoes."""
    text = json.dumps(reply, separators=(",", ":"), ensure_ascii=False)
    return _LONE_SURROGATE.sub(lambda found: f"\\u{ord(found.group()):04x}", text)


def error_reply(
    code: str,
    id: str | int | None,
    type: str | None,
    message: str | None = None,
    job_id: str | None = None,
) -> str:
    error: dict[str, object] = {"code": code, "message": message or _ERRORS[code]}
    if job_id is not None:
        error["job_id"] = job_id
    return encode(
        {
            "v": PROTOCOL_VERSION,
            "id": id,
            "type": type,
            "ok": False,
            "error": error,
        }
    )


def busy(raw: str) -> str:
    """The reply to a request that arrived while too many were unfinished; echoes what it can."""
    try:
        request = _parse(raw)
    except Rejected as rejected:
        return error_reply("busy", rejected.id, rejected.type)
    return error_reply("busy", request.id, request.type)


async def respond(raw: str, snapshots: SnapshotService) -> str:
    """The reply to one request frame. Never raises: every failure is an error reply."""
    try:
        request = _parse(raw)
    except Rejected as rejected:
        return error_reply(rejected.code, rejected.id, rejected.type)
    try:
        served = await snapshots.home(force=request.force)
    except Exception as exc:
        return error_reply(
            "collect_failed", request.id, request.type, type(exc).__name__
        )
    return encode(
        {
            "v": PROTOCOL_VERSION,
            "id": request.id,
            "type": request.type,
            "ok": True,
            "cached": served.cached,
            "age_seconds": round(served.age_seconds, 3),
            "snapshot": served.snapshot.to_payload(),
        }
    )
