# Phase G — Self-Wiring Graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: compose:subagent per task. Steps use checkbox syntax.
> **Prerequisite:** Phase F завершена (6a4f45f): L0-журнал жив, distill_and_route работает.

**Goal:** Граф наполняется рёбрами детерминированно (8+2 минеров), без единого LLM-вызова, с тегированием источников, validity windows и трёхфазным ночным dream-циклом.

**Architecture:** Минеры читают существующие данные (epi_tags, FTS5, сессии, provenance, embedding-векторы), пишут рёбра в epi_edges с тегом `heuristic:<name>`. Инкрементальный режим при записи + ночной batch (graph_enrich в backup_cron). Dual-route retrieval: EDM/ITS primary + S2-exhaustive для enumerative.

**Tech Stack:** SQLite recursive CTE, sentence-transformers (уже в venv), epi_nodes/epi_edges/epi_tags, louvain (A1.6), backup_cron._nightly.

## Global Constraints

- Граф = multi-hop reranker ONLY (HippoRAG2: graph-augmented проигрывает dense на factual — graph-expand OFF на single-hop, D-Mem gated escalation)
- Каждое ребро тегировано `heuristic:<name>`; откат: DELETE WHERE edge_tags LIKE '%heuristic:%'
- Вес = доверие источнику (эвристика 0.3–0.6, ручные 0.8+); never_archive-факты и pinned не эвиктятся
- MOC-хубы/auto-indexes исключены из centrality (Ar9av: 44→83%)
- 65 тулов не растёт (минеры — внутренние функции, не тулы)

---

### Task 1: Фундамент — graph_enrich оркестратор + пре-чистка

**Covers:** S7

**Files:**
- Create: `lifecycle/graph_enrich.py` — оркестратор: `async def graph_enrich(layer: str = "user") -> dict` — вызывает минеров по порядку, возвращает статистику
- Modify: `hooks/user_hooks.py::_nightly` — добавить фазу `graph_enrich` после graph_build
- Modify: `lifecycle/compact.py` — одноразовая чистка: JSON-узлы (raw_type='tool_result'/'recall') из epi_nodes → l0_journal (восстановление из бэкапов опционально, вручную)
- Test: `tests/test_lifecycle/test_graph_enrich.py`

- [ ] **Step 1: Тест**: 236-узловая фикстура с замусоренными JSON-узлами → graph_enrich → JSON-узлы в L0, чистые узлы остались, рёбра от минеров появились.
- [ ] **Step 2: Реализация**: пре-чистка (JSON-узлы → capture() → delete_nodes) + скелет оркестратора (вызовы минеров — заглушки, заполнятся Tasks 2-5) + hook-проводка.
- [ ] **Step 3: Коммит** `feat(G1): graph_enrich orchestrator + node cleanup`.

---

### Task 2: Минеры #1/#2/#4 (существующие данные)

**Covers:** S8 (минеры 1,2,4)

**Files:**
- Create: `lifecycle/graph_miners.py` — минер-функции
- Test: `tests/test_lifecycle/test_graph_miners.py`

**Interfaces:**
- Produces: `async def miner_tags(cm, layer: str) -> dict`, `async def miner_tokens(cm, layer: str) -> dict`, `async def miner_sessions(cm, layer: str) -> dict` — каждая возвращает {'edges': n}.

- [ ] **Step 1: Тест**: 2 узла с общим тегом → ребро tagged; 2 узла с ≥2 общими редкими токенами → topic_overlap (Jaccard); 2 факта одной сессии → same_session. Вес = Jaccard/tag-каунт.
- [ ] **Step 2: Реализация**:
```python
async def miner_tags(cm, layer: str) -> dict:
    conn = await cm.get(DB_NAME)
    rows = await (await conn.execute("""
        SELECT t1.node_id, t2.node_id, COUNT(DISTINCT t1.tag) as shared
        FROM epi_tags t1 JOIN epi_tags t2 ON t1.tag = t2.tag AND t1.node_id < t2.node_id
        JOIN epi_nodes n1 ON n1.node_id = t1.node_id AND n1.layer = ?
        JOIN epi_nodes n2 ON n2.node_id = t2.node_id AND n2.layer = ?
        GROUP BY t1.node_id, t2.node_id HAVING shared > 0
    """, (layer, layer))).fetchall()
    edges = 0
    for a, b, w in rows:
        await conn.execute(
            "INSERT OR IGNORE INTO epi_edges (source_id, target_id, relation, weight, created_at, tags) "
            "VALUES (?, ?, 'tagged', ?, ?, ?)",
            (a, b, min(0.3 + 0.1 * w, 0.6), time.time(), json.dumps(['heuristic:tags'])))
        edges += 1
    await conn.commit()
    return {'edges': edges}
```
(токены/сессии — аналогично по FTS5-match и session-binding из L2-enrich.)
- [ ] **Step 3: Коммит** `feat(G2): miners #1 tags, #2 token-overlap, #4 sessions`.

