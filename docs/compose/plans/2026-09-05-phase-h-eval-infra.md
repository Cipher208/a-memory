# Phase H — Eval & Infrastructure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: compose:subagent per task. Checkbox syntax.
> **Prerequisite:** Phase G завершена (17b32fc): dual-route retrieval, ablation arms, минеры с validity windows.

**Goal:** Eval harness (№11) с ablation arms + memory_audit + import_chat + ariel-cli + A8-бридж + финальная зачистка.

**Architecture:** Eval = отдельный модуль `eval/` с конфигами arms и датасет-адаптерами (LongMemEval-S, LoCoMo), отчёты в JSON+Markdown. memory_audit = расширение memory_diagnose. import_chat = парсеры → L0 → гейты (origin=import). ariel-cli = argparse тонкий поверх query DSL.

**Tech Stack:** Python, existing ablation arms (rag/ablation.py), L0-журнал, memory_conflicts (ридер — наконец), LongMemEval-S dataset (HuggingFace moorcheh/memanto-evaluation тоже есть).

## Global Constraints

- 65 тулов (eval — модуль, не тулы; memory_audit = расширение существующего memory_diagnose)
- Full pre-push gate после каждого Task (ruff × 2, mypy 10 dirs, pytest junit)
- Приоритет: H1 (eval) → H2 (audit/import/cli) → H3 (бридж/остальное)

---

### Task 1: Eval harness ядро (№11)

**Covers:** S11

**Files:**
- Create: `eval/__init__.py`, `eval/harness.py`, `eval/datasets.py`, `eval/report.py`
- Create: `eval/configs/arms.json` (rrf/dense_per_kind/gated/full + компоненты: EDM on/off, inhibition on/off, graph-expand on/off)
- Test: `tests/test_eval/test_harness.py`

**Interfaces:**
- Produces: `eval/harness.py::async def run_eval(dataset: str, arm: str, *, limit: int = 50, judge_model: str = "proxy") -> EvalReport` (dataclass: accuracy, recall@5, precision, noise_isolation, reacquisition_cost, construction_tokens, per_category breakdown); `eval/datasets.py::async def load_longmemeval_s(limit: int) -> list[EvalQuestion]`; `eval/report.py::def render_markdown(reports: list[EvalReport]) -> str`.

- [ ] **Step 1: Failing test**: фикстура 10 вопросов с известными ответами (mini-dataset в tests) → run_eval(arm='full') → EvalReport с заполненными полями; arms различимы (rrf ≠ full при EDM).
- [ ] **Step 2: Реализация**: dataset-адаптер (LongMemEval-S с HF; если недоступен — mini-dataset); runner: вопрос → route_query по arm → ответ → сравнение (proxy-judge: exact/fuzzy match для smoke; LLM-judge интерфейс с заглушкой); метрики из S11 (accuracy, precision/noise, reacquisition: счёт вызовов retrieval после компакции, construction-tokens из L0).
- [ ] **Step 3: Ablation arms wiring**: ablation.py уже переключает — harness прогоняет каждый arm на том же датасете.
- [ ] **Step 4: Отчёт**: JSON + Markdown-таблица (arms × метрики) + честная плашка компонентов.
- [ ] **Step 5: Коммит** `feat(H1): eval harness — LongMemEval-S + arms + metrics`.

---

### Task 2: memory_audit — Letta-doctor расширение

**Covers:** S13 (memory_audit), v16-№3 (конфликт-ридер)

**Files:**
- Modify: `features/diagnostics.py` — контент-чеки
- Test: `tests/test_features/test_memory_audit.py`

- [ ] **Step 1: Тест**: фикстура с конфликтом (2 факта противоречащих) + дубликатом + stale (updated_at > N) + сиротой-линком → memory_audit возвращает эти проблемы в `content_checks`.
- [ ] **Step 2: Реализация**: memory_diagnose расширяется: противоречия (SELECT memory_conflicts unresolved), дубли (BM25 smart_similarity > порог по парам same-kind), stale (updated_at старше X + не pinned), битые [[fact:]]-линки wiki. Каждый: {severity, items, suggestion}.
- [ ] **Step 3: Коммит** `feat(H2): memory_audit — conflict reader + dup/stale checks`.

