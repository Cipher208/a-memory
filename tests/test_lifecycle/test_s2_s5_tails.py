"""S2/S5/S6a tails: read-time fusion + soft expiry + provenance drill-down.

Docs: docs/compose/specs/2026-09-04-phase-fgh-design.md
- [S2] read-time fusion приоритетнее gate-time (Mem0 ADD-only, LoCoMo +21pp):
  search прячет 'earlier'-сторону conflict-split пары, 'later' аннотируется
  superseded_context; строки в БД остаются (история не теряется).
- [S5] Memanto soft expiry: истёкшие (expires_at < now) остаются recallable
  с [EXPIRED]-меткой, полем expired и restorable-провенансом имени правила.
- [S6a-4] провенанс: distill_and_route пишет source_raw_id (L4 — metadata,
  L3 — тег raw:<rid>) → drill_down до исходного l0_journal сырья.
"""

import json
import time
from unittest.mock import MagicMock

import pytest

from shared.connection import connection_manager
from shared.migrations import MigrationManager


class FakeL3:
    def __init__(self) -> None:
        self.saved: list[tuple[str, str, float, list[str]]] = []

    async def save(self, user_id: str, summary: str, weight: float, tags: list[str]) -> int:
        self.saved.append((user_id, summary, weight, tags))
        return len(self.saved)


class FakeMem:
    """Ровно то, что distill_and_route трогает на mem: l3.save (+ _cm для L4)."""

    def __init__(self, cm: object) -> None:
        self._cm = cm
        self.l3 = FakeL3()


@pytest.fixture
async def cm(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)  # патчим base_dir, не подменяем объект
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


# ── [S2] read-time fusion ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_s2_fusion_hides_earlier_annotates_later(cm) -> None:
    from core.memory import CoreMemory

    cmem = CoreMemory(cm=cm)
    # пара в форме C4 condition-splitting (как пишет дистиллятор): ранняя —
    # scope=earlier, поздняя — scope=later + contradicts=<ключ ранней>.
    await cmem.save("fu", "fact:base_postgres", "база: PostgreSQL", metadata={"scope": "earlier"})
    await cmem.save("fu", "fact:base_mysql", "база: MySQL", metadata={"scope": "later", "contradicts": "fact:base_postgres"})
    await cmem.save("fu", "fact:lang_python", "язык: python")  # вне пары — должен остаться

    res = await cmem.search("fu", "база язык")
    by_key = {r["key"]: r for r in res}
    assert "fact:base_postgres" not in by_key, "earlier-сторона скрыта из выдачи"
    assert "fact:base_mysql" in by_key and "fact:lang_python" in by_key
    assert by_key["fact:base_mysql"]["superseded_context"] == {"scope": "later", "has_earlier": True}
    assert "superseded_context" not in by_key["fact:lang_python"], "сирота без пары не аннотируется"

    # история не теряется: обе строки остаются в core_memory
    conn = await cm.get("memory.db")
    rows = await (await conn.execute("SELECT key FROM core_memory WHERE user_id='fu'")).fetchall()
    assert {r["key"] for r in rows} == {"fact:base_postgres", "fact:base_mysql", "fact:lang_python"}


# ── [S5] soft expiry (Memanto) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_s5_expired_marked_not_deleted(cm) -> None:
    from core.memory import CoreMemory

    cmem = CoreMemory(cm=cm)
    await cmem.save("ex", "fact:temp_token", "токен: временный", expires_at=time.time() - 10, metadata={"expiry_rule": "token_ttl_1h"})
    await cmem.save("ex", "fact:perm_rule", "правило: постоянное")

    res = await cmem.search("ex", "токен правило")
    by_key = {r["key"]: r for r in res}
    expired = by_key["fact:temp_token"]
    assert expired["expired"] is True
    assert expired["value"].startswith("[EXPIRED] ")
    assert expired["restorable"] is True  # metadata содержит имя правила-источника

    live = by_key["fact:perm_rule"]
    assert "expired" not in live, "живой факт не помечается"
    assert not live["value"].startswith("[EXPIRED]")

    # мягкое истечение: запись остаётся в БД
    conn = await cm.get("memory.db")
    row = await (await conn.execute("SELECT value FROM core_memory WHERE user_id='ex' AND key='fact:temp_token'")).fetchone()
    assert row is not None and "токен" in row["value"]

    # get_all помечает истёкшие полем expired (S5)
    entries = await cmem.get_all("ex")
    m = {e.key: e for e in entries}
    assert m["fact:temp_token"].expired is True
    assert m["fact:perm_rule"].expired is False


