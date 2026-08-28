"""Auto training-value classification for thoughts.

Classifies a think() text into high / medium / low training value by
scanning for decision markers (DECISION_RE) and outcome markers (OUTCOME_RE).
Pure function; no DB, no IO. Value rides the temporal "thought" event metadata.
"""
from __future__ import annotations

import re

DECISION_RE = re.compile(
    r"\b(decided|decision|choose|chose|решил|решение|выбрал|решили|выбираю)\b",
    re.IGNORECASE,
)
OUTCOME_RE = re.compile(
    r"\b(worked|works|outcome|result|success|получилось|работает|сработало|результат|успешно)\b",
    re.IGNORECASE,
)


def classify_training_value(text: str) -> str:
    """high: decision + outcome present; medium: exactly one; low: neither."""
    has_decision = bool(DECISION_RE.search(text))
    has_outcome = bool(OUTCOME_RE.search(text))
    if has_decision and has_outcome:
        return "high"
    if has_decision or has_outcome:
        return "medium"
    return "low"
