"""S1 order_key + S6 L0-тиры — тесты (verdict только через junitxml)."""

import json
import time
import zlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from shared.connection import connection_manager
from shared.migrations import MigrationManager


@pytest.fixture
async def cm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)  # патчим base_dir, не подменяем объект
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


async def _seed(cm: Any, text: str, *, age_days: float, status: str, decisions: str = "[]") -> int:
    from shared.l0 import capture

    rid = await capture("new_message", "user", "u1", text, ts_override=time.time() - age_days * 86400)
    assert rid is not None
    conn = await cm.get("memory.db")
    await conn.execute("UPDATE l0_journal SET status=?, decisions=? WHERE id=?", (status, decisions, rid))
    await conn.commit()
    return rid


# (a) midpoint


def test_midpoint_start_and_successor() -> None:
    from shared.fractional_index import midpoint

    start = midpoint(None)
    assert start == "08000000"
    assert midpoint(start) > start


def test_midpoint_between() -> None:
    from shared.fractional_index import midpoint

    for a, b in (("1", "3"), ("08", "09"), ("08000000", "08000002"), ("1", "2")):
        m = midpoint(a, b)
        assert a < m < b, (a, m, b)


def test_midpoint_no_degeneration() -> None:
    from shared.fractional_index import midpoint

    a = midpoint(None)
    b = midpoint(a)
    lo = a
    seen = {a}
    for _ in range(10):
        m = midpoint(lo, b)
        assert lo < m < b, (lo, m, b)
        assert m not in seen  # не вырождается в повтор
        seen.add(m)
        lo = m
    assert len(seen) == 11  # a + 10 уникальных между, строки не деградировали


# (b) capture пишет монотонный order_key


@pytest.mark.asyncio
async def test_capture_writes_monotonic_order_key(cm: Any) -> None:
    from shared.l0 import capture

    r1 = await capture("new_message", "user", "u1", "первая запись про базу данных")
    r2 = await capture("new_message", "user", "u1", "вторая запись про базу данных")
    r3 = await capture("new_message", "user", "u1", "третья запись про базу данных")
    assert None not in (r1, r2, r3)
    conn = await cm.get("memory.db")
    rows = await (await conn.execute("SELECT id, order_key FROM l0_journal ORDER BY id")).fetchall()
    keys = [r["order_key"] for r in rows]
    assert all(keys)
    assert keys[0] == "08000000"
    assert keys == sorted(keys)
    assert len(set(keys)) == 3


# (c) tier_l0: cold / warm / свежая / received


@pytest.mark.asyncio
async def test_tier_l0_warm_cold_received(cm: Any) -> None:
    from lifecycle.l0_tiers import tier_l0

    now = time.time()
    cold_text = "Решение по архиву. Полный текст холодной записи. Третья часть не в превью."
    warm_text = "Первое предложение про warm. Второе предложение про warm. Хвост остаётся только в zlib."
    cold_rid = await _seed(cm, cold_text, age_days=200, status="promoted_l4", decisions='[{"gate": "g1", "verdict": "ok"}]')
    warm_rid = await _seed(cm, warm_text, age_days=60, status="saved_l3")
    fresh_rid = await _seed(cm, "Свежая запись. Не трогается.", age_days=1, status="promoted_l4")
    recv_rid = await _seed(cm, "Старая received. Никогда не архивируется и не режется.", age_days=400, status="received")

    res = await tier_l0(now=now)
    assert res["warm"] == 1
    assert res["cold"] == 1

    conn = await cm.get("memory.db")
    # холодный: из журнала удалён, в архиве PLAINTEXT == исходный
    gone = await (await conn.execute("SELECT COUNT(*) FROM l0_journal WHERE id=?", (cold_rid,))).fetchone()
    assert int(gone[0]) == 0
    arch = await (await conn.execute("SELECT text, archived_at FROM l0_cold_archive WHERE id=?", (cold_rid,))).fetchone()
    assert arch is not None
    assert arch["text"] == cold_text
    assert arch["archived_at"] >= now
    # тёплый: tier='warm', text == превью (2 предложения), zlib-восстановление == исходный
    warm = await (await conn.execute("SELECT text, text_z, tier FROM l0_journal WHERE id=?", (warm_rid,))).fetchone()
    assert warm["tier"] == "warm"
    assert warm["text"] == "Первое предложение про warm. Второе предложение про warm."
    assert zlib.decompress(warm["text_z"]).decode("utf-8") == warm_text
    # свежая не тронута
    fresh = await (await conn.execute("SELECT text, tier, text_z FROM l0_journal WHERE id=?", (fresh_rid,))).fetchone()
    assert fresh["tier"] is None and fresh["text_z"] is None
    assert fresh["text"] == "Свежая запись. Не трогается."
    # received-старая НИКОГДА не архивируется и не режется
    recv = await (await conn.execute("SELECT text, tier, status FROM l0_journal WHERE id=?", (recv_rid,))).fetchone()
    assert recv["status"] == "received" and recv["tier"] is None
    assert recv["text"] == "Старая received. Никогда не архивируется и не режется."


