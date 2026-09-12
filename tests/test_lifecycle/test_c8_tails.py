"""C8: novelty-gate + topic-классификация, pinned/private visibility, Lychee boundary detection."""

from __future__ import annotations

import pytest

from lifecycle.segment_consolidation import detect_boundaries


# ── novelty-gate + topic ──


@pytest.mark.asyncio
async def test_novelty_gate_skips_paraphrase(tmp_path):
    from shared.connection import AsyncConnectionManager
    from shared.migrations import MigrationManager
    from lifecycle.distiller import distill_and_route

    class _L3:
        saved = []

        async def save(self, uid, text, score, tags):
            self.saved.append(text)

    class _Mem:
        def __init__(self, cm):
            self._cm = cm
            self.l3 = _L3()

    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    await MigrationManager(cm=cm).migrate()  # memory_conflicts для ConflictResolver
    mem = _Mem(cm)
    text = "Мы решили перейти на PostgreSQL 16 для продакшена"
    r1 = await distill_and_route(mem, None, "u1", text, 0.8)
    assert r1["l4_saved"] >= 1
    # Точный повтор (тот же ключ, Jaccard=1.0 > 0.85) — отсекается novelty-gate
    # ДО конфликта и ДО пере-save (иначе был бы апдейт/condition-split впустую).
    r2 = await distill_and_route(mem, None, "u1", text, 0.8)
    assert r2["l4_saved"] == 0, f"дубликат должен быть отсечён novelty-gate: {r2}"
    assert r2["novelty_skipped"] >= 1


@pytest.mark.asyncio
async def test_topic_tag_on_l3_episode(tmp_path):
    from shared.connection import AsyncConnectionManager
    from shared.migrations import MigrationManager
    from lifecycle.distiller import distill_and_route

    class _L3:
        saved = []

        async def save(self, uid, text, score, tags):
            self.saved.append(tags)

    class _Mem:
        def __init__(self, cm):
            self._cm = cm
            self.l3 = _L3()

    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    await MigrationManager(cm=cm).migrate()
    mem = _Mem(cm)
    # Длинное событие (>=60 симв) → l3; про деплой → topic:deploy
    await distill_and_route(mem, None, "u1", "Вчера ночью прошёл деплой новой версии бэкенда на сервер, всё прошло без инцидентов", 0.5)
    assert mem.l3.saved, "событие ушло в L3"
    assert any("topic:deploy" in t for t in mem.l3.saved[0]), f"topic-тег поставлен: {mem.l3.saved[0]}"


# ── pinned/private visibility ──


@pytest.mark.asyncio
async def test_visibility_pinned_and_private(tmp_path):
    from core.memory import CoreMemory
    from shared.connection import AsyncConnectionManager

    cm = CoreMemory(cm=AsyncConnectionManager(base_dir=str(tmp_path)), layer="user")
    await cm._init_db()
    saved_id = await cm.save("u1", "fact:owner", "сервер принадлежит Мурату", importance=0.4, visibility="pinned")
    assert saved_id > 0
    await cm.save("u1", "fact:secret_token", "токен abc123", importance=0.9, visibility="private")

    pinned = await cm.get_pinned("u1")
    assert [e.key for e in pinned] == ["fact:owner"], "pinned доступен через get_pinned"

    hits = await cm.search("u1", "токен")
    assert hits == [], "private не выходит через recall/search"

    hits2 = await cm.search("u1", "Мурату")
    assert hits2, "обычный/pinned ищется"


@pytest.mark.asyncio
async def test_visibility_invalid_raises(tmp_path):
    from core.memory import CoreMemory
    from shared.connection import AsyncConnectionManager

    cm = CoreMemory(cm=AsyncConnectionManager(base_dir=str(tmp_path)), layer="user")
    await cm._init_db()
    with pytest.raises(ValueError, match="visibility"):
        await cm.save("u1", "k", "v", visibility="secret")


