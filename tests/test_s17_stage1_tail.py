"""S17 Stage-1-tail tests — 7 lost draft keys + EMA gate + ENGRAM procedural.

Каждый тест привязан к пункту S17 диздока (docs/compose/specs/
2026-09-04-phase-fgh-design.md §S17):
1. deterministic_retrieval — bypass inhibit/K-члена в edm_rerank
2. ENGRAM procedural — 14-й MemoryKind, policy, kind_for_text, agent-layer роутинг
3. AdaptiveRAG pre-gate — 27 фич + pre_gate_flags
4. A2 advisory similar_to — в stats дистиллятора и ответе auto_save_text
5. SHA-256 дедуп L0 — content_hash, повтор → rid первоисточника
6. zero-result минер — журнал + вопрос-узел поверх find_or_add
7. counter-signal алиасы — пессимизация superseded-имени в route_query
8. EMA-гейт — adaptive_threshold.gate в auto_save_text
"""

from __future__ import annotations

import pytest

from config import config
from hooks.external import auto_save_text
from rag.ablation import gate_sources, pre_gate_flags, query_features
from rag.dual_route import _apply_counter_signals, route_query
from rag.edm import edm_rerank
from rag.synonyms import load_counter_signals
from shared.l0 import capture, verify_chain
from shared.memory_types import MemoryKind, can_archive, get_policy, kind_for_text, validate_kind


# ── 1. deterministic_retrieval ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deterministic_minmax_raw_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "_data", {"retrieval": {"deterministic": True}}, raising=False)
    pool = [
        {"id": 1, "content": "alpha beta", "title": "", "score": 0.9},
        {"id": 2, "content": "unrelated delta", "title": "", "score": 0.7},
        {"id": 3, "content": "other epsilon", "title": "", "score": 0.5},
        {"id": 4, "content": "more zeta", "title": "", "score": 0.3},
    ]
    det = await edm_rerank(pool, "alpha beta")
    # чистый min-max(raw): порядок = raw-очки (novelty/дедуп не переворачивают);
    # низший (0.3 → 0 после zero-floor) отрезается ITS-порогом — это гейт, не порядок.
    assert [h["id"] for h in det] == [1, 2, 3]
    # raw_activation = сырые очки (ингибиция bypass-нута: 0.5 не давится до ~0.41)
    act = {h["id"]: h["raw_activation"] for h in det}
    assert act[3] == pytest.approx(0.5, abs=1e-3)


@pytest.mark.asyncio
async def test_deterministic_stable_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "_data", {"retrieval": {"deterministic": True}}, raising=False)
    pool = [
        {"id": 1, "content": "alpha beta", "title": "", "score": 0.9},
        {"id": 2, "content": "unrelated delta", "title": "", "score": 0.3},
        {"id": 3, "content": "alpha beta gamma", "title": "", "score": 0.5},
    ]
    first = await edm_rerank(pool, "alpha beta")
    second = await edm_rerank(pool, "alpha beta")
    assert [h["id"] for h in first] == [h["id"] for h in second]
    assert [(h["id"], h["score"], h["raw_activation"]) for h in first] == [(h["id"], h["score"], h["raw_activation"]) for h in second], (
        "тот же пул → байт-в-байт тот же результат"
    )


@pytest.mark.asyncio
async def test_deterministic_off_keeps_status_quo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "_data", {}, raising=False)
    pool = [{"id": 1, "content": "alpha beta", "title": "", "score": 0.9}, {"id": 2, "content": "unrelated", "title": "", "score": 0.2}]
    out = await edm_rerank(pool, "alpha beta")
    assert out, "статус-кво: non-deterministic путь жив"


# ── 2. ENGRAM procedural ─────────────────────────────────────────────────────


def test_procedural_is_14th_kind() -> None:
    assert len(MemoryKind) == 14
    assert validate_kind("procedural")
    p = get_policy("procedural")
    assert p.never_archive and p.decay_rate == 0.0


def test_procedural_never_archives() -> None:
    assert not can_archive("procedural", 0.0, days_since_update=9999)


def test_kind_for_text_procedural_markers() -> None:
    assert kind_for_text("Сделай так: скопируй файл и перезапусти сервис") == MemoryKind.PROCEDURAL
    assert kind_for_text("Порядок действий при деплое: сначала бэкап") == MemoryKind.PROCEDURAL
    assert kind_for_text("Инструкция по сборке: make && make install") == MemoryKind.PROCEDURAL
    assert kind_for_text("я обещаю сделать к пятнице") == MemoryKind.COMMITMENT, "старые маркеры не сломаны"


@pytest.mark.asyncio
async def test_procedural_routes_to_agent_layer() -> None:
    from lifecycle.distiller import distill_and_route

    from core import MemoryManager
    from graph.epistemic import EpistemicGraph
    from shared.connection import connection_manager

    mem = MemoryManager(cm=connection_manager).get_layer("user", "s17eng")
    graph = EpistemicGraph(cm=connection_manager, layer="user")
    text = "Сделай так: сначала прогони ruff, потом mypy, потом pytest"
    r = await distill_and_route(mem, graph, "s17eng", text, 0.9)
    assert r["l4_saved"] >= 1, "procedural роутится в L4 (decay 0)"
    conn = await connection_manager.get("memory.db")
    row = await (await conn.execute("SELECT layer FROM core_memory WHERE user_id='s17eng' AND key LIKE 'procedural:%' LIMIT 1")).fetchone()
    assert row is not None and row["layer"] == "agent", "procedural живёт в agent-слое"


