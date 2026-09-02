"""E10: faceted tag queries — dim-OR within, cross-dim AND; json_each backed."""

import asyncio
import json
import sqlite3

import pytest

from features.query_dsl import query_memory
from shared.connection import connection_manager


@pytest.fixture()
def seeded_db(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    from shared.migrations import migration_manager

    asyncio.run(migration_manager.migrate())
    yield tmp_path
    connection_manager._conns.clear()


def _seed(tmp_path, rows):
    conn = sqlite3.connect(tmp_path / "memory.db")
    for summary, tags in rows:
        conn.execute(
            "INSERT INTO episodes (user_id, layer, summary, emotional_weight, tags, created_at)"
            " VALUES ('default', 'user', ?, 0.5, ?, ?)",
            (summary, json.dumps(tags), 1000.0),
        )
    conn.commit()
    conn.close()


async def test_facet_or_within_dimension(seeded_db):
    _seed(seeded_db, [("a", ["lang:python"]), ("b", ["lang:go"]), ("c", ["lang:rust"])])
    res = await query_memory("default", source="episodes", tags=["lang:python", "lang:go"])
    summaries = {r["summary"] for r in res["rows"]}
    assert summaries == {"a", "b"}  # OR within lang


async def test_facet_and_across_dimensions(seeded_db):
    _seed(
        seeded_db,
        [("py-web", ["lang:python", "area:web"]), ("py-cli", ["lang:python"]), ("go-web", ["lang:go", "area:web"])],
    )
    res = await query_memory("default", source="episodes", tags=["lang:python", "area:web"])
    assert {r["summary"] for r in res["rows"]} == {"py-web"}  # AND across dims


async def test_facet_mixed_or_and(seeded_db):
    _seed(
        seeded_db,
        [("a", ["lang:python", "area:web"]), ("b", ["lang:go", "area:web"]), ("c", ["lang:rust", "area:cli"])],
    )
    res = await query_memory("default", source="episodes", tags=["lang:python", "lang:go", "area:web"])
    assert {r["summary"] for r in res["rows"]} == {"a", "b"}


async def test_plain_value_tag_matches_too(seeded_db):
    _seed(seeded_db, [("plain", ["diff_gap", "auto_review"]), ("x", ["other"])])
    res = await query_memory("default", source="episodes", tags=["diff_gap"])
    assert {r["summary"] for r in res["rows"]} == {"plain"}


async def test_tags_rejected_for_core(seeded_db):
    with pytest.raises(ValueError, match="episodes-only"):
        await query_memory("default", source="core", tags=["lang:python"])


async def test_no_tags_param_backcompat(seeded_db):
    _seed(seeded_db, [("a", ["lang:python"]), ("b", ["lang:go"])])
    res = await query_memory("default", source="episodes", tag="lang:python")
    assert {r["summary"] for r in res["rows"]} == {"a"}
    assert res["filters"]["tag"] == "lang:python"
