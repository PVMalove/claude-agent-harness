"""Цикл Red-Green-Refactor с тест-автором: скриптованный прогон на установленном проекте.

Модель тест-автора заменена заглушкой со сценарием ответов; роль developer играет `DeveloperCycle`,
который кодирует протокол из `harness/orchestration/roles/developer.md`: проверка происхождения
ожидания, статусы `red`, `needs_skeleton` и `contract_gap`, отличие поведенческого Red от ошибки
импорта, fixture или окружения, сразу зелёный тест, лимит из трёх подряд неудачных вызовов и запрет
ослаблять принятое ожидание. Тесты выполняет вызывающий код через `execute`: сценарий
`isolated_test_runner` подставляет установленный `.claude/hooks/run-test.sh`, а без рабочей
песочницы — прямой `unittest`. Сценарий не создаёт dispatch, не пишет в ledger и не меняет
`.harness/project.json`.
"""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MAX_FAILED_CALLS = 3
FORBIDDEN_HANDOFF_KEYS = ("algorithm", "implementation", "snippet")
FEE_REQUIREMENT = "R1: the total is the sum of the prices plus a fee of 1"
# `execute(test_id)` -> (the test passed, the bounded log of the run)
Execute = Callable[[str], tuple[bool, str]]

TEST_SOURCE = """\
import unittest

from src.{module} import total


class Case(unittest.TestCase):
    def test_behavior(self):
        self.assertEqual(total([2, 3]), 6)
"""
SYNTAX_ERROR_SOURCE = "import unittest\n\n\nclass Case(unittest.TestCase)\n    pass\n"
FIXTURE_ERROR_SOURCE = """\
import unittest

from src.{module} import total


class Case(unittest.TestCase):
    def setUp(self):
        raise RuntimeError("fixture is broken")

    def test_behavior(self):
        self.assertEqual(total([2, 3]), 6)
"""
SKELETON = "def total(prices):\n    return 0\n"
IMPLEMENTATION = "def total(prices):\n    return sum(prices) + 1\n"
REFACTORED = "FEE = 1\n\n\ndef total(prices):\n    return sum(prices) + FEE\n"
WEAKENED_SOURCE = TEST_SOURCE.replace("6)", "5)")


class Blocker(Exception):
    """The ordinary blocker path: the developer stops and reports instead of continuing."""


class WeakenedExpectation(Exception):
    """An accepted expectation changed without a justified contract change."""


@dataclass
class Step:
    """One scripted author reply and the test file the stub author writes for it."""

    reply: dict[str, str]
    test_source: str | None = None


class ScriptedAuthor:
    """A model stub: replays the scripted replies and writes the scripted test file."""

    def __init__(self, project: Path, steps: list[Step]) -> None:
        self.project = project
        self.steps = list(steps)
        self.handoffs: list[dict[str, str]] = []

    @property
    def calls(self) -> int:
        return len(self.handoffs)

    def call(self, handoff: dict[str, str]) -> dict[str, str]:
        forbidden = [key for key in FORBIDDEN_HANDOFF_KEYS if key in handoff]
        if forbidden:
            raise AssertionError(f"the handoff carries {forbidden}: no expectation basis")
        if not self.steps:
            raise AssertionError("the author script is exhausted")
        self.handoffs.append(dict(handoff))
        step = self.steps.pop(0)
        if step.test_source is not None:
            author_test_path(self.project, step.reply["test_id"]).write_text(
                step.test_source, encoding="utf-8"
            )
        return dict(step.reply)


def author_test_path(project: Path, test_id: str) -> Path:
    """`tests.<module>.Case.test_behavior` -> `tests/<module>.py`."""
    package, module = test_id.split(".")[:2]
    return project / package / f"{module}.py"