---

### Task 3: import_chat

**Covers:** S13 (import_chat = A6/E5)

**Files:**
- Create: `scripts/import_chat.py` — argparse: `--source claude|chatgpt|memory-json|jsonl --file PATH --user U`
- Test: `tests/test_features/test_import_chat.py`

- [ ] **Step 1: Тест**: фикстура Claude-export JSON → import → записи в l0_journal с raw_type='import', decisions=[{'gate':'import'}] → distill по гейтам.
- [ ] **Step 2: Реализация**: парсеры 4 форматов (claude-conversations.json, chatgpt.json, generic memory-json, jsonl) → нормализация (role, text, ts) → l0.capture(event='import') → батч-distill (watermark-совместимо).
- [ ] **Step 3: Коммит** `feat(H2): import_chat — 4 формата → L0 → gates`.

---

### Task 4: ariel-cli

**Covers:** S13 (ariel-cli, typed_export surface)

**Files:**
- Create: `scripts/ariel_cli.py` — подкоманды: `ls` (wiki_list/core keys), `tree` (MOC/иерархия), `find QUERY` (query_dsl), `grep PATTERN` (content LIKE), `stats`
- Test: smoke через tmp-BD

- [ ] **Step 1: Реализация**: тонкий argparse поверх memory_query + wiki_list; close_all() обязательно (aiosqlite-гоча).
- [ ] **Step 2: Коммит** `feat(H2): ariel-cli — ls/tree/find/grep over memory`.

---

### Task 5: A8 MEMORY.md-бридж

**Covers:** S13 (A8)

**Files:**
- Create: `features/bridge.py` — `async def regenerate_bridge(user_id: str, layer: str = "agent") -> Path` (топ-факты → MEMORY.md, drain-marker секция)
- Modify: `features/backup_cron.py` — nightly вызов
- Test: `tests/test_features/test_bridge.py`

- [ ] **Step 1: Тест**: 10 фактов разной importance → bridge → топ-5 в MEMORY.md, drain-marker присутствует; добавление текста после маркера → ingest в L0 (при следующем проходе).
- [ ] **Step 2: Реализация**: regenerate (MOC-стиль: importance-sorted, importance ≥ threshold, типы только инварианты) + ingest (текст ниже `# === AUTO-DRAIN BELOW ===` → l0.capture).
- [ ] **Step 3: Коммит** `feat(H2): MEMORY.md bridge — regenerate + drain ingest`.

---

### Task 6: Финальная зачистка

**Covers:** S16 (открытые вопросы)

**Files:**
- Modify: `pyproject.toml` — убрать claim «envelope encryption» из description (или пометить как saga/auth-only)
- Modify: `README.md` — если упоминает
- Modify: `mcp_server/server.py` — /ready + alembic-head check (1 строка)

- [ ] **Step 1: crypt-claim fix** (S16: убрать или пометить).
- [ ] **Step 2: /ready alembic check**.
- [ ] **Step 3: Полный pre-push gate + push** `feat(H3): final cleanup — crypt claim, ready check`.

---

## Self-Review
- S11→T1, S13(memory_audit)→T2, S13(import)→T3, S13(cli)→T4, S13(A8)→T5, S16→T6. S12 (библиотеки) — spaCy уже встроен (T2-F), fractional-indexing остаётся на Stage 2 или в G-волне инъекций (deferred). S14 Stage 2 — отдельный план после H.
- Deferred (не теряются, помечены): per-kind caps реализованы в G-T6 ✓; PROV-O/snapshots — H3-опции при времени; LoCoMo event-summarization задача — в Task 1 как доп. категория если датасет доступен.
