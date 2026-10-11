"""Conservative text heuristic that recognises an infrastructure failure in a blocked report.

It only widens what `batch resume-stop` may *propose*: the human approval, the clean worktree,
the report without code and the retry budget still gate recovery. Any sign of a code, test,
scope or requirements problem wins over an infrastructure sign, and unknown text stays unknown.
"""

from __future__ import annotations

import re

from harness.orchestration.core.utils import JsonObject

# Order matters: the first matching category names the failure.
INFRASTRUCTURE_PATTERNS = (
    ("rate-limit", r"\b429\b|rate[- ]?limit|too many requests"),
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
