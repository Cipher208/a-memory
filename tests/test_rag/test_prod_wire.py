"""Phase H Task C1: prod-wire — dual-route в prod-recall, l2_enrich в cron, agent-layer nightly.

RETRIEVAL_MODE (дефолт 'full'): memory_search идёт через route_query (5-source
RRF — генератор кандидатов, EDM/ITS — re-rank); 'rrf' — старый путь.
enrich_sessions подключён в backup_cron._fire_nightly_hooks (после sweep);
agent-layer получает собственный nightly-хук (hooks/agent_hooks.AgentHooks._nightly).
"""

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from rag.multi_source import MultiSourceRAG
from shared.connection import connection_manager
from shared.constants import DB_NAME
from shared.migrations import MigrationManager


@pytest.fixture
async def db(tmp_path):
    original = connection_manager.base_dir
    connection_manager.base_dir = tmp_path
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()
    connection_manager.base_dir = original


def _make_ctx(user_multi: Any) -> MagicMock:
    ctx = MagicMock()
    app = MagicMock()
    app.user_multi = user_multi
    ctx.request_context.lifespan_context = app
    return ctx


class _FakeSource:
    def __init__(self, hits: list[dict[str, Any]]):
        self._hits = hits

    async def search(self, query: str, user_id: str = "default", strategy: str | None = None, limit: int = 10, **kw: Any) -> list[dict[str, Any]]:
        return [dict(h) for h in self._hits][:limit]


async def test_memory_search_routes_via_dual_route(monkeypatch) -> None:
    """Prod memory_search (mode=full, дефолт) должен вызывать route_query."""
    from mcp_server.tools.ops import memory_search

    monkeypatch.delenv("RETRIEVAL_MODE", raising=False)
    queries: list[str] = []

    async def spy_route(rag: Any, query: str, **kw: Any) -> list[dict[str, Any]]:
        queries.append(query)
        return [{"id": 1, "title": "t", "content": "c", "score": 1.0, "source": "core"}]

    monkeypatch.setattr("rag.dual_route.route_query", spy_route)
    ctx = _make_ctx(MultiSourceRAG(rag=None, wiki=None, cm=None))
    res = await memory_search("как настроить бэкап postgres", ctx=ctx)
    assert queries == ["как настроить бэкап postgres"], "memory_search должен идти через route_query при mode=full"
    assert res["count"] == 1 and res["results"][0]["id"] == 1


async def test_memory_search_rrf_mode_keeps_legacy_path(monkeypatch) -> None:
    """RETRIEVAL_MODE='rrf' — статус-кво: route_query НЕ вызывается."""
    from mcp_server.tools.ops import memory_search

    monkeypatch.setenv("RETRIEVAL_MODE", "rrf")
    legacy = AsyncMock(return_value=[{"id": 9, "title": "t", "content": "c", "score": 0.5, "source": "rag"}])
    multi = MultiSourceRAG(rag=None, wiki=None, cm=None)
    multi.search = legacy  # type: ignore[method-assign]
    spy_route = AsyncMock(return_value=[])
    monkeypatch.setattr("rag.dual_route.route_query", spy_route)
    ctx = _make_ctx(multi)
    res = await memory_search("query", ctx=ctx)
    spy_route.assert_not_awaited()
    legacy.assert_awaited_once()
    assert res["count"] == 1


async def test_memory_search_full_edm_reorders(db, monkeypatch) -> None:
    """RETRIEVAL_MODE='full' в prod memory_search: EDM-порядок ≠ сырой RRF-порядок.

    id1 — высший RRF-score, но покрывает 1/4 токенов запроса; id3 покрывает
    все 4 → novelty (β=0.8) выводит его на первое место в full-режиме.
    """
    from mcp_server.tools.ops import memory_search

    monkeypatch.delenv("RETRIEVAL_MODE", raising=False)
    hits = [
        {"id": 1, "title": "a", "content": "postgres советы по производительности", "score": 0.9, "source": "rag"},
        {"id": 2, "title": "b", "content": "recipes for apple pie dessert", "score": 0.5, "source": "rag"},
        {"id": 3, "title": "c", "content": "postgres backup cron settings", "score": 0.85, "source": "rag"},
    ]
    multi = MultiSourceRAG(rag=_FakeSource(hits), wiki=None, cm=connection_manager)
    raw = await multi.search("postgres backup cron settings", user_id="u1", limit=3)
    raw_ids = [h["id"] for h in raw]
    assert raw_ids[0] == 1, f"сырой RRF-путь: порядок по score, raw={raw_ids}"

    ctx = _make_ctx(multi)
    res = await memory_search("postgres backup cron settings", ctx=ctx)
    out_ids = [h["id"] for h in res["results"]]
    assert out_ids != raw_ids, f"EDM должен переранживать сырой RRF: raw={raw_ids}, full={out_ids}"
    assert out_ids and out_ids[0] == 3, f"полное покрытие запроса — топ-1 (novelty): {out_ids}"


def test_backup_cron_fires_enrich_sessions(tmp_path, monkeypatch) -> None:
    """backup_cron._fire_nightly_hooks вызывает enrich_sessions(days=1) после sweep."""
    from features.backup_cron import BackupCron
    from hooks.registry import hook_registry

    spy = AsyncMock(return_value={"rebuilt": 0})
    monkeypatch.setattr("features.l2_enrich.enrich_sessions", spy)
    fire_mock = AsyncMock(return_value={"results": []})
    monkeypatch.setattr(hook_registry, "fire", fire_mock)
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()

    bc = BackupCron(base_dir=str(tmp_path))
    bc._fire_nightly_hooks()

    spy.assert_awaited_once_with(days=1)
    fired_layers = [c.args[1] for c in fire_mock.await_args_list]
    assert fired_layers == ["user", "agent"], f"nightly фаерится обоим слоям, got={fired_layers}"


