"""Tests for typed consolidation — type-aware promotion."""

import pytest

from lifecycle.consolidation import ConsolidationEngine
from shared.connection import AsyncConnectionManager
from shared.memory_types import MemoryKind


@pytest.fixture
async def cm(tmp_path):
    m = AsyncConnectionManager(base_dir=str(tmp_path))
    await m.execute_script(
        "memory.db",
        """
        CREATE TABLE core_memory (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            layer TEXT NOT NULL DEFAULT 'user',
            user_id TEXT, "key" TEXT, value TEXT,
            importance REAL, memory_kind TEXT,
            expires_at REAL, source TEXT DEFAULT 'manual', metadata TEXT,
            created_at REAL, updated_at REAL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_core_layer_user_key ON core_memory(layer, user_id, "key");
    """,
    )
    return m


@pytest.mark.asyncio
async def test_consolidation_promotes_fact_with_kind(cm):
    es = ConsolidationEngine(cm=cm)
    items = [
        {"content": "мой день рождения 15 июня", "memory_kind": MemoryKind.FACT.value, "importance": 0.8},
    ]
    out = await es.consolidate_staging("u", items, min_importance=0.7)
    assert out == {"promoted": 1, "skipped": 0}


@pytest.mark.asyncio
async def test_consolidation_keeps_low_importance_instruction(cm):
    es = ConsolidationEngine(cm=cm)
    items = [
        {"content": "обязательно шифруй бэкапы", "memory_kind": MemoryKind.INSTRUCTION.value, "importance": 0.35},
    ]
    out = await es.consolidate_staging("u", items, min_importance=0.7)
    assert out == {"promoted": 1, "skipped": 0}


@pytest.mark.asyncio
async def test_consolidation_skips_low_importance_fact(cm):
    es = ConsolidationEngine(cm=cm)
    items = [
        {"content": "не помню что это", "memory_kind": MemoryKind.FACT.value, "importance": 0.5},
    ]
    out = await es.consolidate_staging("u", items, min_importance=0.7)
    assert out == {"promoted": 0, "skipped": 1}
