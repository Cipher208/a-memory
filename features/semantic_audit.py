"""E13: semantic audit — post-compaction coverage check. No LLM (cosine).

Complements D3.5 rehydrate: rehydrate re-injects the critical L4 set, this
measures how much of the pre-compaction window those facts actually cover —
mean over prior episodes of MAX cosine(episode, top L4 facts).

Deliberately payload-independent: real harnesses dispatch
post_context_compression WITHOUT a summary/query (Hermes {reason,since,until},
cow {reason}, MiMoCode {old_session_id,reason}), so the original
summary-vs-episodes design never fired in live. Comparing against the L4 set
requires nothing from the harness.

Ceiling: harnesses run ariel with hash-fallback embeddings (deterministic,
crude); with real sentence-transformers the score is meaningful. Best-effort
by convention: never break the compaction dispatch.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def run_semantic_audit(user_id: str, compaction_ts: float, window_hours: float = 24.0, fact_limit: int = 20) -> dict[str, Any]:
    """Coverage of the pre-compaction episode window by the current L4 set.

    Score = mean over up to 20 pre-window episodes of the MAX cosine between
    the episode summary and any L4 fact value. Logged to audit_log (action
    'semantic_audit'), fail-soft end to end.
    """
    out: dict[str, Any] = {"score": None, "compared": 0, "facts": 0}
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
            ep_rows = conn.execute(
                "SELECT summary FROM episodes WHERE user_id=? AND layer='user' AND created_at >= ? AND created_at < ? ORDER BY created_at DESC LIMIT 20",
                (user_id, since, compaction_ts),
            ).fetchall()
            fact_rows = conn.execute(
                "SELECT value FROM core_memory WHERE user_id=? AND layer='user' ORDER BY importance DESC, updated_at DESC LIMIT ?",
                (user_id, max(1, min(int(fact_limit), 50))),
            ).fetchall()
    except sqlite3.Error as exc:
        logger.debug("semantic audit read skipped: %s", exc)
        return out
    summaries = [str(r[0] or "").strip() for r in ep_rows if str(r[0] or "").strip()]
    facts = [str(r[0] or "").strip() for r in fact_rows if str(r[0] or "").strip()]
    out["facts"] = len(facts)
    if not summaries or not facts:
        return out

    try:
        from shared import embeddings as _emb

        vecs = await _emb.embed_texts([*summaries, *facts], prefix="passage: ")
        ep_vecs, fact_vecs = vecs[: len(summaries)], vecs[len(summaries) :]
        per_episode = [max((_emb.similarity(ev, fv) for fv in fact_vecs), default=0.0) for ev in ep_vecs]
        score = round(sum(per_episode) / len(per_episode), 3)
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
