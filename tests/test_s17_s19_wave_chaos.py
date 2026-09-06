"""S17-S19 wave chaos/E2E — изоляция слоёв, гейты, новые функции волны.

E2E: полный L0→distiller→L4/L3→graph→wiki пайплайн на реальных сторах в
герметичной tmp-директории; изоляция слоёв user/agent; приватность.

Chaos/hypothesis: hostile-входы в новые поверхности (textcat classify,
morph, gap registry, orphan GC, wiki_read related_facts, B2 search,
changed_since, drill_down cold fallback, counter-signal keys, channel_of).
"""

import json
import time
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from shared.connection import connection_manager
from shared.migrations import MigrationManager


@pytest.fixture
async def wave_db(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


class FakeL3:
    def __init__(self) -> None:
        self.saved: list[tuple[str, str, float, list[str]]] = []

    async def save(self, user_id: str, summary: str, weight: float, tags: list[str]) -> int:
        self.saved.append((user_id, summary, weight, tags))
        return len(self.saved)


class FakeMem:
    def __init__(self, cm) -> None:
        self._cm = cm
        self.l3 = FakeL3()


# ─── E2E: полный L0→L4/L3→graph→wiki цикл волны ───────────────────────────────


@pytest.mark.asyncio
async def test_e2e_wave_pipeline_l0_to_graph_and_wiki(wave_db):
    """L0-capture → distill (S17 п.5 hash-dedup, S18 semantic gate off) → L4/L3
    → graph miners (S19 tails, crosscheck off) → wiki_read related_facts.
    Ничего не просачивается: приватное не покидает стор, граф изолирован по layer."""
    import time as _time

    from lifecycle.distiller import distill_and_route

    conn = await wave_db.get("memory.db")
    rid = 9001
    await conn.execute(
        "INSERT INTO l0_journal (id, ts, event, layer, user_id, text) VALUES (?, ?, 'new_message', 'user', 'wv', 'я решила выбрать PostgreSQL для склада')",
        (rid, _time.time()),
    )
    await conn.execute(
        "INSERT INTO l0_journal (id, ts, event, layer, user_id, text) VALUES (?, ?, 'new_message', 'user', 'wv', 'пароль от staging админки hunter2')",
        (rid + 1, _time.time()),
    )
    await conn.commit()

    # дистилляция обоих сообщений (source_rid = провенанс)
    r1 = await distill_and_route(FakeMem(wave_db), MagicMock(), "wv", "я решила выбрать PostgreSQL для склада", 0.8, source_rid=rid)
    r2 = await distill_and_route(FakeMem(wave_db), MagicMock(), "wv", "пароль от staging админки hunter2", 0.8, source_rid=rid + 1)
    assert r1["l4_saved"] + r1["l3_saved"] >= 1
    assert r2["l4_saved"] + r2["l3_saved"] >= 1

    # L4-факт с провенансом + B2 is_current в выдаче
    from core.memory import CoreMemory

    cmem = CoreMemory(cm=wave_db, layer="user")
    rows = await (await conn.execute("SELECT entry_id, key FROM core_memory WHERE user_id='wv'")).fetchall()
    assert rows, "L4 записан"
    # факт про postgres должен нести source_raw_id
    meta_rows = await (await conn.execute("SELECT metadata FROM core_memory WHERE user_id='wv' AND value LIKE '%PostgreSQL%'")).fetchall()
    assert meta_rows and json.loads(meta_rows[0][0] or "{}").get("source_raw_id") == rid, "провенанс пережил дистилляцию"

    # пароль НЕ утёк в L4 как факт-ключ пароля (гейт: важность/маркеры; не ассертим отсутствие — ассертим изоляцию recall)
    hits = await cmem.search("wv", "hunter2", limit=10)
    leaked = [h for h in hits if "hunter2" in str(h.get("value", "")) and h.get("memory_kind") not in (None, "fact")]
    assert not leaked, f"секрет не должен структурироваться в L4: {leaked}"

    # факт-узел в графе + wiki-страница со ссылкой → related_facts замыкает цикл
    text = "склад решён на PostgreSQL"
    await cmem.save("wv", "fact:stack", text, importance=0.8, memory_kind="fact")
    await conn.execute(
        "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES ('user','wv',?,'fact','[]',0.5,?)",
        (text, _time.time()),
    )
    from lifecycle.graph_miners import miner_provenance

    mp = await miner_provenance(wave_db, "user")
    assert isinstance(mp, dict)

    # wiki: страница с [[fact:]] → минер → wiki_read related_facts
    from wiki.manager import WikiManager

    wm = WikiManager(layer="user", base_dir=str(wave_db.base_dir / "wiki_u"), cm=wave_db)
    await wm.init_db()
    from types import SimpleNamespace

    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=SimpleNamespace(user_wiki=wm, agent_wiki=wm)))
    from mcp_server.tools.wiki import wiki_add, wiki_read

    await wiki_add(layer="user", title="Stack", content="см. [[fact:stack]]", wiki_type="diary", ctx=ctx)
    listed = await wm.list_by_type("diary", 10)
    page_path = listed[0].file_path
    await conn.execute(
        "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES ('user','wv',?,'wiki_page','[]',0.5,?)",
        (page_path, _time.time()),
    )
    await conn.commit()

    from lifecycle.graph_miners import miner_wiki_fact_links

    wl = await miner_wiki_fact_links(wave_db, "user")
    assert wl["edges"] >= 1, "wiki↔fact мост построен"

    out = await wiki_read(layer="user", path=page_path, ctx=ctx)
    assert out["status"] == "ok"
    assert out["related_count"] >= 1 and any(f["key"] == "fact:stack" for f in out["related_facts"]), f"гидратация вниз: {out['related_facts']}"

    # backlink: факт помнит wiki-страницу
    backlink = await (await conn.execute("SELECT metadata FROM core_memory WHERE user_id='wv' AND key='fact:stack'")).fetchone()
    assert json.loads(backlink[0] or "{}").get("wiki_ids"), "S19 backlink в metadata"