@pytest.mark.asyncio
async def test_tier_l0_disabled_gate(cm: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from config import config
    from lifecycle.l0_tiers import tier_l0

    monkeypatch.setattr(config, "_data", {**config._data, "l0": {"tiers_enabled": False}})
    res = await tier_l0()
    assert res["skipped"] == "disabled"


# (d) read_cold возвращает мета-блок с полным текстом


@pytest.mark.asyncio
async def test_read_cold_meta_block(cm: Any) -> None:
    from lifecycle.l0_tiers import read_cold, tier_l0

    now = time.time()
    cold_text = "Решение по графу без llm. Полный текст читается напрямую. Без распаковки."
    cold_rid = await _seed(cm, cold_text, age_days=200, status="gated_out", decisions='[{"gate": "g1"}]')
    await tier_l0(now=now)

    cold = await read_cold(since_days=1)
    assert len(cold) == 1
    block = cold[0]
    assert block["id"] == cold_rid
    assert block["event"] == "new_message"
    assert block["raw_type"] == "user-message"
    assert block["layer"] == "user"
    assert block["user_id"] == "u1"
    assert block["decisions"] == [{"gate": "g1"}]
    assert block["archived_at"] >= now
    assert block["text"] == cold_text
    assert block["brief"] == cold_text[:200]
    assert block["ts"] == pytest.approx(now - 200 * 86400, abs=1.0)


# (e) export_clack: валидный JSONL, каждая строка парсится, lossless


@pytest.mark.asyncio
async def test_export_clack_jsonl_lossless(cm: Any) -> None:
    from lifecycle.l0_tiers import export_clack, tier_l0

    now = time.time()
    t1 = "Холодная запись один. Полный текст. Три предложения для lossless-проверки."
    t2 = "Холодная запись два. Ещё полный текст. С метаданными решения."
    # Обе записи — в ОДНОМ UTC-месяце: 12:00 UTC 1-го числа месяца точки
    # now-200д. Это всегда ≥199.5д назад (гарантированно cold) и всегда
    # внутри бакета strftime(now-200д). Прежняя арифметика «200−mday»
    # пересекала границу месяца при mday > дней-в-месяце−28 (дата-зависимые
    # фейлы 2026-09-07 и 2026-09-09: 200−9д → t2 уезжал в следующий месяц).
    from datetime import datetime, timezone

    base = now - 200 * 86400
    gy, gmon = time.gmtime(base).tm_year, time.gmtime(base).tm_mon
    ts_same_month = datetime(gy, gmon, 1, 12, 0, tzinfo=timezone.utc).timestamp()
    age_same = (now - ts_same_month) / 86400
    await _seed(cm, t1, age_days=age_same, status="promoted_l4", decisions='[{"gate": "g1", "verdict": "save"}]')
    await _seed(cm, t2, age_days=age_same, status="saved_l3", decisions='[{"gate": "g1", "verdict": "save"}]')
    await _seed(cm, "Живая свежая запись. Осталась в журнале.", age_days=5, status="promoted_l4")
    res = await tier_l0(now=now)
    assert res["cold"] == 2 and res["exported"]

    month = time.strftime("%Y-%m", time.gmtime(now - 200 * 86400))
    path = Path(res["exported"][0])
    assert path.name == f"{month}.clack.jsonl"
    assert path.parent.name == "l0_cold"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    blocks = [json.loads(line) for line in lines]  # каждая строка — валидный JSON
    assert {b["text"] for b in blocks} == {t1, t2}  # plaintext lossless
    assert all(b["decisions"] == [{"gate": "g1", "verdict": "save"}] for b in blocks)
    assert all(set(b) >= {"id", "ts", "event", "raw_type", "layer", "user_id", "decisions", "archived_at", "text"} for b in blocks)

    # идемпотентность: прямой вызов export_clack перезаписывает тот же месяц без дублей
    again = await export_clack(month)
    assert Path(again).read_text(encoding="utf-8") == path.read_text(encoding="utf-8")
