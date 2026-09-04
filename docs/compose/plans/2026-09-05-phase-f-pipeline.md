# Phase F — L0 Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use compose:subagent (recommended) or compose:execute to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Единый приёмник L0 (raw append-only журнал) с гейтами G0/G1/G2: privacy-плейсхолдеры + NER, kind-роутинг инвариант/событие, канонические ключи, конфликт-контракт, watermark + replay, session-close, TTL-тиры.

**Architecture:** `l0_journal` (SQLite, append-only) — единственная дверь для 4 потоков записи. Каждый вход пишет в L0 best-effort, затем гейты G0→G1→G2 пишут вердикты в `decisions JSON`. Дистиллятор без LLM: атомизация → типизация → канонический ключ → роутинг (инварианты→L4, события→L3). Watermark курсор + replay переигрывают гейты по окну.

**Tech Stack:** Python 3.11, SQLite/aiosqlite, alembic (head a26), pydantic, FTS5, existing: hooks/external.py `auto_save_text` (главный писатель), `shared/memory_types.py` (13 kinds, TypePolicy.decay_rate), `rag/conflict.py` (ConflictResolver), `shared/adaptive.py` (EMA), `features/rules.py`.

## Global Constraints

- 65 тулов (не добавлять новые в этой фазе; счётчики в tests/test_features/test_disclosure.py:106, tests/test_mcp/test_diagnostics.py:104, tests/test_mcp/test_recall_tool.py:9)
- alembic-цепочка линейная; новая миграция = ребёнок `20260903_1400_a26`
- Python 3.11, SQLite/aiosqlite, local-first (без облачных зависимостей)
- Conftest: `ARIEL_HASH_EMBEDDINGS=1` уже стоит; `hermetic_global_db` (session) редиректит connection_manager; в тестах патчить `.base_dir`, НЕ подменять singleton-объект (from-import ловит impostor навсегда)
- Ночной контекст: `deterministic_gate_and_registry` autouse фикстура существует
- Pre-push gate: ruff check . && ruff format --check . && mypy (10 dirs) && pytest --junitxml (верdict по junit)
- Все тесты: `ARIEL_HASH_EMBEDDINGS=1 uv run --with-editable . --extra dev pytest ...`

---

### Task 1: l0_journal — миграция + запись (capture)

**Covers:** S1

**Files:**
- Create: `alembic/versions/20260905_1000_f10_l0_journal.py`
- Create: `shared/l0.py`
- Test: `tests/test_shared/test_l0_journal.py`

**Interfaces:**
- Produces: `shared/l0.py::async def capture(event: str, layer: str, user_id: str, text: str, *, source_msg_id: int | None = None, raw_type: str | None = None, decisions: list[dict] | None = None) -> int | None` (best-effort, никогда не бросает); `shared/l0.py::def classify_raw(text: str) -> str` (tool_result/tool_use/recall/evolution/user-message/plain).

- [ ] **Step 1: Миграция**

```python
"""F1 — l0_journal: raw append-only intake."""
import contextlib
from alembic import op
revision: str = "20260905_1000_f10"
down_revision = "20260903_1400_a26"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.execute("""
    CREATE TABLE IF NOT EXISTS l0_journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        event TEXT NOT NULL,
        source_msg_id INTEGER,
        layer TEXT NOT NULL DEFAULT 'user',
        user_id TEXT NOT NULL DEFAULT 'default',
        text TEXT NOT NULL,
        raw_type TEXT NOT NULL DEFAULT 'plain',
        status TEXT NOT NULL DEFAULT 'received',
        decisions TEXT NOT NULL DEFAULT '[]',
        processed_at REAL,
        hash_prev TEXT NOT NULL DEFAULT '',
        hash_self TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_l0_user_ts ON l0_journal(user_id, ts);
    CREATE INDEX IF NOT EXISTS idx_l0_status ON l0_journal(status);
    """)

def downgrade() -> None:
    with contextlib.suppress(Exception):
        op.execute("DROP TABLE IF EXISTS l0_journal")
```

