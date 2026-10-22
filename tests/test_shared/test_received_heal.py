"""L0 received-heal: классификация зависших received-строк.

Спека: .compose/specs/2026-10-22-received-heal-design.md (S1-S9).
"""

import json
import logging
import time

import pytest

from shared.connection import connection_manager
from shared.migrations import MigrationManager


@pytest.fixture
async def cm(tmp_path):
    connection_manager.base_dir = tmp_path  # НЕ подменять объект!
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


async def _captured(cm, text, *, age_s=0.0, layer="user", user_id="u1"):
    """Capture a row and optionally backdate it past the 600 s grace."""
    from shared.l0 import capture

    rid = await capture("new_message", layer, user_id, text)
    assert rid is not None
    if age_s:
        con = await cm.get("memory.db")
        await con.execute("UPDATE l0_journal SET ts=? WHERE id=?", (time.time() - age_s, rid))
        await con.commit()  # WriteDispatch holds its own connection: uncommitted rows are invisible to it
    return rid


async def _tag_l3(cm, rid, *, layer="user", user_id="u1"):
    con = await cm.get("memory.db")
    await con.execute(
        "INSERT INTO episodes (user_id, summary, created_at, tags, layer) VALUES (?, ?, ?, ?, ?)",
        (user_id, "s", time.time(), json.dumps([f"raw:{rid}"]), layer),
    )
    await con.commit()


async def _tag_l4(cm, rid, *, layer="user", user_id="u1"):
    con = await cm.get("memory.db")
    now = time.time()
    await con.execute(
        "INSERT INTO core_memory (user_id, key, value, created_at, updated_at, layer, metadata) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, f"source:{rid}", "v", now, now, layer, json.dumps({"source_raw_id": rid})),
    )
    await con.commit()


async def _status(cm, rid):
    con = await cm.get("memory.db")
    cur = await con.execute("SELECT status, decisions FROM l0_journal WHERE id=?", (rid,))
    row = await cur.fetchone()
    return row[0], json.loads(row[1] or "[]")


@pytest.mark.asyncio
async def test_classify_case_b_saved_l3(cm):
    """Текст уже в L3 (тег raw:<rid>) -> saved_l3, строка закрыта терминально."""
    from shared.l0 import classify_received

    rid = await _captured(cm, "помни: heal проверен", age_s=700)
    await _tag_l3(cm, rid)
    assert await classify_received(rid) == "saved_l3"
    status, _ = await _status(cm, rid)
    assert status == "saved_l3"


@pytest.mark.asyncio
async def test_classify_case_a_unaccounted_exit(cm):
    """Ни L3, ни L4 -> письмо не дошло: unaccounted_exit, строка переоткрыта."""
    from shared.l0 import UNACCOUNTED_EXIT, classify_received

    rid = await _captured(cm, "сообщение без следа в памяти", age_s=700)
    assert await classify_received(rid) == UNACCOUNTED_EXIT
    status, decisions = await _status(cm, rid)
    assert status == "gated_out"
    assert any(d.get("reason") == UNACCOUNTED_EXIT for d in decisions)


@pytest.mark.asyncio
async def test_classify_l4_only_promoted(cm):
    """След только в L4 (metadata.source_raw_id) -> promoted_l4."""
    from shared.l0 import classify_received

    rid = await _captured(cm, "факт живёт в L4", age_s=700)
    await _tag_l4(cm, rid)
    assert await classify_received(rid) == "promoted_l4"
    status, _ = await _status(cm, rid)
    assert status == "promoted_l4"


@pytest.mark.asyncio
async def test_classify_l4_wins_over_l3(cm):
    """S2: оба следа -> promoted_l4 (приоритет L4 как в дистилляторе)."""
    from shared.l0 import classify_received

    rid = await _captured(cm, "и в L3, и в L4", age_s=700)
    await _tag_l3(cm, rid)
    await _tag_l4(cm, rid)
    assert await classify_received(rid) == "promoted_l4"