# ── [S6a-4] provenance drill-down ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_s6a_source_rid_and_drill_down(cm) -> None:
    conn = await cm.get("memory.db")
    await conn.execute(
        "INSERT INTO l0_journal (id, ts, event, layer, user_id, text) VALUES (42, ?, 'new_message', 'user', 'dd', 'я решила выбрать PostgreSQL')",
        (time.time(),),
    )
    await conn.commit()

    from lifecycle.distiller import distill_and_route

    stats = await distill_and_route(FakeMem(cm), MagicMock(), "dd", "я решила выбрать PostgreSQL", 0.8, source_rid=42)
    assert stats["l4_saved"] >= 1
    row = await (await conn.execute("SELECT entry_id, metadata FROM core_memory WHERE user_id='dd'")).fetchone()
    assert json.loads(row["metadata"])["source_raw_id"] == 42, "L4: source_raw_id в metadata"

    from features.diagnostics import drill_down

    d = await drill_down(int(row["entry_id"]), "dd")
    assert d["source_raw_id"] == 42
    assert d["raw_text"] == "я решила выбрать PostgreSQL"
    assert d["raw_event"] == "new_message"


@pytest.mark.asyncio
async def test_s6a_drill_down_survives_cold_archive(cm) -> None:
    """S17 доп.8: L0-строка уехала в l0_cold_archive (tier_l0) → drill_down
    фолбэком достаёт сырье оттуда, помечая archived=True; до фолбэка был тупик."""
    conn = await cm.get("memory.db")
    await conn.execute(
        "INSERT INTO l0_journal (id, ts, event, layer, user_id, text) VALUES (77, ?, 'remember', 'user', 'dd2', 'я решила шифровать бэкапы')",
        (time.time(),),
    )
    await conn.commit()

    from lifecycle.distiller import distill_and_route

    stats = await distill_and_route(FakeMem(cm), MagicMock(), "dd2", "я решила шифровать бэкапы", 0.8, source_rid=77)
    assert stats["l4_saved"] >= 1
    row = await (await conn.execute("SELECT entry_id FROM core_memory WHERE user_id='dd2'")).fetchone()

    # имитируем tier_l0: строка перенесена в холодный тир и удалена из журнала
    await conn.execute(
        "INSERT INTO l0_cold_archive (id, ts, event, raw_type, layer, user_id, decisions, archived_at, text)"
        " SELECT id, ts, event, 'plain', layer, user_id, '[]', ?, text FROM l0_journal WHERE id=77",
        (time.time(),),
    )
    await conn.execute("DELETE FROM l0_journal WHERE id=77")
    await conn.commit()

    from features.diagnostics import drill_down

    d = await drill_down(int(row["entry_id"]), "dd2")
    assert d["source_raw_id"] == 77
    assert d["raw_text"] == "я решила шифровать бэкапы", f"фолбэк в cold archive: {d}"
    assert d["raw_event"] == "remember"
    assert d["archived"] is True, "флаг: сырье из холодного тира"

    # сырья нет нигде — прежний тупик сохраняет контракт
    await conn.execute("DELETE FROM l0_cold_archive WHERE id=77")
    await conn.commit()
    d2 = await drill_down(int(row["entry_id"]), "dd2")
    assert d2["raw_text"] is None and "archived" not in d2
    assert d["raw_ts"] is not None and d["key"] and d["value"]

    # L3: у episodes нет metadata-колонки → провенанс уходит тегом raw:<rid>
    mem = FakeMem(cm)
    stats3 = await distill_and_route(mem, MagicMock(), "dd", "наблюдение: трафик растёт по пятницам", 0.6, source_rid=42)
    assert stats3["l3_saved"] >= 1
    assert any("raw:42" in tags for _, _, _, tags in mem.l3.saved)

    # без провенанса — {'source_raw_id': None}
    from core.memory import CoreMemory

    eid = await CoreMemory(cm=cm).save("dd", "fact:plain", "просто факт без происхождения")
    assert await drill_down(eid, "dd") == {"source_raw_id": None}


