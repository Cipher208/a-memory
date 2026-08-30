"""D1.5 verification layer — check that retrieved memories answer the query.

Deterministic, no LLM: content-token overlap with the query. A semantic hit
with ZERO meaningful-token overlap is noise (embedding/FTS drift) and gets
dropped before it enters the recall report. Graph-expand hits are exempt —
their relevance is structural (1-hop of a verified hit), not lexical.

Ceiling (documented): exact-zero filter only; overlap scoring/ranking is v2.
"""

from __future__ import annotations

import re
from typing import Any

_TOKEN_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ]{3,}")

_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "was",
        "were",
        "did",
        "does",
        "what",
        "when",
        "where",
        "how",
        "who",
        "his",
        "her",
        "that",
        "this",
        "with",
        "from",
        "about",
        "что",
        "как",
        "где",
        "когда",
        "кого",
        "чем",
        "про",
        "это",
        "этот",
        "был",
        "было",
        "для",
        "или",
    }
)


def content_tokens(text: str) -> set[str]:
    """Lowercase content words (>=3 chars, stopwords stripped)."""
    return {t for t in _TOKEN_RE.findall(str(text).lower()) if t not in _STOPWORDS}


def verify_hits(query: str, hits: list[dict[str, Any]], key: str = "content") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition hits into (verified, dropped) by query-token overlap.

    An empty query verifies everything (nothing to check against). A hit is
    dropped only on exact-zero meaningful overlap.
    """
    q_tokens = content_tokens(query)
    if not q_tokens:
        return list(hits), []
    verified: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for h in hits:
        content = str(h.get(key) or h.get("value") or h.get("summary") or h.get("title") or "")
        if content_tokens(content) & q_tokens:
            verified.append(h)
        else:
            dropped.append(h)
    return verified, dropped
