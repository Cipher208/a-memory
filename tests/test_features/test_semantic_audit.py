"""E13: semantic audit — cosine coverage of pre-compaction window, audit-logged."""

import asyncio
import sqlite3
import time

import pytest

from features import semantic_audit
from shared.connection import connection_manager


@pytest.fixture()
def hermetic_base(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    from shared.migrations import migration_manager

    asyncio.run(migration_manager.migrate())
    yield tmp_path
    connection_manager._conns.clear()


def _seed_episodes(tmp_path, summaries, created_at):
    conn = sqlite3.connect(tmp_path / "memory.db")
    for s in summaries:
        conn.execute(
            "INSERT INTO episodes (user_id, layer, summary, emotional_weight, tags, created_at) VALUES ('default', 'user', ?, 0.5, '[]', ?)",
            (s, created_at),
        )
    conn.commit()
    conn.close()


def _fake_embed(vectors):
    async def _embed_texts(texts, prefix=""):
        return [vectors[t] for t in texts]

    return _embed_texts


def _patch_emb(monkeypatch, vectors):
    """Patch shared.embeddings (the module semantic_audit actually calls)."""
    import shared.embeddings as emb

    monkeypatch.setattr(emb, "embed_texts", _fake_embed(vectors))
    return emb


async def test_audit_high_coverage(hermetic_base, monkeypatch):
    now = time.time()
    _seed_episodes(hermetic_base, ["postgres tuning notes", "wal checkpointing"], now - 60)
    # summary is near episode 1's vector
    _patch_emb(
        monkeypatch,
        {
            "postgres tuning notes": [1.0, 0.0],
            "wal checkpointing": [0.0, 1.0],
            "postgres tuning": [0.9, 0.1],
        },
    )
    res = await semantic_audit.run_semantic_audit("default", now, "postgres tuning")
    assert res["compared"] == 2
    assert 0.3 < res["score"] < 0.8  # ~0.99 (aligned ep) + ~0.1 (orthogonal ep) averaged


async def test_audit_empty_window(hermetic_base):
    now = time.time()
    res = await semantic_audit.run_semantic_audit("default", now, "no episodes before this")
    assert res == {"score": None, "compared": 0}


async def test_audit_logs_row(hermetic_base, monkeypatch):
    now = time.time()
    _seed_episodes(hermetic_base, ["topic a"], now - 30)
    _patch_emb(monkeypatch, {"topic a": [1.0]})
    res = await semantic_audit.run_semantic_audit("default", now, "topic a")
    assert res["score"] == pytest.approx(1.0, abs=1e-6)

    from features.audit_trail import AuditTrail

    rows = await AuditTrail().get_history("default", action="semantic_audit")
    assert rows and rows[0]["details"]["compared"] == 1
    assert rows[0]["details"]["window_hours"] == 24.0


async def test_hook_post_compression_invokes_audit(hermetic_base, monkeypatch):
    """_post_context_compression runs the audit fail-soft alongside log_compaction."""
    _seed_episodes(hermetic_base, ["ctx topic"], time.time() - 30)
    _patch_emb(monkeypatch, {"ctx topic": [1.0]})
    from hooks.user_hooks import UserHooks

    hooks = UserHooks()

    class _Rag:
        async def search(self, query, user_id=None, limit=5):
            return []

    ctx = {"user_id": "default", "query": "ctx topic", "_rag": _Rag()}
    res = await hooks._post_context_compression(ctx, mem=None)
    assert res["logged"] is True
    assert hooks._semantic_audit["score"] == pytest.approx(1.0, abs=1e-6)