def classify(passed: bool, log: str) -> str:
    """`green`, `behavioral_red` (a failed assertion only) or `non_behavioral`."""
    if passed:
        return "green"
    if "FAIL:" in log and "AssertionError" in log and "ERROR:" not in log:
        return "behavioral_red"
    return "non_behavioral"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DeveloperCycle:
    """The developer side of the cycle: every decision the protocol assigns to the developer."""

    def __init__(self, project: Path, author: ScriptedAuthor, execute: Execute) -> None:
        self.project = project
        self.author = author
        self.execute = execute
        self.accepted: dict[str, str] = {}
        self.failures: list[str] = []

    def request_test(
        self,
        handoff: dict[str, str],
        *,
        write_skeleton: Callable[[], None],
        clarify: Callable[[dict[str, str], dict[str, str]], dict[str, str]] | None = None,
    ) -> str:
        """Call the author until a behavioral Red with a supported expectation is accepted."""
        failed = 0
        skeleton_done = False
        while True:
            reply = self.author.call(handoff)
            status = reply.get("status")
            if status == "contract_gap":
                if clarify is None:
                    raise Blocker("contract_gap: the seam is not agreed in the brief")
                handoff = clarify(handoff, reply)
                clarify = None
                continue
            kind = classify(*self.execute(reply["test_id"]))
            if status == "needs_skeleton" and kind == "non_behavioral" and not skeleton_done:
                write_skeleton()
                skeleton_done = True
                continue
            problem = self._problem(status, kind, reply, handoff)
            if problem is None:
                self.accepted[reply["test_id"]] = _digest(
                    author_test_path(self.project, reply["test_id"])
                )
                return reply["test_id"]
            self.failures.append(problem)
            failed += 1
            if failed >= MAX_FAILED_CALLS:
                raise Blocker(f"the author failed {failed} calls in a row: {problem}")

    @staticmethod
    def _problem(
        status: str | None, kind: str, reply: dict[str, str], handoff: dict[str, str]
    ) -> str | None:
        """Why the reply is a failed call, or None when it is an accepted behavioral Red."""
        if status != "red":
            return f"status_{status}"
        if kind == "green":
            return "immediately_green"
        if kind != "behavioral_red":
            return "not_behavioral_red"
        source = reply.get("expectation_source", "")
        if handoff["requirement_id"] not in source or "implementation" in source.lower():
            return "unsupported_expectation"
        return None

    def implement(self, test_id: str, write: Callable[[], None]) -> None:
        """The minimal implementation: Green with the accepted test unchanged."""
        write()
        self._require_green(test_id)

    def refactor(self, test_id: str, change: Callable[[], None]) -> None:
        """Refactor only on Green and keep it Green."""
        self._require_green(test_id)
        change()
        self._require_green(test_id)

    def change_contract(self, test_id: str, justification: str) -> None:
        """A justified contract change forgets the accepted test: a new call and check follow."""
        if not justification.strip():
            raise WeakenedExpectation("a contract change needs a justification")
        self.accepted.pop(test_id)

    def _require_green(self, test_id: str) -> None:
        if _digest(author_test_path(self.project, test_id)) != self.accepted[test_id]:
            raise WeakenedExpectation(f"the accepted expectation of {test_id} changed")
        if classify(*self.execute(test_id)) != "green":
            raise Blocker(f"{test_id} is not Green")


def _handoff(module: str, requirement: str = FEE_REQUIREMENT) -> dict[str, str]:
    return {
        "requirement_id": "R1",
        "requirement": requirement,
        "seam": f"src.{module}.total(prices) from the architect brief",
        "signatures": "def total(prices: list[int]) -> int",
        "fixtures": "none",
        "scenario": "two prices, 2 and 3",
    }


def _red(module: str, status: str = "red", source: str | None = None) -> dict[str, str]:
    return {
        "status": status,
        "requirement": "R1",
        "inputs": "prices [2, 3]",
        "expectation_source": source or f"{FEE_REQUIREMENT} (R1): 2 + 3 + 1 = 6",
        "test_id": f"tests.{module}.Case.test_behavior",
    }


def _write(project: Path, module: str, text: str) -> Callable[[], None]:
    def write() -> None:
        (project / "src" / f"{module}.py").write_text(text, encoding="utf-8")

    return write