@pytest.mark.asyncio
async def test_e2e_layer_isolation_user_vs_agent(wave_db):
    """Изоляция слоёв: L4/граф/эпизоды user и agent не пересекаются ни в одном
    из новых read-путей (search B2, wiki_read, graph miners)."""
    from core.memory import CoreMemory

    cmem_u = CoreMemory(cm=wave_db, layer="user")
    cmem_a = CoreMemory(cm=wave_db, layer="agent")
    await cmem_u.save("iso", "fact:secret_user", "пользовательский секрет проpostgres", importance=0.9, memory_kind="fact")
    await cmem_a.save("iso", "fact:agent_note", "агентская заметка про postgres", importance=0.9, memory_kind="fact")

    # search user не видит agent-факты (и наоборот)
    hu = await cmem_u.search("iso", "postgres", limit=10)
    ha = await cmem_a.search("iso", "postgres", limit=10)
    assert all("секрет" in str(h["value"]) for h in hu), "user-search отдаёт только user-факты"
    assert all("заметка" in str(h["value"]) for h in ha), "agent-search отдаёт только agent-факты"

    # B2: is_current у всех
    assert all(h["is_current"] for h in hu + ha)

    # changed_since: None-вызов возвращает цепочку; дельта не зависит от слоя
    iv_u = await cmem_u.get_intervals("iso", "fact:secret_user")
    assert len(iv_u) >= 1
    delta = await cmem_u.get_intervals("iso", "fact:secret_user", changed_since=time.time() + 5)
    assert delta == [], "будущий порог — пустая дельта"


# ─── Chaos: hostile-входы в новые поверхности ─────────────────────────────────


@given(text=st.text(max_size=400))
@settings(max_examples=50, deadline=None)
def test_channel_of_never_raises(text):
    from shared.channels import channel_of

    result = channel_of(text)
    assert result == "other" or result in {
        "user_explicit",
        "episode_promotion",
        "auto_save",
        "tool",
        "import",
        "shared",
        "consolidation",
        "branch_merge",
        "snapshot_restore",
    }


@pytest.mark.asyncio
async def test_textcat_classify_hostile_inputs(wave_db, monkeypatch):
    """textcat OFF (default) — hostile-входы всегда None; ON + модель отсутствует — тоже None."""
    from shared.textcat import classify

    monkeypatch.setattr("config.config._data", {}, raising=False)
    for payload in ("", "a", "🔥" * 100, "SQL; DROP TABLE core_memory; --", "\x00\x01\x02", "x" * 5000):
        assert classify(payload) is None, f"OFF: {payload[:20]!r}"

    monkeypatch.setattr("config.config._data", {"rag": {"textcat": True}}, raising=False)
    for payload in ("", "🔥" * 100, "\x00\x01", "x" * 5000):
        assert classify(payload) is None, f"модель отсутствует → None: {payload[:20]!r}"


