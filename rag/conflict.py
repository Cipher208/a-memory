from __future__ import annotations

"""
Conflict Resolver — async, detects conflicting memory entries.
Uses BM25 + char-trigram Jaccard hybrid for similarity (B3).
"""

import math
import uuid
from contextlib import suppress
from typing import Any

from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME

# ─── B3: BM25 + char-trigram similarity ───


def bm25_pair_similarity(text_a: str, text_b: str, k1: float = 1.5, b: float = 0.75) -> float:
    """BM25 between two documents (pseudo-corpus of 2). Returns [0,1]."""
    ta = text_a.lower().split()
    tb = text_b.lower().split()
    if not ta or not tb:
        return 0.0
    N = 2
    dl_a, dl_b = len(ta), len(tb)
    avg_dl = max((dl_a + dl_b) / 2, 1.0)
    shared = set(ta) & set(tb)
    score = 0.0
    for term in shared:
        idf = math.log((N - 1 + 0.5) / (1 + 0.5) + 1)
        tf_a = ta.count(term)
        tf_a_norm = tf_a * (k1 + 1) / (tf_a + k1 * (1 - b + b * dl_a / avg_dl))
        tf_b = tb.count(term)
        tf_b_norm = tf_b * (k1 + 1) / (tf_b + k1 * (1 - b + b * dl_b / avg_dl))
        score += idf * (tf_a_norm + tf_b_norm) / 2
    return min(score / max(math.log(N + 1) + 1.0, 1.0), 1.0)


def char_ngram_jaccard(text_a: str, text_b: str, n: int = 3) -> float:
    """Char-trigram Jaccard similarity."""

    def grams(t: str) -> set[str]:
        return {t[i : i + n] for i in range(len(t) - n + 1)}

    a, b = grams(text_a.lower()), grams(text_b.lower())
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def smart_similarity(text_a: str, text_b: str) -> float:
    """Adaptive similarity: short→ngram only, medium→weighted, long→BM25-heavy."""
    if not text_a or not text_b:
        return 0.0
    L = max(len(text_a), len(text_b))
    bm25 = bm25_pair_similarity(text_a, text_b)
    j3 = char_ngram_jaccard(text_a, text_b, n=3)
    j4 = char_ngram_jaccard(text_a, text_b, n=4)
    j = 0.5 * j3 + 0.5 * j4
    if L < 80:
        return j
    if L < 400:
        return 0.4 * bm25 + 0.6 * j
    return 0.6 * bm25 + 0.4 * j


# ─── ConflictResolver ───


class ConflictResolver:
    def __init__(self, cm: AsyncConnectionManager | None = None):
        self._cm = cm or connection_manager

    async def _init_db(self) -> None:
        await self._cm.execute_script(
            DB_NAME,
            """
            CREATE TABLE IF NOT EXISTS memory_conflicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL, content TEXT NOT NULL,
                is_conflict INTEGER DEFAULT 0, conflict_group_id TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_conflicts_user ON memory_conflicts(user_id);
            CREATE INDEX IF NOT EXISTS idx_conflicts_group ON memory_conflicts(conflict_group_id);
        """,
        )

    async def check(self, user_id: str, new_content: str, min_similarity: float = 0.3) -> dict[str, Any]:
        await self._init_db()
        conn = await self._cm.get(DB_NAME)
        keywords = [w for w in new_content.split() if len(w) > 3][:5]
        if not keywords:
            return {"content": new_content, "is_conflict": False}

        rows = await self._find_potential_conflicts(conn, user_id, keywords)

        for row in rows:
            existing_id, existing_content, is_conflict, group_id = row
            similarity = float(self._calculate_similarity(new_content, str(existing_content)))
            if similarity > min_similarity and str(existing_content) != new_content:
                return await self._handle_conflict(
                    conn, int(existing_id), str(existing_content), bool(is_conflict), group_id, new_content, similarity
                )

        await conn.execute("INSERT INTO memory_conflicts (user_id, content) VALUES (?, ?)", (user_id, new_content))
        await conn.commit()
        return {"content": new_content, "is_conflict": False}

    async def _find_potential_conflicts(self, conn: Any, user_id: str, keywords: list[str]) -> Any:
        if not keywords:
            return []
        like_conditions = " OR ".join(["content LIKE ?" for _ in keywords])
        like_params = [f"%{kw}%" for kw in keywords]
        # skylos: ignore [SKY-D211] - Parameterized via ?, like_conditions derived from local keyword count only.
        sql = f"SELECT id, content, is_conflict, conflict_group_id FROM memory_conflicts WHERE user_id=? AND ({like_conditions}) LIMIT 5"
        cur = await conn.execute(sql, (user_id, *like_params))
        return await cur.fetchall()

    async def _handle_conflict(
        self, conn: Any, existing_id: int, existing_content: str, is_conflict: bool, group_id: str | None, new_content: str, similarity: float
    ) -> dict[str, Any]:
        gid = str(group_id) if group_id else str(uuid.uuid4())
        if not is_conflict:
            await conn.execute("UPDATE memory_conflicts SET is_conflict=1, conflict_group_id=? WHERE id=?", (gid, existing_id))
        await conn.commit()
        return {
            "content": new_content,
            "is_conflict": True,
            "conflict_group_id": gid,
            "conflicts_with_id": existing_id,
            "similarity": similarity,
        }

    async def get_conflicts(self, conflict_group_id: str) -> list[dict[str, Any]]:
        await self._init_db()
        conn = await self._cm.get(DB_NAME)
        cur = await conn.execute(
            "SELECT id, content, created_at FROM memory_conflicts WHERE conflict_group_id=? ORDER BY created_at DESC",
            (conflict_group_id,),
        )
        rows = await cur.fetchall()
        return [{"id": r[0], "content": r[1], "created_at": r[2]} for r in rows]

    async def resolve(self, conflict_group_id: str, keep_id: int) -> bool:
        """B3: Archive deleted conflicts before removal, add audit trail."""
        await self._init_db()
        conn = await self._cm.get(DB_NAME)

        # Get entries to delete
        cur = await conn.execute(
            "SELECT id, content FROM memory_conflicts WHERE conflict_group_id=? AND id!=?",
            (conflict_group_id, keep_id),
        )
        to_delete = await cur.fetchall()

        # Archive before deletion
        for row in to_delete:
            del_id, del_content = row
            with suppress(Exception):
                from shared.archived_memories import ArchivedMemories

                archive = ArchivedMemories(cm=self._cm)
                await archive.archive(
                    user_id="system",
                    content=f"conflict_{del_id}: {del_content}",
                    importance=0.0,
                    reason="conflict_resolved",
                )

        # Delete and resolve
        await conn.execute("DELETE FROM memory_conflicts WHERE conflict_group_id=? AND id!=?", (conflict_group_id, keep_id))
        await conn.execute("UPDATE memory_conflicts SET is_conflict=0, conflict_group_id=NULL WHERE id=?", (keep_id,))
        await conn.commit()

        # B3: Log audit trail with conflict_group_id
        with suppress(Exception):
            from features.audit_trail import AuditTrail

            at = AuditTrail(cm=self._cm)
            await at.log(
                user_id="system",
                action="conflict_resolved",
                layer="rag",
                target_id=str(keep_id),
                details={
                    "conflict_group_id": conflict_group_id,
                    "kept_id": keep_id,
                    "removed_ids": [r[0] for r in to_delete],
                    "removed_count": len(to_delete),
                },
            )

        return True

    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """B3: BM25 + char-trigram hybrid similarity."""
        return smart_similarity(text1, text2)
