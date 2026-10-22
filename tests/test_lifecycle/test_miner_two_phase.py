"""Two-phase miners: compute вне транзакции, apply — в одной короткой.

Зачем (22.10, живое измерение): `@_maintenance` кладёт ВСЁ тело — чтения, CPU,
сеть — внутрь одной транзакции, и write-lock держится всё тело: на живом доме
`wire_new_node` держал его 2.217 s (slow-write, phase
`lifecycle.graph_miners:wrapper:55`). Read-then-write требует ОДНОЙ
транзакции, поэтому граница не исчезает, а сдвигается: тело читает и считает
(без транзакции, без лока) и возвращает apply-вызов; обёртка гоняет его в
одной короткой транзакции диспетчера.

Контракт, который пинают тесты:
* чтения compute идут через транзакцию-свободный API диспетчера (`dispatch.query`)
  и ДО открытия транзакции;
* apply — ровно одна транзакция диспетчера;
* ни один `commit` не происходит вне этой транзакции;
* borrowed-путь (у вызывающего уже есть транзакция на shared) остаётся
  инлайном: своей транзакции нет, записи видны после коммита внешней.
"""

from collections.abc import AsyncIterator
from typing import Any

import aiosqlite
import pytest

from shared import write_dispatch as wd
from shared.connection import connection_manager
from shared.constants import DB_NAME
from shared.migrations import MigrationManager
from shared.write_dispatch import get_write_dispatch

T = 1_700_000_000.0

#: шесть CPU/сетевых минеров, переведённых на две фазы
SPLIT_MINERS = ["tokens", "sessions", "entities", "markers", "embedding", "wire"]


@pytest.fixture
async def db(tmp_path) -> AsyncIterator[Any]:
    connection_manager.base_dir = tmp_path  # НЕ подменять объект!
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


async def _seed() -> dict[str, int]:
    """Один слой на всех шести: узлы с общими токенами/синонимами/маркером, теги, L0."""
    conn = await connection_manager.get(DB_NAME)

    async def _node(content: str, ts: float) -> int:
        cur = await conn.execute(
            "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES ('user', 'gu', ?, 'fact', '[]', 0.5, ?)",
            (content, ts),
        )
        return int(cur.lastrowid or 0)

    ids = {
        "n1": await _node("Лили принесла отчёт по проекту python sqlite database", T),
        "n2": await _node("Lily обновила документацию python sqlite indexing", T + 60),
        "n3": await _node("сборка postgres падает с ошибкой", T + 7200),
        "n4": await _node("postgres починила конфигурацию, теперь работает", T + 10800),
        # дословно пара из test_miner_embedding_jaccard_threshold: hash-Jaccard >= 0.7 доказан
        "n5": await _node("python sqlite database", T + 14400),
        "n6": await _node("python sqlite indexing", T + 14460),
    }
    for nid in (ids["n1"], ids["n2"]):
        await conn.execute("INSERT OR IGNORE INTO epi_tags (node_id, tag) VALUES (?, 'db')", (nid,))
    for ts, text in ((T, "python sqlite database разбираем"), (T + 60, "python sqlite indexing настраиваем")):
        await conn.execute(
            "INSERT INTO l0_journal (ts, event, source_msg_id, layer, user_id, text, raw_type)"
            " VALUES (?, 'new_message', ?, 'user', 'gu', ?, 'user-message')",
            (ts, int(ts), text),
        )
    await conn.commit()
    return ids


async def _run_miner(miner: str, db: Any, ids: dict[str, int]) -> None:
    """Запуск через публичное имя — ровно так, как их гоняет graph_enrich/дистиллятор."""
    if miner == "wire":
        from lifecycle.graph_miners import wire_new_node

        await wire_new_node(db, "user", ids["n2"], "python sqlite indexing", ["db"])
        return
    from lifecycle import graph_miners

    await getattr(graph_miners, f"miner_{miner}")(db, "user")


