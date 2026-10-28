"""The Home snapshot layer: typed facts mapped from the dashboard data, without TUI dependencies."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from harness.console.data import DashboardData
from harness.console.repo_map import MapEdge, MapFile, RepoMapView
from harness.console.repo_map_settings import MemoryStatus
from harness.console.snapshots import (
    HOME_TTL_SECONDS,
    MAX_REQUEST_BYTES,
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    HomeContext,
    HomeSnapshot,
    SnapshotService,
    build_home_snapshot,
    busy,
    respond,
)
from harness.health.model import CheckResult

ROOT = Path(__file__).parents[2]

DATA = DashboardData(
    ok=14,
    warn=1,
    fail=2,
    skipped=11,
    repo_map_tier="Repo Map: tier=full (offline parser bundle detected)",
    harness_version="1.4.6",
    drift_state="drift",
    active_batches=3,
    problems=(
        ("fail", "files.skill_snapshot", "snapshot differs from 1.4.6"),
        ("fail", "files.integrations", "tracker token missing"),
        ("warn", "memory.index", "index is stale"),
    ),
    memory_index=CheckResult(
        id="memory.index", group="memory", status="ok", message="индекс валиден (FTS5)"
    ),
)
VIEW = RepoMapView(
    payload={},
    origin="кэш",
    commit="a" * 40,
    tier="minimal",
    parser="tree-sitter",
    degradation_reason="",
    estimated_tokens=38400,
    provenance={},
    files=tuple(MapFile(f"f{index}.py", (), "ok") for index in range(4)),
    edges=tuple(MapEdge("f1.py", "f0.py", "import", "high") for _ in range(7)),
    diagnostics=(),
)
MEMORY = MemoryStatus(True, True, ("adr", "glossary"), 18, top_k=5, max_tokens=1000)


def _wire(snapshot: HomeSnapshot) -> dict[str, object]:
    """The payload as a client reads it: tuples are JSON arrays."""
    return json.loads(json.dumps(snapshot.to_payload()))  # type: ignore[no-any-return]


def test_constants_are_the_documented_contract() -> None:
    assert SCHEMA_VERSION == 1
    assert HOME_TTL_SECONDS == 15.0


def test_snapshot_layer_imports_and_collects_without_tui_dependencies(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "project"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    code = """
import importlib.abc, json, sys
class Absent(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'rich', 'textual', 'h11', 'websockets'}:
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, Absent())
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from harness.console.snapshots import collect_home
snapshot = collect_home(Path(sys.argv[2]))
loaded = sorted(set(sys.modules) & {'rich', 'textual', 'h11', 'websockets'})
print(json.dumps({'loaded': loaded, 'version': snapshot.version}))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(ROOT), str(repo)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr[-500:]
    assert json.loads(result.stdout) == {"loaded": [], "version": SCHEMA_VERSION}


def test_snapshot_mirrors_the_home_facts_with_a_cached_map() -> None:
    snapshot = build_home_snapshot(DATA, HomeContext(repo_map=VIEW, memory=MEMORY))
    assert _wire(snapshot) == {
        "version": 1,
        "health": {"ok": 14, "warn": 1, "fail": 2, "skipped": 11},
        "problems": [
            {
                "status": "fail",
                "id": "files.skill_snapshot",
                "message": "snapshot differs from 1.4.6",
            },
            {
                "status": "fail",
                "id": "files.integrations",
                "message": "tracker token missing",
            },
            {"status": "warn", "id": "memory.index", "message": "index is stale"},
        ],
        "problems_total": 3,
        "harness_version": "1.4.6",
        "drift": "drift",
        "active_batches": 3,
        "repo_map": {
            "tier": "full",
            "message": "Repo Map: tier=full (offline parser bundle detected)",
            "head_map": {
                "origin": "кэш",
                "commit": "a" * 40,
                "files": 4,
                "edges": 7,
                "estimated_tokens": 38400,
            },
        },
        "memory": {
            "state": {"status": "ok", "message": "индекс валиден (FTS5)"},
            "policy": {
                "enabled": True,
                "active": True,
                "source_types": ["adr", "glossary"],
                "indexed": 18,
                "problem": "",
                "inactive": "",
                "top_k": 5,
                "max_tokens": 1000,
            },
        },
    }


def test_snapshot_without_map_memory_or_orchestration_keeps_nulls() -> None:
    data = DashboardData(
        ok=0,
        warn=0,
        fail=0,
        skipped=3,
        repo_map_tier="не установлен",
        harness_version="1.4.6",
        drift_state="missing",
        active_batches=None,
    )
    payload = _wire(build_home_snapshot(data, HomeContext()))
    assert payload["active_batches"] is None
    assert payload["drift"] == "missing"
    assert payload["problems"] == [] and payload["problems_total"] == 0
    assert payload["repo_map"] == {
        "tier": None,
        "message": "не установлен",
        "head_map": None,
    }
    assert payload["memory"] == {"state": None, "policy": None}