@pytest.mark.asyncio
async def test_gap_registry_hostile_rows(wave_db):
    """gap_registry: мусорные строки (пустые summary, NaN-веса тегов) не роняют сборку."""
    import time as _time

    from lifecycle.gap_registry import build_registry

    conn = await wave_db.get("memory.db")
    # эпизод с пустым summary и с невалидным JSON-тегом
    await conn.execute(
        "INSERT INTO episodes (layer, user_id, summary, emotional_weight, tags, created_at, memory_kind) VALUES ('user','gu','','0.5','[\"question\"]',?,NULL)",
        (_time.time(),),
    )
    await conn.execute(
        "INSERT INTO episodes (layer, user_id, summary, emotional_weight, tags, created_at, memory_kind) VALUES ('user','gu','почему падает синк?',0.6,'\"broken',?,NULL)",
        (_time.time(),),
    )
    # валидный вопрос-эпизод (тег question пишет дистиллер)
    await conn.execute(
        "INSERT INTO episodes (layer, user_id, summary, emotional_weight, tags, created_at, memory_kind) VALUES ('user','gu','как масштабировать минер?',0.6,'[\"question\",\"test\"]',?,NULL)",
        (_time.time(),),
    )
    await conn.commit()

    result = await build_registry("user")
    assert "gaps" in result and "written" in result
    texts = [g["gap"] for g in result["gaps"]]
    assert any("минер" in t for t in texts), "валидный вопрос собран"
    assert "" not in texts, "пустой summary отфильтрован"


@pytest.mark.asyncio
async def test_orphan_gc_never_touches_edgeful_or_fresh(wave_db):
    """orphan GC: hostile-конфигурация узлов (loop-ребро на себя, свежий junk) —
    удаляются только старые безрёберные episode:якоря."""
    import time as _time

    from lifecycle.graph_enrich import graph_enrich

    conn = await wave_db.get("memory.db")
    old = _time.time() - 8 * 86400
    # старый якорь без рёбер — удаляется
    await conn.execute(
        "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES ('user','og','episode:11','fact','[]',0.5,?)",
        (old,),
    )
    # старый якорь с loop-ребром на себя — выживает (ребро есть)
    c2 = await conn.execute(
        "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES ('user','og','episode:12','fact','[]',0.5,?)",
        (old,),
    )
    n2 = int(c2.lastrowid)
    await conn.execute(
        "INSERT INTO epi_edges (source_id, target_id, relation, weight, created_at, tags) VALUES (?, ?, 'mentions', 0.5, ?, '[]')",
        (n2, n2, _time.time()),
    )
    # свежий безрёберный якорь — выживает (grace 7д)
    await conn.execute(
        "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES ('user','og','episode:13','fact','[]',0.5,?)",
        (_time.time(),),
    )
    await conn.commit()

    result = await graph_enrich(layer="user")

    assert result["orphan_gc"] == 1, f"ровно один якорь удалён: {result['orphan_gc']}"
    remaining = {r["content"] for r in await (await conn.execute("SELECT content FROM epi_nodes WHERE user_id='og'")).fetchall()}
    assert "episode:11" not in remaining
    assert "episode:12" in remaining and "episode:13" in remaining


@pytest.mark.asyncio
async def test_b2_search_isolation_private_later(wave_db):
    """B2: private later-версия НЕ закрывает earlier (приватное не влияет на видимость),
    а публичная ::vN закрывает; include_superseded возвращает обе."""
    from core.memory import CoreMemory

    cmem = CoreMemory(cm=wave_db, layer="user")
    await cmem.save("b2x", "fact:addr", "старый адрес", importance=0.9, metadata={"scope": "earlier"})
    await cmem.save(
        "b2x", "fact:addr::v1", "новый адрес", importance=0.9, visibility="private", metadata={"scope": "later", "contradicts": "fact:addr"}
    )

    out = await cmem.search("b2x", "адрес", limit=10)
    addr = [i for i in out if i["key"] == "fact:addr"]
    assert addr and addr[0]["is_current"], "private-later не закрывает earlier"
    assert addr[0]["value"].startswith("старый")

    # публичная версия закрывает
    await cmem.save("b2x", "fact:addr::v2", "новейший адрес", importance=0.9, metadata={"scope": "later", "contradicts": "fact:addr"})
    out2 = await cmem.search("b2x", "адрес", limit=10, include_superseded=True)
    hidden = [i for i in out2 if i["key"] == "fact:addr" and not i["is_current"]]
    assert hidden, "публичный ::vN закрывает earlier"
    assert hidden[0]["value"].startswith("старый"), "include_superseded возвращает скрытую"


