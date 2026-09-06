"""S18 gap-registry tests: open questions + zero-result tails → nightly registry."""

from pathlib import Path

import pytest

from shared.connection import connection_manager


@pytest.fixture()
async def migrated_cm(tmp_path: Path):
    connection_manager.base_dir = tmp_path
    connection_manager._conns.clear()
    from shared.migrations import MigrationManager

    await MigrationManager(cm=connection_manager).migrate()
    yield tmp_path
    connection_manager._conns.clear()


async def test_registry_collects_question_episodes(migrated_cm) -> None:
    """L3-эпизод с тегом question попадает в registry; обычные — нет."""
    from core.episodic import EpisodicMemory
    from lifecycle.gap_registry import build_registry

    l3 = EpisodicMemory(cm=connection_manager)
    await l3.save("gapu1", "как масштабировать графовый минер?", 0.6, ["question", "test"])
    await l3.save("gapu1", "обычный эпизод без вопроса", 0.5, ["test"])

    result = await build_registry("user")
    assert result["written"] >= 1
    texts = " ".join(g["gap"] for g in result["gaps"])
    assert "масштабировать графовый минер" in texts
    assert "обычный эпизод" not in texts


async def test_registry_idempotent_on_rerun(migrated_cm) -> None:
    """Ночной перезапуск не плодит дубликаты тех же gap-текстов."""
    from core.episodic import EpisodicMemory
    from lifecycle.gap_registry import build_registry

    l3 = EpisodicMemory(cm=connection_manager)
    await l3.save("gapu2", "что делать с дубликатами в графе?", 0.6, ["question"])

    first = await build_registry("user")
    assert first["written"] == 1
    second = await build_registry("user")
    assert second["written"] == 0, f"повторный прогон ничего не пишет: {second}"


async def test_registry_collects_repeated_zero_results(migrated_cm) -> None:
    """Повторные (>=2) zero-result запросы из S17-журнала → gap origin='zero_result'."""
    import time

    from lifecycle.gap_registry import build_registry
    from lifecycle.graph_miners import ensure_zero_result

    await ensure_zero_result(connection_manager)
    conn = await connection_manager.get("memory.db")
    for _ in range(2):
        await conn.execute(
            "INSERT INTO recall_zero_results (ts, layer, user_id, query, query_hash) VALUES (?, 'user', 'zu', 'несуществующий термин xyz', 'h1')",
            (time.time(),),
        )
    await conn.commit()

    result = await build_registry("user")
    zeros = [g for g in result["gaps"] if g["origin"] == "zero_result"]
    assert zeros and any("xyz" in g["gap"] for g in zeros), f"zero-result хвост в registry: {result}"
