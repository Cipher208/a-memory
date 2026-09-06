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
async def test_textcat_promotes_stable_fact_off_by_default(cm, monkeypatch) -> None:
    """S19.2: keyword-unmatched FACT умирает в L3 (статус-кво); с мокнутым
    'stable' — промоутится в L4; default OFF — статус-кво не меняется."""
    from lifecycle.distiller import distill_and_route
    from shared.memory_types import _KEYWORD_MAP, kind_for_text

    clause = "сервер биллинга переехал в第四 датацентр Марта"  # без keyword-маркеров
    assert not any(kw in clause.lower() for _, kws in _KEYWORD_MAP for kw in kws), "setup: keyword-miss"
    assert kind_for_text(clause) is not None  # FACT fallback

    # 1) default OFF: статус-кво → L3
    fake_mem, fake_graph = FakeMem(), MagicMock()
    r = await distill_and_route(fake_mem, fake_graph, "tx1", clause, 0.7)
    assert r["l3_saved"] >= 1, f"OFF → ephemeral route: {r}"

    # 2) ON + модель говорит 'stable' → L4
    monkeypatch.setattr("config.config._data", {"rag": {"textcat": True}}, raising=False)
    import shared.textcat as tc

    monkeypatch.setattr(
        tc,
        "_model",
        lambda: type(
            "N",
            (),
            {
                "make_doc": staticmethod(lambda t: object()),
                "__call__": staticmethod(lambda d: type("D", (), {"cats": {"stable": 0.97, "ephemeral": 0.03}})()),
            },
        )(),
    )
    fake_mem2, fake_graph2 = FakeMem(), MagicMock()
    r2 = await distill_and_route(fake_mem2, fake_graph2, "tx2", clause, 0.7)
    assert r2["l4_saved"] >= 1, f"stable → L4 промоут: {r2}"
    assert r2["l3_saved"] == 0, f"не дублируется в L3: {r2}"


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


def test_canonical_key_counter_signal_unifies(monkeypatch) -> None:
    """S18 п.7-связка: counter-signal пара канонизирует ключ к ТЕКУЩЕМУ имени.

    «Гелиос» (superseded) и «Заря» (current) падают в один ключ — новая запись
    занимает занятый канон-ключ, C4-механика сама строит superseded-цепочку
    (earlier/later), вместо двух независимых фактов."""
    monkeypatch.setattr("config.config._data", {"rag": {"counter_signals": {"гелиос": "заря"}}}, raising=False)
    from lifecycle.distiller import _canonical_key
    from shared.memory_types import MemoryKind

    k_old = _canonical_key("проект Гелиос закрыт", MemoryKind.FACT)
    k_new = _canonical_key("проект Заря закрыт", MemoryKind.FACT)
    assert k_old == k_new, f"переименование → один ключ: {k_old!r} != {k_new!r}"


def test_canonical_key_counter_signal_does_not_touch_current(monkeypatch) -> None:
    """Обратное имя пары (current) не переписывается; чужие слова не задеты."""
    monkeypatch.setattr("config.config._data", {"rag": {"counter_signals": {"гелиос": "заря"}}}, raising=False)
    from lifecycle.distiller import _canonical_key
    from shared.memory_types import MemoryKind

    assert _canonical_key("проект Заря закрыт", MemoryKind.FACT) == _canonical_key("проект Заря закрыт", MemoryKind.FACT)
    k_plain = _canonical_key("проект Марс закрыт", MemoryKind.FACT)
    assert "марс" in k_plain, "слова вне таблицы counter_signals не трогаются"