@pytest.mark.asyncio
async def test_s6a_conflict_path_keeps_source_rid(cm) -> None:
    """Конфликтная ветка (C4) тоже несёт source_raw_id в metadata 'later'."""
    from lifecycle.distiller import distill_and_route

    conn = await cm.get("memory.db")
    await distill_and_route(FakeMem(cm), MagicMock(), "cx", "я решила выбрать PostgreSQL", 0.8, source_rid=7)
    await distill_and_route(FakeMem(cm), MagicMock(), "cx", "я решила выбрать MySQL", 0.8, source_rid=9)
    rows = await (await conn.execute("SELECT metadata FROM core_memory WHERE user_id='cx' ORDER BY entry_id")).fetchall()
    metas = [json.loads(r["metadata"]) for r in rows]
    assert metas[0]["source_raw_id"] == 7
    assert metas[1]["scope"] == "later" and metas[1]["source_raw_id"] == 9


@pytest.mark.asyncio
async def test_all_distill_call_sites_pass_source_rid(cm, tmp_path, monkeypatch) -> None:
    """Аудит 05.09 (P0): replay/import/bridge передают source_rid — каждый
    L4-факт знает свою строку L0 (drill-down до сырья со всех входов)."""
    from features.bridge import ingest_drain
    from features.replay import replay
    from scripts.import_chat import import_records
    from shared.l0 import capture

    # 1) replay: строка L0 → дистилляция с source_rid
    rid = await capture("new_message", "user", "rp", "решила кэш чистить по расписанию еженощно")
    assert rid is not None
    res = await replay(since_days=1)
    assert res["processed"] >= 1
    conn = await cm.get("memory.db")
    rows = await (await conn.execute("SELECT metadata FROM core_memory WHERE user_id='rp'")).fetchall()
    assert rows and json.loads(rows[0]["metadata"]).get("source_raw_id") == rid

    # 2) import: ts → L0 → distill с source_rid
    p = tmp_path / "conv.json"
    p.write_text(
        json.dumps(
            [
                {
                    "uuid": "c1",
                    "name": "conv",
                    "messages": [{"role": "user", "content": "решила бэкапы шифровать ключом", "created_at": "2024-01-15T10:30:00Z"}],
                }
            ]
        ),
        encoding="utf-8",
    )
    res_i = await import_records("claude", str(p), "imp")
    assert res_i["captured"] == 1
    rows_i = await (await conn.execute("SELECT metadata FROM core_memory WHERE user_id='imp'")).fetchall()
    assert rows_i and json.loads(rows_i[0]["metadata"]).get("source_raw_id") is not None, "import-факт несёт source_raw_id"

    # 3) bridge: drain → capture → distill с source_rid
    bpath = tmp_path / "ingest.md"
    bpath.write_text("notes\n# === AUTO-DRAIN BELOW ===\nрешила мониторинг усилить позже\n", encoding="utf-8")
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)  # bridge пишет/читает файл относительно data-dir
    res_b = await ingest_drain("br", "user", base_path=str(bpath))
    assert res_b["ingested"] >= 1
    rows_b = await (await conn.execute("SELECT metadata FROM core_memory WHERE user_id='br'")).fetchall()
    assert rows_b and json.loads(rows_b[0]["metadata"]).get("source_raw_id") is not None, "bridge-факт несёт source_raw_id"


@pytest.mark.asyncio
async def test_skip_distill_rows_routed_direct(cm) -> None:
    """S6a-1 единый вход: строки с skip_distill (remember/think) replay
    помечает routed_direct и НЕ дистиллирует повторно."""
    from features.replay import replay
    from shared.l0 import capture

    rid = await capture("remember", "user", "sk", "вручную сохранённый факт для проверки", decisions=[{"gate": "mcp_remember", "skip_distill": True}])
    assert rid is not None
    res = await replay(since_days=1)
    assert res["processed"] == 0
    conn = await cm.get("memory.db")
    row = await (await conn.execute("SELECT status FROM l0_journal WHERE id=?", (rid,))).fetchone()
    assert row[0] == "routed_direct"
