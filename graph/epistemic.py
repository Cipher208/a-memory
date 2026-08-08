from __future__ import annotations
"""
Epistemic Graph — async, layer-aware tags and relations
"""

import contextlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from shared.connection import connection_manager
from shared.constants import DB_NAME

logger = logging.getLogger(__name__)


@dataclass
class EpistemicNode:
    node_id: int
    user_id: str
    layer: str
    content: str
    node_type: str
    tags: list[str]
    confidence: float
    created_at: float


USER_TAGS = {
    "fact_about_user": "Fact about user",
    "user_decision": "User decision",
    "user_preference": "User preference",
    "user_emotion": "User emotion",
}

AGENT_TAGS = {
    "learned_from": "Learned from error",
    "decided_because": "Agent decision",
    "evolved_to": "Personality evolved",
    "felt_in_context": "Emotion in context",
    "wiki_contains": "Second brain",
    "error_pattern": "Error pattern",
    "correction_pattern": "Correction pattern",
    "personality_trait": "Personality trait",
}


class EpistemicGraph:
    USER_TAGS = USER_TAGS
    AGENT_TAGS = AGENT_TAGS

    def __init__(self, cm=None, layer: str = "user"):
        self._cm = cm or connection_manager
        self.layer = layer

    @staticmethod
    def tag_description(tag: str) -> str | None:
        """Return human-readable description for a known tag, or None."""
        return USER_TAGS.get(tag) or AGENT_TAGS.get(tag)

    @staticmethod
    def is_known_tag(tag: str) -> bool:
        """Check whether a tag belongs to USER_TAGS or AGENT_TAGS."""
        return tag in USER_TAGS or tag in AGENT_TAGS

    async def init_db(self):
        await self._cm.execute_script(
            DB_NAME,
            """
            CREATE TABLE IF NOT EXISTS epi_nodes (
                node_id INTEGER PRIMARY KEY AUTOINCREMENT,
                layer TEXT NOT NULL DEFAULT 'user',
                user_id TEXT NOT NULL,
                content TEXT NOT NULL,
                node_type TEXT NOT NULL,
                tags TEXT,
                confidence REAL DEFAULT 0.5,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS epi_edges (
                source_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                relation TEXT NOT NULL,
                weight REAL DEFAULT 0.8,
                created_at REAL NOT NULL,
                PRIMARY KEY (source_id, target_id, relation)
            );
            CREATE INDEX IF NOT EXISTS idx_epi_layer ON epi_nodes(layer);
            CREATE INDEX IF NOT EXISTS idx_epi_user ON epi_nodes(user_id);
            CREATE INDEX IF NOT EXISTS idx_epi_type ON epi_nodes(node_type);
            CREATE INDEX IF NOT EXISTS idx_epi_tags ON epi_nodes(tags);
            CREATE TABLE IF NOT EXISTS epi_tags (
                node_id INTEGER NOT NULL,
                tag TEXT NOT NULL,
                PRIMARY KEY (node_id, tag),
                FOREIGN KEY (node_id) REFERENCES epi_nodes(node_id)
            );
            CREATE INDEX IF NOT EXISTS idx_epi_tags_tag ON epi_tags(tag);
        """,
        )
        # Migration: add layer column if missing
        with contextlib.suppress(Exception):
            await self._cm.execute_script(DB_NAME, "ALTER TABLE epi_nodes ADD COLUMN layer TEXT NOT NULL DEFAULT 'user'")

    async def add_node(self, user_id: str, content: str, node_type: str, tags: list[str] | None = None, confidence: float = 0.5) -> int:
        if tags:
            known = {**USER_TAGS, **AGENT_TAGS}
            for tag in tags:
                if tag not in known:
                    logger.debug("tag %r not in USER_TAGS/AGENT_TAGS — allowing as free-form", tag)
        conn = await self._cm.get(DB_NAME)
        cursor = await conn.execute(
            "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.layer, user_id, content, node_type, json.dumps(tags or []), confidence, time.time()),
        )
        node_id = cursor.lastrowid
        if tags:
            for tag in tags:
                await conn.execute(
                    "INSERT OR IGNORE INTO epi_tags (node_id, tag) VALUES (?, ?)",
                    (node_id, tag),
                )
        await conn.commit()
        return node_id

    async def add_edge(self, source_id: int, target_id: int, relation: str, weight: float = 0.8):
        conn = await self._cm.get(DB_NAME)
        await conn.execute(
            "INSERT OR REPLACE INTO epi_edges (source_id, target_id, relation, weight, created_at) VALUES (?, ?, ?, ?, ?)",
            (source_id, target_id, relation, weight, time.time()),
        )
        await conn.commit()

    async def query_by_tag(self, user_id: str, tag: str, limit: int = 20) -> list[EpistemicNode]:
        conn = await self._cm.get(DB_NAME)
        cur = await conn.execute(
            """SELECT n.* FROM epi_nodes n
               JOIN epi_tags t ON t.node_id = n.node_id
               WHERE n.layer=? AND n.user_id=? AND t.tag=?
               ORDER BY n.confidence DESC LIMIT ?""",
            (self.layer, user_id, tag, limit),
        )
        rows = await cur.fetchall()
        return [self._row_to_node(r) for r in rows]

    async def query_by_type(self, user_id: str, node_type: str, limit: int = 20) -> list[EpistemicNode]:
        conn = await self._cm.get(DB_NAME)
        cur = await conn.execute(
            "SELECT * FROM epi_nodes WHERE layer=? AND user_id=? AND node_type=? ORDER BY confidence DESC LIMIT ?",
            (self.layer, user_id, node_type, limit),
        )
        rows = await cur.fetchall()
        return [self._row_to_node(r) for r in rows]

    async def get_neighbors(self, node_id: int, depth: int = 1) -> list[dict[str, Any]]:
        conn = await self._cm.get(DB_NAME)
        sql = """
        WITH RECURSIVE graph AS (
            SELECT e.source_id, e.target_id, e.relation, e.weight, 1 as d
            FROM epi_edges e WHERE e.source_id = ?
            UNION ALL
            SELECT e.source_id, e.target_id, e.relation, e.weight, g.d + 1
            FROM epi_edges e JOIN graph g ON e.source_id = g.target_id
            WHERE g.d < ?
        )
        SELECT n.node_id, n.content, n.node_type, n.tags, g.relation, g.weight
        FROM graph g JOIN epi_nodes n ON g.target_id = n.node_id
        WHERE n.layer = ?
        """
        cur = await conn.execute(sql, (node_id, depth, self.layer))
        rows = await cur.fetchall()
        return [
            {
                "id": r[0],
                "content": r[1],
                "type": r[2],
                "tags": json.loads(r[3]) if r[3] else [],
                "relation": r[4],
                "weight": r[5],
            }
            for r in rows
        ]

    async def find_path(self, source_id: int, target_id: int, max_depth: int | None = None) -> list[dict[str, Any]]:
        if max_depth is None:
            try:
                from config import config

                max_depth = config.get("graph", "max_depth") or 3
            except Exception:
                max_depth = 3
        conn = await self._cm.get(DB_NAME)
        sql = """
        WITH RECURSIVE path AS (
            SELECT source_id, target_id, relation, weight, 1 as d
            FROM epi_edges WHERE source_id = ?
            UNION ALL
            SELECT e.source_id, e.target_id, e.relation, e.weight, p.d + 1
            FROM epi_edges e JOIN path p ON e.source_id = p.target_id
            WHERE p.d < ?
        )
        SELECT target_id, relation, weight, d FROM path WHERE target_id = ? LIMIT 1
        """
        cur = await conn.execute(sql, (source_id, max_depth, target_id))
        rows = await cur.fetchall()
        return [{"target": r[0], "relation": r[1], "weight": r[2], "depth": r[3]} for r in rows]

    async def count_nodes(self, user_id: str | None = None) -> int:
        conn = await self._cm.get(DB_NAME)
        if user_id:
            cur = await conn.execute("SELECT COUNT(*) FROM epi_nodes WHERE layer=? AND user_id=?", (self.layer, user_id))
        else:
            cur = await conn.execute("SELECT COUNT(*) FROM epi_nodes WHERE layer=?", (self.layer,))
        row = await cur.fetchone()
        return row[0] if row else 0

    def _row_to_node(self, row) -> EpistemicNode:
        return EpistemicNode(
            node_id=row["node_id"],
            user_id=row["user_id"],
            layer=row["layer"],
            content=row["content"],
            node_type=row["node_type"],
            tags=json.loads(row["tags"]) if row["tags"] else [],
            confidence=row["confidence"],
            created_at=row["created_at"],
        )