def test_map_tier_falls_back_to_the_cached_map_when_the_check_names_none() -> None:
    data = DashboardData(
        ok=0,
        warn=0,
        fail=0,
        skipped=0,
        repo_map_tier="не установлен",
        harness_version="1",
        drift_state="clean",
        active_batches=0,
    )
    card = build_home_snapshot(data, HomeContext(repo_map=VIEW)).repo_map
    assert card.tier == "minimal" and card.head_map is not None


def test_problems_are_capped_and_the_reply_stays_below_the_message_limit() -> None:
    from harness.console.server import MESSAGE_LIMIT

    long = "я" * 500
    problems = tuple(("fail", f"check.{index}", long) for index in range(1000))
    data = DashboardData(
        ok=0,
        warn=0,
        fail=1000,
        skipped=0,
        repo_map_tier="не установлен",
        harness_version="1",
        drift_state="clean",
        active_batches=None,
        problems=problems,
    )
    snapshot = build_home_snapshot(data, HomeContext())
    assert isinstance(snapshot, HomeSnapshot)
    assert len(snapshot.problems) == 100 and snapshot.problems_total == 1000
    assert all(len(problem.message) <= 300 for problem in snapshot.problems)
    assert snapshot.problems[0].id == "check.0"
    encoded = json.dumps(snapshot.to_payload(), ensure_ascii=False).encode()
    assert len(encoded) < MESSAGE_LIMIT


@pytest.mark.parametrize("char", ['"', "\\", "\x01", "\n", "я", "😀"])
def test_the_whole_reply_stays_below_the_message_limit_whatever_the_text(
    char: str,
) -> None:
    """JSON escaping inflates quotes, backslashes (x2) and control characters (x6): the limit
    holds for the serialized reply of any text, not for the raw text."""
    from harness.console.server import MESSAGE_LIMIT

    text = char * 500
    data = DashboardData(
        ok=0,
        warn=0,
        fail=250,
        skipped=0,
        repo_map_tier=text,
        harness_version=text,
        drift_state=text,
        active_batches=None,
        problems=tuple((text, text, text) for _ in range(250)),
        memory_index=CheckResult(id="memory.index", group="memory", status="ok", message=text),
    )
    memory = MemoryStatus(
        True, False, (text,) * 40, 3, problem=text, inactive=text, top_k=5, max_tokens=9
    )
    view = RepoMapView(
        payload={},
        origin=text,
        commit=text,
        tier="minimal",
        parser="tree-sitter",
        degradation_reason="",
        estimated_tokens=1,
        provenance={},
        files=(),
        edges=(),
        diagnostics=(),
    )
    snapshot = build_home_snapshot(data, HomeContext(repo_map=view, memory=memory))

    def collect(repo: Path) -> HomeSnapshot:
        return snapshot

    service = SnapshotService(Path("."), clock=FakeClock(), collect=collect)
    raw = asyncio.run(respond('{"id": "x", "type": "home.snapshot"}', service))
    assert len(raw.encode("utf-8")) < MESSAGE_LIMIT
    reply = json.loads(raw)
    assert reply["ok"] is True and reply["snapshot"]["problems_total"] == 250
    shown = reply["snapshot"]["problems"]
    assert 0 < len(shown) <= 100
    assert shown[0]["message"] and text.startswith(shown[0]["message"])


@pytest.mark.parametrize("raw", ["a" * 5000, "ё" * 400])
def test_a_clipped_message_is_a_prefix_of_the_original(raw: str) -> None:
    data = DashboardData(
        ok=0,
        warn=0,
        fail=1,
        skipped=0,
        repo_map_tier="x",
        harness_version="1",
        drift_state="clean",
        active_batches=None,
        problems=(("fail", "a.b", raw),),
    )
    message = build_home_snapshot(data, HomeContext()).problems[0].message
    assert raw.startswith(message) and 0 < len(message) <= 300


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CountingCollect:
    """A collect seam that counts calls and can be slow or fail on chosen calls."""

    def __init__(self, *, delay: float = 0.0, fail_on: tuple[int, ...] = ()) -> None:
        self.calls = 0
        self._delay = delay
        self._fail_on = fail_on
        self._lock = threading.Lock()

    def __call__(self, repo: Path) -> HomeSnapshot:
        with self._lock:
            self.calls += 1
            number = self.calls
        time.sleep(self._delay)
        if number in self._fail_on:
            raise RuntimeError("collect failed")
        return build_home_snapshot(DATA, HomeContext())