- [ ] **Step 2: Failing test** (`tests/test_shared/test_l0_journal.py`)

```python
import pytest
from shared.connection import AsyncConnectionManager, connection_manager
from shared.migrations import MigrationManager

@pytest.fixture
async def cm(tmp_path):
    connection_manager.base_dir = tmp_path  # НЕ подменять объект!
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()

@pytest.mark.asyncio
async def test_capture_writes_row_and_classifies(cm, tmp_path):
    from shared.l0 import capture, classify_raw
    assert classify_raw('[{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]') == "tool_result"
    assert classify_raw('{"type": "tool_use", "name": "f", "input": {}}') == "tool_use"
    assert classify_raw('[ariel recall]\n- [session] x') == "recall"
    assert classify_raw('[EVOLUTION] sweep done') == "evolution"
    assert classify_raw('обычное сообщение про проект') == "user-message"
    rid = await capture("new_message", "user", "u1", "помни: я решил перейти на wal")
    assert rid is not None
    row = await (await (await cm.get("memory.db")).execute("SELECT raw_type, status FROM l0_journal WHERE id=?", (rid,))).fetchone()
    assert row[0] == "user-message" and row[1] == "received"

@pytest.mark.asyncio
async def test_capture_never_raises(cm):
    from shared.l0 import capture
    rid = await capture("new_message", "user", "u1", "x", raw_type=None)
    assert rid is not None  # даже с None raw_type — классифицирует сам
```

- [ ] **Step 3: Реализация `shared/l0.py`**

```python
"""L0 raw intake — единственный вход конвейера (append-only, best-effort)."""
from __future__ import annotations
import json, time, hashlib
from typing import Any
from shared.connection import connection_manager
from shared.constants import DB_NAME

async def capture(event: str, layer: str, user_id: str, text: str, *,
                  source_msg_id: int | None = None, raw_type: str | None = None,
                  decisions: list[dict] | None = None) -> int | None:
    """Append-only intake. Никогда не бросает — сбой L0 не блокирует поток."""
    try:
        conn = await connection_manager.get(DB_NAME)
        cur = await conn.execute(
            "INSERT INTO l0_journal (ts, event, source_msg_id, layer, user_id, text, raw_type, status, decisions)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'received', ?)",
            (time.time(), event, source_msg_id, layer, user_id, text,
             raw_type or classify_raw(text), json.dumps(decisions or [], ensure_ascii=False)),
        )
        await conn.commit()
        return int(cur.lastrowid or 0)
    except Exception:
        return None

def classify_raw(text: str) -> str:
    t = text.strip()
    if t.startswith("[{") or t.startswith('{"'):
        try:
            obj = json.loads(t) if not t.startswith("[{") else json.loads(t)
            if isinstance(obj, dict) and obj.get("type") == "tool_result":
                return "tool_result"
            if isinstance(obj, dict) and obj.get("type") == "tool_use":
                return "tool_use"
        except ValueError:
            pass
        return "tool_result" if "tool_use_id" in t[:200] else "plain"
    for prefix in ("[ariel recall]", "[ariel memory]", "[ariel proposals]"):
        if t.startswith(prefix):
            return "recall"
    if t.startswith("[EVOLUTION]"):
        return "evolution"
    if "tool_use_id" in t[:200]:
        return "tool_result"
    return "user-message"
```

- [ ] **Step 4: Коммит** `git add -A && git commit -m "feat(F1): l0_journal table + capture/classify intake"`

---

### Task 2: G0 privacy gate — placeholders + NER (spaCy)

**Covers:** S2 (G0)

**Files:**
- Modify: `mcp_server/utils/privacy.py` (добавить `sanitize()` поверх strip_secrets)
- Modify: `hooks/external.py:auto_save_text` — capture + sanitize на входе
- Test: `tests/test_hooks/test_privacy_gate.py`

**Interfaces:**
- Produces: `mcp_server/utils/privacy.py::def sanitize(text: str, *, use_ner: bool = True) -> tuple[str, dict[str, str]]` → (де-идентифицированный текст, reverse-map). Placeholders: `⟨EMAIL_1⟩`, `⟨PERSON_2⟩` — same value → same index.

