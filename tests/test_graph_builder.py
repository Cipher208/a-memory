"""B1.3: multi-stage graph consolidation — episodes → entities + links."""

import asyncio

from lifecycle.graph_builder import build_from_episodes, discover_entities, extract_links
from shared.connection import AsyncConnectionManager
from shared.constants import DB_NAME


def _make(tmp_path):
    cm = AsyncConnectionManager(base_dir=str(tmp_path))

    async def init():
        from core.episodic import EpisodicMemory
        from features.audit_trail import AuditTrail
        from graph.epistemic import EpistemicGraph

        await EpistemicGraph(cm=cm, layer="user").init_db()
        await EpisodicMemory(cm=cm, layer="user")._init_db()
        await AuditTrail(cm=cm)._init_db()

    asyncio.run(init())
    return cm


def test_extract_links_en():
    links = extract_links("Alice works at Acme. Bob knows Alice.")
    assert len(links) == 2
    assert (links[0].subject, links[0].relation, links[0].object_type) == ("Alice", "works_with", "organization")
    assert (links[1].subject, links[1].relation) == ("Bob", "knows")


def test_extract_links_ru():
    # Note: RU inflected forms ("с Алисой") land as separate entities —
    # documented ceiling of the regex extractor.
    links = extract_links("Алиса работает в Яндекс. Борис знает Алиса.")
    assert len(links) == 2
    assert (links[0].subject, links[0].object, links[0].relation) == ("Алиса", "Яндекс", "works_with")
    assert links[1].relation == "knows"


def test_extract_no_false_positives():
    # Common sentence starts must not become entities
    assert extract_links("This is a plain sentence. В нём нет связей.") == []


def test_discover_entities():
    found = discover_entities("Сегодня встретил Бориса и talked to Claire about the plan.")
    assert "Бориса" in found
    assert "Claire" in found


def test_build_from_episodes_idempotent(tmp_path):
    """Entities/edges are created once; a re-run does not duplicate them."""
    cm = _make(tmp_path)

    async def t():
        from core.episodic import EpisodicMemory

        epi = EpisodicMemory(cm=cm, layer="user")
        await epi.save("u1", "Алиса работает в Яндекс. Борис знает Алиса.", 0.8)
        await epi.save("u1", "Встретил Клэр, обсудили план", 0.6)

        stats1 = await build_from_episodes(cm, "u1", layer="user")
        assert stats1["episodes"] == 2
        assert stats1["entities"] == 4  # Алиса, Яндекс, Борис, Клэр
        assert stats1["edges"] == 2

        conn = await cm.get(DB_NAME)
        people = await (await conn.execute("SELECT COUNT(*) FROM epi_nodes WHERE node_type IN ('person','organization')")).fetchone()
        assert people[0] == 4

        stats2 = await build_from_episodes(cm, "u1", layer="user")
        assert stats2["entities"] == 0  # all already known
        people2 = await (await conn.execute("SELECT COUNT(*) FROM epi_nodes WHERE node_type IN ('person','organization')")).fetchone()
        assert people2[0] == 4

    asyncio.run(t())


def test_nightly_runs_graph_build(tmp_path, monkeypatch):
    """_nightly includes a graph_build phase (non-fatal)."""
    from hooks.user_hooks import UserHooks

    cm = _make(tmp_path)
    monkeypatch.setattr("shared.connection.connection_manager", cm)

    async def t():
        result = await UserHooks("u1")._nightly({"layer": "user"})
        assert result["action"] == "create_diary"
        assert "graph_build" in result
        assert result["graph_build"]["episodes"] == 0

    asyncio.run(t())
