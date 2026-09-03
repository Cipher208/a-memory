from __future__ import annotations

"""
Epistemic Graph — async, layer-aware tags and relations
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from shared.connection import connection_manager
from shared.constants import DB_NAME

if TYPE_CHECKING:
    from shared.connection import AsyncConnectionManager

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

# B1.2 Social Memory Graph: entity node types + relationship vocabulary.
# Entities are deduplicated per (layer, user_id, node_type, content) —
# re-adding "Alice" returns the existing node instead of forking it.
SOCIAL_NODE_TYPES = {"person", "organization"}
SOCIAL_RELATIONS = {"knows", "works_with", "family_of", "friend_of", "met", "mentions"}

# B1.7 Causal memory: action → outcome links. Weight = causal strength.
CAUSAL_RELATIONS = {"caused", "led_to", "prevented"}
CAUSAL_NODE_TYPES = {"action", "outcome"}


class EpistemicGraph:
    USER_TAGS = USER_TAGS
    AGENT_TAGS = AGENT_TAGS
    SOCIAL_NODE_TYPES = SOCIAL_NODE_TYPES
    SOCIAL_RELATIONS = SOCIAL_RELATIONS
    CAUSAL_RELATIONS = CAUSAL_RELATIONS
    CAUSAL_NODE_TYPES = CAUSAL_NODE_TYPES

    def __init__(self, cm: AsyncConnectionManager | None = None, layer: str = "user") -> None:
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

    async def init_db(self) -> None:
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
                tags TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (source_id, target_id, relation)
            );
            CREATE INDEX IF NOT EXISTS idx_epi_layer ON epi_nodes(layer);
            CREATE INDEX IF NOT EXISTS idx_epi_user ON epi_nodes(user_id);
            CREATE INDEX IF NOT EXISTS idx_epi_type ON epi_nodes(node_type);
            CREATE INDEX IF NOT EXISTS idx_epi_tags ON epi_nodes(tags);
            CREATE INDEX IF NOT EXISTS idx_epi_scope_conf ON epi_nodes(layer, user_id, node_type, confidence DESC);
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
        try:
            await self._cm.execute_script(DB_NAME, "ALTER TABLE epi_nodes ADD COLUMN layer TEXT NOT NULL DEFAULT 'user'")
        except Exception:
            # Table already has column or other harmless migration error
            logger.debug("Layer column migration skipped (likely already exists)")
        # A2.4: edge tags column for pre-existing DBs
        try:
            await self._cm.execute_script(DB_NAME, "ALTER TABLE epi_edges ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
        except Exception:
            logger.debug("epi_edges tags column skipped (likely already exists)")

    async def find_or_add_entity(
        self,
        user_id: str,
        name: str,
        entity_type: str = "person",
        tags: list[str] | None = None,
        confidence: float = 0.5,
    ) -> tuple[int, bool]:
        """Social entity upsert: exact-match dedup per (layer, user, type, content).

        Returns (node_id, created).
        """
        conn = await self._cm.get(DB_NAME)
        cur = await conn.execute(
            "SELECT node_id FROM epi_nodes WHERE layer=? AND user_id=? AND node_type=? AND content=? LIMIT 1",
            (self.layer, user_id, entity_type, name),
        )
        row = await cur.fetchone()
        if row:
            return int(row["node_id"]), False
        node_id = await self.add_node(user_id, name, entity_type, tags, confidence)
        return node_id, True

    async def record_causal(
        self,
        user_id: str,
        action: str,
        outcome: str,
        relation: str = "led_to",
        strength: float = 0.8,
    ) -> tuple[int, int]:
        """Record an action → outcome causal link (B1.7).

        Creates an "action" node and an "outcome" node (idempotent by exact
        content within the layer+user scope) joined by an edge whose weight
        is the causal strength. Non-causal relations are rejected.
        """
        if relation not in CAUSAL_RELATIONS:
            raise ValueError(f"relation must be one of {sorted(CAUSAL_RELATIONS)}, got {relation!r}")

        conn = await self._cm.get(DB_NAME)

        async def _node(node_type: str, content: str) -> int:
            cur = await conn.execute(
                "SELECT node_id FROM epi_nodes WHERE layer=? AND user_id=? AND node_type=? AND content=? LIMIT 1",
                (self.layer, user_id, node_type, content),
            )
            row = await cur.fetchone()
            if row:
                return int(row["node_id"])
            return await self.add_node(user_id, content, node_type, None, strength)

        action_id = await _node("action", action)
        outcome_id = await _node("outcome", outcome)
        await self.add_edge(action_id, outcome_id, relation, strength)
        return action_id, outcome_id

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
        node_id = int(cursor.lastrowid or 0)
        if tags:
            for tag in tags:
                await conn.execute(
                    "INSERT OR IGNORE INTO epi_tags (node_id, tag) VALUES (?, ?)",
                    (node_id, tag),
                )
        await conn.commit()
        return node_id

    async def add_edge(self, source_id: int, target_id: int, relation: str, weight: float = 0.8, tags: list[str] | None = None) -> None:
        """Create/replace an edge. A2.4: optional `tags` (JSON list on the edge —
        traversal filters like `_inverse:blocked_by` or `_value_regex:deploy.*`).
        """
        conn = await self._cm.get(DB_NAME)
        await conn.execute(
            "INSERT OR REPLACE INTO epi_edges (source_id, target_id, relation, weight, created_at, tags) VALUES (?, ?, ?, ?, ?, ?)",
            (source_id, target_id, relation, weight, time.time(), json.dumps(tags or [])),
        )
        await conn.commit()

    async def query_by_tag(self, user_id: str, tag: str, limit: int = 20) -> list[EpistemicNode]:
        sql = """SELECT n.* FROM epi_nodes n
                 JOIN epi_tags t ON t.node_id = n.node_id
                 WHERE n.layer=? AND n.user_id=? AND t.tag=?
                 ORDER BY n.confidence DESC LIMIT ?"""
        return await self._query_nodes(sql, (self.layer, user_id, tag, limit))

    async def query_by_type(self, user_id: str, node_type: str, limit: int = 20) -> list[EpistemicNode]:
        sql = "SELECT * FROM epi_nodes WHERE layer=? AND user_id=? AND node_type=? ORDER BY confidence DESC LIMIT ?"
        return await self._query_nodes(sql, (self.layer, user_id, node_type, limit))

    async def _query_nodes(self, sql: str, params: tuple[Any, ...]) -> list[EpistemicNode]:
        conn = await self._cm.get(DB_NAME)
        cur = await conn.execute(sql, params)
        rows = await cur.fetchall()
        return [self._row_to_node(dict(r)) for r in rows]

    async def get_neighbors(self, node_id: int, depth: int = 1, relation: str | None = None, tag: str | None = None) -> list[dict[str, Any]]:
        """Neighbors via recursive CTE. A2.4: optional `relation` and edge-`tag`
        (substring match on the edge's JSON tags — `_inverse`/`_value_regex`).
        """
        conn = await self._cm.get(DB_NAME)
        edge_filter = "WHERE e.source_id = ?"
        params_list: list[Any] = [node_id]
        if relation:
            edge_filter += " AND e.relation = ?"
            params_list.append(relation)
        sql = f"""
        WITH RECURSIVE graph AS (
            SELECT e.source_id, e.target_id, e.relation, e.weight, e.tags as etags, 1 as d
            FROM epi_edges e {edge_filter}
            UNION ALL
            SELECT e.source_id, e.target_id, e.relation, e.weight, e.tags as etags, g.d + 1
            FROM epi_edges e JOIN graph g ON e.source_id = g.target_id
            WHERE g.d < ?
        )
        SELECT n.node_id, n.content, n.node_type, n.tags, g.relation, g.weight, g.etags
        FROM graph g JOIN epi_nodes n ON g.target_id = n.node_id
        WHERE n.layer = ?
        """
        params_list.append(depth)
        params_list.append(self.layer)
        cur = await conn.execute(sql, tuple(params_list))
        rows = await cur.fetchall()
        out = [
            {
                "id": r[0],
                "content": r[1],
                "type": r[2],
                "tags": json.loads(r[3]) if r[3] else [],
                "relation": r[4],
                "weight": r[5],
                "edge_tags": json.loads(r[6]) if r[6] else [],
            }
            for r in rows
        ]
        if tag:
            out = [n for n in out if any(tag in et for et in n["edge_tags"])]
        return out

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

    async def delete_nodes_older_than(self, user_id: str, cutoff: float) -> int:
        """Delete this layer's nodes created after cutoff; cleans tags/edges."""
        sql_ids = "SELECT node_id FROM epi_nodes WHERE layer=? AND user_id=? AND created_at > ?"
        conn = await self._cm.get(DB_NAME)
        cur = await conn.execute(sql_ids, (self.layer, user_id, cutoff))
        ids = [int(r[0]) for r in await cur.fetchall()]
        return await self.delete_nodes(ids)

    async def find_nodes_matching(self, user_id: str, content_pattern: str, limit: int = 50) -> list[EpistemicNode]:
        """Nodes in this layer whose content matches a SQL LIKE pattern."""
        sql = "SELECT * FROM epi_nodes WHERE layer=? AND user_id=? AND content LIKE ? LIMIT ?"
        return await self._query_nodes(sql, (self.layer, user_id, content_pattern, limit))

    async def delete_nodes(self, node_ids: list[int]) -> int:
        """Delete nodes by id along with their tags and touching edges."""
        if not node_ids:
            return 0
        conn = await self._cm.get(DB_NAME)
        placeholders = ",".join(["?"] * len(node_ids))
        ids = tuple(node_ids)
        await conn.execute(f"DELETE FROM epi_tags WHERE node_id IN ({placeholders})", ids)
        await conn.execute(
            f"DELETE FROM epi_edges WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})",
            ids + ids,
        )
        cur = await conn.execute(f"DELETE FROM epi_nodes WHERE node_id IN ({placeholders})", ids)
        await conn.commit()
        return int(cur.rowcount)

    def _row_to_node(self, row: dict[str, Any]) -> EpistemicNode:
        return EpistemicNode(
            node_id=int(row["node_id"]),
            user_id=str(row["user_id"]),
            layer=str(row["layer"]),
            content=str(row["content"]),
            node_type=str(row["node_type"]),
            tags=list(json.loads(row["tags"])) if row["tags"] else [],
            confidence=float(row["confidence"]),
            created_at=float(row["created_at"]),
        )
