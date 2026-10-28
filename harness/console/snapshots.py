"""Typed console facts for clients that cannot import the TUI: standard library only.

`build_home_snapshot` is the one mapping from the dashboard data and the Home context to JSON-safe
values; the Home screen of the TUI shows the same facts. Nothing here imports `rich`, `textual`,
`h11` or `websockets`."""

from __future__ import annotations

import asyncio
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

PROBLEMS_LIMIT = 100
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


def _clip(text: str, limit: int) -> str:
    """At most `limit` UTF-8 bytes of `text`, cut on a character boundary."""
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _repo_map_card(data: DashboardData, view: RepoMapView | None) -> RepoMapCard:
    found = _TIER.search(data.repo_map_tier)
    tier = found.group(1) if found else (view.tier if view is not None else None)
    head_map = (
        HeadMap(
            origin=view.origin,
            commit=view.commit,
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
        state=MemoryState(check.status, _clip(check.message, MESSAGE_BYTES))
        if check is not None
        else None,
        policy=MemoryPolicy(
            enabled=status.enabled,
            active=status.active,
            source_types=status.source_types,
            indexed=status.indexed,
            problem=_clip(status.problem, MESSAGE_BYTES),
            inactive=status.inactive,
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
            Problem(status, _clip(check_id, ID_BYTES), _clip(message, MESSAGE_BYTES))
            for status, check_id, message in data.problems[:PROBLEMS_LIMIT]
        ),
        problems_total=len(data.problems),
        harness_version=data.harness_version,
        drift=data.drift_state,
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
        self._lock = asyncio.Lock()
        self._snapshot: HomeSnapshot | None = None
        self._stamp = 0.0

    def _fresh(self) -> Served | None:
        if self._snapshot is None:
            return None
        age = self._clock() - self._stamp
        return Served(self._snapshot, True, age) if age < self._ttl else None

    async def home(self, *, force: bool = False) -> Served:
        """The cached snapshot while it is younger than the TTL, else a rebuilt one. `force`
        always rebuilds. A failed collect raises and leaves the cache as it was."""
        if not force and (served := self._fresh()) is not None:
            return served
        async with self._lock:
            # A rebuild that finished while this request waited serves it too.
            if not force and (served := self._fresh()) is not None:
                return served
            snapshot = await asyncio.to_thread(self._collect, self._repo)
            self._snapshot, self._stamp = snapshot, self._clock()
            return Served(snapshot, False, 0.0)