- [ ] **Step 1: Зависимость.** `uv add spacy && uv run python -m spacy download en_core_web_sm` (модель 12MB; лицензии — ядро MIT, модель CC BY-SA; атрибуция в NOTICE — отдельный шаг Task 8).

- [ ] **Step 2: Failing test** (`tests/test_hooks/test_privacy_gate.py`)

```python
from mcp_server.utils.privacy import sanitize

def test_placeholder_stability():
    t = "Email ann@example.com и потом снова ann@example.com"
    out, m = sanitize(t, use_ner=False)
    assert "ann@example.com" not in out
    assert out.count("⟨EMAIL_1⟩") == 2  # same value → same placeholder
    assert m["⟨EMAIL_1⟩"] == "ann@example.com"

def test_ner_masks_person_and_org():
    out, m = sanitize("Аня работает в Acme Corp", use_ner=True)
    assert "Аня" not in out and "Acme Corp" not in out

def test_prose_not_destroyed():
    out, m = sanitize("Кисонька впервые вышла на прогулку", use_ner=True)
    assert "Кисонька" in out  # не персона по словарю — сохраняется
```

- [ ] **Step 3: Реализация** — в `mcp_server/utils/privacy.py` добавить (lazy NER, process-wide):

```python
_nlp = None
def _get_nlp():
    global _nlp
    if _nlp is None:
        import spacy
        _nlp = spacy.load("en_core_web_sm")  # ru-проза не парсится NER'ом — ок, regex-тир ловит structured
    return _nlp

_NER_LABELS = {"PERSON", "ORG", "GPE", "LOC"}

def sanitize(text: str, *, use_ner: bool = True) -> tuple[str, dict[str, str]]:
    """Replace secrets/PII with stable typed placeholders. Reverse map не персистится."""
    import re
    out, mapping = text, {}
    patterns = [(r"[\w.+-]+@[\w-]+\.[\w.]+", "EMAIL"), (r"\+?\d[\d\s()-]{8,}\d", "PHONE"),
                (r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "IP")]
    def _sub(m):
        val = m.group(0)
        key = f"⟨{kind}_{sum(1 for k in mapping if k.startswith(f'⟨{kind}_'))+1}⟩"
        mapping[key] = val
        return key
    for pattern, kind in patterns:
        out = re.sub(pattern, lambda m: _sub_m(mapping, m.group(0), kind), out)
    if use_ner:
        try:
            doc = _get_nlp()(out)
            for ent in reversed(doc.ents):
                if ent.label_ in _NER_LABELS and ent.text not in mapping.values():
                    key = f"⟨{ent.label_}_{len(mapping)+1}⟩"
                    mapping[key] = ent.text
                    out = out[:ent.start_char] + key + out[ent.end_char:]
        except Exception:
            pass  # NER недоступен — regex-тир уже отработал
    return out, mapping

def _sub_m(mapping: dict, val: str, kind: str) -> str:
    for k, v in mapping.items():
        if v == val:
            return k
    key = f"⟨{kind}_{len(mapping)+1}⟩"
    mapping[key] = val
    return key
```

- [ ] **Step 4: Wire в `hooks/external.py::auto_save_text`** — после transcript-guard, ДО evaluate_importance:

```python
    # G0 privacy: secrets/PII → placeholders (reverse map не персистится)
    from mcp_server.utils.privacy import sanitize
    text, _priv_map = sanitize(text)
```
(текст ниже по потоку уже де-идентифицирован; result получает `_priv_map` только для in-memory restore при необходимости).

- [ ] **Step 5: Коммит** `git commit -m "feat(F-G0): privacy placeholders + spaCy NER tier"`

---

### Task 3: G1 дистиллятор — атомизация + канонические ключи + kind-роутинг

**Covers:** S2 (G1), S6a-принципы

