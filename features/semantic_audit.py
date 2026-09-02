"""E13: semantic audit — post-compaction coverage check. No LLM (cosine).

Complements D3.5 rehydrate: rehydrate re-injects the critical set, this
measures how much of the pre-compaction window the compaction summary
actually covers (mean cosine between summary and prior episode summaries).

Ceiling: harnesses run ariel with hash-fallback embeddings (deterministic,
crude); with real sentence-transformers the score is meaningful. Best-effort
by convention: never break the compaction dispatch.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def run_semantic_audit(user_id: str, compaction_ts: float, summary: str, window_hours: float = 24.0) -> dict[str, Any]:
    """Compare the compaction summary against the pre-compaction episode window.

    Score = mean cosine(summary, episode) over up to 20 prior episodes —
    the honest average-coverage metric. Logged to audit_log (action
    'semantic_audit'), fail-soft end to end.
    """
    out: dict[str, Any] = {"score": None, "compared": 0}
    summary = str(summary or "").strip()
    if not summary:
        return out
    from pathlib import Path

    from shared.connection import connection_manager
    from shared.constants import DB_NAME

    base = connection_manager.base_dir
    if not base:
        return out
    db_path = Path(str(base)) / DB_NAME
    if not db_path.exists():
        return out

    import sqlite3

    since = compaction_ts - window_hours * 3600
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                "SELECT summary FROM episodes WHERE user_id=? AND layer='user' AND created_at >= ? AND created_at < ? ORDER BY created_at DESC LIMIT 20",
                (user_id, since, compaction_ts),
            ).fetchall()
    except sqlite3.Error as exc:
        logger.debug("semantic audit read skipped: %s", exc)
        return out
    summaries = [str(r[0] or "").strip() for r in rows if str(r[0] or "").strip()]
    if not summaries:
        return out

    try:
        from shared import embeddings as _emb

        vecs = await _emb.embed_texts([summary, *summaries], prefix="passage: ")
        target, rest = vecs[0], vecs[1:]
        score = round(sum(_emb.similarity(target, v) for v in rest) / len(rest), 3)
        out.update({"score": score, "compared": len(summaries)})
    except Exception as exc:
        logger.debug("semantic audit embed skipped: %s", exc)
        return out

    try:
        from features.audit_trail import AuditTrail

        await AuditTrail().log(user_id, "semantic_audit", layer="user", details={**out, "window_hours": window_hours})
    except Exception as exc:
        logger.debug("semantic audit log skipped: %s", exc)
    return out
