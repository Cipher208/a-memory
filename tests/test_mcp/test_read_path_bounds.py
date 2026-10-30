"""F53 / F54 / F33 / F77 / F84 — the read path is bounded, scoped and single.

Each test names the mutation it kills:

* F53 — a caller-supplied ``limit`` reaching SQL unchanged (``LIMIT -1`` is
  unlimited in SQLite, ``LIMIT 0`` returns nothing).
* F54 — ``memory_graph_edges`` filtering the layer but not the user on BOTH
  endpoints, so a join pulls a foreign node's content.
* F33 — the enumerative route returning the whole S2 collection uncapped.
* F77 — the important/pinned inject blocks never debiting the budget.
* F84 — every ``AppContext()`` appending another full set of hook handlers.
"""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from shared.connection import connection_manager
from shared.migrations import MigrationManager

# The bound the tools must enforce (F53). Kept as a literal so the red run
# shows the actual row count instead of an ImportError before the fix exists;
# the test below pins it to the implementation so the two cannot drift.
MAX_TOOL_LIMIT = 200
TOTAL_ROWS = 250


def _ctx(app: object) -> object:
    ctx = MagicMock()
    ctx.request_context = MagicMock()
    ctx.request_context.lifespan_context = app
    return ctx


@pytest.fixture
async def read_db(tmp_path, monkeypatch):
    """A migrated temp database wired into the global manager, no live bases."""
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


def _app(cm: object, multi: object | None = None) -> SimpleNamespace:
    from core import MemoryManager
    from graph.epistemic import EpistemicGraph

    app = SimpleNamespace(mm=MemoryManager(cm=cm), user_graph=EpistemicGraph(layer="user", cm=cm))
    if multi is not None:
        app.user_multi = multi
    return app


async def _seed_history(cm: object, count: int) -> None:
    conn = await cm.get("memory.db")
    for i in range(count):
        await conn.execute(
            "INSERT INTO core_memory_history (layer, user_id, key, new_value, commit_hash, triggered_by, created_at)"
            " VALUES ('user', 'alice', ?, ?, ?, 'test', ?)",
            (f"k{i}", f"v{i}", f"h{i}", time.time()),
        )
    await conn.commit()


async def _seed_nodes(cm: object, count: int, user_id: str = "alice", prefix: str = "alice") -> None:
    conn = await cm.get("memory.db")
    for i in range(count):
        await conn.execute(
            "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES ('user', ?, ?, 'fact', '[]', 0.5, ?)",
            (user_id, f"{prefix}-{i}", time.time()),
        )
    await conn.commit()


