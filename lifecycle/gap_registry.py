"""S18 gap-registry (3M Find Gap): open questions → nightly registry.

L3-эпизоды с тегом question = незакрытые вопросы; плюс повторные
zero-result-запросы (S17-журнал) = провалы recall. Registry — плоская
таблица для proactive acquisition; потребитель — ночной отчёт.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from shared.connection import connection_manager
from shared.constants import DB_NAME

logger = logging.getLogger(__name__)

_GAP_MAX_AGE_DAYS = 7


async def _ensure_table(cm: Any) -> None:
    await cm.execute_script(
        DB_NAME,
        """
        CREATE TABLE IF NOT EXISTS memory_gaps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            layer TEXT NOT NULL DEFAULT 'user',
            user_id TEXT NOT NULL,
            gap TEXT NOT NULL,
            origin TEXT NOT NULL DEFAULT 'question',
            gap_hash TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_gaps_hash ON memory_gaps(gap_hash);
        """,
    )


def _gap_hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.strip().lower().encode("utf-8", "ignore")).hexdigest()


async def build_registry(layer: str = "user", limit: int = 20) -> dict[str, Any]:
    """Собрать свежие question-эпизоды + zero-result-хвосты → registry. Best-effort.

    Идемпотентно по gap_hash: повторный ночной прогон не пишет дубликаты.
    """
    cm = connection_manager
    await _ensure_table(cm)
    conn = await cm.get(DB_NAME)
    written = 0
    cutoff = time.time() - _GAP_MAX_AGE_DAYS * 86400
    try:
        # L3-вопросы: ночной срез по ВСЕМ пользователям (search_by_tag —
        # per-user API, registry — кросс-пользовательский агрегат).
        q_rows = await (
            await conn.execute(
                "SELECT user_id, summary FROM episodes"
                " WHERE layer=? AND created_at >= ? AND tags LIKE '%\"question\"%'"
                " ORDER BY created_at DESC LIMIT ?",
                (layer, cutoff, limit),
            )
        ).fetchall()
        for r in q_rows:
            text = str(r["summary"]).strip()
            if not text:
                continue
            before = conn.total_changes
            await conn.execute(
                "INSERT INTO memory_gaps (ts, layer, user_id, gap, origin, gap_hash)"
                " SELECT ?, ?, ?, ?, 'question', ?"
                " WHERE NOT EXISTS (SELECT 1 FROM memory_gaps WHERE gap_hash=?)",
                (time.time(), layer, str(r["user_id"]), text[:300], _gap_hash(text), _gap_hash(text)),
            )
            written += conn.total_changes - before
    except Exception:
        logger.exception("gap registry question scan failed")
    try:
        z_rows = await (
            await conn.execute(
                "SELECT user_id, query FROM recall_zero_results WHERE layer=? GROUP BY query_hash, user_id HAVING COUNT(*) >= 2 LIMIT ?",
                (layer, limit),
            )
        ).fetchall()
        for r in z_rows:
            text = str(r["query"]).strip()[:300]
            before = conn.total_changes
            await conn.execute(
                "INSERT INTO memory_gaps (ts, layer, user_id, gap, origin, gap_hash)"
                " SELECT ?, ?, ?, ?, 'zero_result', ?"
                " WHERE NOT EXISTS (SELECT 1 FROM memory_gaps WHERE gap_hash=?)",
                (time.time(), layer, str(r["user_id"]), text, _gap_hash(text), _gap_hash(text)),
            )
            written += conn.total_changes - before
        await conn.commit()
    except Exception:
        # recall_zero_results — lazy-ensure таблица (минер S17); до первого
        # ночного прогона её нет — zero-result ветка просто молчит.
        logger.debug("gap registry zero-result scan skipped: no journal yet")
    rows = await (await conn.execute("SELECT ts, user_id, gap, origin FROM memory_gaps ORDER BY id DESC LIMIT ?", (limit,))).fetchall()
    return {"gaps": [dict(r) for r in rows], "written": written}