**Files:**
- Create: `lifecycle/distiller.py`
- Modify: `hooks/external.py::auto_save_text` — replace `mem.l3.save(...)`+`graph.add_node` блок на distiller-вызов
- Test: `tests/test_features/test_distiller.py`

**Interfaces:**
- Produces: `lifecycle/distiller.py::@dataclass Atom(clause: str, kind: MemoryKind, importance: float, key: str)`; `async def distill_and_route(mem, graph, user_id: str, text: str, score: float, *, event: str) -> dict[str, Any]` (routes: L4 вvariants с conflict-check, L3 events; returns {"l4_saved": n, "l3_saved": n, "conflicts": n}).
- Consumes: `kind_for_text` (shared/memory_types), `adaptive_threshold.gate`, `apply_rules` (features/rules), `ConflictResolver.check` (rag/conflict).

- [ ] **Step 1: Failing test**

```python
import pytest
from shared.connection import AsyncConnectionManager
from shared.migrations import MigrationManager

@pytest.fixture
async def cm(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)  # from shared.connection import connection_manager
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()

@pytest.mark.asyncio
async def test_invariant_routes_to_l4_event_to_l3(cm):
    from lifecycle.distiller import distill_and_route
    from core.memory import CoreMemory
    l4 = CoreMemory(cm=cm, layer="user")
    # инвариант: «решила» = decision → L4
    r1 = await distill_and_route(fake_mem, fake_graph, "u1", "я решила перейти на PostgreSQL", 0.8)
    assert r1["l4_saved"] >= 1
    # событие: «наблюдаю» = observation → L3
    r2 = await distill_and_route(fake_mem, fake_graph, "u1", "наблюдение: трафик растёт по пятницам", 0.6)
    assert r2["l3_saved"] >= 1
    rows = await (await (await cm.get("memory.db")).execute("SELECT key FROM core_memory WHERE user_id='u1'")).fetchall()
    assert all(not k[0].startswith("staging_") for k in rows), "ключи канонические, не обрубки"

@pytest.mark.asyncio
async def test_conflict_not_silent_update(cm):
    r1 = await distill_and_route(fake_mem, fake_graph, "u1", "база проекта: PostgreSQL", 0.8)
    r2 = await distill_and_route(fake_mem, fake_graph, "u1", "база проекта: MySQL", 0.8)
    assert r2["conflicts"] >= 1  # второе противоречит первому — запись, не затирание
```

- [ ] **Step 2: Реализация `lifecycle/distiller.py`** (ядро):

```python
"""G1 distiller: atomize → type → canonical key → route (invariant→L4, event→L3)."""
from __future__ import annotations
import re
from dataclasses import dataclass
from shared.memory_types import MemoryKind, get_policy, kind_for_text

_CLAUSE_SPLIT = re.compile(r"[,;]?\s+(?:и|но|причём|а|хотя)\s+|\.\s+")

@dataclass
class Atom:
    clause: str
    kind: MemoryKind
    importance: float
    key: str

def _canonical_key(clause: str, kind: MemoryKind) -> str:
    from rag.synonyms import load_synonyms
    words = re.findall(r"[а-яёa-z0-9]+", clause.lower())
    canon = [w for w in words if len(w) > 2][:4]
    return f"{kind.value}:" + "_".join(canon) if canon else f"{kind.value}:misc"

def atomize(text: str) -> list[str]:
    parts = _CLAUSE_SPLIT.split(text.strip())
    return [p.strip() for p in parts if len(p.strip()) >= 8][:10]

def route_kind(kind: MemoryKind) -> str:
    """Invariant→l4, event→l3 — по TypePolicy.decay_rate (0=никогда не умирает)."""
    return "l4" if get_policy(kind).decay_rate <= 0.005 else "l3"

async def distill_and_route(mem, graph, user_id: str, text: str, score: float, *, event: str = "new_message") -> dict:
    from core.memory import CoreMemory
    from rag.conflict import ConflictResolver
    cmem = CoreMemory(cm=mem._cm if hasattr(mem, "_cm") else None, layer="user") if mem is None else mem
    stats = {"l4_saved": 0, "l3_saved": 0, "conflicts": 0}
    resolver = ConflictResolver()
    for clause in atomize(text):
        kind = kind_for_text(clause)
        key = _canonical_key(clause, kind)
        target = route_kind(kind)
        conflict = await resolver.check(user_id, clause)
        if target == "l4":
            if conflict and getattr(conflict, "has_conflict", False):
                stats["conflicts"] += 1
                await cmem.save(user_id, key, clause, importance=score,
                                memory_kind=kind.value, source=f"{event}:contradiction",
                                metadata={"contradiction": True})
                continue
            await cmem.save(user_id, key, clause, importance=score, memory_kind=kind.value, source=event)
            stats["l4_saved"] += 1
        else:
            await mem.l3.save(user_id, clause[:500], score, [event, kind.value])
            stats["l3_saved"] += 1
    return stats
```