class _FakeMulti:
    """A store that reproduces SQLite's LIMIT semantics: negative means no limit."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.seen_limit: int | None = None

    async def search(self, query: str, **kwargs: object) -> list[dict[str, object]]:
        limit = kwargs.get("limit")
        self.seen_limit = limit if isinstance(limit, int) else None
        if self.seen_limit is None or self.seen_limit < 0:
            return list(self.rows)
        return list(self.rows[: self.seen_limit])


async def _count_from_tool(tool: str, cm: object, limit: int) -> int:
    if tool == "memory_history":
        from mcp_server.tools.ops import memory_history

        out = await memory_history(action="list", user_id="alice", limit=limit, ctx=_ctx(_app(cm)))
        return int(out["count"])
    if tool == "memory_graph_nodes":
        from mcp_server.tools.graph import memory_graph_nodes

        out = await memory_graph_nodes(user_id="alice", limit=limit, ctx=_ctx(_app(cm)))
        return int(out["count"])
    if tool == "memory_search":
        from mcp_server.tools.ops import memory_search

        multi = _FakeMulti([{"id": i, "content": f"row {i}", "source": "wiki"} for i in range(TOTAL_ROWS)])
        out = await memory_search(query="postgres", sources="wiki", limit=limit, ctx=_ctx(_app(cm, multi)))
        return int(out["count"])
    raise AssertionError(f"unknown tool {tool}")


@pytest.mark.parametrize("tool", ["memory_history", "memory_graph_nodes", "memory_search"])
@pytest.mark.parametrize("limit", [-1, 10**6])
async def test_tool_limit_is_clamped(read_db, tool: str, limit: int) -> None:
    """Kills F53: a caller limit passed straight into ``LIMIT ?``.

    ``-1`` is SQLite's "no limit" and ``10**6`` is just large: both must come
    back bounded by the tool boundary, never as the whole table.
    """
    await _seed_history(read_db, TOTAL_ROWS)
    await _seed_nodes(read_db, TOTAL_ROWS)

    count = await _count_from_tool(tool, read_db, limit)

    assert count <= MAX_TOOL_LIMIT, f"{tool}(limit={limit}) returned {count} rows — the tool boundary did not clamp"
    assert count < TOTAL_ROWS, f"{tool}(limit={limit}) returned the whole table ({count} rows)"

    from mcp_server.tools.base import MAX_TOOL_LIMIT as IMPLEMENTED_CAP

    assert IMPLEMENTED_CAP == MAX_TOOL_LIMIT, "the test's bound drifted from mcp_server.tools.base"


async def test_limit_zero_still_returns_something(read_db) -> None:
    """Kills F53: ``LIMIT 0`` returns nothing, which reads as "no data"."""
    await _seed_history(read_db, 3)

    from mcp_server.tools.ops import memory_history

    out = await memory_history(action="list", user_id="alice", limit=0, ctx=_ctx(_app(read_db)))

    assert out["count"] == 1, f"limit=0 must clamp to 1, got {out['count']} rows"


async def test_graph_edges_does_not_cross_users(read_db) -> None:
    """Kills F54: edges filtered by layer only, so a join leaks a foreign node."""
    conn = await read_db.get("memory.db")
    now = time.time()
    ids: dict[str, int] = {}
    for user in ("alice", "bob"):
        for i in range(2):
            cur = await conn.execute(
                "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at)"
                " VALUES ('user', ?, ?, 'fact', '[]', 0.5, ?)",
                (user, f"{user}-{i}", now),
            )
            ids[f"{user}-{i}"] = int(cur.lastrowid)
    edges = [
        (ids["alice-0"], ids["alice-1"], "alice_rel"),
        (ids["bob-0"], ids["bob-1"], "bob_rel"),
        (ids["bob-0"], ids["alice-0"], "bob_to_alice"),
    ]
    for src, tgt, rel in edges:
        await conn.execute(
            "INSERT INTO epi_edges (source_id, target_id, relation, weight, created_at) VALUES (?, ?, ?, 0.8, ?)",
            (src, tgt, rel, now),
        )
    await conn.commit()

    from mcp_server.tools.graph import memory_graph_edges

    out = await memory_graph_edges(user_id="alice", ctx=_ctx(_app(read_db)))

    assert out["edges"], "alice's own edge must still be returned"
    for edge in out["edges"]:
        assert str(edge["source_content"]).startswith("alice"), f"foreign source leaked: {edge}"
        assert str(edge["target_content"]).startswith("alice"), f"foreign target leaked: {edge}"


class _FakeWiki:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    async def list_all(self, limit: int = 50, status: str | None = "active") -> list[dict[str, object]]:
        return self._rows[:limit]


async def test_enumerative_route_respects_the_limit() -> None:
    """Kills F33: the enumerative branch returns every S2 hit, ignoring ``limit``."""
    from rag.dual_route import route_query

    rows = [{"entry_id": i, "title": f"diary-{i}", "content": f"запись {i}", "wiki_type": "diary"} for i in range(20)]
    rag = SimpleNamespace(wiki=_FakeWiki(rows))

    hits = await route_query(rag, "list all diary entries", limit=5)

    assert len(hits) <= 5, f"enumerative route returned {len(hits)} hits for limit=5"


async def _empty_async(*args, **kwargs):
    return []


async def test_inject_budget_covers_every_block(monkeypatch) -> None:
    """Kills F77: important/pinned append without debiting ``remaining``."""
    import features.inject as inj
    from shared.tokens import estimate_tokens

    class _L1:
        def get_recent(self, n):
            return []

    class _L3:
        async def search_by_tag(self, user_id, tag, limit):
            return []

    class _L4:
        async def get_all(self, user_id, limit):
            now = time.time()
            return [SimpleNamespace(key=f"k{i}", value="x" * 80, importance=0.9, visibility="visible", created_at=now) for i in range(5)]

        async def get_pinned(self, user_id, limit):
            now = time.time()
            return [SimpleNamespace(key=f"p{i}", value="y" * 80, importance=0.9, created_at=now) for i in range(5)]

    monkeypatch.setattr(inj, "_pending_proposals", _empty_async)
    mem = SimpleNamespace(l1=_L1(), l3=_L3(), l4=_L4())

    # Tight budget: pinned alone fits, pinned + important does not. Without the
    # debit both would be appended and the pair would exceed the budget; with it
    # the second block is refused. Pinned must be the one that wins — the block
    # is documented as always injected, which is why it now claims the budget
    # before the importance-ranked block can spend it.
    tight = await inj.build_inject_blocks(mem, rag=None, user_id="u", text="", budget=150)
    tight_kinds = [b["kind"] for b in tight]
    tight_total = sum(estimate_tokens(b["content"]) for b in tight if b["kind"] != "cache_break")

    assert "pinned" in tight_kinds, f"pinned must always be injected, kinds={tight_kinds}"
    assert "important" not in tight_kinds, f"important must not fit the remaining budget, kinds={tight_kinds}"
    assert tight_total <= 150, f"inject budget is 150 tokens but the blocks sum to {tight_total}: {tight_kinds}"

    # Generous budget: both blocks fit and both are built.
    roomy = await inj.build_inject_blocks(mem, rag=None, user_id="u", text="", budget=400)
    roomy_kinds = [b["kind"] for b in roomy]
    roomy_total = sum(estimate_tokens(b["content"]) for b in roomy if b["kind"] != "cache_break")

    assert "pinned" in roomy_kinds and "important" in roomy_kinds, f"both blocks must fit a 400-token budget, kinds={roomy_kinds}"
    assert roomy_total <= 400, f"inject budget is 400 tokens but the blocks sum to {roomy_total}: {roomy_kinds}"


def test_appcontext_does_not_double_the_hook_registry() -> None:
    """Kills F84: every ``AppContext()`` appends another full set of handlers."""
    from hooks.registry import hook_registry
    from mcp_server.context import AppContext

    AppContext()
    after_first = {name: len(handlers) for name, handlers in hook_registry._hooks.items()}
    first_new_message = len(hook_registry._hooks.get("new_message", []))

    AppContext()
    after_second = {name: len(handlers) for name, handlers in hook_registry._hooks.items()}

    assert after_second == after_first, f"a second AppContext() doubled the registry: {after_first} -> {after_second}"
    assert len(hook_registry._hooks.get("new_message", [])) == first_new_message, "new_message gained handlers on the second construction"


async def test_graph_edges_default_does_not_read_another_user(read_db) -> None:
    """Kills F54's remaining hole: an omitted user_id fell back to layer-only.

    The sibling node readers default to `"default"`; the edges tool defaulted to
    an empty string, which the SQL treated as "no user predicate" — so a caller
    who simply left the argument out read every user's edges and their content.
    """
    conn = await read_db.get("memory.db")
    now = time.time()
    ids: dict[str, int] = {}
    for user in ("default", "bob"):
        for i in range(2):
            cur = await conn.execute(
                "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at)"
                " VALUES ('user', ?, ?, 'fact', '[]', 0.5, ?)",
                (user, f"{user}-{i}", now),
            )
            ids[f"{user}-{i}"] = int(cur.lastrowid)
    for src, tgt, rel in (
        (ids["default-0"], ids["default-1"], "mine"),
        (ids["bob-0"], ids["bob-1"], "bob_rel"),
    ):
        await conn.execute(
            "INSERT INTO epi_edges (source_id, target_id, relation, weight, created_at) VALUES (?, ?, ?, 0.8, ?)",
            (src, tgt, rel, now),
        )
    await conn.commit()

    from mcp_server.tools.graph import memory_graph_edges

    # No user_id argument at all — the default must not be "every user".
    out = await memory_graph_edges(ctx=_ctx(_app(read_db)))

    assert out["edges"], "the default user's own edge must still be returned"
    for edge in out["edges"]:
        assert str(edge["source_content"]).startswith("default"), f"foreign source leaked on the default call: {edge}"
        assert str(edge["target_content"]).startswith("default"), f"foreign target leaked on the default call: {edge}"