---

### Task 3: Минер #5 провенанс-мосты + #7 co-retrieval журнал

**Covers:** S8 (минеры 5, 7)

**Files:**
- Modify: `features/recall.py` — журнал co-retrieval пар (после каждого recall: пары hit-ids → audit_trail или новая таблица)
- Create: `lifecycle/graph_miners.py::miner_provenance` (эпизод→wiki→факт через metadata.parents)
- Create: `lifecycle/graph_miners.py::miner_co_retrieval` (ночной: SELECT пар из журнала, COUNT ≥ 2 → ребро)
- Test: `tests/test_lifecycle/test_graph_miners.py` (добавить кейсы)

- [ ] **Step 1: Журнал**: после каждого recall записывать пары (node_id_a, node_id_b) hits в recall_co_pairs таблицу (или audit_trail JSON) — MINIMAL: одна колонка в существующем recall_events.
- [ ] **Step 2: Минер #5**: JOIN core_memory.metadata.parents → episodes → wiki (метадата wiki_id) → рёбра sourced_from.
- [ ] **Step 3: Минер #7**: SELECT pairs, HAVING count >= 2 → co_recalled с весом частота/окно.
- [ ] **Step 4: Коммит** `feat(G3): provenance bridges + co-retrieval miner`.

---

### Task 4: Минер #9 эмбеддинг + #3 сущности (spaCy) + инкрементальный режим

**Covers:** S8 (минеры 9, 3, инкрементальный режим)

**Files:**
- Modify: `lifecycle/graph_miners.py` — `miner_embedding` (Jaccard MIB ≥0.7, top-k=15), `miner_entities` (spaCy doc.ents → co_mentions)
- Modify: `lifecycle/distiller.py` — после роутинга лёгкие проверки (tags/entities/synonyms) — инкрементальный вызов минеров
- Test: расширить test_graph_miners.py

- [ ] **Step 1: Тест**: 2 узла с embedding-сходством ≥0.7 → semantic_overlap; текст с «Лили» в двух узлах → co_mentions после канонизации.
- [ ] **Step 2: Реализация**: embedding-минер — кодировать content+tags+aliases (A-MEM rich embedding), попарный Jaccard (O(n²) на 236 узлах — копейки), top-k=15; entity-минер — словарь (Лили/Люси/Hermes/SQLite...) + spaCy NER для латиницы.
- [ ] **Step 3: Коммит** `feat(G3): embedding + entity miners, incremental mode`.

### Task 4b: Минеры #6 маркеры led_to + #8 структурные инварианты

**Covers:** S8 (минеры 6, 8 — план-чейнджер: пропущены в v1 плана, поймано ревью 04.09)

**Files:**
- Modify: `lifecycle/graph_miners.py` — `miner_markers` (led_to), `miner_structural` (co-citation + louvain-расширение + belief propagation)
- Test: расширить test_graph_miners.py

**Interfaces:**
- Consumes: минеры #1/#2 уже наплели рёбер (для #8 co-citation нужны существующие связи); louvain из A1.6 (wiki_communities).
- Produces: рёбра `led_to` (вес 0.3, тег `heuristic:marker`), `co_cited` (вес = число общих цитирующих), буст importance целевых узлов (belief propagation).