- [ ] **Step 3: Wire в `auto_save_text`** — заменить блок `await mem.l3.save(...)` + `graph.add_node(...)` (hooks/external.py:158-161) на:

```python
    from lifecycle.distiller import distill_and_route
    route_stats = await distill_and_route(mem, graph, user_id, text, score)
    result["saved_l3"] = route_stats["l3_saved"] > 0
    result["saved_graph"] = route_stats["l3_saved"] > 0  # граф наполняют минеры, не прямая запись
    result["routes"] = route_stats
```
(remove `await mem.l3.save(...)` и `await graph.add_node(...)` — remember тоже перестаёт дублировать: отдельный шаг Task 7.)

- [ ] **Step 4: Коммит** `git commit -m "feat(F-G1): distiller — atomize, canonical keys, kind-routing, conflicts"`

---

### Task 4: Compact-to-budget + priority signal

**Covers:** S2 (compact-to-budget, priority)

**Files:**
- Modify: `shared/memory_types.py:41` — TypePolicy add `retrieval_priority: float = 0.5` (per-kind: fact/decision 0.9, preference 0.6, context 0.1...)
- Create: `lifecycle/compact.py` — `async def compact_under_budget(user_id: str, layer: str, budget: int = 500) -> dict`
- Test: `tests/test_lifecycle/test_compact.py`

- [ ] **Step 1: TypePolicy priority** — добавить поле `retrieval_priority: float = 0.5`, задать per-kind в `_POLICIES` (fact 0.9, decision 0.9, commitment 0.8, context 0.1, observation 0.3...).
- [ ] **Step 2: compact_under_budget**: SELECT importance + ACT-R activation → если объём > budget: эвикт lowest activation до <= budget; **никогда не эвиктит never_archive**. Возврат {"evicted": n}.
- [ ] **Step 3: Ночная привязка** — вызов из backup_cron._nightly после graph_build (1 строка).
- [ ] **Step 4: Тест**: 600 фактов → compact_under_budget(500) → 500 осталось, never_archive не тронут.
- [ ] **Step 5: Коммит** `feat(F): compact-to-budget + retrieval priority signal`

---

### Task 5: TTL-параметр + B5 защиты свипа

**Covers:** S6 (B4/B5)

**Files:**
- Modify: `mcp_server/tools/memory.py::memory_remember` — параметр `ttl_minutes: int = 0` (0=без TTL) → expires_at = now + ttl*60
- Create: `lifecycle/l0_sweep.py` — `async def sweep_expired(*, min_remain: int = 50, stop_pct: float = 0.8) -> dict` (delete expires_at < now; protections; cleaner_summary)
- Test: `tests/test_lifecycle/test_ttl_sweep.py`

- [ ] **Step 1: Тест**: remember с ttl_minutes=1 → expires_at заполнен; sweep удаляет expired, но не трогает fresh (min_remain) и останавливается если expired >80% (anti-mass-delete); cleaner_summary возвращён.
- [ ] **Step 2: Реализация** sweep: `DELETE WHERE expires_at < now` с проверками `COUNT(*) remaining >= min_remain` и `expired_pct < 0.8`; summary → l0_journal.
- [ ] **Step 3: Коммит** `feat(F): ttl param + protected expiry sweep`

