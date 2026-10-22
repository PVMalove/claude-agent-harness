"""Failed pytest nodeids from a JUnit report (xunit2).

pytest writes a case as ``classname`` (the dotted module path plus the class chain) and ``name``
(the test function with its parameter id), without a file attribute. The exact nodeid
``path.py::Class::name`` is rebuilt by looking the longest dotted module prefix up on disk under the
pytest rootdir. A case whose module is gone is reported as unresolved instead of being guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from .stage_evidence import StageEvidenceError

_FAILING = ("failure", "error")


@dataclass(frozen=True)
class JunitFailures:
    """Failed cases of one report: exact nodeids in report order, plus the unresolvable ones."""

    nodeids: tuple[str, ...]
    unresolved: tuple[str, ...]


def _module_file(dotted: list[str], root: Path) -> tuple[str, list[str]] | None:
    """The longest dotted prefix that is a Python file under ``root``, and the remaining parts."""
    for length in range(len(dotted), 0, -1):
        relative = "/".join(dotted[:length]) + ".py"
        if (root / relative).is_file():
            return relative, dotted[length:]
    return None


def _nodeid(classname: str, name: str, root: Path) -> str | None:
    if not classname:
        # A collection error: the case is named after the module itself.
        found = _module_file(name.split("."), root)
        return found[0] if found is not None and not found[1] else None
    found = _module_file(classname.split("."), root)
    if found is None:
        return None
    module, classes = found
    return "::".join([module, *classes, name])


def failed_nodeids(xml_text: str, root: Path) -> JunitFailures:
    """The nodeids of every failed or errored case of a JUnit report, each once.

    ``root`` is the pytest rootdir the report was written for. Skipped cases are not failures.
    """
    try:
        document = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise StageEvidenceError(
            f"the JUnit report is not valid XML: {exc}",
            remedy="rerun the stage so that pytest writes a complete report",
        ) from exc
    if document.tag not in ("testsuites", "testsuite"):
        raise StageEvidenceError(
            f"the JUnit report has an unexpected root element <{document.tag}>",
            remedy="pass the file pytest wrote with --junitxml",
        )
    found: list[str] = []
    unresolved: list[str] = []
    for case in document.iter("testcase"):
        if not any(case.find(tag) is not None for tag in _FAILING):
            continue
        classname, name = case.get("classname", ""), case.get("name", "")
        nodeid = _nodeid(classname, name, root)
        if nodeid is not None:
            if nodeid not in found:
                found.append(nodeid)
            continue
        label = f"{classname}::{name}" if classname else name
        if label not in unresolved:
            unresolved.append(label)
    return JunitFailures(tuple(found), tuple(unresolved))