- [ ] **Step 1: Тест #6 (маркеры)**: узел A про X (ts=T), узел B про X с маркером «починила/теперь работает/сломалось/переделали» (ts=T+N, N в [5мин, 30д]) → ребро A→B `led_to` weight=0.3, tags=['heuristic:marker']; без временной близости → ребра нет.
- [ ] **Step 2: Тест #8a (co-citation)**: узлы A и B, оба упомянуты в узле C → ребро A↔B `co_cited` weight=0.3; **Step 2b (belief propagation)**: confidence(A)=0.9, ребро A→B (weight 0.5) → UPDATE epi_nodes SET confidence = conf(B) + 0.1·conf(A)·w для B (одноразовый буст, не рекурсивный).
- [ ] **Step 3: Тест #8b (louvain-расширение)**: сообщества уже посчитаны A1.6 → внутри сообщества пары узлов БЕЗ рёбер, но с общим тегом, получают слабое ребро weight=0.2 `heuristic:community_bridge`.
- [ ] **Step 4: Реализация miner_markers**: SELECT пар узлов с общим токеном/сущностью, ts-дельта в [5мин, 30д], второй содержит маркер-словарь (починила|исправила|теперь работает|сломалось|переделали|решено|закрыто) → led_to.
- [ ] **Step 5: Реализация miner_structural**: co-citation (SELECT пар из общих third-party рёбер) + louvain-мосты (SELECT пар внутри сообщества без прямого ребра с общим тегом) + belief propagation (UPDATE confidence по входящим высоко-конфидентным рёбрам).
- [ ] **Step 6: Прогон GREEN + регресс test_graph_miners.py.**
- [ ] **Step 7: Коммит** `feat(G3): miner #6 led_to markers, #8 structural invariants`.

---

### Task 5: Санитария — inhibition, validity windows, MAD, valence, hub exclusion

**Covers:** S9

**Files:**
- Create: `lifecycle/graph_sanitation.py`
- Test: `tests/test_lifecycle/test_graph_sanitation.py`

- [ ] **Step 1: Lateral inhibition** (SYNAPSE формула): û_i = max(0, u_i − β·Σ(u_k−u_i)·𝕀[u_k>u_i]), β=0.15, M=7 — поверх весов рёбер минеров.
- [ ] **Step 2: Validity windows**: колонки valid_from/valid_to на epi_edges (alembic) + derived_from/coupled_with typed edges + O(|E|) recheck propagation (StateMem).
- [ ] **Step 3: MAD-пороги**: τ = median − κ·MAD per-miner вместо fixed.
- [ ] **Step 4: Valence-typed** (prism 10 типов) + volatility-классы (RoMem: predicate-type таблица).
- [ ] **Step 5: Hub exclusion**: WHERE node_type NOT IN ('moc','auto_index') в centrality/louvain.
- [ ] **Step 6: Коммит** `feat(G): graph sanitation — inhibition, validity, MAD, valence, hub exclusion`.

---

### Task 6: Dual-route retrieval (EDM/ITS + S2 exhaustive + D-Mem escalation)

**Covers:** S10

**Files:**
- Modify: `rag/search.py` / `rag/multi_source.py` — EDM re-rank (α·R+β·N+γ·G−δ·K), ITS threshold-gating (min-max, k≤100), lateral inhibition pre-step
- Create: `rag/dual_route.py` — question-type router (factual→RRF/EDM; enumerative→S2 exhaustive; multi-hop→D-Mem escalation)
- Test: `tests/test_rag/test_dual_route.py`

- [ ] **Step 1: Тест**: factual-вопрос → RRF/EDM (graph-expand OFF); «list all X» → S2-exhaustive; precision-замер (Tenure-стиль: не должен вернуть чужой скоуп).
- [ ] **Step 2: EDM re-rank реализация**: α·R (FTS-rank) + β·N (set-операции: термы запроса минус покрытые) + γ·G (led_to-цепочки) − δ·K (semantic-dedup cosine) — нормализация per-query min-max.
- [ ] **Step 3: S2-exhaustive**: категория → агрегатная summary → полный спуск детей (без top-k).
- [ ] **Step 4: Коммит** `feat(G): dual-route retrieval — EDM/ITS, S2 exhaustive, escalation`.

---

### Task 7: ENGRAM-абляция arm (для №11, но код здесь)

**Covers:** S10 (ablation arm B)

**Files:**
- Create: `rag/ablation.py` — конфиг-переключатели: `RETRIEVAL_MODE = 'rrf' | 'dense_per_kind' | 'gated'`
- Test: параметры читаются, arms переключаются

- [ ] **Step 1: Реализация**: env/config флаг → multi_source включает/выключает источники; dense-per-kind arm = search по kind_поля без фьюжена.
- [ ] **Step 2: Коммит** `feat(H-prep): retrieval ablation arms`.

---

## Self-Review
- S7→T1, S8→T2-T4, S9→T5, S10→T6-T7. S11-S14 → Phase H plan. S2-S6 → Phase F (выполнено).
- Deferred из F: per-kind caps + precedence rules инъекции → включить в Task 6 (dual-route).