@pytest.mark.asyncio
async def test_resave_preserves_operator_visibility(tmp_path):
    """A re-upsert on the same key must NOT resurrect an operator-hidden row.

    F1 sanitation (2026-09-12): hidden garbage fact: rows came back 'visible'
    because every auto_save rewrite of the same canonical key re-ran the
    default `visibility='visible'`.
    """
    from core.memory import CoreMemory
    from shared.connection import AsyncConnectionManager

    manager = AsyncConnectionManager(base_dir=str(tmp_path))
    cm = CoreMemory(cm=manager, layer="user")
    await cm._init_db()
    eid = await cm.save("u1", "fact:owner", "владелец сервера — Мурат", importance=0.5)
    conn = await manager.get("memory.db")
    await conn.execute("UPDATE core_memory SET visibility='hidden' WHERE entry_id=?", (eid,))
    await conn.commit()

    await cm.save("u1", "fact:owner", "владелец сервера — Мурат", importance=0.6)
    row = await (await conn.execute("SELECT visibility FROM core_memory WHERE entry_id=?", (eid,))).fetchone()
    assert row["visibility"] == "hidden", "re-save without the arg preserves the operator flag"

    await cm.save("u1", "fact:owner", "владелец сервера — Мурат", importance=0.6, visibility="visible")
    row = await (await conn.execute("SELECT visibility FROM core_memory WHERE entry_id=?", (eid,))).fetchone()
    assert row["visibility"] == "visible", "an explicit visibility arg still overrides"


@pytest.mark.asyncio
async def test_inject_pinned_block(tmp_path):
    """pinned-факт попадает в inject даже при низкой важности."""
    from core.memory import CoreMemory
    from features.inject import build_inject_blocks
    from shared.connection import AsyncConnectionManager

    cm = CoreMemory(cm=AsyncConnectionManager(base_dir=str(tmp_path)), layer="user")
    await cm._init_db()
    await cm.save("u1", "pref:язык", "общаемся на русском", importance=0.3, visibility="pinned")

    class _L4Stub:
        get_all = cm.get_all
        get_pinned = cm.get_pinned

    class _Mem:
        l4 = _L4Stub()

        class l1:  # noqa: N801 — mirror атрибутов MemorySystem (l1/l3 lowercase)
            @staticmethod
            def get_recent(n):
                return []

        class l3:  # noqa: N801
            @staticmethod
            async def search_by_tag(uid, tag, limit):
                return []

    blocks = await build_inject_blocks(_Mem(), rag=None, user_id="u1", text="")
    pinned_blocks = [b for b in blocks if b["kind"] == "pinned"]
    assert pinned_blocks, f"pinned-блок в inject: {blocks}"
    assert "pref:язык" in pinned_blocks[0]["content"]


# ── Lychee boundary detection ──


def test_detect_boundaries_splits_on_topic_shift():
    records = [
        (1, "деплой прошёл успешно на сервер"),
        (2, "деплой завершился, сервер работает"),
        (3, "кэш redis чистится по расписанию"),
        (4, "кэш redis прогревается утром"),
        (5, "деплой откатили из-за ошибки"),
    ]
    segs = detect_boundaries(records, token_cap=900)
    assert segs, "сегменты получены"
    assert sorted(rid for seg in segs for rid in seg) == [1, 2, 3, 4, 5], "все записи покрыты без потерь"
    assert len(segs) >= 2, f"тематические сдвиги дают границы: {segs}"


def test_detect_boundaries_token_cap():
    long_text = "слово " * 200  # ~400 токенов каждый
    records = [(i, long_text) for i in range(1, 6)]
    segs = detect_boundaries(records, token_cap=600)
    assert all(len(seg) <= 2 for seg in segs), f"token cap режет сегменты: {[len(s) for s in segs]}"


def test_detect_boundaries_empty():
    assert detect_boundaries([]) == []
