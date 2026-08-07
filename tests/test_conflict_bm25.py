"""Tests for B3: BM25 + char-trigram conflict similarity."""

import pytest

from rag.conflict import (
    ConflictResolver,
    bm25_pair_similarity,
    char_ngram_jaccard,
    smart_similarity,
)


def test_jaccard_basic():
    assert char_ngram_jaccard("hello", "hello") == 1.0
    assert char_ngram_jaccard("abc", "xyz") == 0.0


def test_bm25_pair_basic():
    s = bm25_pair_similarity("redis cluster pipelining", "redis cluster issue")
    assert 0.0 < s < 1.0
    s_unrelated = bm25_pair_similarity("redis cluster", "cats and dogs playing")
    assert s > s_unrelated


def test_smart_similarity_short_uses_ngrams():
    a = "Python is best"
    b = "Python best"
    s = smart_similarity(a, b)
    char_ngram_jaccard(a, b, 3)
    assert s > 0.5


def test_smart_similarity_long_uses_both_signals():
    a = "Python multi-paragraph text about multi-threading and memory model"
    b = "Python multi-paragraph text about concurrency and memory model"
    s = smart_similarity(a, b)
    assert s > 0.4


def test_smart_similarity_rejects_unrelated():
    s = smart_similarity("Python is the best language", "Cats love fish in rivers")
    assert s < 0.15


def test_smart_similarity_empty_inputs():
    assert smart_similarity("", "abc") == 0.0
    assert smart_similarity("abc", "") == 0.0
    assert smart_similarity("", "") == 0.0


@pytest.mark.asyncio
async def test_resolve_archives_and_audits(tmp_path):
    from shared.connection import AsyncConnectionManager

    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    cr = ConflictResolver(cm=cm)
    await cr._init_db()

    # Create conflict group
    conn = await cm.get("memory.db")
    # Init audit table for logging
    from features.audit_trail import AuditTrail

    at = AuditTrail(cm=cm)
    await at._init_db()

    await conn.execute(
        "INSERT INTO memory_conflicts (user_id, content, is_conflict, conflict_group_id) VALUES (?, ?, 1, ?)", ("u", "old version", "grp1")
    )
    await conn.execute(
        "INSERT INTO memory_conflicts (user_id, content, is_conflict, conflict_group_id) VALUES (?, ?, 1, ?)", ("u", "new version", "grp1")
    )
    await conn.commit()

    # Resolve: keep_id=2
    result = await cr.resolve("grp1", keep_id=2)
    assert result is True

    # Verify only kept entry remains (conflict_group_id set to NULL after resolve)
    cur = await conn.execute("SELECT id, content FROM memory_conflicts WHERE user_id='u'")
    rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "new version"

    # Verify audit log
    cur = await conn.execute("SELECT action, details FROM audit_log WHERE action='conflict_resolved'")
    audit_row = await cur.fetchone()
    assert audit_row is not None
    assert "conflict_group_id" in str(audit_row[1])
