"""Tests for shared/ module — async."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def test_dream_buffer(tmp_path):
    from shared.connection import AsyncConnectionManager
    from shared.dream_buffer import DreamBuffer

    async def t():
        cm = AsyncConnectionManager(base_dir=str(tmp_path))
        db = DreamBuffer(cm=cm)
        await db.add("test_sh", "s1", "msg", 0.5)
        assert await db.count("test_sh") >= 1
        await db.cleanup_old(max_age_hours=0, max_count=0)
        await cm.close_all()

    asyncio.run(t())


def test_archived_memories(tmp_path):
    """Свой manager на tmp_path, а не глобальный.

    Тест строил `ArchivedMemories()` без `cm`, то есть на глобальном
    `connection_manager`, и тем самым писал прямо в рабочий каталог памяти:
    в `~/.mcp-ariel-memory/memory.db` осталось 32 строки `user_id='test_sh'`
    от 24.08.2026 — до того, как conftest получил фикстуру `hermetic_global_db`
    (коммит 90c9149). После неё тест стал ещё и непроходимым в одиночку:
    таблицу `archived_memories` создавала только миграция, а без соседа,
    который её прогонял, вызов падал на `no such table`.

    Хозяин базы теперь свой, и он же проверяет настоящее свойство класса:
    таблица появляется сама (см. `ArchivedMemories.ensure`).
    """
    from shared.archived_memories import ArchivedMemories
    from shared.connection import AsyncConnectionManager

    async def t():
        cm = AsyncConnectionManager(base_dir=str(tmp_path))
        am = ArchivedMemories(cm=cm)
        await am.archive("test_sh", "Old memory", importance=0.2, reason="test")
        archived = await am.get_archived("test_sh")
        assert len(archived) >= 1
        await cm.close_all()

    asyncio.run(t())


def test_archived_memories_works_on_a_migrated_database(tmp_path):
    """На мигрированной базе класс работает и таблицу не ломает."""
    from shared.archived_memories import ArchivedMemories
    from shared.connection import AsyncConnectionManager
    from shared.migrations import MigrationManager

    async def t():
        cm = AsyncConnectionManager(base_dir=str(tmp_path))
        await MigrationManager(cm=cm).migrate()
        am = ArchivedMemories(cm=cm)
        assert await am.count("test_sh") == 0
        await am.archive("test_sh", "Migrated db row", reason="test")
        assert await am.count("test_sh") == 1
        await cm.close_all()

    asyncio.run(t())


def test_embedding_cache(tmp_path):
    """Тот же принцип: свой manager, чтобы файл не трогал общий каталог вовсе."""
    from shared.connection import AsyncConnectionManager
    from shared.embeddings import EmbeddingCache

    async def t():
        cm = AsyncConnectionManager(base_dir=str(tmp_path))
        ec = EmbeddingCache(cm=cm)
        emb = await ec.embed_single("test")
        assert len(emb) == 384
        await cm.close_all()

    asyncio.run(t())


def test_metrics():
    from shared.metrics import metrics

    metrics.inc("test_counter")
    prom = metrics.render_prometheus()
    assert "test_counter" in prom


def test_middleware():
    from shared.middleware import MiddlewareContext, MiddlewarePipeline, ValidationMiddleware

    async def t():
        p = MiddlewarePipeline()
        p.add(ValidationMiddleware())
        ctx = MiddlewareContext(tool_name="test", user_id="u", args={"key": "k", "value": "v"})
        return await p.execute(ctx, lambda c: {"ok": True})

    r = asyncio.run(t())
    assert r["ok"] is True
