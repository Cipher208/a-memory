"""Graph enrich orchestrator (Phase G Task 1): node pre-cleanup + miner skeleton.

Pre-cleanup sweeps fact-nodes whose content is raw JSON / tool output / recall
dumps out of epi_nodes — each is first captured to the append-only l0_journal
(event='graph_cleanup') so nothing is lost, then deleted with its edges/tags.
Miners (Tasks 2-5) plug into MINERS from lifecycle.graph_miners; stubs
contribute no edges yet.
"""

from __future__ import annotations

from typing import Any

from shared.connection import connection_manager
from shared.constants import DB_NAME

# Content markers of raw-harness junk that must never live as graph nodes.
_JUNK_LIKE = ("[{%", "%tool_use_id%", "%[ariel recall]%")


async def graph_enrich(layer: str = "user") -> dict[str, Any]:
    """Pre-clean JSON junk from the graph, then run miners. Returns stats."""
    from graph.epistemic import EpistemicGraph
    from lifecycle.graph_miners import MINERS
    from shared.l0 import capture

    cm = connection_manager
    conn = await cm.get(DB_NAME)
    junk = await (
        await conn.execute(
            f"SELECT node_id, user_id, content FROM epi_nodes"
            f" WHERE layer=? AND node_type='fact'"
            f" AND (content LIKE {' OR content LIKE '.join(['?'] * len(_JUNK_LIKE))})",
            (layer, *_JUNK_LIKE),
        )
    ).fetchall()

    cleaned = 0
    ids: list[int] = []
    for row in junk:
        # capture() never raises; junk must reach L0 before its node is gone.
        await capture(event="graph_cleanup", layer=layer, user_id=str(row["user_id"]), text=str(row["content"]))
        ids.append(int(row["node_id"]))
    if ids:
        cleaned = await EpistemicGraph(cm=cm, layer=layer).delete_nodes(ids)

    miners: dict[str, dict[str, int]] = {}
    for name, miner in MINERS.items():
        try:
            res = await miner(cm, layer)
            miners[name] = {"edges": int(res.get("edges", 0))}
        except Exception:
            miners[name] = {"edges": 0}

    # G5 sanitation: validity recheck (рёбра вне окна → status='expired').
    from lifecycle.graph_sanitation import validate_edges

    expired = await validate_edges(conn)

    return {"nodes_cleaned": cleaned, "miners": miners, "sanitation": {"expired": expired}}