def _service(
    collect: CountingCollect, clock: FakeClock, ttl: float = 15.0
) -> SnapshotService:
    return SnapshotService(Path("."), ttl=ttl, clock=clock, collect=collect)


def test_home_is_collected_once_within_the_ttl_and_again_at_its_boundary() -> None:
    async def scenario() -> None:
        clock, collect = FakeClock(), CountingCollect()
        service = _service(collect, clock)
        first = await service.home()
        assert (first.cached, first.age_seconds, collect.calls) == (False, 0.0, 1)
        clock.advance(14.5)
        second = await service.home()
        assert (second.cached, second.age_seconds, collect.calls) == (True, 14.5, 1)
        assert second.snapshot is first.snapshot
        clock.advance(0.5)
        third = await service.home()
        assert (third.cached, third.age_seconds, collect.calls) == (False, 0.0, 2)

    asyncio.run(scenario())


def test_default_ttl_is_the_documented_constant() -> None:
    async def scenario() -> None:
        clock, collect = FakeClock(), CountingCollect()
        service = SnapshotService(Path("."), clock=clock, collect=collect)
        await service.home()
        clock.advance(HOME_TTL_SECONDS - 0.001)
        assert (await service.home()).cached
        clock.advance(0.001)
        assert not (await service.home()).cached

    asyncio.run(scenario())


def test_force_rebuilds_inside_the_ttl_and_refreshes_the_cache() -> None:
    async def scenario() -> None:
        clock, collect = FakeClock(), CountingCollect()
        service = _service(collect, clock)
        await service.home()
        clock.advance(10)
        forced = await service.home(force=True)
        assert (forced.cached, forced.age_seconds, collect.calls) == (False, 0.0, 2)
        clock.advance(10)
        # 20 s after the first build, but only 10 s after the forced one.
        later = await service.home()
        assert (later.cached, later.age_seconds, collect.calls) == (True, 10.0, 2)

    asyncio.run(scenario())


def test_concurrent_requests_share_one_collect() -> None:
    async def scenario() -> None:
        collect = CountingCollect(delay=0.2)
        service = _service(collect, FakeClock())
        served = await asyncio.gather(*(service.home() for _ in range(5)))
        assert collect.calls == 1
        assert sorted(item.cached for item in served) == [False, True, True, True, True]
        assert len({id(item.snapshot) for item in served}) == 1

    asyncio.run(scenario())


def test_concurrent_forced_requests_each_rebuild_in_turn() -> None:
    async def scenario() -> None:
        collect = CountingCollect(delay=0.05)
        service = _service(collect, FakeClock())
        served = await asyncio.gather(
            service.home(force=True), service.home(force=True)
        )
        assert collect.calls == 2 and not any(item.cached for item in served)

    asyncio.run(scenario())


def test_a_failed_collect_is_not_cached() -> None:
    async def scenario() -> None:
        collect = CountingCollect(fail_on=(1,))
        service = _service(collect, FakeClock())
        with pytest.raises(RuntimeError):
            await service.home()
        served = await service.home()
        assert (served.cached, collect.calls) == (False, 2)
        assert (await service.home()).cached and collect.calls == 2

    asyncio.run(scenario())


def test_a_slow_collect_does_not_block_the_event_loop() -> None:
    async def scenario() -> None:
        collect = CountingCollect(delay=0.4)
        service = _service(collect, FakeClock())
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        task = asyncio.create_task(ticker())
        try:
            await service.home()
        finally:
            task.cancel()
        assert ticks >= 10

    asyncio.run(scenario())


def _ask(raw: str, collect: CountingCollect | None = None) -> dict[str, object]:
    collect = collect or CountingCollect()
    service = _service(collect, FakeClock())
    return json.loads(asyncio.run(respond(raw, service)))  # type: ignore[no-any-return]


def _failure(reply: dict[str, object]) -> tuple[object, object, object, object]:
    error = reply["error"]
    assert isinstance(error, dict)
    assert reply["v"] == PROTOCOL_VERSION and reply["ok"] is False
    assert isinstance(error["message"], str) and 0 < len(error["message"]) < 80
    return reply["id"], reply["type"], error["code"], error["message"]


def test_a_home_request_is_answered_with_the_snapshot_and_its_freshness() -> None:
    reply = _ask('{"id": "r1", "type": "home.snapshot", "ignored": [1]}')
    assert reply == {
        "v": 1,
        "id": "r1",
        "type": "home.snapshot",
        "ok": True,
        "cached": False,
        "age_seconds": 0.0,
        "snapshot": _wire(build_home_snapshot(DATA, HomeContext())),
    }


@pytest.mark.parametrize("id", ["a" * 64, 7, 0, -3])
def test_the_request_id_is_echoed_as_sent(id: object) -> None:
    reply = _ask(json.dumps({"id": id, "type": "home.snapshot"}))
    assert reply["ok"] is True and reply["id"] == id