def _case(project: Path, name: str, steps: list[Step], execute: Execute):
    module = f"cyc_{name}"
    author = ScriptedAuthor(project, steps)
    return module, author, DeveloperCycle(project, author, execute)


def _expect_blocker(label: str, action: Callable[[], object]) -> None:
    try:
        action()
    except Blocker:
        return
    sys.exit(f"test-author cycle {label}: expected the blocker path")


def _full_cycle(project: Path, execute: Execute) -> None:
    """Normal cycle: Red, minimal implementation, Refactor on Green, then the next test."""
    module, author, cycle = _case(
        project, "normal", [Step(_red("cyc_normal"), TEST_SOURCE.format(module="cyc_normal"))], execute
    )
    _write(project, module, SKELETON)()
    test_id = cycle.request_test(_handoff(module), write_skeleton=_write(project, module, SKELETON))
    if author.calls != 1 or cycle.failures:
        sys.exit("test-author cycle: the normal cycle needed more than one author call")
    cycle.implement(test_id, _write(project, module, IMPLEMENTATION))
    cycle.refactor(test_id, _write(project, module, REFACTORED))


def _skeleton(project: Path, execute: Execute) -> None:
    """`needs_skeleton`: the developer writes signatures only, then the author reruns."""
    module = "cyc_skeleton"
    steps = [
        Step(_red(module, "needs_skeleton"), TEST_SOURCE.format(module=module)),
        Step(_red(module)),
    ]
    _, author, cycle = _case(project, "skeleton", steps, execute)
    cycle.request_test(_handoff(module), write_skeleton=_write(project, module, SKELETON))
    if author.calls != 2 or cycle.failures:
        sys.exit("test-author cycle: needs_skeleton was not handled by one skeleton and a rerun")
    if "sum(" in (project / "src" / f"{module}.py").read_text(encoding="utf-8"):
        sys.exit("test-author cycle: the skeleton carries logic")


def _contract_gap(project: Path, execute: Execute) -> None:
    """`contract_gap`: a clarified seam continues, an unagreed seam is the blocker path."""
    module = "cyc_gap"
    steps = [
        Step(_red(module, "contract_gap")),
        Step(_red(module), TEST_SOURCE.format(module=module)),
    ]
    _, author, cycle = _case(project, "gap", steps, execute)
    _write(project, module, SKELETON)()
    clarified = {}

    def clarify(handoff: dict[str, str], reply: dict[str, str]) -> dict[str, str]:
        clarified.update(handoff, seam=f"{handoff['seam']}; the fee is a fixed 1")
        return clarified

    cycle.request_test(
        _handoff(module), write_skeleton=_write(project, module, SKELETON), clarify=clarify
    )
    if author.calls != 2 or author.handoffs[1]["seam"] == author.handoffs[0]["seam"]:
        sys.exit("test-author cycle: the contract gap was not clarified before the rerun")
    unagreed = ScriptedAuthor(project, [Step(_red(module, "contract_gap"))])
    _expect_blocker(
        "contract_gap without a clarification",
        lambda: DeveloperCycle(project, unagreed, execute).request_test(
            _handoff(module), write_skeleton=_write(project, module, SKELETON)
        ),
    )


def _not_behavioral(project: Path, execute: Execute) -> None:
    """Syntax, fixture and unsupported-expectation replies are failed calls, not Red."""
    module = "cyc_nonbeh"
    steps = [
        Step(_red(module), SYNTAX_ERROR_SOURCE),
        Step(_red(module), FIXTURE_ERROR_SOURCE.format(module=module)),
        Step(_red(module), TEST_SOURCE.format(module=module)),
    ]
    _, _, cycle = _case(project, "nonbeh", steps, execute)
    _write(project, module, SKELETON)()
    cycle.request_test(_handoff(module), write_skeleton=_write(project, module, SKELETON))
    if cycle.failures != ["not_behavioral_red", "not_behavioral_red"]:
        sys.exit(f"test-author cycle: a non-behavioral failure counted as Red: {cycle.failures}")
    steps = [
        Step(
            _red(module, source="R1: copied from the implementation"),
            TEST_SOURCE.format(module=module),
        ),
        Step(_red(module)),
    ]
    _, _, cycle = _case(project, "nonbeh", steps, execute)
    cycle.request_test(_handoff(module), write_skeleton=_write(project, module, SKELETON))
    if cycle.failures != ["unsupported_expectation"]:
        sys.exit(f"test-author cycle: an unsupported expectation was accepted: {cycle.failures}")


