"""Короткий маршрут `/fast-implement` с тест-автором: скриптованный прогон на установленном проекте.

Модель тест-автора заменена заглушкой, а роль агента `/fast-implement` играет `ShortRoute`. Он
кодирует правила скила: seam согласует разработчик до первого теста, ответ `test-author-preflight.sh`
выбирает маршрут (ровно `{"status":"ok"}` даёт цикл с автором, любой другой ответ — обычный `/tdd` с
названной причиной), а сам цикл ведёт тот же `DeveloperCycle`, что и в полном маршруте developer.
Шагов coordinator, architect и QA у короткого маршрута нет: сценарий проверяет, что их нет в журнале
шагов, а dispatch и ledger не создаются. Реальный Claude Code и качество тестов, которые пишет
модель, этот сценарий не проверяет.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from scripts.clean_room.claude_probe import claude_environment
from scripts.clean_room.isolated_test_runner import (
    _direct_execute,
    _hook,
    _install,
    _preflight,
    _runner_execute,
)
from scripts.clean_room.isolated_test_runner_build_db import _settings
from scripts.clean_room.support import run_health
from scripts.clean_room.test_author_cycle import (
    IMPLEMENTATION,
    MAX_FAILED_CALLS,
    REFACTORED,
    SKELETON,
    SYNTAX_ERROR_SOURCE,
    TEST_SOURCE,
    Blocker,
    DeveloperCycle,
    Execute,
    Scenario,
    ScriptedAuthor,
    Step,
    WeakenedExpectation,
    _digest,
    _expect_blocker,
    _handoff,
    _module,
    _red,
    author_test_path,
    classify,
)

Route = Literal["author", "ordinary"]
FORBIDDEN_STEPS = frozenset({"coordinator", "architect", "qa"})
FAILING_COMMAND = ["python3", "-c", "import sys; sys.exit(1)"]
# Preflight answers that keep the ordinary route: a refusal reason, or a failed hook (no JSON).
FALLBACK_ANSWERS: tuple[tuple[str, dict[str, object]], ...] = (
    ("isolation", {"status": "disabled", "reason": "isolation_unavailable"}),
    ("guard", {"status": "disabled", "reason": "guard_hook_unavailable"}),
    ("no test_paths", {"status": "disabled", "reason": "test_paths_missing"}),
    ("failed hook", {}),
)


class SeamNotAgreed(Exception):
    """The developer has not agreed the seam: the skill stops and asks before any test."""


def choose_route(preflight: dict[str, object]) -> tuple[Route, str]:
    """The route a preflight answer selects and the reason the developer is told."""
    if preflight == {"status": "ok"}:
        return "author", ""
    reason = preflight.get("reason")
    if preflight.get("status") == "disabled" and isinstance(reason, str) and reason:
        return "ordinary", reason
    return "ordinary", "preflight_failed"


class ShortRoute:
    """One `/fast-implement` session: the same cycle as the developer, nothing added."""

    def __init__(self, scenario: Scenario, preflight: dict[str, object]) -> None:
        self.scenario = scenario
        self.route, self.reason = choose_route(preflight)
        self.steps = ["preflight"]
        self.author: ScriptedAuthor | None = None
        self.cycle: DeveloperCycle | None = None

    def begin(self, module: str, *, seam_agreed: bool, script: list[Step]) -> str:
        """Agree the seam, then reach a verified behavioral Red; returns the test id."""
        if not seam_agreed:
            self.steps.append("ask_seam")
            raise SeamNotAgreed(
                "the seam is not agreed: ask the developer before any test"
            )
        self.steps.append("seam_agreed")
        skeleton = self.scenario.writer(module, SKELETON)
        skeleton()  # the seam already exists: an import error is not a Red
        if self.route == "ordinary":
            return self._ordinary_red(module)
        self.author, self.cycle = self.scenario.case(script)
        self.steps.append("author_call")
        return self.cycle.request_test(_handoff(module), write_skeleton=skeleton)

    def _ordinary_red(self, module: str) -> str:
        """Ordinary `/tdd`: the agent writes the test itself and still requires a behavioral Red."""
        test_id = f"tests.{module}.Case.test_behavior"
        author_test_path(self.scenario.project, test_id).write_text(
            TEST_SOURCE.format(module=module), encoding="utf-8"
        )
        if classify(*self.scenario.execute(test_id)) != "behavioral_red":
            raise Blocker("the ordinary test is not a behavioral Red")
        self.steps.append("ordinary_test")
        return test_id

    def finish(self, module: str, test_id: str) -> None:
        """Green with the minimal implementation, then Refactor on Green."""
        implement = self.scenario.writer(module, IMPLEMENTATION)
        refactor = self.scenario.writer(module, REFACTORED)
        if self.cycle is None:
            implement()
            self._green(test_id)
            refactor()
            self._green(test_id)
        else:
            self.cycle.implement(test_id, implement)
            self.cycle.refactor(test_id, refactor)
        self.steps += ["implement", "refactor"]

    def _green(self, test_id: str) -> None:
        if classify(*self.scenario.execute(test_id)) != "green":
            raise Blocker(f"{test_id} is not Green")


def _fail(label: str, message: str) -> None:
    sys.exit(f"fast-implement route {label}: {message}")


def _no_extra_steps(label: str, project: Path, route: ShortRoute) -> None:
    if FORBIDDEN_STEPS & set(route.steps):
        _fail(label, f"the short route added a step: {route.steps}")
    if (project / ".harness" / "orchestration").exists():
        _fail(label, "the short route created orchestration state")


def _cleanup(project: Path) -> None:
    for directory in ("src", "tests"):
        for path in (project / directory).glob("cyc_*.py"):
            path.unlink()


def _author_cases(scenario: Scenario) -> None:
    """With preflight `ok`: agreed seam, the shared limits and weakening rules, route parity."""
    module = _module("short")
    good = _good_script(module)
    route = ShortRoute(scenario, {"status": "ok"})
    test_id = route.begin(module, seam_agreed=True, script=good)
    route.finish(module, test_id)
    expected = ["preflight", "seam_agreed", "author_call", "implement", "refactor"]
    if route.steps != expected or route.route != "author" or route.author is None:
        _fail("short cycle", f"unexpected steps {route.steps}")
    if route.author.calls != 1:
        _fail("short cycle", f"the author was called {route.author.calls} times")
    _no_extra_steps("short cycle", scenario.project, route)

    unagreed = ShortRoute(scenario, {"status": "ok"})
    _expect_seam_question(
        "unagreed seam",
        lambda: unagreed.begin(module, seam_agreed=False, script=good),
    )
    _cleanup(scenario.project)
    if unagreed.author is not None or list(
        (scenario.project / "tests").glob("cyc_*.py")
    ):
        _fail(
            "unagreed seam", "an author call or a test came before the seam was agreed"
        )

    module = _module("shortlimit")
    limit = ShortRoute(scenario, {"status": "ok"})
    _expect_blocker(
        "short attempt limit",
        lambda: limit.begin(
            module,
            seam_agreed=True,
            script=[
                Step(_red(module), SYNTAX_ERROR_SOURCE)
                for _ in range(MAX_FAILED_CALLS + 1)
            ],
        ),
    )
    if limit.author is None or limit.author.calls != MAX_FAILED_CALLS:
        _fail("short attempt limit", "the limit differs from the full route")

    module = _module("shortweak")
    weak = ShortRoute(scenario, {"status": "ok"})
    weak_id = weak.begin(module, seam_agreed=True, script=_good_script(module))
    weakened = TEST_SOURCE.format(module=module).replace("6)", "4)")
    try:
        assert weak.cycle is not None
        weak.cycle.implement(
            weak_id,
            lambda: author_test_path(scenario.project, weak_id).write_text(
                weakened, encoding="utf-8"
            ),
        )
    except WeakenedExpectation:
        pass
    else:
        _fail("short weakening", "a weakened accepted expectation reached Green")
    _cleanup(scenario.project)
    _parity(scenario)


def _good_script(module: str) -> list[Step]:
    return [Step(_red(module), TEST_SOURCE.format(module=module))]


def _expect_seam_question(label: str, action: Callable[[], object]) -> None:
    try:
        action()
    except SeamNotAgreed:
        return
    _fail(label, "expected the developer to be asked for the seam")


def _parity(scenario: Scenario) -> None:
    """The same author script gives the full route and the short route the same outcome."""
    module = _module("parity")

    def script() -> list[Step]:
        return [
            Step(_red(module), SYNTAX_ERROR_SOURCE),
            Step(_red(module), TEST_SOURCE.format(module=module)),
        ]

    skeleton = scenario.writer(module, SKELETON)
    skeleton()
    _, full = scenario.case(script())
    full_id = full.request_test(_handoff(module), write_skeleton=skeleton)
    full_digest = _digest(author_test_path(scenario.project, full_id))
    _cleanup(scenario.project)
    skeleton()
    short = ShortRoute(scenario, {"status": "ok"})
    short_id = short.begin(module, seam_agreed=True, script=script())
    assert short.cycle is not None
    if (short_id, short.cycle.failures) != (full_id, full.failures):
        _fail(
            "parity",
            f"{short.cycle.failures} differs from the full route {full.failures}",
        )
    if _digest(author_test_path(scenario.project, short_id)) != full_digest:
        _fail("parity", "the accepted test differs from the full route")
    _cleanup(scenario.project)


def _fallback_cases(
    scenario: Scenario, answers: tuple[tuple[str, dict[str, object]], ...]
) -> None:
    """Every non-`ok` answer keeps the ordinary route, names the reason and delivers a simple test."""
    for label, answer in answers:
        module = _module("ordinary")
        route = ShortRoute(scenario, answer)
        test_id = route.begin(module, seam_agreed=True, script=[])
        route.finish(module, test_id)
        expected = [
            "preflight",
            "seam_agreed",
            "ordinary_test",
            "implement",
            "refactor",
        ]
        if (
            route.route != "ordinary"
            or route.steps != expected
            or route.author is not None
        ):
            _fail(
                f"fallback ({label})", f"unexpected route {route.route}: {route.steps}"
            )
        if not route.reason or (answer and route.reason != answer["reason"]):
            _fail(f"fallback ({label})", f"the reason is not stated: {route.reason!r}")
        _no_extra_steps(f"fallback ({label})", scenario.project, route)
        _cleanup(scenario.project)


def _preflight_matches(project: Path, label: str, expected: dict[str, object]) -> None:
    answer = _preflight(project, env=claude_environment(project))
    if answer != expected:
        _fail(label, f"preflight answered {answer}, expected {expected}")


def _without_test_paths(project: Path, direct: Scenario) -> None:
    """An old project without `test_paths`: disabled, the ordinary route, nothing else changes."""
    config = project / ".harness" / "project.json"
    original = config.read_text(encoding="utf-8")
    data = json.loads(original)
    data.pop("test_paths", None)
    config.write_text(json.dumps(data), encoding="utf-8")
    try:
        expected = {"status": "disabled", "reason": "test_paths_missing"}
        _preflight_matches(project, "old project", expected)
        _fallback_cases(direct, (("old project", expected),))
    finally:
        config.write_text(original, encoding="utf-8")


def _failed_hook(project: Path, direct: Scenario) -> None:
    """A guard hook that fails keeps the ordinary route; its bytes come back afterwards."""
    guard = project / ".claude" / "hooks" / "test-author-guard.py"
    original = guard.read_bytes()
    guard.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    try:
        expected = {"status": "disabled", "reason": "guard_hook_unavailable"}
        _preflight_matches(project, "failed hook", expected)
        _fallback_cases(direct, (("failed hook", expected),))
    finally:
        guard.write_bytes(original)


def _unavailable_isolation(
    project: Path, test_root: Path, direct: Scenario, isolated: bool
) -> None:
    """No working sandbox: the real refusal when the host has none, a PATH without bwrap otherwise."""
    expected = {"status": "disabled", "reason": "isolation_unavailable"}
    environment = claude_environment(project)
    if isolated:
        if os.name == "nt":
            return
        bin_dir = test_root / "fast-route-no-bwrap"
        bin_dir.mkdir()
        for name in ("dirname", "git"):
            found = shutil.which(name)
            if found is None:
                _fail("isolation", f"{name} is not on PATH")
            (bin_dir / name).symlink_to(found)
        (bin_dir / "python3").symlink_to(Path(sys.executable).resolve())
        environment["PATH"] = str(bin_dir)
    answer = _preflight(project, env=environment)
    if answer != expected:
        _fail("isolation", f"preflight answered {answer}, expected {expected}")
    _fallback_cases(direct, (("isolation", answer),))


def _build_database_refusals(project: Path) -> None:
    """A failed protected build or database is a refusal, never a behavioral Red, and counts as a failure."""
    config = project / ".harness" / "project.json"
    original = config.read_bytes()
    runner = json.loads(original)["test_runner"]
    (project / "artifacts").mkdir(exist_ok=True)
    cases = (
        (
            "build_failed",
            {
                **runner,
                "build": {
                    "command": FAILING_COMMAND,
                    "artifact_paths": ["artifacts/**"],
                },
            },
        ),
        (
            "database_unavailable",
            {
                **runner,
                "database": {
                    "engine": "postgresql",
                    "test_ids": ["tests.*"],
                    "command": FAILING_COMMAND,
                    "ready_command": FAILING_COMMAND,
                },
            },
        ),
    )
    try:
        for reason, settings in cases:
            _settings(project, settings)
            scenario = Scenario(project, _runner_execute(project))
            module = _module("refusal")
            skeleton = scenario.writer(module, SKELETON)
            skeleton()
            test_id = f"tests.{module}.Case.test_behavior"
            author_test_path(project, test_id).write_text(
                TEST_SOURCE.format(module=module), encoding="utf-8"
            )
            answer = _hook(project, test_id)
            if answer.get("reason") != reason or answer.get("result") != "refused":
                _fail(reason, f"the runner answered {answer}")
            route = ShortRoute(scenario, {"status": "ok"})
            script = [
                Step(_red(module), TEST_SOURCE.format(module=module)) for _ in range(4)
            ]
            _expect_blocker(
                reason, lambda: route.begin(module, seam_agreed=True, script=script)
            )
            if (
                route.cycle is None
                or route.cycle.failures != ["not_behavioral_red"] * MAX_FAILED_CALLS
            ):
                _fail(reason, "a runner refusal counted as Red")
            _cleanup(project)
    finally:
        config.write_bytes(original)
        run_health(project)


def run(ctx: SimpleNamespace) -> None:
    """Короткий маршрут на установленных проектах: новый, старый без `test_paths`, откаты.

    Читает из контекста: `test_root`.
    """
    project = _install(ctx.test_root / "fast_route_project")
    isolated = _hook(project, "--probe").get("status") == "ok"
    execute: Execute = (
        _runner_execute(project) if isolated else _direct_execute(project)
    )
    scenario = Scenario(project, execute)
    direct = Scenario(project, _direct_execute(project))
    _preflight_matches(
        project,
        "new project",
        {"status": "ok"}
        if isolated
        else {"status": "disabled", "reason": "isolation_unavailable"},
    )
    try:
        _author_cases(scenario)
        _without_test_paths(project, direct)
        _failed_hook(project, direct)
        _unavailable_isolation(project, ctx.test_root, direct, isolated)
        _fallback_cases(direct, FALLBACK_ANSWERS)
        if isolated:
            _build_database_refusals(project)
    finally:
        _cleanup(project)
    print(
        "fast-implement route: short route verified"
        + (
            ""
            if isolated
            else " without isolation (build and database refusals not run)"
        ),
        flush=True,
    )
