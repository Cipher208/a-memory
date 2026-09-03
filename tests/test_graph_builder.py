"""B1.3: multi-stage graph consolidation — episodes → entities + links."""

import asyncio

from lifecycle.graph_builder import build_from_episodes, discover_entities, extract_links
from shared.connection import connection_manager
from shared.constants import DB_NAME


def _make(tmp_path):
    """Migrated global singleton on tmp (same pattern as the phase-E fixtures).

    Returns the global manager — tests must monkeypatch `.base_dir`, never
    swap the singleton object itself (from-imports in wiki.manager et al.
    would capture the impostor permanently).
    """
    connection_manager.base_dir = tmp_path

    async def init():
        from shared.migrations import MigrationManager

        await MigrationManager(cm=connection_manager).migrate()

    asyncio.run(init())
    connection_manager._conns.clear()
    return connection_manager


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

    # Migrate the GLOBAL singleton onto tmp — do NOT swap the object out:
    # modules that already did `from shared.connection import
    # connection_manager` (wiki.manager, ...) would capture the impostor at
    # their first import inside this test and keep it forever.
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()

    async def t():
        from shared.migrations import MigrationManager

        await MigrationManager(cm=connection_manager).migrate()
        result = await UserHooks("u1")._nightly({"layer": "user"})
        assert result["action"] == "create_diary"
        assert "graph_build" in result
        assert result["graph_build"]["episodes"] == 0
        # A1.5: the wiki sibling runs right next to it (same night)
        assert "wiki_graph_build" in result
        assert set(result["wiki_graph_build"]) == {"pages", "links"}

    asyncio.run(t())
    connection_manager._conns.clear()