def _immediately_green(project: Path, execute: Execute) -> None:
    """A test that passes at once is returned explicitly and is not a successful cycle."""
    module = "cyc_green"
    steps = [
        Step(_red(module), TEST_SOURCE.format(module=module)),
        Step(_red(module), TEST_SOURCE.format(module=module).replace("6)", "7)")),
    ]
    _write(project, module, IMPLEMENTATION)()
    _, author, cycle = _case(project, "green", steps, execute)
    cycle.request_test(_handoff(module), write_skeleton=_write(project, module, SKELETON))
    if cycle.failures != ["immediately_green"] or author.calls != 2:
        sys.exit(f"test-author cycle: an immediately green test was accepted: {cycle.failures}")


def _attempt_limit(project: Path, execute: Execute) -> None:
    """Three failed calls in a row end in the blocker path, with no dispatch and no ledger."""
    module = "cyc_limit"
    steps = [Step(_red(module), SYNTAX_ERROR_SOURCE) for _ in range(MAX_FAILED_CALLS + 1)]
    _, author, cycle = _case(project, "limit", steps, execute)
    before = _digest(project / ".harness" / "project.json")
    _expect_blocker(
        "attempt limit",
        lambda: cycle.request_test(
            _handoff(module), write_skeleton=_write(project, module, SKELETON)
        ),
    )
    if author.calls != MAX_FAILED_CALLS:
        sys.exit(f"test-author cycle: the author was called {author.calls} times")
    if (project / ".harness" / "orchestration").exists():
        sys.exit("test-author cycle: a local author retry created orchestration state")
    if _digest(project / ".harness" / "project.json") != before:
        sys.exit("test-author cycle: a local author retry changed project.json")


def _weakening(project: Path, execute: Execute) -> None:
    """An accepted expectation is never weakened to reach Green."""
    module = "cyc_weak"
    source = TEST_SOURCE.format(module=module)
    steps = [Step(_red(module), source)]
    _, _, cycle = _case(project, "weak", steps, execute)
    _write(project, module, SKELETON)()
    test_id = cycle.request_test(
        _handoff(module), write_skeleton=_write(project, module, SKELETON)
    )

    def weaken_and_pass() -> None:
        author_test_path(project, test_id).write_text(
            WEAKENED_SOURCE.format(module=module), encoding="utf-8"
        )
        _write(project, module, SKELETON.replace("0", "5"))()

    try:
        cycle.implement(test_id, weaken_and_pass)
    except WeakenedExpectation:
        pass
    else:
        sys.exit("test-author cycle: a weakened expectation reached Green")
    try:
        cycle.change_contract(test_id, " ")
    except WeakenedExpectation:
        pass
    else:
        sys.exit("test-author cycle: a contract change needed no justification")
    cycle.change_contract(test_id, "R1 now defines a fee of 2")
    if test_id in cycle.accepted:
        sys.exit("test-author cycle: a changed contract kept the old accepted expectation")


def verify(project: Path, execute: Execute) -> None:
    """Run every cycle case on the installed project, and remove the scenario files afterwards."""
    cases = (
        _full_cycle,
        _skeleton,
        _contract_gap,
        _not_behavioral,
        _immediately_green,
        _attempt_limit,
        _weakening,
    )
    try:
        for case in cases:
            case(project, execute)
    finally:
        for pattern, directory in (("cyc_*.py", "src"), ("cyc_*.py", "tests")):
            for path in (project / directory).glob(pattern):
                path.unlink()
