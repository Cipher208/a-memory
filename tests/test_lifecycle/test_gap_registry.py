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


# ── the missing third stage: reading the registry back ────────────────────────
#
# S18 named the chain «L3-questions → ночной registry → proactive acquisition» and
# planned only the first two links. The table therefore had a writer and no reader.
# The read side is `open_gaps`; the surface is the session brief, where the agent
# already reads what is pending.


async def test_ensure_table_upgrades_a_pre_existing_table(migrated_cm) -> None:
    """The live table exists without the observation columns — they must be ADDED.

    `CREATE TABLE IF NOT EXISTS` is a no-op on an existing table, so on every base that
    already has `memory_gaps` the new columns would silently never appear and `open_gaps`
    would raise on its first call in production while every fresh-DB test stayed green.
    """
    import sqlite3

    from lifecycle.gap_registry import _ensure_table

    old = migrated_cm / "memory.db"
    with sqlite3.connect(str(old)) as conn:
        conn.execute(
            "CREATE TABLE memory_gaps ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,"
            " layer TEXT NOT NULL DEFAULT 'user', user_id TEXT NOT NULL, gap TEXT NOT NULL,"
            " origin TEXT NOT NULL DEFAULT 'question', gap_hash TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO memory_gaps (ts, layer, user_id, gap, origin, gap_hash) VALUES (1.0, 'user', 'old', 'старая строка', 'question', 'h_old')"
        )
        conn.commit()

    await _ensure_table(connection_manager)

    with sqlite3.connect(str(old)) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_gaps)")}
        assert {"last_seen", "seen_count"} <= cols, f"колонки наблюдения не добавлены: {sorted(cols)}"
        row = conn.execute("SELECT gap, seen_count FROM memory_gaps WHERE gap_hash='h_old'").fetchone()
    assert row is not None, "существующая строка обязана пережить апгрейд"
    assert row[0] == "старая строка"
    assert int(row[1]) == 1, "существующие строки получают видимость 1, а не 0"


async def test_repeat_observation_bumps_instead_of_write(migrated_cm) -> None:
    """A hole seen again is the SAME hole: count it, do not write a second row.

    Repetition is the strongest signal this table can carry — the plan's own word for
    what to collect is «свежие» (fresh), and a hole that keeps being observed is the one
    worth pursuing. `written` stays a count of NEW rows, so a rerun still reports 0.
    """
    from core.episodic import EpisodicMemory
    from lifecycle.gap_registry import build_registry, open_gaps

    l3 = EpisodicMemory(cm=connection_manager)
    await l3.save("gapu3", "почему падает ночной проход?", 0.6, ["question"])

    await build_registry("user")
    second = await build_registry("user")
    assert second["written"] == 0, f"повтор не пишет новую строку: {second}"

    conn = await connection_manager.get("memory.db")
    rows = await (await conn.execute("SELECT gap, seen_count FROM memory_gaps")).fetchall()
    assert len(rows) == 1, f"одна строка на один текст: {rows}"
    assert int(rows[0][1]) == 2, f"видимость выросла: {rows}"

    gaps = await open_gaps("user")
    assert len(gaps) == 1 and gaps[0]["seen_count"] == 2


async def test_open_gaps_ranks_repeated_holes_first(migrated_cm) -> None:
    """A hole observed three times outranks one observed once — that is the point."""
    import time

    from lifecycle.gap_registry import _ensure_table, open_gaps

    await _ensure_table(connection_manager)  # lazy-ensure: the table only exists after a build
    conn = await connection_manager.get("memory.db")
    now = time.time()
    await conn.execute(
        "INSERT INTO memory_gaps (ts, layer, user_id, gap, origin, gap_hash, last_seen, seen_count)"
        " VALUES (?, 'user', 'u1', 'редкий вопрос', 'question', 'h1', ?, 1)",
        (now, now),
    )
    await conn.execute(
        "INSERT INTO memory_gaps (ts, layer, user_id, gap, origin, gap_hash, last_seen, seen_count)"
        " VALUES (?, 'user', 'u1', 'настойчивая дыра', 'question', 'h2', ?, 3)",
        (now, now),
    )
    await conn.commit()

    gaps = await open_gaps("user")
    assert [g["gap"] for g in gaps[:2]] == ["настойчивая дыра", "редкий вопрос"], gaps


