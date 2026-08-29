"""B1.7: causal memory — action → outcome links in the epistemic graph."""

import asyncio

from graph.epistemic import CAUSAL_RELATIONS, EpistemicGraph
from shared.connection import AsyncConnectionManager
from shared.constants import DB_NAME


def test_causal_vocabulary():
    assert {"caused", "led_to", "prevented"} <= CAUSAL_RELATIONS


def test_record_causal_creates_nodes_and_edge(tmp_path):
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    graph = EpistemicGraph(cm=cm, layer="user")

    async def t():
        await graph.init_db()
        action_id, outcome_id = await graph.record_causal(
            "u1",
            "migrated billing to PostgreSQL",
            "checkout latency dropped 40%",
            relation="led_to",
            strength=0.9,
        )
        assert action_id > 0 and outcome_id > action_id

        conn = await cm.get(DB_NAME)
        action = await (await conn.execute("SELECT node_type, content FROM epi_nodes WHERE node_id=?", (action_id,))).fetchone()
        outcome = await (await conn.execute("SELECT node_type, content FROM epi_nodes WHERE node_id=?", (outcome_id,))).fetchone()
        assert action["node_type"] == "action"
        assert outcome["node_type"] == "outcome"

        edge = await (
            await conn.execute("SELECT relation, weight FROM epi_edges WHERE source_id=? AND target_id=?", (action_id, outcome_id))
        ).fetchone()
        assert edge["relation"] == "led_to"
        assert abs(float(edge["weight"]) - 0.9) < 1e-9

    asyncio.run(t())


def test_record_causal_validates_relation(tmp_path):
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    graph = EpistemicGraph(cm=cm, layer="user")

    async def t():
        await graph.init_db()
        try:
            await graph.record_causal("u1", "a", "b", relation="knows")
            raised = False
        except ValueError:
            raised = True
        assert raised, "non-causal relation must be rejected"

    asyncio.run(t())
