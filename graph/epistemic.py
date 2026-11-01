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
from shared.write_dispatch import WriteDispatch, get_write_dispatch

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
# Node types "action"/"outcome"/"error_outcome" are created by miners as
# literals; no registry — a restricted set here would drift (error_outcome).
CAUSAL_RELATIONS = {"caused", "led_to", "prevented"}


async def _insert_node(
    conn: Any,
    layer: str,
    user_id: str,
    content: str,
    node_type: str,
    tags: list[str] | None,
    confidence: float,
) -> int:
    """Insert one epi_nodes row plus its tags. Caller owns the transaction."""
    cursor = await conn.execute(
        "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (layer, user_id, content, node_type, json.dumps(tags or []), confidence, time.time()),
    )
    node_id = int(cursor.lastrowid or 0)
    for tag in tags or []:
        await conn.execute("INSERT OR IGNORE INTO epi_tags (node_id, tag) VALUES (?, ?)", (node_id, tag))
    return node_id


class EpistemicGraph:
    USER_TAGS = USER_TAGS
    AGENT_TAGS = AGENT_TAGS
    SOCIAL_NODE_TYPES = SOCIAL_NODE_TYPES
    SOCIAL_RELATIONS = SOCIAL_RELATIONS
    CAUSAL_RELATIONS = CAUSAL_RELATIONS

    def __init__(self, cm: AsyncConnectionManager | None = None, layer: str = "user") -> None:
        self._cm = cm or connection_manager
        self.layer = layer

    @property
    def _dispatch(self) -> WriteDispatch:
        # Resolved on every access so a repointed manager is honoured.
        return get_write_dispatch(self._cm)

    @staticmethod
    def tag_description(tag: str) -> str | None:
        """Return human-readable description for a known tag, or None."""
        return USER_TAGS.get(tag) or AGENT_TAGS.get(tag)

    @staticmethod
    def is_known_tag(tag: str) -> bool:
        """Check whether a tag belongs to USER_TAGS or AGENT_TAGS."""
        return tag in USER_TAGS or tag in AGENT_TAGS

    async def init_db(self) -> None:
        await self._dispatch.execute_script(
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
            await self._dispatch.execute_script(DB_NAME, "ALTER TABLE epi_nodes ADD COLUMN layer TEXT NOT NULL DEFAULT 'user'")
        except Exception:
            # Table already has column or other harmless migration error
            logger.debug("Layer column migration skipped (likely already exists)")
        # A2.4: edge tags column for pre-existing DBs
        try:
            await self._dispatch.execute_script(DB_NAME, "ALTER TABLE epi_edges ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'")
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

        async def _upsert(conn: Any) -> tuple[int, bool]:
            cur = await conn.execute(
                "SELECT node_id FROM epi_nodes WHERE layer=? AND user_id=? AND node_type=? AND content=? LIMIT 1",
                (self.layer, user_id, entity_type, name),
            )
            row = await cur.fetchone()
            if row:
                return int(row["node_id"]), False
            return await _insert_node(conn, self.layer, user_id, name, entity_type, tags, confidence), True

        return await self._dispatch.atomic(DB_NAME, _upsert)

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

        async def _record(conn: Any) -> tuple[int, int]:
            async def _node(node_type: str, content: str) -> int:
                cur = await conn.execute(
                    "SELECT node_id FROM epi_nodes WHERE layer=? AND user_id=? AND node_type=? AND content=? LIMIT 1",
                    (self.layer, user_id, node_type, content),
                )
                row = await cur.fetchone()
                if row:
                    return int(row["node_id"])
                return await _insert_node(conn, self.layer, user_id, content, node_type, None, strength)

            action_id = await _node("action", action)
            outcome_id = await _node("outcome", outcome)
            await conn.execute(
                "INSERT OR REPLACE INTO epi_edges (source_id, target_id, relation, weight, created_at, tags) VALUES (?, ?, ?, ?, ?, ?)",
                (action_id, outcome_id, relation, strength, time.time(), json.dumps([])),
            )
            return action_id, outcome_id

        return await self._dispatch.atomic(DB_NAME, _record)

    async def add_node(self, user_id: str, content: str, node_type: str, tags: list[str] | None = None, confidence: float = 0.5) -> int:
        if tags:
            known = {**USER_TAGS, **AGENT_TAGS}
            for tag in tags:
                if tag not in known:
                    logger.debug("tag %r not in USER_TAGS/AGENT_TAGS — allowing as free-form", tag)

        async def _insert(conn: Any) -> int:
            return await _insert_node(conn, self.layer, user_id, content, node_type, tags, confidence)

        return await self._dispatch.atomic(DB_NAME, _insert)

    async def add_edge(self, source_id: int, target_id: int, relation: str, weight: float = 0.8, tags: list[str] | None = None) -> None:
        """Create/replace an edge, optionally tagged.

        A2.4: `tags` is a JSON list on the edge — traversal filters like
        `_inverse:blocked_by` or `_value_regex:deploy.*`.
        """
        await self._dispatch.write(
            DB_NAME,
            "INSERT OR REPLACE INTO epi_edges (source_id, target_id, relation, weight, created_at, tags) VALUES (?, ?, ?, ?, ?, ?)",
            (source_id, target_id, relation, weight, time.time(), json.dumps(tags or [])),
        )

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
        rows = await self._dispatch.query(DB_NAME, sql, params)
        return [self._row_to_node(r) for r in rows]

    async def get_neighbors(self, node_id: int, depth: int = 1, relation: str | None = None, tag: str | None = None) -> list[dict[str, Any]]:
        """Neighbors via recursive CTE.

        A2.4: optional `relation` and edge-`tag` (substring match on the
        edge's JSON tags — `_inverse`/`_value_regex`).
        """
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
        rows = await self._dispatch.query(DB_NAME, sql, tuple(params_list))
        out = [
            {
                "id": r["node_id"],
                "content": r["content"],
                "type": r["node_type"],
                "tags": json.loads(r["tags"]) if r["tags"] else [],
                "relation": r["relation"],
                "weight": r["weight"],
                "edge_tags": json.loads(r["etags"]) if r["etags"] else [],
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
        rows = await self._dispatch.query(DB_NAME, sql, (source_id, max_depth, target_id))
        return [{"target": r["target_id"], "relation": r["relation"], "weight": r["weight"], "depth": r["d"]} for r in rows]

    async def count_nodes(self, user_id: str | None = None) -> int:
        if user_id:
            total = await self._dispatch.scalar(DB_NAME, "SELECT COUNT(*) FROM epi_nodes WHERE layer=? AND user_id=?", (self.layer, user_id))
        else:
            total = await self._dispatch.scalar(DB_NAME, "SELECT COUNT(*) FROM epi_nodes WHERE layer=?", (self.layer,))
        return int(total or 0)

    async def count_edges(self, user_id: str | None = None) -> dict[str, int]:
        """Count this layer's edges, split into live and dead by weight.

        Returns `{"total", "live", "dead"}`.

        `epi_edges` carries no `layer` column, so an edge belongs to the layer
        of its SOURCE node — that is the convention counted here. It is NOT the
        convention `memory_graph_edges` lists by: that reader requires both
        endpoints in the requested layer, because it returns the target node's
        content and a source-only filter would pull a foreign layer's text into
        the answer. The two therefore diverge on a cross-layer edge: counted
        here, not listed there. Today no such edge exists; a node re-layering
        would create them, and this counter would then exceed what the tool
        shows. An edge whose source node is gone is counted by none of the three
        numbers rather than guessed into one.

        "Dead" is `weight <= 0`, not `weight < NREM_FLOOR`: measured on all
        three house databases, only 0.1% of edges fall strictly between zero and
        the 0.05 floor, so the coarse split is the accurate one. A dead edge is
        not garbage — it is the resting state from which the next miner pass
        revives it (see `_insert_edge`), which is why nothing prunes zero
        eagerly.
        """
        where = "s.layer = ?"
        params: tuple[Any, ...] = (self.layer,)
        if user_id:
            where += " AND s.user_id = ?"
            params = (self.layer, user_id)
        row = await self._dispatch.select_one(
            DB_NAME,
            (
                "SELECT COUNT(*) AS total,"
                " COALESCE(SUM(CASE WHEN e.weight > 0 THEN 1 ELSE 0 END), 0) AS live,"
                " COALESCE(SUM(CASE WHEN e.weight <= 0 THEN 1 ELSE 0 END), 0) AS dead"
                f" FROM epi_edges e JOIN epi_nodes s ON e.source_id = s.node_id WHERE {where}"
            ),
            params,
        )
        if not row:
            return {"total": 0, "live": 0, "dead": 0}
        return {"total": int(row["total"]), "live": int(row["live"]), "dead": int(row["dead"])}

    async def delete_nodes_older_than(self, user_id: str, cutoff: float) -> int:
        """Delete this layer's nodes created after cutoff; cleans tags/edges."""
        sql_ids = "SELECT node_id FROM epi_nodes WHERE layer=? AND user_id=? AND created_at > ?"
        rows = await self._dispatch.query(DB_NAME, sql_ids, (self.layer, user_id, cutoff))
        ids = [int(r["node_id"]) for r in rows]
        return await self.delete_nodes(ids)

    async def find_nodes_matching(self, user_id: str, content_pattern: str, limit: int = 50) -> list[EpistemicNode]:
        """Nodes in this layer whose content matches a SQL LIKE pattern."""
        sql = "SELECT * FROM epi_nodes WHERE layer=? AND user_id=? AND content LIKE ? LIMIT ?"
        return await self._query_nodes(sql, (self.layer, user_id, content_pattern, limit))

    async def delete_nodes(self, node_ids: list[int]) -> int:
        """Delete nodes by id along with their tags and touching edges."""
        if not node_ids:
            return 0
        placeholders = ",".join(["?"] * len(node_ids))
        ids = tuple(node_ids)

        async def _delete(conn: Any) -> int:
            await conn.execute(f"DELETE FROM epi_tags WHERE node_id IN ({placeholders})", ids)
            await conn.execute(
                f"DELETE FROM epi_edges WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})",
                ids + ids,
            )
            cur = await conn.execute(f"DELETE FROM epi_nodes WHERE node_id IN ({placeholders})", ids)
            return int(cur.rowcount or 0)

        return await self._dispatch.atomic(DB_NAME, _delete)

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