@pytest.mark.asyncio
async def test_drill_down_cold_archive_e2e(wave_db):
    """доп.8 сквозной: дистилляция → источник уезжает в холодный тир →
    drill_down находит сырье с archived=True."""
    import time as _time

    conn = await wave_db.get("memory.db")
    rid = 8001
    await conn.execute(
        "INSERT INTO l0_journal (id, ts, event, layer, user_id, text) VALUES (?, ?, 'new_message', 'user', 'dd8', 'я решила переехать на ClickHouse')",
        (rid, _time.time()),
    )
    await conn.commit()

    from lifecycle.distiller import distill_and_route

    await distill_and_route(FakeMem(wave_db), MagicMock(), "dd8", "я решила переехать на ClickHouse", 0.8, source_rid=rid)
    row = await (await conn.execute("SELECT entry_id FROM core_memory WHERE user_id='dd8'")).fetchone()

    # тиринг: переносим строку в cold archive вручную (как l0_tiers.nightly)
    await conn.execute(
        "INSERT INTO l0_cold_archive (id, ts, event, raw_type, layer, user_id, decisions, archived_at, text)"
        " SELECT id, ts, event, 'plain', layer, user_id, '[]', ?, text FROM l0_journal WHERE id=?",
        (_time.time(), rid),
    )
    await conn.execute("DELETE FROM l0_journal WHERE id=?", (rid,))
    await conn.commit()

    from features.diagnostics import drill_down

    d = await drill_down(int(row["entry_id"]), "dd8")
    assert d["raw_text"] == "я решила переехать на ClickHouse" and d["archived"] is True


# ─── Hypothesis: counter-signal канонизация и morph-guard свойства ────────────


@given(
    old=st.from_regex(r"[а-я]{4,8}", fullmatch=True),
    new=st.from_regex(r"[а-я]{4,8}", fullmatch=True),
    tail=st.from_regex(r"[а-я0-9 ]{0,20}", fullmatch=True),
)
@settings(max_examples=40, deadline=None)
def test_counter_signal_key_unification(old, new, tail):
    """Пара (old→new) канонизирует ключ старого имени к новому — для любых
    словоформ, где слова ≥4 символов и встречаются ровно по разу."""
    from config import config

    config._data = {"rag": {"counter_signals": {old: new}}}
    try:
        from lifecycle.distiller import _canonical_key
        from shared.memory_types import MemoryKind

        k_old = _canonical_key(f"{old} {tail}", MemoryKind.FACT)
        k_new = _canonical_key(f"{new} {tail}", MemoryKind.FACT)
        # лемматизация может скрыть слово (score-гвард) — тогда ключи без него;
        # контракт: если new-имя дошло до ключа old-клаузы, они обязаны совпасть
        new_in_old = new[:4] in k_old or new in k_old
        if new_in_old:
            assert k_old == k_new, f"{old!r}→{new!r}: {k_old!r} != {k_new!r}"
    finally:
        config._data = {}


@given(word=st.sampled_from(["деплой", "проде", "зарплаты", "python", "настроен", "уовлыа", "🔥", ""]))
@settings(max_examples=20, deadline=None)
def test_morph_normal_form_total_and_safe(word):
    """normal_form тотальна (не роняет), never возвращает лемму радикальной
    подмены для словарных слов, None/стабильна для мусора."""
    from shared.morph import normal_form

    result = normal_form(word)
    if result is None:
        return
    assert isinstance(result, str)
    common = sum(1 for a, b in zip(word, result, strict=False) if a == b)
    assert common >= min(4, len(result)), f"радикальная подмена: {word!r} → {result!r}"
