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


@pytest.mark.asyncio
async def test_replay_skips_case_b_and_never_calls_distill(cm, monkeypatch):
    """S5: текст уже сохранён -> реплей не дистиллирует его повторно (spy)."""
    from features.replay import replay

    rid = await _captured(cm, "реплей не должен дистиллировать это", age_s=86400)
    await _tag_l3(cm, rid)

    calls = []

    async def spy(*a, **k):
        calls.append(k.get("source_rid"))
        return {"l4_saved": 0, "l3_saved": 1, "conflicts": 0}

    import lifecycle.distiller as dist_mod

    monkeypatch.setattr(dist_mod, "distill_and_route", spy)
    out = await replay(since_days=7, respect_gate=False, max_rows=10)
    assert calls == []  # RED: сейчас spy вызывается — эхо-взрыв 120 строк
    assert out["processed"] == 0
    status, _ = await _status(cm, rid)
    assert status == "saved_l3"


@pytest.mark.asyncio
async def test_replay_distills_case_a_after_heal(cm, monkeypatch):
    """S5/regression: письмо без следа -> unaccounted -> ЭТОТ же реплей его спасает."""
    from features.replay import replay

    rid = await _captured(cm, "реплей спасает несдавшееся письмо", age_s=86400)
    calls = []

    async def spy(*a, **k):
        calls.append(k.get("source_rid"))
        return {"l4_saved": 0, "l3_saved": 1, "conflicts": 0}

    import lifecycle.distiller as dist_mod

    monkeypatch.setattr(dist_mod, "distill_and_route", spy)
    out = await replay(since_days=7, respect_gate=False, max_rows=10)
    # Может быть GREEN и до правки — регрессионный щит: guard обязан ПРОПУСКАТЬ
    # эту строку через claim к дистиллятору, а не бросать её.
    assert calls == [rid]
    assert out["processed"] == 1
    status, _ = await _status(cm, rid)
    assert status == "saved_l3"


@pytest.mark.asyncio
async def test_replay_skips_fresh_received(cm, monkeypatch):
    """S5: свежая строка (< грейса) не реплеится — ждёт своего писателя/ночи."""
    from features.replay import replay

    rid = await _captured(cm, "свежая, реплей ждёт грейса")
    calls = []

    async def spy(*a, **k):
        calls.append(k.get("source_rid"))
        return {"l4_saved": 0, "l3_saved": 1, "conflicts": 0}

    import lifecycle.distiller as dist_mod

    monkeypatch.setattr(dist_mod, "distill_and_route", spy)
    out = await replay(since_days=7, respect_gate=False, max_rows=10)
    assert calls == []  # RED: сейчас свежая строка дистиллируется в первый же прогон
    assert out["skipped"] >= 1
    status, _ = await _status(cm, rid)
    assert status == "received"


@pytest.mark.asyncio
async def test_sweep_mixture_and_counters(cm):
    """S6: sweep лечит смесь вердиктов и точно считает."""
    from shared.l0 import sweep_received

    b = await _captured(cm, "строка уже в памяти", age_s=700)
    await _tag_l3(cm, b)
    a = await _captured(cm, "строка без следа", age_s=700)
    l4 = await _captured(cm, "строка в L4", age_s=700)
    await _tag_l4(cm, l4)
    fresh = await _captured(cm, "свежая строка")

    counts = await sweep_received()
    assert counts == {"saved_l3": 1, "promoted_l4": 1, "unaccounted_exit": 1, "fresh": 1}
    assert (await _status(cm, b))[0] == "saved_l3"
    assert (await _status(cm, a))[0] == "gated_out"
    assert (await _status(cm, l4))[0] == "promoted_l4"
    assert (await _status(cm, fresh))[0] == "received"


@pytest.mark.asyncio
async def test_sweep_never_raises_on_a_broken_reader(cm, monkeypatch):
    """S6: битый чтение -> пустые счётчики, без исключения (ночь не падает)."""
    from shared.l0 import sweep_received

    class Boom:
        async def query(self, *a, **k):
            raise RuntimeError("db down")

    import shared.l0 as l0_mod

    monkeypatch.setattr(l0_mod, "_dispatch", lambda: Boom())
    counts = await sweep_received()
    assert counts == {"saved_l3": 0, "promoted_l4": 0, "unaccounted_exit": 0, "fresh": 0}