---

### Task 6: Watermark + Replay

**Covers:** S4

**Files:**
- Create: `features/replay.py` — `async def replay(*, since_days: int = 7, gate: str = "g1") -> dict`
- Modify: `hooks/external.py` — после蒸馏: UPDATE l0_journal SET status='saved_l3', processed_at=? WHERE id=?
- Test: `tests/test_features/test_replay.py`

- [ ] **Step 1: Тест**: capture 3 raw → replay(since_days=1) → processed_at заполнены, повторный replay — no-op (config-hash key); порог изменился → replay со status='gated_out' переобрабатывает.
- [ ] **Step 2: Реализация**: выборка `WHERE ts > cutoff AND (status='received' OR status='gated_out')`, прогон через distill_and_route с новыми порогами, config-hash (hash текущих порогов) в decisions.
- [ ] **Step 3: Коммит** `feat(F): replay — gate re-run over L0 window`

---

### Task 7: Session-close + L2 enrichment + E6 lessons

**Covers:** S2 (session-close, E6), S3 (L2 enrichment)

**Files:**
- Modify: `hooks/user_hooks.py::_session_ended` — добавить extraction
- Create: `features/session_close.py` — `async def extract_and_stage(...)` (preferences/experience/lessons → staging)
- Test: `tests/test_features/test_session_close.py`

- [ ] **Step 1: Тест**: session_ended с текстами сессии → staging получает preference/experience/anti_pattern записи (A5-regex: «предпочитаю», «оказалось что», «больше не делать»).
- [ ] **Step 2: L2 enrichment**: `features/l2_enrich.py::async def enrich_sessions(days=1)` — L0-записи окна биндятся к sessions по ts, summary пересобирается из фактических текстов (not just state_deltas).
- [ ] **Step 3: E6**: failures → `kind=error_pattern` записи с тегом `lesson` (anti_patterns producer).
- [ ] **Step 4: Коммит** `feat(F3): session-close extraction, L2 enrichment, E6 lessons`

---

### Task 8: CLI-точки + NOTICE + финальный gate

**Covers:** S4 (replay CLI entry), S12 (атрибуция)

**Files:**
- Create: `scripts/l0_cli.py` — argparse: `replay --since N --gate g1`, `sweep`, `stats` (статусы L0)
- Modify: `NOTICE` — spaCy MIT + model CC BY-SA attribution
- Test: ручной smoke + существующий сьют

- [ ] **Step 1: CLI** (thin wrapper над features/replay.py + l0 stats).
- [ ] **Step 2: NOTICE**: `spacy: MIT; en_core_web_sm: CC BY-SA 4.0 (Models by Explosion AI)` .
- [ ] **Step 3: Полный pre-push gate** (все 4 команды, verdict по junit).
- [ ] **Step 4: Коммит + push** `feat(F): cli entry points + NOTICE attribution`.

---

## Self-Review checklist (заполняется при написании)

- Spec coverage: S1→T1, S2(G0)→T2, S2(G1)→T3, S2(compact/priority)→T4, S6(B4/B5)→T5, S4→T6, S2(session-close/E6)+S3→T7, S4(CLI)/S12(NOTICE)→T8. S5 (wiki) — сознательно НЕ в этом плане: wiki-механики (rewrite-not-append, redirect-stubs) идут отдельным планом после F (зависимость от [[fact:]] планирования). S0/S6a/S6b — principles/классификатор внутри T1/T2. S7-S10 (граф) — Phase G plan. S11-S14 (eval/infra) — Phase H plan. S15 — волновой порядок соблюдён.
- Типы: Atom/kind_for_text/distill_and_route согласованы между T3-шагами; capture/classify_raw между T1-T2.
- Placeholder-скан: чисто.

## Execution Handoff

Plan сохранён: `docs/compose/plans/2026-09-05-phase-f-pipeline.md`. После аппрува design doc → выполнение (subagent per task — рекомендую: задачи независимы, TDD-циклы изолированы).
