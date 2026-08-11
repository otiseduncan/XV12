from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CurrentInformationRequirement:
    required: bool
    mode: str = "current"
    reason: str = ""
    signals: tuple[str, ...] = ()


_META_OR_STABLE = re.compile(
    r"\b(?:define\s+(?:the\s+word\s+)?current|what\s+does\s+(?:the\s+word\s+)?current\s+mean|"
    r"electric(?:al)?\s+current|fictional|imaginary|role[ -]?play|"
    r"how\s+(?:a\s+)?web\s+search(?:es)?\s+work)\b",
    re.I,
)
_LOCAL_OR_PRIVATE = re.compile(
    r"\b(?:my\s+(?:inbox|mail|email|calendar|agenda|reminder|task|note|project|repo(?:sitory)?)|"
    r"this\s+(?:project|repo(?:sitory)?|workspace)|local\s+(?:runtime|machine|database)|"
    r"calibration\s+iq\s+(?:status|health)|(?:current\s+)?(?:status|health)\s+(?:of|for)\s+calibration\s+iq|"
    r"xoduz(?:['’]s)?\s+(?:status|health|capabilities)|"
    r"(?:your|xoduz(?:['’]s)?)\s+(?:current\s+)?(?:status|capabilities))\b",
    re.I,
)

_FRESH_SIGNALS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("today", re.compile(r"\b(?:today|tonight)\b", re.I)),
    ("now", re.compile(r"\b(?:right\s+now|as\s+of\s+now|at\s+the\s+moment)\b", re.I)),
    ("latest", re.compile(r"\b(?:latest|newest|most\s+recent)\b", re.I)),
    ("recent", re.compile(r"\b(?:recent|recently)\b", re.I)),
    ("near_term", re.compile(r"\bthis\s+(?:morning|afternoon|evening|weekend|week|month|year)\b", re.I)),
    (
        "updates",
        re.compile(
            r"\b(?:any\s+(?:new\s+)?updates?|new\s+updates?|further\s+updates?|"
            r"updates?\s+(?:on|about|with)|latest\s+(?:updates?|developments?)|"
            r"anything\s+new\s+(?:on|about|with))\b",
            re.I,
        ),
    ),
    (
        "happening",
        re.compile(
            r"\b(?:what(?:'s|\s+is)\s+(?:happening|going\s+on)\s+(?:with|in|around)|"
            r"what\s+are\s+the\s+updates?\s+(?:on|about|with))\b",
            re.I,
        ),
    ),
    (
        "current_attribute",
        re.compile(
            r"\bcurrent\s+(?:price|version|release|schedule|law|regulation|rule|"
            r"officeholder|leader|president|prime\s+minister|chief|ceo|status|score|result)s?\b",
            re.I,
        ),
    ),
)


def assess_current_information(message: str) -> CurrentInformationRequirement:
    """Detect high-confidence requests whose correctness requires fresh external evidence."""

    text = " ".join(str(message or "").split())
    if not text:
        return CurrentInformationRequirement(False, reason="empty request")
    if _META_OR_STABLE.search(text):
        return CurrentInformationRequirement(False, reason="stable or metalinguistic request")
    if _LOCAL_OR_PRIVATE.search(text):
        return CurrentInformationRequirement(False, reason="local or private request")

    signals = tuple(name for name, pattern in _FRESH_SIGNALS if pattern.search(text))
    if not signals:
        return CurrentInformationRequirement(False, reason="no high-confidence freshness signal")

    news_signals = {"today", "now", "latest", "recent", "near_term", "updates", "happening"}
    mode = "news" if news_signals.intersection(signals) else "general"
    return CurrentInformationRequirement(
        True,
        mode=mode,
        reason="high-confidence freshness requirement",
        signals=signals,
    )
