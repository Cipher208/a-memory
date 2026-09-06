"""G1 distiller: atomize → canonical key → kind-routing (инвариант→L4, событие→L3)."""

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
    """Ровно то, что distill_and_route трогает на mem: l3.save (+ опц. _cm)."""

    def __init__(self) -> None:
        self.l3 = FakeL3()


@pytest.fixture
async def cm(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)  # патчим base_dir, не подменяем объект
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


@pytest.mark.asyncio
async def test_invariant_routes_to_l4_event_to_l3(cm) -> None:
    from lifecycle.distiller import distill_and_route

    fake_mem, fake_graph = FakeMem(), MagicMock()
    # инвариант: «решила» = decision → L4
    r1 = await distill_and_route(fake_mem, fake_graph, "u1", "я решила перейти на PostgreSQL", 0.8)
    assert r1["l4_saved"] >= 1
    # событие: наблюдение/факт с decay > порога → L3 через mem.l3.save
    r2 = await distill_and_route(fake_mem, fake_graph, "u1", "наблюдение: трафик растёт по пятницам", 0.6)
    assert r2["l3_saved"] >= 1
    assert fake_mem.l3.saved, "event atom must reach mem.l3.save"
    rows = await (await (await cm.get("memory.db")).execute("SELECT key FROM core_memory WHERE user_id='u1'")).fetchall()
    assert all(not k[0].startswith("staging_") for k in rows), "ключи канонические, не обрубки"


@pytest.mark.asyncio
async def test_conflict_not_silent_update(cm) -> None:
    from lifecycle.distiller import distill_and_route

    fake_mem, fake_graph = FakeMem(), MagicMock()
    await distill_and_route(fake_mem, fake_graph, "u1", "база проекта: PostgreSQL", 0.8)
    r2 = await distill_and_route(fake_mem, fake_graph, "u1", "база проекта: MySQL", 0.8)
    assert r2["conflicts"] >= 1  # второе противоречит первому — запись с флагом, не затирание


@pytest.mark.asyncio
async def test_conflict_condition_splitting_keeps_both(cm) -> None:
    """C4: конфликт → ОБЕ записи в L4 с scope-метаданными и importance ×0.9.

    Ранняя — {'scope': 'earlier'}, поздняя — {'scope': 'later',
    'contradicts': first_key}; l4_saved учитывает обе (маршрут не молчит).
    """
    import json as _json

    from lifecycle.distiller import distill_and_route

    fake_mem, fake_graph = FakeMem(), MagicMock()
    r1 = await distill_and_route(fake_mem, fake_graph, "u2", "я решила выбрать PostgreSQL", 0.8)
    assert r1["l4_saved"] >= 1
    r2 = await distill_and_route(fake_mem, fake_graph, "u2", "я решила выбрать MySQL", 0.8)
    assert r2["conflicts"] >= 1
    assert r2["l4_saved"] >= 2, "обе записи сохранены — конфликт не глушит счётчик маршрута"
    conn = await cm.get("memory.db")
    rows = await (await conn.execute("SELECT key, value, importance, metadata FROM core_memory WHERE user_id='u2' ORDER BY entry_id")).fetchall()
    assert len(rows) == 2
    earlier, later = rows
    assert "PostgreSQL" in earlier["value"] and "MySQL" in later["value"]
    meta_e = _json.loads(earlier["metadata"])
    meta_l = _json.loads(later["metadata"])
    assert meta_e["scope"] == "earlier"
    assert meta_l["scope"] == "later"
    assert meta_l["contradicts"] == earlier["key"]
    assert earlier["importance"] == pytest.approx(0.8 * 0.9)  # конфликт снижает уверенность
    assert later["importance"] == pytest.approx(0.8 * 0.9)


@pytest.mark.asyncio
async def test_conflict_same_canon_key_no_self_overwrite(cm) -> None:
    """Аудит 05.09 (P0): save — upsert по UNIQUE(layer,user,key); канон-ключ
    lossy (kind + первые 4 токена) — конфликтующие клаузы, различающиеся
    ПОСЛЕ 4-го токена, получают ОДИН ключ, и later-запись молча затирала
    earlier. Теперь later версонируется ::vN — обе строки живут."""
    import json as _json

    from lifecycle.distiller import distill_and_route

    fake_mem, fake_graph = FakeMem(), MagicMock()
    # различие только после 4-го канон-токена ("ночью"/"утром") → ключ идентичен;
    # "решила" → DECISION (decay 0) → L4-роутинг
    r1 = await distill_and_route(fake_mem, fake_graph, "u3", "решила деплой бэкенда проводить ночью по расписанию", 0.8)
    assert r1["l4_saved"] >= 1
    r2 = await distill_and_route(fake_mem, fake_graph, "u3", "решила деплой бэкенда проводить утром по расписанию", 0.8)
    assert r2["conflicts"] >= 1

    conn = await cm.get("memory.db")
    rows = await (await conn.execute("SELECT key, value, metadata FROM core_memory WHERE user_id='u3' ORDER BY entry_id")).fetchall()
    assert len(rows) == 2, f"обе записи живы (no self-overwrite): {[(r['key'], r['value'][:40]) for r in rows]}"
    metas = sorted(_json.loads(r["metadata"])["scope"] for r in rows)
    assert metas == ["earlier", "later"]
    assert rows[0]["value"] != rows[1]["value"], "значения не перезаписаны"


@pytest.mark.asyncio
async def test_semantic_dedup_skips_near_duplicate(cm, monkeypatch) -> None:
    """S18 п.5: косинус > 0.92 против существующего same-kind факта — skip + advisory.

    Эмбеддинги мокнуты детерминированно: первый факт в [1,0,0], парафраз в
    [0.99, 0.1, 0] (cos ≈ 0.995), чужой текст в [0, 1, 0] (cos = 0)."""
    from lifecycle.distiller import distill_and_route

    fake_mem, fake_graph = FakeMem(), MagicMock()

    from shared import embeddings as emb_mod

    vectors: dict[str, list[float]] = {}

    async def _fake_embed(texts: list[str], *, prefix: str = "") -> list[list[float]]:
        out = []
        for t in texts:
            key = t.strip()
            if key not in vectors:
                vectors[key] = [1.0, 0.0, 0.0] if "Acme" in t else ([0.99, 0.1, 0.0] if "Corporation" in t else [0.0, 1.0, 0.0])
            out.append(vectors[key])
        return out

    monkeypatch.setattr("config.config._data", {"memory": {"semantic_dedup": True}}, raising=False)
    monkeypatch.setattr(emb_mod, "embed_texts", _fake_embed)

    r1 = await distill_and_route(fake_mem, fake_graph, "semu1", "решил работать в Acme Corp постоянно", 0.9)
    assert r1["l4_saved"] >= 1, "первый факт сохраняется (база пуста)"
    r2 = await distill_and_route(fake_mem, fake_graph, "semu1", "решил работать в Acme Corporation вечно", 0.9)
    assert r2["l4_saved"] == 0, f"парафрай сошёлся по косинусу — дубликат не сохранён: r2={r2}"
    assert r2["semantic_skipped"] >= 1, "счётчик semantic_skipped проставлен"
    assert r2["similar_to"], "A2-advisory содержит ключ существующего факта"
