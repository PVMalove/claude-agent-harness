"""Conservative text heuristic that recognises an infrastructure failure in a blocked report.

It only widens what `batch resume-stop` may *propose*: the human approval, the clean worktree,
the report without code and the retry budget still gate recovery. Any sign of a code, test,
scope or requirements problem wins over an infrastructure sign, and unknown text stays unknown.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from harness.orchestration.core.utils import JsonObject

# Order matters: the first matching category names the failure.
INFRASTRUCTURE_PATTERNS = (
    ("rate-limit", r"\b429\b|rate[- ]?limit|too many requests"),
    (
        "capacity",
        r"(?:provider|model|runtime|account|service) (?:is )?(?:at|out of|lacks?) capacity"
        r"|insufficient capacity|capacity (?:exhausted|unavailable|limit)"
        r"|no capacity available|capacity error",
    ),
    (
        "service-unavailable",
        r"service (?:is )?unavailable|provider (?:is )?unavailable"
        r"|temporarily unavailable|(?:http(?:/\d(?:\.\d)?)?|status(?: code)?)[\s:=]*503\b",
    ),
    ("timeout", r"timed? ?out|\btimeout\b|deadline exceeded"),
    (
        "network",
        r"connection (?:reset|refused|aborted|closed)|\beconn\w*|network (?:error|unreachable)"
        r"|temporary failure in name resolution|tls handshake|broken pipe",
    ),
    (
        "runtime-crash",
        r"(?:worker|session|runtime|process|adapter) (?:crashed|died|was killed|exited unexpectedly"
        r"|terminated unexpectedly)|segmentation fault|out of memory|\boom\b",
    ),
    ("resources", r"no space left|disk full|quota exceeded"),
    (
        "service",
        r"(?:docker|container|daemon|api|server|service|runtime) (?:is )?(?:unavailable|not running"
        r"|unreachable|down)|\b50[234]\b|overloaded",
    ),
)
# Anything about the work itself: never recovered as an environment failure.
CODE_DEFECT_PATTERNS = (
    r"assert(?:ion)?",
    r"tests? (?:failed|failing)|failing tests?",
    r"\blint\b|\bmypy\b|type ?check|syntax ?error|compil",
    r"merge conflict|\bconflicts?\b",
    r"requirements?|ambiguous|clari(?:fy|fication)",
    r"out of scope|prohibited|forbidden|not allowed",
)


def classify(*texts: object) -> JsonObject | None:
    """The infrastructure category, matched pattern and source text, or None when unsure."""
    joined = "\n".join(text for text in texts if isinstance(text, str) and text.strip())
    lowered = joined.lower()
    if not lowered or any(re.search(p, lowered) for p in CODE_DEFECT_PATTERNS):
        return None
    for category, pattern in INFRASTRUCTURE_PATTERNS:
        match = re.search(pattern, lowered)
        if match:
            return {
                "method": "heuristic",
                "category": category,
                "matched": match.group(0),
                "text": joined[:500],
            }
    return None


# Categories a runtime adapter or probe may name in a structured field, and the HTTP statuses
# that name one; both are evidence, unlike the text heuristic above.
STRUCTURED_CATEGORIES = frozenset(
    {category for category, _ in INFRASTRUCTURE_PATTERNS} | {"runtime-unavailable"}
)
_STATUS_CATEGORIES = {429: "rate-limit", 503: "service-unavailable"}


def _structured(
    category: str, field: str, value: object, failure: Mapping[str, object]
) -> JsonObject:
    return {
        "method": "structured",
        "category": category,
        "field": field,
        "value": value,
        "text": str(failure.get("error", ""))[:500],
    }


def classify_runtime_failure(failure: Mapping[str, object] | None) -> JsonObject | None:
    """Classify structured runtime fields before falling back to the text heuristic.

    Capacity, rate-limit and service-unavailable stay distinct.  A structured category is evidence
    supplied by the adapter or a runtime probe; a free-form message remains a heuristic and never
    proves by itself that a worker exited.
    """
    if not isinstance(failure, Mapping):
        return None
    for field in ("category", "kind"):
        value = failure.get(field)
        if isinstance(value, str):
            category = value.strip().lower().replace("_", "-").replace(" ", "-")
            if category in STRUCTURED_CATEGORIES:
                return _structured(category, field, value, failure)
    status = failure.get("status_code")
    if isinstance(status, str) and status.isdigit():
        status = int(status)
    if isinstance(status, int) and status in _STATUS_CATEGORIES:
        return _structured(_STATUS_CATEGORIES[status], "status_code", status, failure)
    return classify(
        failure.get("error"), failure.get("message"), failure.get("details")
    )
