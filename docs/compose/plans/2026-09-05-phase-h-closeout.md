# Phase H-Closeout — Zero-Deferred Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: compose:subagent per task. Checkbox syntax.
> **Prerequisite:** Phase G+H завершены (головной 89a40c0). Аудит general-44: 3 разрыва + ~15 S-якорей + 10 заплаток.
> **Принцип (user 2026-09-05):** ZERO-DEFERRED, ZERO-PAYOFFS — чистый отлаженный проект, не костыли.

**Goal:** Все пропущенные S-якоря реализованы, все заплатки устранены, prod-wire завершён.

**Architecture:** Wire-фиксы (Task 1-2) подключают построенное к прод-пути. Минеры (Task 3-4) достраивают граф. Privacy/NLP (Task 5) усиливает гейт. Детерминизм (Task 6) + CLACK (Task 7) + eval-метрики (Task 8) закрывают дизайн.

**Tech Stack:** существующий стек + networkx, sqlite-vec (опция), ru-NER словарь.

## Global Constraints

- Полный pre-push gate после каждого Task (ruff × 2, mypy 10 dirs, pytest junit)
- 65 тулов не растёт (все новые функции — внутренние или CLI)
- 1254 тестов — база; каждый Task не должен ломать
- Взвешенный Hamming и прочие квантовые скальпели — применить к rag/quantize.py
- NEVER break существующие тесты: если тест противоречит новому контракту — адаптировать тест (пометить)

---

### Task C1: Wire dual-route в prod + enrich_sessions в cron + agent-layer

**Covers:** S10 (route_query в prod), S3 (l2_enrich), S7 (agent-layer nightly)

**Files:**
- Modify: `rag/multi_source.py::MultiSourceRAG.search` — если RETRIEVAL_MODE in (full/dense_per_kind/gated): route_query как пре-дверь, RRF = генератор кандидатов для EDM re-rank
- Modify: `features/backup_cron.py::_fire_nightly_hooks` — добавить `enrich_sessions(days=1)` (после sweep)
- Modify: `hooks/agent_hooks.py` — добавить `nightly` хук (mirror user_hooks._nightly: graph_enrich + компакт + свип)
- Test: `tests/test_rag/test_prod_wire.py` — RETRIEVAL_MODE='full' в prod-пути memory_search → EDM-порядок отличается от сырого RRF; enrich_sessions вызывается backup_cron

- [ ] **Step 1: Failing test**: memory_search с включённым full-режимом → результаты отсортированы по EDM (не по raw RRF); enrich_sessions(mock L0) → sessions.summary обновлён.
- [ ] **Step 2: Реализация**: multi_source.search → `if get_mode() != 'legacy': route = await route_query(...)` + EDM re-rank результатов; backup_cron добавляет enrich_sessions; agent_hooks._nightly (первый в agent_hooks!).
- [ ] **Step 3: Коммит** `feat(HC1): wire dual-route to prod, l2_enrich to cron, agent-layer nightly`.

---

### Task C2: import_chat ts + hash-chain writer + min_remain уже есть

**Covers:** S1 (hash-chain), H3-fix (ts)

**Files:**
- Modify: `scripts/import_chat.py` — ts из экспортов (claude: message.created_at; chatgpt: message.create_time; jsonl: ts поле; memory-json: ts) → capture(source_msg_id=None, decisions=[{'orig_ts': ts}]) → l0_journal.ts = orig_ts (не now)
- Modify: `shared/l0.py::capture` — параметр `ts_override: float | None = None`; hash-chain: hash_self = sha256(hash_prev + text + raw_type + str(ts))
- Test: `tests/test_shared/test_l0_chain.py` — импорт с ts=1.0 → l0_journal.ts == 1.0; hash-цепочка последовательна (hash_self[i] = sha256(hash_self[i-1]+...))

- [ ] **Step 1: Тест**: import с orig_ts → l0.ts == orig_ts; последовательная запись → hash-chain verify: пересчитать chain, битых нет.
- [ ] **Step 2: Реализация**: capture(ts_override) — ts = orig_ts if ts_override else time.time(); hash-chain: SELECT hash_self FROM l0_journal ORDER BY id DESC LIMIT 1 → hash_prev; hash_self = sha256(prev+text). import_chat парсит ts → ts_override.
- [ ] **Step 3: Коммит** `feat(HC2): import preserves orig_ts; L0 hash-chain tamper-evidence`.