# ── 3. AdaptiveRAG pre-gate ──────────────────────────────────────────────────


def test_query_features_27_keys() -> None:
    f = query_features("как настроить postgres?")
    assert len(f) == 27, f"ожидалось 27 фич, пришло {len(f)}"
    # исторический контракт первых 4 ключей стабилен
    assert f["is_question"] and f["has_entity"] and not f["is_enumerative"]
    assert f["length"] == 3


def test_query_features_groups() -> None:
    f = query_features("сделай так: проверь дедлайн 05.09 на сервере")
    assert f["is_imperative"] and f["has_date"] and f["has_deadline"]
    f2 = query_features("привет")
    assert f2["is_greeting"] and f2["is_short"]
    f3 = query_features("помнишь, мы вчера чинили дедлайн?")
    assert f3["wants_episodic"] and f3["has_relative_time"]


def test_gate_sources_new_rules() -> None:
    code_feat = query_features("почему тест падает с Traceback в run.py?")
    flags = gate_sources(code_feat)
    assert flags["episodic"] is False and flags["graph"] is False, "код-запрос: эпизоды/граф не релевантны"
    epi_feat = query_features("помнишь, мы вчера чинили дедлайн?")
    flags2 = gate_sources(epi_feat)
    assert flags2["episodic"] is True and flags2["wiki"] is False, "биографический запрос: эпизоды, не документация"


def test_pregate_flags_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "_data", {}, raising=False)
    assert pre_gate_flags("как настроить postgres?") == {}


def test_pregate_flags_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "_data", {"retrieval": {"pregate": True}}, raising=False)
    flags = pre_gate_flags("погода сегодня")
    assert flags == {"include_rag": True, "include_wiki": False, "include_episodic": False, "include_core": True}


# ── 4. A2 advisory similar_to ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_distiller_similar_to_on_conflict() -> None:
    from lifecycle.distiller import distill_and_route

    from core import MemoryManager
    from graph.epistemic import EpistemicGraph
    from shared.connection import connection_manager

    mem = MemoryManager(cm=connection_manager).get_layer("user", "s17a2")
    graph = EpistemicGraph(cm=connection_manager, layer="user")
    r1 = await distill_and_route(mem, graph, "s17a2", "решил: user works at Acme corp", 0.9)
    assert r1["l4_saved"] >= 1
    r2 = await distill_and_route(mem, graph, "s17a2", "решил: user works at Acme corp office", 0.9)
    assert r2["conflicts"] >= 1, "конфликт пойман ConflictResolver-ом"
    assert r2["similar_to"], "advisory similar_to заполнен при конфликте"


# ── 5. SHA-256 дедуп L0 ──────────────────────────────────────────────────────


@pytest.fixture
async def migrated_cm():
    """Hermetic global DB + полная схема (l0_journal и co-таблицы живут миграциями)."""
    from shared.connection import connection_manager
    from shared.migrations import MigrationManager

    await MigrationManager(cm=connection_manager).migrate()
    return connection_manager


@pytest.mark.asyncio
async def test_l0_dedup_returns_first_rid(migrated_cm) -> None:
    rid1 = await capture("new_message", "user", "s17dedup", "identical command output 12345")
    rid2 = await capture("new_message", "user", "s17dedup", "identical command output 12345")
    assert rid1 is not None and rid2 == rid1, "повтор → rid первоисточника"
    from shared.constants import DB_NAME

    conn = await migrated_cm.get(DB_NAME)
    n = (await (await conn.execute("SELECT COUNT(*) c FROM l0_journal WHERE user_id='s17dedup'")).fetchone())["c"]
    assert n == 1, "строка одна"


@pytest.mark.asyncio
async def test_l0_dedup_scoped_by_layer_user(migrated_cm) -> None:
    rid1 = await capture("new_message", "user", "u-one", "same text across layers 777")
    rid2 = await capture("new_message", "agent", "u-two", "same text across layers 777")
    assert rid1 != rid2, "дедуп ограничен (layer, user)"


@pytest.mark.asyncio
async def test_l0_dedup_keeps_chain_intact(migrated_cm) -> None:
    await capture("new_message", "user", "s17chain", "chain text AAA")
    await capture("new_message", "user", "s17chain", "chain text AAA")  # дедуп-хит
    await capture("new_message", "user", "s17chain", "chain text BBB")
    broken = await verify_chain()
    assert not broken, f"hash-chain цел после дедупа: {broken}"