@pytest.mark.asyncio
@pytest.mark.parametrize("miner", SPLIT_MINERS)
async def test_compute_reads_before_any_transaction(db, monkeypatch, miner):
    """Чтения compute идут через query диспетчера (без транзакции) и ДО atomic."""
    ids = await _seed()
    dispatch = get_write_dispatch(db)
    events: list[str] = []
    real_query, real_atomic = dispatch.query, dispatch.atomic

    async def query(db_name: str, sql: str, params: Any = ()) -> list[Any]:
        events.append("query")
        return await real_query(db_name, sql, params)

    async def atomic(db_name: str, fn: Any) -> Any:
        events.append("atomic")
        return await real_atomic(db_name, fn)

    monkeypatch.setattr(dispatch, "query", query)
    monkeypatch.setattr(dispatch, "atomic", atomic)

    await _run_miner(miner, db, ids)

    assert "query" in events, f"{miner}: compute не читал через транзакцию-свободный API — чтения сидят в транзакции"
    first_atomic = events.index("atomic")
    assert "query" not in events[first_atomic:], f"{miner}: чтение после открытия транзакции apply"


@pytest.mark.asyncio
@pytest.mark.parametrize("miner", SPLIT_MINERS)
async def test_apply_is_exactly_one_dispatch_transaction(db, monkeypatch, miner):
    """Apply — ровно одна транзакция диспетчера."""
    ids = await _seed()
    dispatch = get_write_dispatch(db)
    calls: list[Any] = []
    real_atomic = dispatch.atomic

    async def atomic(db_name: str, fn: Any) -> Any:
        calls.append(fn)
        return await real_atomic(db_name, fn)

    monkeypatch.setattr(dispatch, "atomic", atomic)

    await _run_miner(miner, db, ids)

    assert len(calls) == 1, f"{miner}: apply должен быть ровно одной транзакцией, вызовов: {len(calls)}"


@pytest.mark.asyncio
@pytest.mark.parametrize("miner", SPLIT_MINERS)
async def test_no_commit_outside_the_transaction(db, monkeypatch, miner):
    """Ни один commit не выходит за транзакцию диспетчера (shared и его соединение)."""
    ids = await _seed()
    shared = await db.get(DB_NAME)
    dispatch = get_write_dispatch(db)
    dconn = await dispatch._cm.get(DB_NAME)
    escapes: list[str] = []
    for label, conn in (("shared", shared), ("dispatch", dconn)):
        real_commit = conn.commit

        async def commit(_real=real_commit, _label=label) -> None:
            if wd._active.get() is None:
                escapes.append(_label)
            await _real()

        monkeypatch.setattr(conn, "commit", commit)

    await _run_miner(miner, db, ids)

    assert escapes == [], f"{miner}: commit вне транзакции диспетчера: {escapes}"


@pytest.mark.asyncio
@pytest.mark.parametrize("miner", SPLIT_MINERS)
async def test_borrowed_caller_keeps_one_transaction(db, monkeypatch, miner):
    """Открытая транзакция на shared: минер инлайном — своей транзакции нет, записи не видны до коммита."""
    ids = await _seed()
    shared = await db.get(DB_NAME)
    dispatch = get_write_dispatch(db)
    atomic_calls = 0
    real_atomic = dispatch.atomic

    async def atomic(db_name: str, fn: Any) -> Any:
        nonlocal atomic_calls
        atomic_calls += 1
        return await real_atomic(db_name, fn)

    monkeypatch.setattr(dispatch, "atomic", atomic)
    db_file = connection_manager.base_dir / "memory.db"

    async def _heuristic_edges() -> int:
        async with aiosqlite.connect(db_file) as other:
            row = await (await other.execute("SELECT COUNT(*) FROM epi_edges WHERE tags LIKE '%heuristic:%'")).fetchone()
            return int(row[0])

    # Прогрев: вторичные кэши (embedding-векторы, NER) живут своими записями через
    # диспетчера — без прогрева их запись входит в чужую транзакцию как блокирующая
    # попытка (BUSY), а тест проверяет контракт borrowed-пути, не кэши.
    await _run_miner(miner, db, ids)
    warmed = await _heuristic_edges()
    atomic_calls = 0  # считаем только borrowed-прогон
    await shared.execute("BEGIN IMMEDIATE")
    try:
        await _run_miner(miner, db, ids)

        assert atomic_calls == 0, f"{miner}: своя транзакция при занятом shared — раньше было инлайном"
        assert await _heuristic_edges() == warmed, f"{miner}: запись утекла в читателя до коммита внешней транзакции"
    finally:
        await shared.rollback()