@pytest.mark.asyncio
async def test_classify_fresh_row_inside_grace_is_untouched(cm):
    """S3: строка моложе 600 с не судится — писатель ещё может её штамповать."""
    from shared.l0 import STATUS_RECEIVED, classify_received

    rid = await _captured(cm, "только что пришло")
    assert await classify_received(rid) is None
    status, _ = await _status(cm, rid)
    assert status == STATUS_RECEIVED


@pytest.mark.asyncio
async def test_classify_evidence_is_scope_bound(cm):
    """S3: raw:<rid> чужого user_id — не наш след -> unaccounted_exit."""
    from shared.l0 import UNACCOUNTED_EXIT, classify_received

    rid = await _captured(cm, "чужой след не считается", age_s=700)
    await _tag_l3(cm, rid, user_id="other")
    assert await classify_received(rid) == UNACCOUNTED_EXIT
    # layer — часть scope: чужой layer при СВОЁМ user_id тоже не доказательство
    rid2 = await _captured(cm, "чужой слой не считается", age_s=700)
    await _tag_l3(cm, rid2, layer="agent")
    assert await classify_received(rid2) == UNACCOUNTED_EXIT


@pytest.mark.asyncio
async def test_classify_never_raises_on_a_broken_reader(cm, monkeypatch, caplog):
    """S3: битое чтение -> None, строка не тронута (WARNING пишет сам хелпер)."""
    from shared.l0 import STATUS_RECEIVED, classify_received

    caplog.set_level(logging.WARNING)
    rid = await _captured(cm, "читатель сломан", age_s=700)

    class Boom:
        async def select_one(self, *a, **k):
            raise RuntimeError("db down")

        async def query(self, *a, **k):
            raise RuntimeError("db down")

    import shared.l0 as l0_mod

    monkeypatch.setattr(l0_mod, "_dispatch", lambda: Boom())
    assert await classify_received(rid) is None
    assert "classify_received" in caplog.text  # WARNING пишет сам хелпер
    status, _ = await _status(cm, rid)
    assert status == STATUS_RECEIVED


@pytest.mark.asyncio
async def test_find_block_reenters_after_case_a_heal(cm):
    """S4: письмо без следа классифицируется прямо в find_block и НЕ блокирует повтор."""
    from shared.l0 import find_block

    text = "повтор после незакрытой записи"
    rid = await _captured(cm, text, age_s=700)
    # RED: сейчас received-строка возвращается как дубль; после фикса — None
    assert await find_block("user", "u1", text) is None
    status, _ = await _status(cm, rid)
    assert status == "gated_out"  # классификация закрыла её unaccounted_exit
    # и повторная запись проходит: find_block снова её не видит
    assert await find_block("user", "u1", text) is None


@pytest.mark.asyncio
async def test_find_block_still_blocks_case_b(cm):
    """S4: текст уже в памяти -> строка остаётся блоком-дублем (плюс терминальный статус)."""
    from shared.l0 import find_block

    text = "помни: дубль остаётся дублем"
    rid = await _captured(cm, text, age_s=700)
    await _tag_l3(cm, rid)
    assert await find_block("user", "u1", text) == rid
    status, _ = await _status(cm, rid)
    assert status == "saved_l3"


@pytest.mark.asyncio
async def test_find_block_never_raises_even_if_classifier_does(cm, monkeypatch):
    """S4: контракт never-raise find_block локален — падающий классификатор не роняет путь."""
    import shared.l0 as l0_mod
    from shared.l0 import find_block

    text = "классификатор сломан, но find_block жив"
    rid = await _captured(cm, text, age_s=700)

    def boom(*a, **k):
        raise RuntimeError("classifier exploded")

    monkeypatch.setattr(l0_mod, "classify_received", boom)
    # старый ответ сохраняется: received-строка остаётся дублем
    assert await find_block("user", "u1", text) == rid
    status, _ = await _status(cm, rid)
    assert status == "received"  # классификатор не тронул строку