async def test_open_gaps_drops_rows_outside_the_window(migrated_cm) -> None:
    """The registry is a rolling window over fresh holes, not a backlog.

    The plan's word is «свежие»; nothing here claims an old row was answered, so the
    honest thing is to stop showing it rather than to invent a closure it cannot know.
    """
    import time

    from lifecycle.gap_registry import _ensure_table, open_gaps

    await _ensure_table(connection_manager)  # lazy-ensure: the table only exists after a build
    conn = await connection_manager.get("memory.db")
    now = time.time()
    await conn.execute(
        "INSERT INTO memory_gaps (ts, layer, user_id, gap, origin, gap_hash, last_seen, seen_count)"
        " VALUES (?, 'user', 'u1', 'старая дыра', 'question', 'h1', ?, 1)",
        (now - 30 * 86400, now - 30 * 86400),
    )
    await conn.execute(
        "INSERT INTO memory_gaps (ts, layer, user_id, gap, origin, gap_hash, last_seen, seen_count)"
        " VALUES (?, 'user', 'u1', 'свежая дыра', 'question', 'h2', ?, 1)",
        (now, now),
    )
    await conn.commit()

    gaps = await open_gaps("user", max_age_days=7)
    texts = [g["gap"] for g in gaps]
    assert "свежая дыра" in texts
    assert "старая дыра" not in texts, f"окно не соблюдено: {texts}"


async def test_reader_upgrades_a_pre_existing_table_by_itself(migrated_cm) -> None:
    """THE ONE THAT WOULD HAVE NEEDED A MANUAL STEP: a reader must fix the schema itself.

    On the live base `memory_gaps` already exists WITHOUT the observation columns, and
    `CREATE TABLE IF NOT EXISTS` cannot add them. A reader that only selects would return
    an empty list forever (its OperationalError guard swallows `no such column`) and the
    brief would stay silent until the nightly build happened to run — a deploy-order trap
    with a manual upgrade step as the workaround. Every entry point in this family of lazy
    tables calls `_ensure_table` (`features/scratchpad.py::read_entries` does), so these do
    too, and no manual step exists.
    """
    import sqlite3
    import time

    from lifecycle.gap_registry import open_gaps

    old = migrated_cm / "memory.db"
    now = time.time()
    with sqlite3.connect(str(old)) as conn:
        conn.execute(
            "CREATE TABLE memory_gaps ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,"
            " layer TEXT NOT NULL DEFAULT 'user', user_id TEXT NOT NULL, gap TEXT NOT NULL,"
            " origin TEXT NOT NULL DEFAULT 'question', gap_hash TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO memory_gaps (ts, layer, user_id, gap, origin, gap_hash)"
            " VALUES (?, 'user', 'u1', 'старая дыра без колонок', 'question', 'h_old')",
            (now,),
        )
        conn.commit()

    gaps = await open_gaps("user")

    assert [g["gap"] for g in gaps] == ["старая дыра без колонок"], f"читатель не починил схему: {gaps}"
    assert gaps[0]["seen_count"] == 1, "существующая строка получает видимость 1"
    with sqlite3.connect(f"file:{old}?mode=ro", uri=True) as conn:
        left = conn.execute("SELECT COUNT(*) FROM memory_gaps WHERE last_seen = 0").fetchone()[0]
    assert left == 0, "last_seen забэкфиллен из ts, иначе строка скрыта с первого дня"


async def test_reader_does_not_write_on_the_steady_state(migrated_cm) -> None:
    """After the upgrade a reader is a no-op — no UPDATE scan on a session-start path."""
    import sqlite3
    import time

    from lifecycle.gap_registry import open_gaps

    old = migrated_cm / "memory.db"
    now = time.time()
    with sqlite3.connect(str(old)) as conn:
        conn.execute(
            "CREATE TABLE memory_gaps ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,"
            " layer TEXT NOT NULL DEFAULT 'user', user_id TEXT NOT NULL, gap TEXT NOT NULL,"
            " origin TEXT NOT NULL DEFAULT 'question', gap_hash TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO memory_gaps (ts, layer, user_id, gap, origin, gap_hash) VALUES (?, 'user', 'u1', 'дыра', 'question', 'h1')",
            (now,),
        )
        conn.commit()

    await open_gaps("user")  # first call upgrades
    with sqlite3.connect(f"file:{old}?mode=ro", uri=True) as conn:
        stamp = conn.execute("SELECT last_seen FROM memory_gaps WHERE gap_hash='h1'").fetchone()[0]
    await open_gaps("user")  # steady state
    with sqlite3.connect(f"file:{old}?mode=ro", uri=True) as conn:
        again = conn.execute("SELECT last_seen FROM memory_gaps WHERE gap_hash='h1'").fetchone()[0]
    assert stamp == again, "повторный вызов читателя не трогает строки"


async def test_open_gaps_returns_empty_on_a_base_without_the_table(tmp_path) -> None:
    """Reading must degrade to empty, never raise: the caller is a session-start path."""
    original = connection_manager.base_dir
    connection_manager.base_dir = tmp_path
    connection_manager._conns.clear()
    try:
        from lifecycle.gap_registry import open_gaps

        assert await open_gaps("user") == []
    finally:
        connection_manager._conns.clear()
        connection_manager.base_dir = original