# ── 6. zero-result минер ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_zero_result_journal_and_miner(migrated_cm) -> None:
    from lifecycle.graph_miners import log_zero_result, miner_zero_results
    from shared.constants import DB_NAME

    cm = migrated_cm
    await log_zero_result(cm, "user", "s17zr", "квантовый флюксор колеблется")
    await log_zero_result(cm, "user", "s17zr", "квантовый флюксор колеблется")
    conn = await cm.get(DB_NAME)
    n = (await (await conn.execute("SELECT COUNT(*) c FROM recall_zero_results")).fetchone())["c"]
    assert n == 2
    res = await miner_zero_results(cm, "user")
    assert res["edges"] >= 1, "повторный провальный запрос → вопрос-узел surfaced"


@pytest.mark.asyncio
async def test_zero_result_single_hit_not_surfaced(migrated_cm) -> None:
    from lifecycle.graph_miners import log_zero_result, miner_zero_results
    from shared.constants import DB_NAME

    cm = migrated_cm
    conn = await cm.get(DB_NAME)
    await conn.execute("DELETE FROM recall_zero_results")  # сессионная БД: изолируем журнал
    await conn.commit()
    await log_zero_result(cm, "user", "s17zr2", "единичный провал запроса XYZ")
    res = await miner_zero_results(cm, "user")
    assert res["edges"] == 0, "один провал — не сигнал"


# ── 7. counter-signal алиасы ─────────────────────────────────────────────────


def test_counter_signals_loader_default_empty() -> None:
    assert load_counter_signals() == {}, "default {} — механика нейтральна"


def test_counter_signal_pessimizes_not_drops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "_data", {"rag": {"counter_signals": {"гелиос": "заря"}}}, raising=False)
    hits = [
        {"id": 1, "title": "", "content": "проект Гелиос закрыт", "score": 0.8},
        {"id": 2, "title": "", "content": "проект Заря активен", "score": 0.8},
    ]
    out = _apply_counter_signals(hits, "расскажи про проекты")
    by_id = {h["id"]: h for h in out}
    assert by_id[1]["score"] == pytest.approx(0.8 * 0.7), "superseded-хит пессимизирован"
    assert by_id[1]["counter_signal"] is True
    assert by_id[2]["score"] == pytest.approx(0.8), "актуальный хит не тронут"
    assert len(out) == 2, "не дроп"


def test_counter_signal_query_mentioning_old_name_not_penalized() -> None:
    hits = [{"id": 1, "title": "", "content": "проект Гелиос закрыт", "score": 0.8}]
    out = _apply_counter_signals(hits, "что стало с Гелиос")
    assert out[0].get("counter_signal") is not True, "запрос про старое имя — пессимизации нет"


@pytest.mark.asyncio
async def test_route_query_applies_counter_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeRag:
        async def search(self, query, user_id="default", limit=10, **kw):
            return [
                {"id": 1, "title": "", "content": "проект Гелиос закрыт", "score": 0.8, "source": "rag"},
                {"id": 2, "title": "", "content": "проект Заря активен", "score": 0.8, "source": "rag"},
            ]

    monkeypatch.setattr(config, "_data", {"rag": {"counter_signals": {"гелиос": "заря"}}}, raising=False)
    hits = await route_query(_FakeRag(), "расскажи про проекты", limit=10)
    assert hits, "хиты не дропнуты"
    by_id = {h["id"]: h for h in hits}
    assert by_id[1]["score"] < by_id[2]["score"], "superseded-хит ниже актуального после полного пути"


# ── 8. EMA-гейт ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_save_ema_gate_blocks_below_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.adaptive import adaptive_threshold

    monkeypatch.setattr(adaptive_threshold, "_current_value", 0.99, raising=False)
    mem, graph = _FakeMem(), _FakeGraph()
    result = await auto_save_text(mem, graph, "u1", "нейтральный текст без признаков важности " + "x" * 40)
    assert result["saved_l3"] is False, "score ниже EMA-порога → ничего не сохраняется"
    assert not mem.saved


@pytest.mark.asyncio
async def test_auto_save_ema_gate_feeds_and_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    from shared.adaptive import adaptive_threshold

    monkeypatch.setattr(adaptive_threshold, "_current_value", 0.3, raising=False)
    mem, graph = _FakeMem(), _FakeGraph()
    text = "какое решение по кэшу? " + "x" * 80
    result = await auto_save_text(mem, graph, "u1", text)
    assert result["saved_l3"] is True, "score выше EMA-порога → L3 сохраняется"
    assert adaptive_threshold._current_value != 0.3, "EMA покормлена score-ом (gate-контракт)"


class _FakeL1:
    def get_recent(self, n: int = 10) -> list:
        return []


class _FakeL3:
    def __init__(self, store: list) -> None:
        self._store = store

    async def save(self, user_id: str, summary: str, weight: float, tags: list) -> int:
        self._store.append((user_id, summary, weight, tags))
        return 1


class _FakeMem:
    def __init__(self) -> None:
        self.l1 = _FakeL1()
        self.saved: list = []
        self.l3 = _FakeL3(self.saved)
        self.l4 = None

    async def remember(self, key: str, value: str, importance: float) -> int:
        return 2


class _FakeGraph:
    def __init__(self) -> None:
        self.nodes: list = []

    async def add_node(self, *a, **kw) -> int:
        return 7