---

### Task C3: Минеры #10 триплет-минер + behavior-аннотации (S6b)

**Covers:** S6b (триплеты query→tool→outcome, behavior-аннотации)

**Files:**
- Create: `lifecycle/graph_miners.py::miner_tool_triplets` — tool_use + tool_result пары (l0_journal, связка по tool_use_id) → узлы (query/action/outcome) → рёбра `query_tool`/`tool_outcome`
- Create: `lifecycle/tool_stats.py` — per-tool статистика (call count, error rate, avg outcome) → JSON/таблица для Stage 2 MCP hints
- Test: `tests/test_lifecycle/test_tool_triplets.py`

- [ ] **Step 1: Тест**: L0 с tool_use(id=t1) + tool_result(tool_use_id=t1) → miner → узлы + рёбра; behavior-статистика: 3 вызова, 1 ошибка → error_rate 0.33.
- [ ] **Step 2: Реализация**: triplet-минер — SELECT l0_journal пары по tool_use_id (raw_type='tool_use'/'tool_result') → find_or_add узлов → рёбра. tool_stats: SELECT по event/raw_type → JSON-отчёт.
- [ ] **Step 3: Wire** в graph_enrich MINERS + behavior-stats в report.
- [ ] **Step 4: Коммит** `feat(G-miner10): tool triplets + behavior annotations data`.

---

### Task C4: Privacy ru-тир — словарь персон + condition-splitting repair

**Covers:** S2 (privacy: en-NER gap на русском), conflicts (condition-splitting)

**Files:**
- Modify: `mcp_server/utils/privacy.py` — ru-словарный тир: известные персоны проекта (Лили/Люси/Аня/Эли/мамочка + синонимы из rag.synonyms) маскируются как ⟨PERSON_ru_N⟩; threshold-конфиг
- Modify: `lifecycle/distiller.py` — conflict → condition-splitting: обе записи сохраняются с scope-условием в metadata (не просто contradiction-флаг)
- Test: `tests/test_hooks/test_privacy_ru.py`, расширить test_distiller.py

- [ ] **Step 1: Тест**: «Мамочка сказала перейти на PostgreSQL» → «Мамочка» замаскирован; «Лили красива» → «Лили» замаскирован (sentence-initial!); condition-splitting: конфликтующие факты обе сохранены с metadata.scope условиями.
- [ ] **Step 2: Реализация**: privacy: `RU_PERSONAS` словарь (load from config rag.ru_personas + синонимы) → regex word-boundary; distiller: при conflict → обе записи (не одна с contradiction), metadata.scope = «до X» / «после X» / source-context.
- [ ] **Step 3: Коммит** `feat(F-G0): ru persona tier + condition-splitting repair`.

---

### Task C5: Eval-метрики расширение + memory_kind бэкфилл

**Covers:** S11 (NDCG, drift, negative-controls, two-judge), S13 (estate-quality)

**Files:**
- Modify: `eval/harness.py` — NDCG@k, drift score (old/new confusion), negative-control протокол (shuffled-edge должен падать), two-judge (proxy + второй fuzzy)
- Create: `scripts/backfill_memory_kind.py` — rag_chunks.memory_kind бэкфилл по kind_for_text
- Test: расширить test_eval

- [ ] **Step 1: Тест**: NDCG на mini (perfect ranking = 1.0); drift: KU-пара — old-answer после update = fail; negative-control: shuffled-arms должны давать accuracy < real arms.
- [ ] **Step 2: Реализация**: NDCG формула (DCG/IDCG), drift = old-value в ответе на KU, negative-control — режим report.py с shuffled arms.
- [ ] **Step 3: Бэкфилл**: скрипт rag_chunks → memory_kind по kind_for_text(chunk.content) → UPDATE. Помечено в docstring для ingestor (future fill).
- [ ] **Step 4: Коммит** `feat(H-eval): NDCG/drift/negative-controls + memory_kind backfill`.