def test_an_absent_id_is_answered_with_null() -> None:
    assert _ask('{"type": "home.snapshot"}')["id"] is None


def test_age_is_rounded_to_milliseconds() -> None:
    async def scenario() -> dict[str, object]:
        clock = FakeClock()
        service = _service(CountingCollect(), clock)
        await respond('{"type": "home.snapshot"}', service)
        clock.advance(1.23456)
        reply: dict[str, object] = json.loads(
            await respond('{"type": "home.snapshot"}', service)
        )
        return reply

    reply = asyncio.run(scenario())
    assert (reply["cached"], reply["age_seconds"]) == (True, 1.235)


def test_force_is_forwarded_to_the_service() -> None:
    async def scenario() -> tuple[int, dict[str, object]]:
        collect = CountingCollect()
        service = _service(collect, FakeClock())
        await respond('{"type": "home.snapshot"}', service)
        await respond('{"type": "home.snapshot", "force": false}', service)
        forced = json.loads(
            await respond('{"type": "home.snapshot", "force": true}', service)
        )
        return collect.calls, forced

    calls, forced = asyncio.run(scenario())
    assert calls == 2 and forced["cached"] is False


@pytest.mark.parametrize("raw", ["data", "", "{", '{"type": "home.snapshot"', "nul"])
def test_text_that_is_not_json_is_bad_json(raw: str) -> None:
    collect = CountingCollect()
    assert _failure(_ask(raw, collect)) == (
        None,
        None,
        "bad_json",
        "Request is not valid JSON.",
    )
    assert collect.calls == 0


@pytest.mark.parametrize(
    "raw,id",
    [
        ("[]", None),
        ("42", None),
        ('"home.snapshot"', None),
        ("{}", None),
        ('{"id": "q"}', "q"),
        ('{"id": "q", "type": 1}', "q"),
        ('{"id": "q", "type": null}', "q"),
        ('{"id": "q", "type": "home.snapshot", "force": "yes"}', "q"),
        ('{"id": "q", "type": "home.snapshot", "force": 1}', "q"),
        ('{"id": true, "type": "home.snapshot"}', None),
        ('{"id": 1.5, "type": "home.snapshot"}', None),
        ('{"id": null, "type": "home.snapshot"}', None),
        ('{"id": ["q"], "type": "home.snapshot"}', None),
        ('{"id": "' + "a" * 65 + '", "type": "home.snapshot"}', None),
    ],
)
def test_wrong_types_of_known_keys_are_bad_request(raw: str, id: object) -> None:
    collect = CountingCollect()
    reply_id, _type, code, _message = _failure(_ask(raw, collect))
    assert (reply_id, code) == (id, "bad_request")
    assert collect.calls == 0


def test_an_unknown_type_is_refused_with_its_id_and_type_echoed() -> None:
    assert _failure(_ask('{"id": 5, "type": "settings.write"}')) == (
        5,
        "settings.write",
        "unknown_type",
        "Unknown request type.",
    )


def test_a_request_is_too_large_above_the_byte_limit_before_it_is_parsed() -> None:
    def padded(size: int) -> str:
        body = '{"type": "home.snapshot", "pad": "%s"}'
        return body % ("x" * (size - len(body % "")))

    assert len(padded(MAX_REQUEST_BYTES)) == MAX_REQUEST_BYTES
    assert _ask(padded(MAX_REQUEST_BYTES))["ok"] is True
    collect = CountingCollect()
    assert _failure(_ask(padded(MAX_REQUEST_BYTES + 1), collect)) == (
        None,
        None,
        "too_large",
        "Request is too large.",
    )
    # Not JSON at all, and multi-byte: the limit counts bytes, and size wins over syntax.
    assert _failure(_ask("я" * (MAX_REQUEST_BYTES // 2 + 1)))[2] == "too_large"
    assert collect.calls == 0


def test_a_failed_collect_names_only_the_exception_class() -> None:
    class Boom(Exception):
        pass

    def collect(repo: Path) -> HomeSnapshot:
        raise Boom("/secret/path leaked")

    service = SnapshotService(Path("."), clock=FakeClock(), collect=collect)
    raw = asyncio.run(respond('{"id": "z", "type": "home.snapshot"}', service))
    assert "secret" not in raw
    assert _failure(json.loads(raw)) == ("z", "home.snapshot", "collect_failed", "Boom")


def test_busy_is_an_error_reply_that_echoes_the_request_id() -> None:
    reply = json.loads(busy('{"id": 9, "type": "home.snapshot"}'))
    assert _failure(reply) == (
        9,
        "home.snapshot",
        "busy",
        "Too many unfinished requests.",
    )
    assert _failure(json.loads(busy("garbage")))[2] == "busy"
