"""S13 semantica decision read-surface: trace_decision_chain.

Given an action node (epi_nodes), rebuild the causal chain action → outcome →
... — a forward BFS over epi_edges with relation in CAUSAL_RELATIONS (what
graph/epistemic.py::record_causal actually writes — E17a/B1.7:
{"caused", "led_to", "prevented"}; "blocked" from early S13 drafts does not
exist in the writer). Read-only, no LLM.

Depth is capped by `depth` (default 5); cycles are cut off by the visited set.
Scope: nodes are filtered by (layer, user_id) — other users' chains are not visible.
"""

from __future__ import annotations

from typing import Any

from graph.epistemic import CAUSAL_RELATIONS


async def trace_decision_chain(node_id: int, user_id: str, depth: int = 5, *, layer: str = "user") -> dict[str, Any]:
    """BFS over causal edges from a node: {'root': {...} | None, 'chain': [...]}.

    chain is in BFS traversal order: {node_id, content, node_type, relation,
    strength, depth}, where relation/strength is the edge the node was reached
    by and depth is the distance from the root (1-based). root uses the same
    format with relation/strength=None, depth=0. A non-existent node_id (or a
    node of another user_id/layer) → {'root': None, 'chain': []}.
    """
    from shared.connection import connection_manager
    from shared.constants import DB_NAME

    conn = await connection_manager.get(DB_NAME)
    root_row = await (
        await conn.execute(
            "SELECT node_id, content, node_type FROM epi_nodes WHERE node_id=? AND layer=? AND user_id=?",
            (int(node_id), layer, user_id),
        )
    ).fetchone()
    if root_row is None:
        return {"root": None, "chain": []}

    root = {
        "node_id": int(root_row["node_id"]),
        "content": str(root_row["content"]),
        "node_type": str(root_row["node_type"]),
        "relation": None,
        "strength": None,
        "depth": 0,
    }

    chain: list[dict[str, Any]] = []
    visited = {int(node_id)}
    frontier = [int(node_id)]
    causal = sorted(CAUSAL_RELATIONS)
    for level in range(1, max(0, depth) + 1):
        if not frontier:
            break
        placeholders = ",".join("?" * len(frontier))
        causal_ph = ",".join("?" * len(causal))
        rows = await (
            await conn.execute(
                f"""SELECT e.target_id, e.relation, e.weight, n.content, n.node_type
                    FROM epi_edges e JOIN epi_nodes n ON n.node_id = e.target_id
                    WHERE e.source_id IN ({placeholders}) AND e.relation IN ({causal_ph})
                      AND n.layer = ? AND n.user_id = ?""",
                (*frontier, *causal, layer, user_id),
            )
        ).fetchall()
        frontier = []
        for r in rows:
            target_id = int(r["target_id"])
            if target_id in visited:
                continue
            visited.add(target_id)
            chain.append(
                {
                    "node_id": target_id,
                    "content": str(r["content"]),
                    "node_type": str(r["node_type"]),
                    "relation": str(r["relation"]),
                    "strength": float(r["weight"]),
                    "depth": level,
                }
            )
            frontier.append(target_id)
    return {"root": root, "chain": chain}