async def test_agent_nightly_hook_runs_on_empty_graph(db) -> None:
    """AgentHooks._nightly существует, регистрируется и не падает на пустом графе."""
    from hooks.agent_hooks import AgentHooks
    from hooks.registry import hook_registry

    hooks = AgentHooks()
    res = await hooks._nightly({"trigger": "backup_cron"})
    assert "graph_enrich" in res, f"agent nightly прогоняет graph_enrich(layer='agent'), res={res}"

    hook_registry.register_instance(hooks)
    fired = await hook_registry.fire("nightly", "agent", {"trigger": "backup_cron"})
    assert fired.get("handler_count", 0) >= 1, f"nightly фаерится на agent-слой, got={fired}"


# ---------------------------------------------------------------------------
# issue #78 — the four layer-scoped sources must follow the instance layer,
# and the production construction site must hand them a connection manager.
# Every other test builds MultiSourceRAG explicitly with cm=, which is exactly
# why the live server could run for months with self.cm=None.
# ---------------------------------------------------------------------------

#: Isolate one source family per assertion: everything else off.
_NO_RAG_NO_WIKI = {"include_rag": False, "include_wiki": False}
_MEMORY_ONLY = {**_NO_RAG_NO_WIKI, "include_graph": False, "include_entities": False}
_GRAPH_ONLY = {**_NO_RAG_NO_WIKI, "include_episodic": False, "include_core": False, "include_entities": False}
_ENTITY_ONLY = {**_NO_RAG_NO_WIKI, "include_episodic": False, "include_core": False, "include_graph": False}


async def test_app_context_wires_cm_and_layer_into_both_multi_rag(db) -> None:
    """issue #78 bug 2: the two lines in AppContext passed neither cm nor layer."""
    from mcp_server.context import AppContext

    app = AppContext()

    assert app.user_multi.cm is not None, "user_multi без cm: graph/entities в проде всегда []"
    assert app.agent_multi.cm is not None, "agent_multi без cm: graph/entities в проде всегда []"
    assert app.user_multi.layer == "user"
    assert app.agent_multi.layer == "agent", "agent_multi держал дефолт 'user': agent-dream читал user-строки"


async def test_episodic_and_core_sources_follow_the_instance_layer(db) -> None:
    """issue #78 bug 1: both built their memory object with the layer defaulted to 'user'."""
    conn = await db.get(DB_NAME)
    now = time.time()
    for layer in ("user", "agent"):
        await conn.execute(
            "INSERT INTO episodes (layer, user_id, summary, emotional_weight, tags, created_at) VALUES (?, 'default', ?, 0.5, '[]', ?)",
            (layer, f"zqzqxw {layer} episode", now),
        )
        await conn.execute(
            "INSERT INTO core_memory (layer, user_id, key, value, created_at, updated_at) VALUES (?, 'default', 'zqzqxw', ?, ?, ?)",
            (layer, f"{layer} core fact", now, now),
        )
    await conn.commit()

    agent = MultiSourceRAG(rag=None, wiki=None, cm=db, layer="agent")
    user = MultiSourceRAG(rag=None, wiki=None, cm=db, layer="user")

    agent_hits = await agent.search("zqzqxw", user_id="default", limit=10, **_MEMORY_ONLY)
    user_hits = await user.search("zqzqxw", user_id="default", limit=10, **_MEMORY_ONLY)

    assert {h["source"] for h in agent_hits} == {"episodic", "core"}, agent_hits
    assert all("agent" in h["content"] for h in agent_hits), agent_hits
    assert {h["source"] for h in user_hits} == {"episodic", "core"}, user_hits
    assert all("user" in h["content"] for h in user_hits), user_hits


async def test_graph_and_entity_sources_respect_the_layer(db) -> None:
    """`epi_nodes` carries a layer column the two node sources never filtered on."""
    conn = await db.get(DB_NAME)
    now = time.time()
    for layer in ("user", "agent"):
        await conn.execute(
            "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES (?, 'default', ?, 'fact', '[]', 0.5, ?)",
            (layer, f"grafanadash {layer} node postgresql", now),
        )
    await conn.commit()

    agent = MultiSourceRAG(rag=None, wiki=None, cm=db, layer="agent")
    user = MultiSourceRAG(rag=None, wiki=None, cm=db, layer="user")

    agent_graph = await agent.search("grafanadash", user_id="default", limit=10, **_GRAPH_ONLY)
    user_graph = await user.search("grafanadash", user_id="default", limit=10, **_GRAPH_ONLY)
    assert agent_graph and all("agent" in h["content"] for h in agent_graph), agent_graph
    assert user_graph and all("user" in h["content"] for h in user_graph), user_graph

    # "postgres" canonises to the postgresql class, so the node texts are reachable.
    agent_ent = await agent.search("postgres", user_id="default", limit=10, **_ENTITY_ONLY)
    user_ent = await user.search("postgres", user_id="default", limit=10, **_ENTITY_ONLY)
    assert agent_ent and all("agent" in h["content"] for h in agent_ent), agent_ent
    assert user_ent and all("user" in h["content"] for h in user_ent), user_ent
