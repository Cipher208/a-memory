"""D1.16 reflection system — deterministic meta-memories (no LLM, B1.6 ceiling).

A reflection is a higher-order insight computed from memory activity:
windowed episode counts, recurring topic tokens, automation totals. Written
by the `memory_reflect` tool or the nightly 5th phase; listing is the
meta-memory read-back. Sync sqlite3 over connection_manager (rehydrate.py
pattern) — missing table degrades gracefully.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from collections import Counter
from typing import Any

logger = logging.getLogger(__name__)

_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "to",
        "of",
        "in",
        "on",
        "for",
        "with",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "this",
        "that",
        "it",
        "its",
        "as",
        "at",
        "by",
        "from",
        "not",
        "но",
        "и",
        "в",
        "на",
        "с",
        "за",
        "к",
        "по",
        "у",
        "же",
        "что",
        "как",
        "это",
        "для",
        "или",
    }
)


def _db_path() -> Any:
    from shared.connection import connection_manager

    return connection_manager.base_dir / "memory.db"


def _top_topics(summaries: list[str], limit: int = 3) -> list[str]:
    words: Counter[str] = Counter()
    for s in summaries:
        for w in re.findall(r"[a-zA-Zа-яА-ЯёЁ]{4,}", str(s).lower()):
            if w not in _STOPWORDS:
                words[w] += 1
    return [w for w, _ in words.most_common(limit)]


def build_reflection(mem: Any, user_id: str, period_hours: int = 24) -> dict[str, Any]:
    """Compute a deterministic reflection over the memory window."""
    cutoff = time.time() - period_hours * 3600
    episodes: list[Any] = []
    try:
        episodes = [e for e in mem.l3.get_episodes(user_id, 20) if float(getattr(e, "created_at", 0) or 0) >= cutoff]
    except Exception as exc:
        logger.debug("reflection episodes read failed: %s", exc)

    summaries = [str(getattr(e, "summary", "") or "") for e in episodes]
    top = _top_topics(summaries)

    stats: dict[str, Any] = {
        "episodes_window": len(episodes),
        "top_topics": top,
        "window_hours": period_hours,
    }

    parts = [
        f"{len(episodes)} episodes captured in the last {period_hours}h",
    ]
    if top:
        parts.append(f"recurring topics: {', '.join(top)}")
    else:
        parts.append("no dominant topic — memory activity is diffuse")
    insight = ". ".join(parts) + "."

    return {"insight": insight, "stats": stats}


def save_reflection(user_id: str, topic: str, insight: str, stats: dict[str, Any]) -> int:
    """Insert one reflection row. Returns the row id (0 on failure)."""
    try:
        with sqlite3.connect(str(_db_path())) as conn:
            cursor = conn.execute(
                "INSERT INTO reflections (user_id, layer, topic, insight, stats_json, created_at) VALUES (?, 'user', ?, ?, ?, ?)",
                (user_id, topic, insight, json.dumps(stats, ensure_ascii=False), time.time()),
            )
            conn.commit()
            return int(cursor.lastrowid or 0)
    except Exception as exc:
        logger.debug("reflections insert failed: %s", exc)
        return 0


def list_reflections(user_id: str, topic: str = "", limit: int = 10) -> list[dict[str, Any]]:
    """Recent reflections, newest first; topic filter via LIKE when given."""
    try:
        with sqlite3.connect(str(_db_path())) as conn:
            conn.row_factory = sqlite3.Row
            if topic:
                rows = conn.execute(
                    "SELECT * FROM reflections WHERE user_id = ? AND topic LIKE ? ORDER BY created_at DESC LIMIT ?",
                    (user_id, f"%{topic}%", limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM reflections WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                    (user_id, limit),
                ).fetchall()
            return [dict(r) for r in rows]
    except Exception as exc:
        logger.debug("reflections read failed: %s", exc)
        return []


def nightly_reflection(mem: Any, user_id: str) -> dict[str, Any]:
    """Nightly 5th phase: write the daily reflection row."""
    out = build_reflection(mem, user_id, period_hours=24)
    rid = save_reflection(user_id, "daily", out["insight"], out["stats"])
    return {"written": rid > 0, "id": rid, "insight": out["insight"]}
