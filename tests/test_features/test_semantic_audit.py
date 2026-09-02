"""E13: semantic audit — pre-window episode coverage by the L4 set, audit-logged.

Payload-independent by design: live harnesses dispatch post_context_compression
WITHOUT a summary (Hermes/cow/MiMoCode verified), so the audit compares the
pre-compaction window against the L4 facts rehydrate actually re-injects.
"""

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


def _seed(tmp_path, episodes, facts):
    conn = sqlite3.connect(tmp_path / "memory.db")
    for s in episodes:
        conn.execute(
            "INSERT INTO episodes (user_id, layer, summary, emotional_weight, tags, created_at) VALUES ('default', 'user', ?, 0.5, '[]', ?)",
            (s, time.time() - 30),
        )
    for v in facts:
        conn.execute(
            "INSERT INTO core_memory (layer, user_id, key, value, importance, created_at, updated_at) VALUES ('user', 'default', ?, ?, 0.9, ?, ?)",
            (f"k_{v[:12]}", v, time.time(), time.time()),
        )
    conn.commit()
    conn.close()


def _patch_emb(monkeypatch, vectors):
    import shared.embeddings as emb

    async def _embed_texts(texts, prefix=""):
        return [vectors[t] for t in texts]

    monkeypatch.setattr(emb, "embed_texts", _embed_texts)
    return emb


async def test_audit_scores_coverage(hermetic_base, monkeypatch):
    _seed(hermetic_base, ["postgres tuning notes", "unrelated chatter"], ["postgres tuning guide"])
    _patch_emb(
        monkeypatch,
        {
            "postgres tuning notes": [1.0, 0.0],
            "unrelated chatter": [0.0, 1.0],
            "postgres tuning guide": [1.0, 0.0],
        },
    )
    res = await semantic_audit.run_semantic_audit("default", time.time())
    assert res["compared"] == 2
    assert res["facts"] == 1
    # aligned episode ~1.0, orthogonal ~0.0 → mean ~0.5
    assert 0.3 < res["score"] < 0.7


async def test_audit_empty_window_or_facts(hermetic_base):
    now = time.time()
    assert semantic_audit.run_semantic_audit.__doc__  # payload-independent contract
    res = await semantic_audit.run_semantic_audit("default", now)
    assert res == {"score": None, "compared": 0, "facts": 0}


async def test_audit_logs_row(hermetic_base, monkeypatch):
    _seed(hermetic_base, ["topic a"], ["topic a fact"])
    _patch_emb(monkeypatch, {"topic a": [1.0], "topic a fact": [1.0]})
    res = await semantic_audit.run_semantic_audit("default", time.time())
    assert res["score"] == pytest.approx(1.0, abs=1e-6)

    from features.audit_trail import AuditTrail

    rows = await AuditTrail().get_history("default", action="semantic_audit")
    assert rows and rows[0]["details"]["compared"] == 1


async def test_hook_payload_independent(hermetic_base, monkeypatch):
    """Live-shape payload (no query, like real harnesses) still produces the audit."""
    _seed(hermetic_base, ["ctx topic"], ["ctx fact"])
    _patch_emb(monkeypatch, {"ctx topic": [1.0], "ctx fact": [1.0]})
    from hooks.user_hooks import UserHooks

    hooks = UserHooks()
    hook_registry = __import__("hooks.registry", fromlist=["hook_registry"]).hook_registry
    hook_registry.register_instance(hooks)
    try:
        ctx = {"user_id": "default", "reason": "compaction"}  # ← the REAL live payload shape
        res = await hooks._post_context_compression(ctx, mem=None)
        assert res["logged"] is True
        assert res["semantic_audit"]["score"] == pytest.approx(1.0, abs=1e-6)
    finally:
        hook_registry._hooks.pop("post_context_compression", None)
