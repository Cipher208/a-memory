"""Status-broadcast detector: agent work reports are process echoes, not memory.

Pure module (stdlib only), sibling of shared/dialogue.py — the dialogic
detector rejects the owner's short chat, this one rejects the agent's long
close-out reports: "X SHIPPED (2026-09-12, commit 66be3ab, gate 1697/0)".
Their durable truth lives in git, MEMORY.md and day-close checkpoints; the
auto-save pipeline must not re-derive L4 facts from the echoes.

Precision-first co-occurrence: one status word AND one structural companion
(commit hash / gate count / mypy count / pytest tail / dated stamp) are both
required. Either half alone never fires, so prose that merely mentions a fix,
a hash or a date stays eligible. Residual gray lines without companions are
intentionally left to the adaptive EMA gate. Spec:
~/.compose/specs/2026-09-12-status-broadcast-detector-design.md (S3).
"""

from __future__ import annotations

import re

# Exact status-report words at word boundaries, case-insensitive. Cyrillic
# forms are DATA. Deliberately no bare nouns (migration, cutover, error):
# only the verbs/adjectives a close-out broadcast is built around.
_STATUS_WORDS = (
    "shipped",
    "shipping",
    "verified",
    "verification",
    "fixed",
    "fix",
    "closed",
    "complete",
    "completed",
    "accepted",
    "done",
    "restored",
    "deployed",
    "executed",
    "created",
    "confirmed",
    "committed",
    "expanded",
    "ported",
    "proven",
    "success",
    "закрыт",
    "закрыта",
    "закрыто",
    "закрыты",
    "стартовал",
    "стартовала",
    "выполнен",
    "выполнена",
    "выполнено",
    "готово",
    "подтверждён",
    "подтвержден",
    "подтверждена",
    "подтверждено",
    "обновлены",
    "обновлено",
    "записан",
    "записана",
    "исправлен",
    "исправлена",
    "добавлен",
    "добавлена",
    "портирован",
    "портирована",
)

# Headers that open a close-out report even without a status verb.
_HEAD_ANCHORS = ("DAY CLOSE", "CHECKPOINT", "STAGE ")

_STATUS_RE = re.compile(r"\b(?:" + "|".join(_STATUS_WORDS) + r")\b", re.IGNORECASE)
_COMPANION_RES = (
    re.compile(r"\b[0-9a-f]{7,8}\b"),  # commit hash
    re.compile(r"\bgate\s+\d+(?:/\d+)?", re.IGNORECASE),  # "gate 1697/0"
    re.compile(r"\bmypy\b.{0,16}?\d{2,3}", re.IGNORECASE),  # "mypy 232 clean"
    re.compile(r"\b\d+ passed\b", re.IGNORECASE),  # pytest tail
    re.compile(r"\((?:19|20)\d{2}-\d{2}-\d{2}"),  # dated stamp
)


def is_status_broadcast(text: str) -> bool:
    """Return True for the agent close-out-report register — never a durable fact.

    Co-occurrence only (see module docstring). A head anchor in the first 40
    chars counts as the status half.
    """
    if not text:
        return False
    head_hit = any(anchor in text[:40].upper() for anchor in _HEAD_ANCHORS)
    if not (head_hit or _STATUS_RE.search(text)):
        return False
    return any(rx.search(text) for rx in _COMPANION_RES)
