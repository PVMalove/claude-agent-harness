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
        r"|http\s*503|\b503\b|temporarily unavailable",
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


_STRUCTURED_CATEGORIES = {
    "capacity": "capacity",
    "capacity-exhausted": "capacity",
    "insufficient-capacity": "capacity",
    "rate-limit": "rate-limit",
    "rate_limit": "rate-limit",
    "service-unavailable": "service-unavailable",
    "service_unavailable": "service-unavailable",
    "runtime-unavailable": "runtime-unavailable",
    "runtime_unavailable": "runtime-unavailable",
    "timeout": "timeout",
    "network": "network",
    "runtime-crash": "runtime-crash",
    "resources": "resources",
    "service": "service",
}


def classify_runtime_failure(failure: Mapping[str, object] | None) -> JsonObject | None:
    """Classify structured runtime fields before falling back to bounded error text.

    Capacity, rate-limit and service-unavailable are intentionally distinct.  A structured
    category is evidence supplied by the runtime adapter; a free-form message remains a heuristic
    and cannot by itself prove that a worker exited.
    """
    if not isinstance(failure, Mapping):
        return None
    for field in ("category", "failure_category", "kind"):
        value = failure.get(field)
        if isinstance(value, str):
            normalized = value.strip().lower().replace(" ", "-")
            category = _STRUCTURED_CATEGORIES.get(normalized)
            if category is not None:
                return {
                    "method": "structured",
                    "category": category,
                    "field": field,
                    "value": value,
                    "text": str(failure.get("error", ""))[:500],
                }
    status = failure.get("status_code") or failure.get("http_status")
    if status == 429 or status == "429":
        return {
            "method": "structured",
            "category": "rate-limit",
            "field": "status_code",
            "value": status,
            "text": str(failure.get("error", ""))[:500],
        }
    if status in {503, "503"}:
        return {
            "method": "structured",
            "category": "service-unavailable",
            "field": "status_code",
            "value": status,
            "text": str(failure.get("error", ""))[:500],
        }
    return classify(failure.get("error"), failure.get("message"), failure.get("details"))


classify_failure = classify_runtime_failure