---

### Task C6: S9-хвосты — FOK-gate, CAMA N_eff, трёхфазный dream, S2 compression constraint

**Covers:** S9/S10 хвосты

**Files:**
- Modify: `rag/edm.py` — FOK-gate τ=0.12 (reject до LLM), CAMA max-presence + N_eff (Hill diversity) в EDM-фьюжен
- Modify: `lifecycle/graph_enrich.py` — трёхфазный dream (NREM decay → REM bridge → Insight abstract) + S2 compression constraint
- Test: расширить test_graph_enrich.py, test_dual_route.py

- [ ] **Step 1: Тест**: FOK-gate: activation топ-кандидата < 0.12 → reject до LLM; N_eff низкий → abstention-флаг; dream: NREM ослабляет неактивные, REM мостит изолированные, Insight создаёт абстракции; S2 constraint: категория с 1 ребёнком не проходит.
- [ ] **Step 2: Реализация**: FOK-gate в route_query (после ITS, до возврата); N_eff в EDM-фьюжен; dream-фазы в graph_enrich (последовательные проходы); S2 constraint в s2_exhaustive.
- [ ] **Step 3: Коммит** `feat(G): FOK-gate, CAMA N_eff, three-phase dream, S2 constraint`.

---

### Task C7: S13-хвосты — cycles-daemon, cost-cap, gap-reader, Mermaid

**Covers:** S13

**Files:**
- Create: `features/cycles.py` — cycles-конфиг (60s/1ч/3ч/24ч) + chimera cost-cap (per-cycle/rolling-60m/per-task)
- Modify: `features/diagnostics.py::audit_content` — gbrain gap-reader флаги (unknown/stale/uncited/contradicting) + create_safety verdict
- Create: `lifecycle/graph_mermaid.py` — render_mermaid(layer) → Mermaid-строка
- Test: `tests/test_features/test_cycles.py` + расширить audit/mermaid

- [ ] **Step 1: Тест**: cycles конфиг читается; cost-cap блокирует превышение; gap-reader флагует; Mermaid содержит узлы/рёбра.
- [ ] **Step 2: Реализация**: cycles (dataclass + config.yaml retrieval-style), cost-cap (3 капа: per-cycle/rolling/per-task), gap-reader (продолжение audit_content), Mermaid (render nodes+edges в mermaid-syntax).
- [ ] **Step 3: Коммит** `feat(H3): cycles + cost-cap + gap-reader + Mermaid canvas`.

---

### Task C8: S2-хвосты — novelty-gate, topic-классификация, segment-consolidation, pinned/private

**Covers:** S2/S5/S16 хвосты

**Files:**
- Modify: `lifecycle/distiller.py` — semantic novelty-gate (paraphrase-Jaccard > порог → skip как дубликат), topic-классификация (hall_keywords → wiki-тип/epi_tags)
- Modify: `features/inject.py` — pinned-флаг + private-флаг (visibility колонка)
- Create: `lifecycle/segment_consolidation.py` — Lychee boundary detection (Eq1-4) для L0-группировки
- Test: расширить test_distiller.py, test_inject.py, test_segment_consolidation.py

- [ ] **Step 1: Тест**: paraphrase-дубликат → novelty-gate skip; topic → тег; pinned факт всегда в inject; private не в search; boundary detection режет по surprise.
- [ ] **Step 2: Реализация**: novelty-gate (Jaccard против существующих same-key, > 0.85 → skip), topic-классификация (словарь тем), pinned/private колонки (alembic g21), boundary detection (Lychee формулы).
- [ ] **Step 3: Коммит** `feat(F-final): novelty gate, topic classification, pinned/private, segment consolidation`.

---

### Task 9 (финал): Полный gate + коммит + push

**Covers:** все

- [ ] **Step 1: Полный pre-push gate** (4 команды).
- [ ] **Step 2: Коммит + push** `feat(HC): zero-deferred closeout complete`.

## Self-Review
- Все S-якоря и заплатки покрыты C1-C8. Порядок: C1-C2 (wire, быстрые) → C3-C5 (минеры/eval) → C6-C8 (хвосты) → финал.
