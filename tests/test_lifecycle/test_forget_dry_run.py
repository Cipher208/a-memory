"""`forget` must say what it is about to cut, and be able to cut an exact list.

Why these exist (Discussion #1): a `forget(scope="fuzzy")` with a short key
archived seven episodes where two were meant — the episodes were recoverable, but
nothing had warned the caller and nothing could name the seven beforehand. A dry
run and an id-scoped cut are the two halves of the answer.

The tests below hold three promises, and the second is the one that matters:
the preview is the same computation as the pass (so it cannot drift), it writes
nothing (so it is safe to call always), and a named-id cut touches exactly what
was named (so nothing else pays for the mistake).
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from shared.connection import connection_manager
from shared.migrations import MigrationManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

USER = "u_dry"


@pytest.fixture
async def cm(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """A real migrated database, not a mocked one.

    `dry_run` is a claim about rows on disk; asserting it against MagicMock would
    prove that the mock was not called, which is a weaker statement and a
    different one.
    """
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


@pytest.fixture
async def app(cm: Any) -> Any:
    from mcp_server.context import AppContext

    return AppContext()


@pytest.fixture
def ctx(app: Any) -> Any:
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


async def _seed_episode(cm: Any, summary: str, layer: str = "user") -> int:
    conn = await cm.get("memory.db")
    cursor = await conn.execute(
        "INSERT INTO episodes (layer, user_id, summary, emotional_weight, tags, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (layer, USER, summary, 0.5, "[]", time.time()),
    )
    await conn.commit()
    return int(cursor.lastrowid)


async def _count_episodes(cm: Any) -> int:
    conn = await cm.get("memory.db")
    return int((await (await conn.execute("SELECT COUNT(*) FROM episodes WHERE user_id=?", (USER,))).fetchone())[0])


async def _count_archived(cm: Any) -> int:
    conn = await cm.get("memory.db")
    return int((await (await conn.execute("SELECT COUNT(*) FROM archived_memories WHERE user_id=?", (USER,))).fetchone())[0])


async def _archived_rows(cm: Any) -> list[Any]:
    conn = await cm.get("memory.db")
    return list(
        await (await conn.execute("SELECT memory_type, original_id, archive_reason FROM archived_memories WHERE user_id=?", (USER,))).fetchall()
    )


async def test_dry_run_writes_nothing(app: Any, ctx: Any, cm: Any) -> None:
    """The whole promise: a preview of three episodes leaves three episodes."""
    from mcp_server.tools.primitives.forget import forget

    ids = [await _seed_episode(cm, f"preview me {i}") for i in range(3)]
    before = await _count_episodes(cm)

    result = await forget(user_id=USER, scope="episode_ids", episode_ids=ids, dry_run=True, ctx=ctx)

    assert result["dry_run"] is True
    assert result["deleted_l3"] == 0
    assert result["deleted_l4"] == 0
    assert result["deleted_graph"] == 0
    assert [e["episode_id"] for e in result["episodes"]] == ids
    assert await _count_episodes(cm) == before
    assert await _count_archived(cm) == 0, "dry run archived something — the shadow bin is no longer a preview"


async def test_the_preview_names_what_the_real_pass_deletes(app: Any, ctx: Any, cm: Any) -> None:
    """One computation, two modes — the reason the preview cannot drift."""
    from mcp_server.tools.primitives.forget import forget

    ids = [await _seed_episode(cm, f"same both times {i}") for i in range(3)]

    preview = await forget(user_id=USER, scope="episode_ids", episode_ids=ids, dry_run=True, ctx=ctx)
    real = await forget(user_id=USER, scope="episode_ids", episode_ids=ids, dry_run=False, ctx=ctx)

    assert [e["episode_id"] for e in preview["episodes"]] == [e["episode_id"] for e in real["episodes"]]
    assert preview["episodes"] == real["episodes"]
    assert real["deleted_l3"] == 3
    assert preview["deleted_l3"] == 0
    assert await _count_episodes(cm) == 0


async def test_episode_ids_touches_only_what_was_named(app: Any, ctx: Any, cm: Any) -> None:
    """The answer to a fuzzy match that swept too far: nothing else pays."""
    from mcp_server.tools.primitives.forget import forget

    ids = [await _seed_episode(cm, f"neighbour {i}") for i in range(5)]
    doomed = [ids[1], ids[3]]

    result = await forget(user_id=USER, scope="episode_ids", episode_ids=doomed, ctx=ctx)

    assert result["deleted_l3"] == 2
    assert await _count_episodes(cm) == 3
    survivors = await (await (await cm.get("memory.db")).execute("SELECT episode_id FROM episodes WHERE user_id=?", (USER,))).fetchall()
    assert sorted(int(r[0]) for r in survivors) == [ids[0], ids[2], ids[4]]


async def test_episode_ids_reports_an_id_that_names_nothing(app: Any, ctx: Any, cm: Any) -> None:
    """Ids are reported, not guessed at: `episodes.episode_id` and
    `core_memory.entry_id` overlap in a live base, so an id that matches no
    episode must come back as such rather than being tried elsewhere."""
    from mcp_server.tools.primitives.forget import forget

    real = await _seed_episode(cm, "exists")

    result = await forget(user_id=USER, scope="episode_ids", episode_ids=[real, 987654321], ctx=ctx)

    assert result["not_found"] == [987654321]
    assert [e["episode_id"] for e in result["episodes"]] == [real]
    assert result["deleted_l3"] == 1


async def test_episode_ids_archives_before_deleting(app: Any, ctx: Any, cm: Any) -> None:
    """The bin is what makes the cut reversible — so it is asserted, not assumed."""
    from mcp_server.tools.primitives.forget import forget

    ids = [await _seed_episode(cm, f"keep me {i}") for i in range(2)]

    await forget(user_id=USER, scope="episode_ids", episode_ids=ids, ctx=ctx)

    rows = await _archived_rows(cm)
    assert len(rows) == 2
    assert {r["memory_type"] for r in rows} == {"episode"}
    assert sorted(int(r["original_id"]) for r in rows) == sorted(ids)
    assert {r["archive_reason"] for r in rows} == {"forget_primitive_episode_ids"}


async def test_dry_run_without_the_shadow_bin_still_previews(app: Any, ctx: Any, cm: Any) -> None:
    """`shadow_bin=False` is a legitimate call, and a preview of it must not raise."""
    from mcp_server.tools.primitives.forget import forget

    ids = [await _seed_episode(cm, "no bin")]
    before = await _count_episodes(cm)

    result = await forget(user_id=USER, scope="episode_ids", episode_ids=ids, dry_run=True, shadow_bin=False, ctx=ctx)

    assert result["deleted_l3"] == 0
    assert [e["episode_id"] for e in result["episodes"]] == ids
    assert await _count_episodes(cm) == before


async def test_fuzzy_dry_run_is_quiet_on_a_key_that_matches_nothing(app: Any, ctx: Any, cm: Any) -> None:
    """A miss is a miss: zeros and empty lists, no exception, nothing archived."""
    from mcp_server.tools.primitives.forget import forget

    await _seed_episode(cm, "unrelated episode")

    result = await forget(user_id=USER, key="zqzqxw-no-such-key", scope="fuzzy", dry_run=True, ctx=ctx)

    assert result["deleted_l4"] == result["deleted_l3"] == result["deleted_graph"] == 0
    assert result["entries"] == result["episodes"] == result["graph_nodes"] == []
    assert await _count_archived(cm) == 0


async def test_an_empty_key_is_refused_rather_than_matching_everything(app: Any, ctx: Any, cm: Any) -> None:
    """Why the guard exists. `key` became optional for `episode_ids`, and `fuzzy`
    passes it into a LIKE pattern — `%%` matches every row and every graph node.
    Before the guard, an omitted key would have emptied the layer."""
    from mcp_server.tools.primitives.forget import forget

    await _seed_episode(cm, "should survive an accidental fuzzy")

    result = await forget(user_id=USER, scope="fuzzy", ctx=ctx)

    assert result["error"] == "key_required"
    assert await _count_episodes(cm) == 1, "the empty-key guard did not hold"
    assert await _count_archived(cm) == 0


async def test_an_empty_episode_ids_list_is_refused(app: Any, ctx: Any, cm: Any) -> None:
    from mcp_server.tools.primitives.forget import forget

    result = await forget(user_id=USER, scope="episode_ids", episode_ids=[], ctx=ctx)

    assert result["error"] == "episode_ids_required"


async def test_recent_refuses_a_dry_run_instead_of_answering_zero(app: Any, ctx: Any, cm: Any) -> None:
    """`recent` has no shadow bin, so it cannot be previewed. Returning zeros
    would read as "nothing would be deleted" — a refusal that says why is the
    only honest answer for the one scope with no way back."""
    from mcp_server.tools.primitives.forget import forget

    result = await forget(user_id=USER, scope="recent", minutes=60, dry_run=True, ctx=ctx)

    assert result["error"] == "dry_run_unsupported"
    assert "shadow bin" in result["detail"]
    assert "cannot be previewed" in result["detail"]


async def test_the_original_counters_are_still_there(app: Any, ctx: Any, cm: Any) -> None:
    """Back-compat: callers that read only the three integers keep working."""
    from mcp_server.tools.primitives.forget import forget

    result = await forget(user_id=USER, key="no-such-key", scope="exact", ctx=ctx)

    assert set(result) >= {"deleted_l4", "deleted_l3", "deleted_graph"}
    assert result["deleted_l4"] == 0
    assert result["dry_run"] is False
