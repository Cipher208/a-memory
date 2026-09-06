"""S17 доп.11: HDBSCAN embedding clusters + louvain cross-check."""

import struct
import time

import pytest

from shared.connection import connection_manager
from shared.constants import DB_NAME
from shared.migrations import MigrationManager

T = 1700000000.0


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


async def _seed(text: str, vec: list[float]) -> None:
    from shared.embeddings import EmbeddingCache, _get_model

    cache = EmbeddingCache()
    await cache.ensure()
    tag = cache._cache_model_tag(_get_model())
    conn = await connection_manager.get(DB_NAME)
    await conn.execute(
        "INSERT OR REPLACE INTO embedding_cache (text_hash, embedding, model_name) VALUES (?, ?, ?)",
        (cache._hash_text(text), struct.pack(f"{len(vec)}f", *vec), tag),
    )
    await conn.commit()


async def _node(text: str) -> int:
    conn = await connection_manager.get(DB_NAME)
    cur = await conn.execute(
        "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES ('user','cu',?,'fact','[]',0.5,?)",
        (text, time.time()),
    )
    await conn.commit()
    return int(cur.lastrowid)


_V_A = [0.9 if i % 2 == 0 else -0.9 for i in range(384)]
_V_B = [-0.9 if i % 2 == 0 else 0.9 for i in range(384)]
_V_C = [0.9 if i % 4 < 2 else -0.9 for i in range(384)]


@pytest.mark.asyncio
async def test_clusters_report_and_louvain_agreement(db):
    """3 плотные группы × 4 узла → ≥2 кластера; группа с рёбрами даёт
    louvain_agreement=1.0; мало данных → skipped."""
    texts = []
    for prefix, vec in (("альфа", _V_A), ("бета", _V_B), ("гамма", _V_C)):
        for n in range(4):
            t = f"{prefix} тема номер {n} деталь {len(texts)}"
            texts.append((t, vec))
            await _seed(t, vec)
    ids = [await _node(t) for t, _ in texts]
    # первая группа (альфа) — клика → louvain одно сообщество → agreement 1.0
    conn = await connection_manager.get(DB_NAME)
    for i in range(4):
        for j in range(i + 1, 4):
            await conn.execute(
                "INSERT OR IGNORE INTO epi_edges (source_id, target_id, relation, weight, created_at, tags) VALUES (?, ?, 'mentions', 0.5, ?, '[]')",
                (ids[i], ids[j], time.time()),
            )
    await conn.commit()

    from lifecycle.embedding_clusters import cluster_embeddings

    report = await cluster_embeddings(db, layer="user", user_id="cu")
    assert report.get("skipped") is None, f"данных достаточно: {report}"
    assert report["node_count"] == 12
    assert len(report["clusters"]) >= 2, f"группы разделены: {report}"
    sizes = {c["size"] for c in report["clusters"]}
    assert 4 in sizes, f"плотная группа целиком в одном кластере: {report}"
    linked = [c for c in report["clusters"] if set(c["nodes"]) == set(ids[:4])]
    assert linked and linked[0]["louvain_agreement"] == 1.0, f"связанная группа согласована с louvain: {report}"


@pytest.mark.asyncio
async def test_clusters_skipped_when_few_vectors(db):
    rot = _V_A[10:] + _V_A[:10]  # поворот, не урезание — dim 384 сохранён
    for n in range(3):
        t = f"единичная тема {n}"
        await _seed(t, rot)
        await _node(t)
    from lifecycle.embedding_clusters import cluster_embeddings

    report = await cluster_embeddings(db, layer="user", user_id="cu")
    assert report["clusters"] == []
    assert "skipped" in report and report["node_count"] == 3
