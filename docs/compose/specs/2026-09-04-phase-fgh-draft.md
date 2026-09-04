# Phase F/G/H — Draft (working document)

> **Status: DRAFT v1.** Мои предложения (2026-09-04, brainstorm сессия 4) — расходный
> материал для слияния с наработками пользователя (подаются частями в чат).
> После слияния: триаж → master-list (+ волны/усилия). НЕ финальная спека.
> Решения уже принятые: L0 = сырой журнал (append-only); порядок — сначала план F/G/H,
> Stage 2 вливается сюда; Phase A–D закрыты (отложенное — только по решениям:
> MOVED→MUSE, PARKED, [NEW], Stage 2).

---

## Phase F — L0-журнал + конвейер L0→L4 с явными гейтами

### Диагноз текущего потока

- `auto_save_text` (hooks/external.py) решает всё одним скором за один проход:
  L3 + graph + staging в одном стейте. Гейт сказал «нет» — текст исчез.
- `memory_dispatch_log` хранит только *решение* (saved_l3/l4/graph флаги), не текст —
  переиграть решение задним числом нечем.
- Параметры гейта (EMA, rules.yaml) мутируют со временем, история решений невоспроизводима.
- L1-кольцо живёт в harness'е — серверная сторона про отклонённое не знает ничего.

### [F1] L0 = `l0_journal` (append-only)

- Таблица: `id, ts, event, source_msg_id, layer, user_id, text, status, decisions JSON`.
- Пишется **первым**, до любого гейта, best-effort (как dispatch_log — сбой не блокирует поток).
- `status`: `received` → `skipped` / `gated_out` / `saved_l3` / `staged` / `promoted_l4` / `replayed`.
- `decisions` JSON — след каждого гейта: `[{gate: "g0", verdict, score?, reason}]`.

### [F2] Гейты — явные шаги, вердикт каждого → в L0-строку

- **G0 входной**: transcript-guard (`_looks_like_dump`, уже живой) + session-дедуп.
  Мусор → `status='skipped'`, в L0 остаётся (аудит-след), дальше не идёт.
- **G1 importance**: EMA (`shared/adaptive.py`) + rules engine (D1.9) — логика без изменений,
  вердикт логируется. Проход → L3 (+graph node).
- **G2 promotion**: L3→L4 — min_weight + transcript-guard (уже в consolidate_episodes)
  + idempotency; вердикт → L0.
- Staging (C1.11) остаётся «парковкой» между G1 и G2 (dream-маркеры — боковой путь).

### [F2+ · 2026-09-04] ПЕРЕРАБОТКА по пайплайну Эли и Лили (a-memory-l0-l4-pipeline.md) — ПРИНИМАЕТСЯ КАК ОСНОВА

> Их дизайн точнее моего черновика: главный недостающий инсайт — **G1 роутит по типу
> (инвариант vs событие), а не только по важности**. Ниже — слитая версия; пункт помечен [ЭЛИ]
> если из их дока, [MOЁ] если из v1-черновика, [NEW] — слияние.

**Код-факты их диагноза — проверены 04.09, все реальны:**
- L2 `sessions`: сообщений нет (summary/state_deltas/topics only) — [ЭЛИ] ✓
- `consolidate_staging` ключ `staging_{content[:30]}` — обрубок, та же болезнь что `ep_` — [ЭЛИ] ✓
- `decay_rate` из policy используется ТОЛЬКО в forgetting_ritual (nightly), promotion игнорирует — [ЭЛИ] ✓
- `memory_remember` пишет value в L4 И тот же текст в граф-узел — дублирование контента — [ЭЛИ] ✓
- ⚠️ поправка: CLACK codec в проекте НЕ найден (ни a-memory, ни cowagent) — сжатие L0 = A3-extractive, не CLACK [MOЁ-проверка]
- ⚠️ поправка: `add_edge` «не вызывается нигде» — устарело: B1.3 graph_builder строчит рёбра (но extract_links не находит связей на наших текстах), A1.6 communities тоже; в проде 2 ребра потому что минеры не наполняют, а не потому что «таблица не подключена» [MOЁ-проверка]
- ⚠️ поправка: sentence-transformers 6.0.0 УСТАНОВЛЕН в venv (ленивая загрузка при первом embed) — минер #9 (semantic_overlap) возможен СЕЙЧАС; их «нужна настоящая модель» уже выполнено [MOЁ-проверка]

**Слитая целевая линия (термины дока):**
```
L0 RAW (l0_journal, append-only) — ЕДИНЫЙ приёмник, сортировочная станция
 │  G0: transcript-guard + session-дедуп + strip_secrets (A1 🔴)
 │  классификатор: тип сырья (tool | recall | message | import) + атомизация на клаузы (без LLM)
 ├── тулы        → триплеты query→tool→outcome → рёбра графа           [ЭЛИ, NEW-тип потока]
 ├── recall-дампы → co-retrieval статистика → ребро co_recalled (минер #7)  [ЭЛИ = G-минер #7]
 └── сообщения   → дистиллятор (G1):
       типизация (kind_for_text + A5-regex) + importance (EMA+rules) + канонический ключ
       ├── инварианты (fact/decision/rule/instruction/commitment/goal/relationship;
       │     low decay_rate / never_archive) → L4 core
       │     конфликт со старым → rag/conflict.py → memory_conflicts + тег contradiction
       │       (Pending Contradictions, НЕ молчаливый UPDATE)                [ЭЛИ 🔑]
       ├── события (observation/question/context; высокий decay_rate) → L3 эпизоды
       └── всё с source_raw_id → провенанс до исходного сырья (drill-down C3) [ЭЛИ+MOЁ]
WIKI = L4.5 knowledge layer — НАМЕРЕННАЯ запись, НЕ дистилляция из L0       [ЭЛИ 🔑]
   ├── [[fact:ключ]] linking с L4 (факт помнит wiki_id) — без дублирования контента
   ├── ночью: провенанс-мосты wiki↔L4↔L0 (минер #5)
   └── MOC = граф-хаб (A1.1 уже есть)
```

**Правила дистилляции (принимаются целиком):**
- атомизация на клаузы (союзы/точки/запятые перед «и/но/причём») — резать на утверждения [ЭЛИ]
- канонический ключ: семантический хедер через словари/синонимы («decision:граф_без_llm»),
  повтор → UPDATE updated_at + recency-boost, НЕ дубль и НЕ INSERT OR REPLACE [ЭЛИ 🔑]
- L4 = store-семантика (последнее значение), L3 = лента событий, L0 = архив, wiki = намеренное знание [ЭЛИ]
- promotion уважает policy.decay_rate: инвариант (never_archive/low decay) vs событие [ЭЛИ 🔑]
- **одноразовая чистка ПЕРЕД минерами**: замусоренные JSON-узлы графа → вынести в L0 (иначе
  минеры свяжут мусор с мусором) [ЭЛИ 🔑]

**L0-разрастание (приняты их 5 механизмов, с поправками):**
1. ~~CLACK не найден~~ → **РЕШЕНО**: CLACK — концепт Лили (`cow/knowledge/concepts/clack.md`, статус концепт): lossless-формат, читаемый LLM **напрямую без распаковки**; «meta = decision vector» — мета-индекс (хэш/summary/ts/вес/теги/сущности) достаточен для решения без вызова тулов; родня — context-codec.md (оглавление = точка входа, словарное кодирование паттернов). → **Слои L0**: горячий = живые строки; тёплый = A3-extractive+zlib; **холодный = CLACK-архивы** (lossless, самодостаточные, drill-down жив без распаковки). Реализация формата — отдельная задача F (или H, по объёму).
2. дедуп по SHA-256 блока: повторный вывод команды хранится 1 раз, ссылки — уже в A1-объёме
3. три тира жизни: горячий 0–30д → тёплый 30–180д (сжатое) → холодный (>180д или дистилляция+N дней без recall → CLACK-архив, из L0 удаляется) [ЭЛИ 🔑]
4. дистилляция освобождает сырьё: вытащил факты → сырьё кандидат в холодный [ЭЛИ 🔑]
5. source_raw_id переживает сырьё (drill-down ведёт в архив) [ЭЛИ]
6. [МОЁ, дополняет] B5-защиты (min-остаток, стоп 80%) распространить и на L0-свип
7. [МОЁ, дополняет] цифры их оценки (2MB/день → 16MB/год со сжатием) — согласуются с ретеншеном 7–14д из v1; берём их схему 30/180 + свип, а не мой плоский 7–14д

**Карта входов → один приёмник (принимается):** MCP-тулы / авто-хуки / консолидация / сырые
дампы — 4 параллельных потока сейчас; remember дублирует в граф; graph_add и agent-хуки
бесконтрольны. → Все входы идут через L0-приёмник, который различает «сырьё» (→L0) и
«дистиллят» (→L3/L4 с провенансом). remember перестаёт дублировать в граф (только провенанс-ссылка).

**Влияние на уже записанные пункты:**
- A5 (regex-дистиллятор) — становится частью G1, а не отдельным тулом
- A4 (origin-канал) — расширяется до source_raw_id-citation (их провенанс шире)
- B1 (4-tier формализация) — их термины: L4=Semantic, procedural=Procedural, L3=Episodic
- C3 (drill-down) — их схема Persona→Scenario→Atom→Conversation реализуется через L0
- E9/B9 session-close — извлечение preferences/experience на commit — их пункт 4 подтверждён
- G-минеры — их список 8+1 сигналов ПЕРЕКРЫВАЕТ мой G-черновик и детальнее (теги/токены/
  сущности/сессии/провенанс-мосты/маркеры led_to/co-retrieval/структурные инварианты +
  эмбеддинг-слой); приоритет их плана: #1/#2/#4 сейчас → #5 → #7 (журнал recall-пар —
  расширить audit_trail) → #9 (модель уже установлена!) → #3/#6
- минер #7 требует ЖУРНАЛ co-retrieval пар — нового (audit_trail не ведёт пары) [ЭЛИ 🔑]

### [F3] Replay — воспроизводимость решений

- Инструмент повторного прогона гейтов по окну L0: `replay --since -7d --gate g1`.
- Сценарий: поменял порог/rules → переиграл неделю → staging получил отвергнутое ранее.
- Идемпотентность: повторный replay не дублирует (ключ = source_msg_id + gate + config-hash).

### [F4] dispatch_log → view над L0

- Миграционно ничего не ломаем: старая таблица остаётся, новые записи может дублировать
  или пере-вычисляться; цель — одна таблица телеметрии в итоге.

### Открытые вопросы F

- Ретеншен L0: предложение 7–14 дней + vacuum (объёмы маленькие, партиционирование не нужно).
- Полный текст vs обрезка в журнале: предложение — полный (WAL выдержит).
- Кто пишет в L0: только auto_save_text или все события (nightly, diff, dream)?

---

## Phase G — само-прошивающийся граф без LLM (ПЕРЕРАБОТАНО v14: основа = минеры Эли/Лили)

> Мой v1-черновик (co-occurrence/similar_to/follows_in_time) ПОГЛОЩЁН списком из
> `a-memory-graph-miners.md` — он шире (8 сигналов + эмбеддинг-слой) и привязан к
> существующей инфраструктуре. Источник: /home/murat/cow/knowledge/analysis/a-memory-graph-miners.md

**Фундамент (прежде минеров)**: рёбра пишут только builder'ы (B1.3 ночной, A1.6 communities,
MCP relates_to/causal) — реальных связей они не находят; recall-телеметрии пар НЕТ (нужен
журнал); эмбеддинги: hash-fallback в тестах, но sentence-transformers 6.0.0 в venv УСТАНОВЛЕН
(ленивая загрузка при первом embed) — минер #9 возможен сейчас, а не «после установки модели».

**Минеры (детерминированные; каждое ребро тегирует источник `heuristic:*`):**

| # | Сигнал | Инфраструктура | Приоритет (план Эли/Лили) |
|---|--------|----------------|---------------------------|
| 1 | Общие теги → `tagged` (вес = число общих) | epi_tags есть | сейчас |
| 2 | Общие редкие токены + синонимы → `topic_overlap` (Jaccard) | FTS5 + synonyms A3.1 | сейчас |
| 3 | Детерминированные сущности → `co_mentions` + канонизация (Лили/Lily/лисёныш) | словарь | фича |
| 4 | Сессионная близость → `same_session` | L2/L3 таймстампы | сейчас |
| 5 | Провенанс-мосты → `sourced_from` (эпизод→wiki→факт) | source_id/wiki_id есть | после фундамента |
| 6 | Маркеры результата → `led_to` heuristic («починила», «сломалось») | словарь маркеров | фича |
| 7 | Co-retrieval → `co_recalled` (вес = частота/N) | ⚠️ журнал recall-пар НОВЫЙ (расширить audit_trail) | после журнала |
| 8 | Структурные инварианты (co-citation, louvain-расширение, belief propagation) | louvain A1.6 | batch |
| 9 | Эмбеддинг-слой `semantic_overlap` (Jaccard MIB ≥0.7, top-k, вес 0.5–0.6) | модель в venv; O(n²) на 236 узлах ≈ 28k пар | после #1/#2/#4 |

**Режимы**: инкрементальный (при записи узла: теги/сущности/синонимы) + ночной batch
(co-retrieval, co-citation, communities, belief propagation). Вес = доверие к источнику
(эвристика 0.3–0.6, ручные/LLM 0.8+). Откат: `DELETE WHERE edge_tags LIKE '%heuristic:%'` —
эвристика никогда не врёт навсегда (в духе Shadow Bin).

**Инфраструктура из моего v1 (сохранена как рамка минеров):**
- Санитария: min-weight, decay только `heuristic:*`-рёбер, cap на узел, prune <0.05 —
  ручные/LLM рёбра не трогаются (маркировка источника решает)
- Ночная фаза `graph_enrich` в `_nightly` = оркестратор batch-режима (backup_cron уже стреляет)
- Пре-шаг: одноразовая чистка (Эли) — JSON-узлы из графа в L0, иначе минеры свяжут мусор с
  мусором (удалённые 04.09 дампы есть в бэкапах *.bak-pre-* — восстановить в L0 при вводе)

**Честный потолок** (их формулировка): сходство ≠ связь; `semantic_overlap` без порога+top-k
шумит; каузальность — за маркерами #6. Эмбеддинг-минер попутно подсвечивает мусорные узлы
(аномальные векторы) — двойная польза с L0-чисткой.

## Phase H — общие доработки (кандидат-лист, триаж после слияния)

- **Stage 2** (slots, URI-ключи, inject/key consolidation, deprecated aliases) —
  влить сюда или вести отдельно (решение при слиянии).
- **E6** negative memory (`/banthis`, anti_patterns) — [NEW] из E-триажа.
- **E12** savings ledger (per-call token accounting).
- **E8** sandboxing (Docker hardening, --read-only --cap-drop=ALL).
- **typed_export CLI** — модуль-сирота без производственной поверхности (аудит, кандидат #6).
- **Автоматизация tool-счётчиков** — 3 теста + доки правятся руками, уже дважды ловили рассинхрон.
- **9 admin-тулов вне tiers** — либо tier, либо формальный ops-only манифест.
- *(дополнить из наработок пользователя)*

---

## Log слияния

- 2026-09-04: v1 — черновик из brainstorm (мои предложения до получения наработок).
- 2026-09-04: v2 — разбор топ-11 идей конкурентов (пользователь). Вердикты:

### Идеи конкурентов → вердикты (2026-09-04)

| # | Идея | Статус | Вердикт |
|---|------|--------|---------|
| 1 | MCP behavior-аннотации (Basic Memory) | 🟢 новое — fork поддерживает `annotations: ToolAnnotations` (проверено: сигнатура `MCPServer.tool()`), сейчас не передаётся | Stage 2: разметка readOnly/destructive/idempotent при сведении тулов |
| 2 | Cross-encoder rerank вторым проходом | 🟡 частично — есть RRF + 1-hop graph-expand (B1.6) + kind_weights (E15) | Парк до №11: только если бенчмарк покажет провал RRF |
| 3 | /doctor-аудит качества (Letta) | 🟡 60% есть — `memory_diagnose` (E3) = здоровье системы; `rag/conflict.py` (ConflictResolver) пишет `memory_conflicts`, но РИДЕТЕЛЯ НЕТ (сирота из аудита) | Phase F: `memory_audit`-отчёт (противоречия/дубли/устаревшее) как ридер над существующей таблицей |
| 4 | Bi-temporal (Graphiti) | ✅ есть — A2.1 `e8a5d13`, `core_memory_temporal` + get_at_time/intervals | — |
| 5 | Provenance факт→эпизод (Graphiti) | ✅ есть — B1.4 metadata.parents + get_lineage + D1.6 memory_fact_blame | — |
| 6 | Triplet embeddings рёбер (Cognee) | 🟡 умно/дорого; граф почти пуст (2 ребра/122 узла) | Парк до результатов Phase G |
| 7 | Импорт чужих чатов (Basic Memory) | 🟢 новое | Phase F: `scripts/import_chat.py` — claude/chatgpt/memory.json → L0 → гейты → память (bootstrapping — второй сценарий конвейера после live-потока) |
| 8 | Entity linking (Mem0) | 🟢 новое | Phase G: нормализация имён + alias-таблица («Лисичка»=«кисонька»=«Lily») — precondition/4-й генератор для co-occurrence |
| 9 | Git/Markdown экспорт (Letta/BM) | 🟡 частично — memory_data, typed_export (сирота), wiki уже md на диске | Backlog-low (спорно при NaCl-шифровании); заодно дать surface typed_export |
| 10 | Sleep-time цикл (Letta) | 🟡 80% есть — `backup_cron` ЖИВОЙ в проде (journal: срабатывает, стреляет nightly-хуки обоих слоёв) | Вливается в Phase G: `graph_enrich` = новая фаза _nightly, не отдельный планировщик |
| 11 | Бенчмарки LoCoMo/LongMemEval (Mem0) | 🟢 новое | **Phase H №1** — eval-фреймворк: единственный способ доказать качество + приёмочный тест для F (до/после replay) |

Приоритетный порядок добавок: **11 (eval) → 3 (memory_audit) → 7 (import_chat) → 8 (entity linking) → 1 (аннотации при Stage 2)**.

- 2026-09-04: v3 — волна A от пользователя (механики записи и пайплайна). Вердикты:

### Волна A — механики записи и пайплайна (пользователь)

| # | Идея | Статус | Вердикт |
|---|------|--------|---------|
| A1 | SHA-256 дедуп + privacy-фильтр на записи (agentmemory) | 🟡 **механика есть, покрытие дырявое**: `strip_secrets` + `_DedupCache` SHA-256 TTL=300s (ровно 5-мин окно!) — но ТОЛЬКО в `memory_remember` (L4-путь); `auto_save_text` → L3 идёт БЕЗ фильтра и дедупа | **Phase F, G0-гейт**: вынести strip_secrets + дедуп из memory_remember в общий вход конвейера — секреты сейчас свободно текут в L3/L0. Это находка безопасности, приоритет |
| A2 | Near-duplicate advisory `similarTo` на save | 🟡 ConflictResolver (rag/conflict.py, BM25 smart_similarity) уже ловит на записи → `memory_conflicts` (ридера нет), advisory-возврата нет | **Phase F G1**: вернуть advisory в ответе save + писать memory_conflicts; ридер — memory_audit (идея №3); эмбеддинг-вариант — G2 similar_to |
| A3 | Synthetic compression без LLM | 🟡 `memory_compress` (D1.4) — для tool-output; reflections (D1.16) детерминированы, но поверхностны (счётчики) — смыслового сжатия нет | **Phase F**: extractive-сжатие (top-BM25 фразы + первая/последняя + decision-паттерны) как шаг суммаризации в конвейере/ночи — бесплатно, качество ниже LLM (осознанный потолок) |
| A4 | Write-time provenance (origin-канал user/agent/tool/import/shared) | 🟡 B1.4 parents + D1.6 blame покрывают факт→наблюдения для промоций; гранулярности канала нет (source: user_explicit/episode_promotion/... — без tool/import/shared) | **Phase F**: L0 даёт id наблюдений (citation по определению); + поле `channel` в metadata — S-effort |
| A5 | Zero-LLM regex ingestion фактов (MemoryPalace) | 🟢 новое — regex для LINKS есть (B1.3), для ФАКТОВ нет | **Phase F**: опциональный pre-classifier в G1 («запомни:», «я решил», «не делай» → высокий importance + guess memory_kind); заодно keyless-режим |
| A6 | Import чужих форматов (claude/chatgpt/memory-json, jsonl) | 🟢 = топ-№7 | **Phase F**: `scripts/import_chat.py`, форматы +jsonl; origin=import (через A4-channel) |
| A7 | Session crystals + lessons mining на импорте | 🟢 новое; строительные блоки есть: consolidation, cls_replay (D1.18), error_pattern (D1.8), E6 negative memory [NEW] | **Phase F (импорт) + ночная фаза**: кристалл = A3-сжатие сессии; lessons → E6 anti_patterns (наконец мотивация E6!) |
| A8 | MEMORY.md-бридж (двунаправленное файл-зеркало) | 🟢 новое; прецеденты: wiki=md на диске, session-start inject, у Люси drain-marker паттерн (ручной) | **Phase H**: `memory_bridge` — топ-факты → MEMORY.md (регенерация как MOC) + ingest секции ниже drain-маркера демоном (write-путь для агентов без MCP) |

**Приоритет внутри волны**: A1 (безопасность — секреты в L3!) → A2 → A4 → A5 → A3 → A6/A7 → A8.
**Синергия**: A1+A2+A4+A5 садятся прямо в гейты F; A6+A7 — второй сценарий F (импорт); A3 — общий строительный блок (F-сжатие + A7-кристаллы); A7 даёт наконец продюсера для E6.

- 2026-09-04: v4 — волна B (забывание, консолидация, жизненный цикл). Вердикты:

### Волна B — забывание / консолидация / жизненный цикл (пользователь)

| # | Идея | Статус | Вердикт |
|---|------|--------|---------|
| B1 | 4-tier Working→Episodic→Semantic→Procedural | 🟡 маппинг почти 1:1: L1 harness-ring→Working, L3→Episodic, L4→Semantic, procedural_memory (D2.5)→Procedural — но слои не связаны явной моделью переходов | **Phase F**: формализация = таблица переходов L0→L4 (VALID_TRANSITIONS B1.5 уже есть для отельных хопов) + declaration в доках; S-effort |
| B2 | Recall hygiene (superseded вне индексов, цепочка сохраняется) | 🟡 bi-temporal (A2.1) хранит цепочку; «superseded вне поиска» реализовано частично — FTS ищет по актуальным, но deleted/superseded флагов нет | **Phase F**: formalize — is_current-вью или status-колонка в core_memory (deleted→archived уже есть); проверить recall не возвращает закрытые интервалы |
| B3 | Decay по Эббингаузу | ✅/🟡 **ACT-R уже в ранкере** (D1.17): `actr_activation` в multi_source (×1.3 на оба источника, access_count из D1.19 was_useful) — НО: колонок access_count/last_accessed в core_memory НЕТ (частота считается на лету), и никакого автоматического eviction по decay нет | **Phase H**: сверить формулы (ACT-R -0.5d vs Эббингауз exp) — оставить ACT-R (теория базируется на той же кривой); добавить авто-эвикт stale как nightly-фазу после замера eval (№11) |
| B4 | TTL-флаг «забудь через N» | 🟡 `expires_at` колонка ЕСТЬ (A2.6-эпоха), в проде 1 строка; на туле `memory_remember` параметра НЕТ; cleaner'а нет | **Phase F**: `ttl_minutes` параметр на remember/staging + nightly-свип expired (в backup_cron-фазу) — S-effort, удобный volatile-контекст |
| B5 | Защиты TTL-cleaner (мин. остаток, стоп при 80% expired, cleaner_summary) | 🟢 новое; наш cleanup (memory_cleanup) дедупит/архивирует без защит; forget primitive — без барьеров | **Phase F**: правила конвейерного свипа: min-остаток на слой, стоп-порог, cleaner_summary в L0/аудит — protect-инварианты для B4-свипа |
| B6 | UPDATE > MERGE > CREATE + лимит сущностей с принудительным merge | 🟢 новое; разрастание реально: 136 фактов/122 узла растут, dedup только exact | **Phase G (после entity linking №8)**: лимит активных узлов на user → принудительный MERGE холодных/similar_to — эвристика размера, needs eval-цифры (№11) |
| B7 | Heat-арифметика merge: sum(heat)+1 | 🟢 новое — простой детерминированный инвариант | **Phase G**: константа для B6-merge (вес merged-узла), 1 строчка логики |
| B8 | `[PERSONA_UPDATE_REQUEST]` out-of-band сигнал | 🟢 новое; по духу = DREAM-маркеры (C1.12), но от консолидатора к профилю; persona в MUSE (D3.x MOVED) | **Парк/в MUSE**: запросы про эволюцию личности — вне ariel per user decision 01.09; если речь про agent-facts — переформулировать как DREAM-маркер |
| B9 | Session commit → async extraction | 🟡 session_end хук ЕСТЬ (C1.4, post_session_diff Hermes-only); extraction — только episode save, preferences/experience не извлекаются | **Phase F**: G2-фаза «session-close» на post_session_diff/context_threshold: вытащить preferences/опыты через A5-regex → staging; async бесплатно (демон) |
| B10 | Introspection engine 24ч (повторяющиеся темы → Core) | 🟡 reflections (D1.16) считают темы, но НЕ продвигают; promote есть только у scratchpad (D1.15) | **Phase F/H**: расширение nightly_reflection: recurring-topic ≥N → staging proposal (не прямой write — через review C1.11) — S-effort |

**Приоритет внутри волны**: B4+B5 (TTL+защиты, S) → B2 (hygiene) → B1 (формализация) → B9 (session-close) → B10 → B3-эвикт (после №11) → B6/B7 (после G+eval). B8 — парк/MUSE.
**Ключевая находка**: ACT-R уже в ранкере, но у него нет частотной колонки — D1.19-фидбек даёт access_count только на чтении из таблицы recall_events (1 запись в проде). B3-«Эббингауз» = по сути «подкрутить ACT-R частоту», не новая формула.

- 2026-09-04: v5 — волна C (хранение, структура, wiki). Вердикты:

### Волна C — хранение / структура / wiki (пользователь)

| # | Идея | Статус | Вердикт |
|---|------|--------|---------|
| C1 | Wiki-страница-«сцена» (Core Traits / Preferences / Implicit Signals / Core Narrative Trigger→Action→Result / Evolution Trajectory / Pending Contradictions) | 🟡 блоки есть: wiki-типы+skill (D2.1), typed kinds 13 шт (relationship/preference/decision...), bi-temporal, memory_conflicts (писатель есть); нет — шаблона сцены иtrajectory-секции | **Phase F (конвейер)**: сцена = шаблон страницы + генератор из typed-фактов (MOC-стиль), секция Pending Contradictions = ридер memory_conflicts (идея №3, memory_audit), Trajectory = читает core_memory_temporal; LLM-free; persona-эволюция НЕ вшиваем (D3.x→MUSE) — только факто-сцены |
| C2 | Двухслойное хранение: БД (доказательства) + Markdown (структура) | ✅/🟡 архитектурно уже так: wiki = md на диске (white-box), БД = индекс+доказательства; но core/episodes — БД-only | Верно как принцип; частично закрывается A8-бриджем (H) и сценами (C1). Новой работы нет, отметить в спеке как принцип |
| C3 | Drill-down цепочка Persona→Scenario→Atom→Conversation (node_id-ссылки от абстракции к raw) | 🟡 B1.4 parents + D1.6 blame дают links вверх; нет сквозной навигации вниз (факт → L0-наблюдения) и UI-траверса | **Phase F**: L0 даёт атомы (id наблюдений) → parents-цепочки удлиняются до L0; drill-down = выгрузка get_lineage + L0-записей (для начала — в memory_fact_blame добавить l0_ids); C11-viewer — consumer |
| C4 | Pinned + semantic slots поверх ACT-R | 🟢 новое; inject-блоки (S5) имеют kind/score, но pinned-набора нет | **Phase H**: `pinned` флаг/коллекция на core_memory → inject всегда вставляет первым (до relevant), бюджет резервирует slice; просто, полезно для identity/границ |
| C5 | Private/Public-гейтинг | 🟢 новое; visibility-колонок нет; D1.13 scopes — это user/agent-изоляция, не приватность контента | **Phase H**: `private` флаг на L4/L3 → exclude из recall_protocol/inject/wiki_search по умолчанию (override-параметр для владельца); NaCl-at-rest уже есть? — ПРОВЕРИТЬ (PyNaCl в venv, но шифрование в коде не найдено — отдельный вопрос) |
| C6 | Charter/steward-концепция слоёв («у слоя цель и владелец») | 🟢 новый ракурс; у нас контракты размыты по коду | **Спека F**: секция «Layer Charter» — таблица слой/цель/владелец/кто пишет/кто чистит (из F-гейтов это выйдет естественно); чисто документационный + discipline-маркеры в inject |
| C7 | Языковая адаптивность контента | 🟢 ничего нет (0 вхождений lang-логики); но контент и так пишется на языке пользователя агентом | **Парк**: LLM-free детект (cyrillic-ratio) дёшев, но сценарий неясен — перевод? хранение дубликатов? Вернуться если появится реальный б pain |
| C8 | Пространственная метафора wings/rooms/drawers | ✅/🟡 у нас уже: per-agent каталоги (cowagent/hermes/mimocode/eli + layers), branches (D1.11) = комнаты, stash (D1.12) = ящики | Формализовать названиями в спеке-чартере (C6); новой механики не нужно — YAGNI |
| C9 | AAAK-диалект lossy-компрессии (−80% токенов) | 🟢 новое; есть memory_compress (D1.4) для tool-output, но диалекта плотного хранения/экспорта нет | **Phase H**: опциональный компакт-рендер фактов (одна строка: `PROJ: x \| JOR→y \| ★4`) для recall-сканирования при большом бюджете-дефиците; после №11 (мерить потерю качества) |
| C10 | Экспорт в человекочитаемый Markdown + git | 🟡 = топ-№9 | Backlog-low (дубль), но typed_export surface + `memory_data` md-рендер — в H если A8-бридж пойдёт хорошо |
| C11 | Session Replay viewer (таймлайн prompt→tool→result) | 🟡 dashboard ЕСТЬ (stats/facts/episodes/audit endpoints) — replay-таймлайна нет; L0 (F1) даст данные, dispatch_log уже даёт половину | **Phase H** (после F1): `/replay?session=` на dashboard — таймлайн из L0 (served из journal), play/pause = фильтр по окну; потребляет C3-drill-down |

**Приоритет внутри волны**: C1 (сцены — видимый результат F) → C3 (drill-down к L0) → C2-принцип в спеку → C4/C5 (H, простые флаги) → C6 (чартер) → C11 (H, после F1) → C9/C10 → C7/C8 парк/бесплатно.
**Вопрос для пользователя**: NaCl/шифрование at-rest — в коде не найдено (PyNaCl только в зависимостях). Есть ли где-то включённое шифрование, или это была идея из ранних планов? (влияет на C5 и C10-верdictы)

**Решение вопроса про шифрование (04.09, поиск)**: envelope-encrypt ЕСТЬ (`features/secrets.py`: master key в .env, encrypt_json/decrypt_json) — но покрывает ТОЛЬКО saga-state и auth-store. Основные данные (memory.db, wiki-файлы, L1-кольца) НЕ зашифрованы, при этом «envelope encryption» заявлен в pyproject-описании. → C10 (md-экспорт) перестаёт быть «спорным»: экспорт не слабее текущего состояния. Отдельный хвост: либо шифровать БД-файлы (SQLCipher/приложение-level), либо убрать claim из описания.

- 2026-09-04: v6 — волна D (поиск и ретрив). Вердикты:

### Волна D — поиск и ретрив (пользователь)

| # | Идея | Статус | Вердикт |
|---|------|--------|---------|
| D1 | Cross-encoder rerank вторым проходом | 🟡 = топ-№2 (дубль) | Парк до №11-eval |
| D2 | Tiered loading abstract/overview/details (OpenViking) | 🟢 новое; у wiki есть progressive disclosure (list→read, D2.1), но 3-уровневых представлений нет; graph nodes и core-факты — один уровень | **Phase H/G**: `.abstract` (~100 tok) генерировать при записи (A3-сжатие), overview = сцена/первая секция; дёшево для wiki (frontmatter-поле), сложнее для фактов; измерить экономию на №11 |
| D3 | Directory-recursive retrieval (спуск по директориям) | 🟢 ядро есть: MOC-хабы (A1.1) + wiki_query BFS (A1.4) + graph-expand (B1.6) — но retrieval НЕ идёт «сперва директория, потом контент» | **Phase H**: retrieval-роутер: query → сначала MOC/тематические узлы (cheap), потом контент-поиск внутри победившей темы; гибрид «навигация→контент» вместо плоского скоринга |
| D4 | Observable retrieval (журнал траектории поиска) | 🟡 recall возвращает source-теги per-hit («fts5»/«mib»/«graph»/«graph_expand»), но полной траектории (что пересекли, что отбросили) нет; memory_diagnose — здоровье, не трейс | **Phase H (после F1)**: ретрив-трейс в L0 (событие retrieval: план → источники → отброшенное) → diagnose показывает «почему такой результат»; дёшево после F |
| D5 | Cache-friendly инъекция (prepend/append, вырезание старых тегов) | 🟡 E9 `<cache:break>` + stable-first ordering УЖЕ ЕСТЬ; чистки старых `<relevant-memories>` из истории нет (harness-side), prepend/append-позиционирование — harness contract | **Phase F (spекa инъекции)**: зафиксировать контракт: volatile-блоки в хвост последнего user-msg, стабильные в system; чистка старых тегов — задача harness-адаптеров (cow/Hermes/MiMo), не сервера |
| D6 | Char-бюджеты + таймауты с graceful-деградацией | 🟡 E2-breaker + hash-fallback УЖЕ ПОЛНЫЙ стек (3 фейла → 30s → fallback); char-бюджетов per-memory нет (только токен-бюджет блоков S5 + maxLines в compress) | **Phase F**: maxChars per memory при инъекции (обрезка хвоста) — S; таймауты покрыты |
| D7 | RRF k=60 + session diversification ≤3/сессию | 🟡 k=60 УЖЕ дефолт (`rag/search.py:140`); диверсификации нет | **Phase H**: session-diversity cap в rerank (после №11 — влияет на качество, мерить); k=60 совпадает |
| D8 | Токен-бюджет инъекции default 2000 | ✅ ЕСТЬ ТОЧНО ТАК: `budget: int = 2000` в recall_protocol и inject (S5) | — (совпадение 1:1) |
| D9 | Triplet embeddings рёбер | 🟡 = топ-№6 (дубль) | Парк до G-результатов |
| D10 | Entity linking | 🟢 = топ-№8 (дубль) | Phase G (уже записано) |
| D11 | Recency `exp(−λΔt)` + amplification `(1+αw)` | 🟡 ACT-R покрывает (base-level = −d·ln(t) + частота); MemoryMuse-формула проще/линейнее | **Phase H**: не заменять, а ПРОГНАТЬ обе на №11-данных; ACT-R теоретически строже (частота+интервалы), Mnemosyne-вариант — baseline для сравнения |
| D12 | BFS по графу знаний как третий поток | 🟡 graph УЖЕ пятый источник MultiSourceRAG (`_from_graph`, include_graph=True) — но реализован SQL-LIKE примитивом, не BFS по связям | **Phase G**: upgrade `_from_graph` до entity-resolve (№8 linking) → BFS от узла (A1.4-CTE уже есть) → соседние факты; авто-включится в RRF |

**Приоритет внутри волны**: D6 (maxChars, S) → D5-спека (контракт инъекции для harness'ов) → D4 (трейс, после F1) → D3 (директории) → D2 (tiered) → D7 (диверсификация) → D11 (прогнать формулы) → D1/D9 парк.
**Ключевая находка**: половина волны уже в стапе — k=60, budget=2000 (совпадение 1:1 с agentmemory), breaker+fallback, graph-источник. Реальные пробелы: maxChars-обрезка, retrieval-трейс, директорий-спуск, BFS-upgrade граф-потока.

- 2026-09-04: v7 — волна E (продукт, интеграции, инфраструктура). Вердикты:

### Волна E — продукт / интеграции / инфраструктура (пользователь)

| # | Идея | Статус | Вердикт |
|---|------|--------|---------|
| E1 | MCP behavior-аннотации | 🟡 дубль топ-№1 | Stage 2 (разметка при сведении) |
| E2 | /doctor-аудит качества | 🟡 дубль топ-№3; у нас memory_diagnose/heal (E3) + сирота memory_conflicts | Phase F: расширить memory_diagnose контент-чеками (конфликты-ридер, дубли, устаревшее) — именно «расширить», как пользователь и пишет |
| E3 | Sleep-time планировщик | 🟡 дубль топ-№10 — backup_cron жив и стреляет nightly | Закрыто; G добавляет graph_enrich-фазу |
| E4 | Agent Loadout (ассеты + ACL private/team/restricted) | 🟢 новое; у нас D1.13 scopes (user/agent), branches (D1.11) — но ACL-гранулярности нет | **Парк**: 5 инстансов изолированы файлами, real multi-tenant не нужен; C5-private-флаг закрывает ближайший сценарий. Вернуться при появлении shared-инстанса |
| E5 | Cold-start импорт | 🟡 дубль A6/№7 | Phase F: import_chat |
| E6 | Авто-извлечение Skill из завершённых задач (`mem:create-skill`) | 🟡 D2.2 promote_episodes УЖЕ умеет промоцию задним числом (episode_ids → skill page, idempotent, merge при коллизии); auto_promote_fresh ночью берёт только dream_skill-эпизоды | **Phase F/H**: ретроспективный mine — nightly расширяет окно: все эпизоды недели → A5-regex «задача завершена/решил/работает» → promote_candidates → staging (не прямой write); переиспользует promote_episodes 1:1 |
| E7 | LLM-Proxy паттерн (baseURL-подмена) | 🟢 большое; транспорт уже закрыт 3 интеграциями (MCP/плагин/hooks) + MiMoCode messages.transform | **Парк**: revisit только для harness без расширяемости; конфликтует с no-LLM принципом ariel (прокси обязан пропускать чат через себя) |
| E8 | WhisperGate: nano-модель решает инициативу | 🟢 новое, но ariel принципиально keyless/no-LLM (B1.6 verdict) | **Парк/MUSE**: дешёвый гейт инициативы — задача harness-демона (у cowagent уже есть importance-гейт диспетчера); ariel остаётся без LLM |
| E9 | Scheduler-циклы как продукт (интервалы 60с/1ч/3ч/24ч) | 🟡 backup_cron (backup+nightly) и daemon poll-loop есть; единой config-таблицы циклов нет; gap-reports (C1.10) есть, inactivity check-in нет | **Phase F**: `cycles` в конфиге daemon: {gap_check: 1h, staging_reminder: 60s?, inactivity: 3h, dream: 24h} — единая таблица вместо россыпи констант; daemon уже циклится |
| E10 | `wake_up`-тул (атомарный подъём Core в контекст) | 🟡 есть по сути: memory_context_inject (бюджетные блоки) + memory_recap (recovery pack) | Закрыто существующими тулами; в Stage 2 — сведение имён (wake_up как alias, если захочется) |
| E11 | `mine`-индексация кодовой базы | 🟡 project-примитив УЖЕ майнит: `project(mine)` → graphify extract → project_symbols (5000 cap) + docs_to_wiki (A1.7) для доков + wiki→rag (A2.3) | Закрыто почти всё; хвост: graphify — внешний бинарник (нет в стапе → graceful skip уже есть); опционально скрипт-обёртка `mine ./project` = project(mine) + docs_to_wiki --source. H, S |
| E12 | Бенчмарки (LoCoMo/LongMemEval-S/BEAM, R@5) | 🟢 дубль топ-№11 | **Phase H №1** (не меняется) |
| E13 | `ov`-CLI: ls/tree/find/grep по памяти | 🟡 typed_export.py — CLI-модуль-сирота (audit находка); query DSL (D1.7) даёт фильтры, MOC/tree даёт иерархию | **Phase H**: `ariel-cli` поверх query DSL + wiki_list (ls/tree/find/grep) — заодно даёт surface typed_export-сироте; S-M |
| E14 | Health/livez/ready + доктор | 🟡 /health, /ready, /alive УЖЕ есть (middlewares.py:14 + app.py:38)! доктор = memory_diagnose/heal | Закрыто на 90%; хвост: /ready проверять alembic-head (сейчас, видимо, только liveness) — 1 строка |
| E15 | Mermaid-канвас (node_id в контексте) | 🟢 новое; epi_nodes/edges → mermaid-рендер тривиален; потребители: сцены C1, MOC, dashboard | **Phase H (после G-наполнения)**: `graph_mermaid` рендер (wiki-страница + inject-блок опционально); до наполнения графа — пустой канвас |
| E16 | Бенчмарк-цифры «до/после» (48→76%, −61% токенов) | 🟡 дубль №11 + формат | Phase H №1: формат отчёта принять (quality + token-savings), reproduсить на своих данных |

**Приоритет внутри волны**: E12/E16 (eval — главный) → E2 (doctor-расширение) → E6 (ретро-skill mine) → E9 (cycles-таблица) → E13 (CLI) → E14 (readyz-строка) → E15 (mermaid после G). E4/E7/E8 — парк (по границам продукта), E5/E1/E3/E10 — закрыто дублями.
**Итог волны**: из 16 пунктов 6 — дубли уже записанного, 4 — «уже сделано» (E3/E10/E11/E14), 2 парка по границам продукта (E7/E8), реальные новинки: E6-ретро-майн, E9-cycles, E13-CLI, E15-mermaid — все S-класса.

- 2026-09-04: v8 — волна F конкурентов (уроки «чего не делать») + сводка паттернов. **Итог разборного док-документа.**
  NB: буквы волн конкурентов (A–F) — разметка входного дока; Phase F/G/H — выходной план. Не путать.

### Уроки «чего не делать» (волна F конкурентов) — проверка нашего дизайна

| # | Урок | Наш статус | Вердикт |
|---|------|-----------|---------|
| F1 | LLM-capture — не единственный путь записи | ✅ подтверждает: ручные тулы + примитивы + rules + A5-regex (план) — три не-LLM пути уже в дизайне | Действуем как есть |
| F2 | Не хранить plaintext — «у нас NaCl» | ❌ **НЕ ПОДТВЕРЖДАЕТСЯ** (находка v6): envelope-encrypt покрывает только saga-state/auth-store; memory.db, wiki, L1 — plaintext, при этом «envelope encryption» заявлен в pyproject-описании | **Хвост-действие**: либо шифровать данные (SQLCipher на БД / app-level для L1/wiki), либо убрать claim из описания пакета. Слушать решение пользователя |
| F3 | Миграции first-class с первого дня | ✅ alembic + staged mutations (C1.11) + bi-temporal + wiki-md; сегодняшние a25/a26 — живое доказательство урока (две таблицы были вне цепочки — починены в тот же день) | Действуем как есть |
| F4 | LLM write в файлы требует лимитов и нормализаторов (TencentDB баг #88 — стёртые scene_blocks) | ✅ реализовано: safe_resolve (E7), lint+secrets-scan на add/update, cap имени 80+hash (general-3 фикс), skill 4KB cap, update() переписывает ровно одну страницу (нет bulk-wipe пути) | Действуем как есть; при сценах C1 — генератор пишет секциями, не whole-file |
| F5 | Не плодить тулы без тиров | ✅ решено: ARIEL_EXPOSE tiers, 65→65 тулов, counter-тесты; исключение — 8 admin-тулов вне tiers (документировано, ops-surface) | Действуем как есть |
| F6 | Не зависеть от внешнего бинарника (agentmemory+iii-engine ломается на апдейтах) | 🟡 **одно исключение**: project(mine) зовёт внешний `graphify` CLI — но graceful (try/except → «skipped»), без version-пиннинга, деградация тихая | Отметить в E11-вердикте: при развити mine — либо vendor pure-python экстрактор, либо оставить graceful-skip как контракт |
| F7 | Не гнаться за «self-evolving» маркетингом | ✅ подтверждает: настоящая эволюция = staged mutations + consolidation + evolve-примитив (persona-эволюция — MUSE) | Действуем как есть |

### 10 повторяющихся паттернов (2+ конкурентов) → карта покрытия черновиком

| Паттерн | Покрытие |
|---------|----------|
| 1. Tiered/abstract-слои | F-конвейер (гейты) + D2-tiered + C1-сцены — записано |
| 2. Запись с дедупом + privacy | A1 → G0-гейт (🔴 находка: фильтр есть только на L4-пути) |
| 3. Cache-friendly инъекция | D5 → E9 уже делает stable-first + cache:break; F-спека фиксирует контракт |
| 4. Провенанс/трассируемость | B1.4 + D1.6 + L0-citation (F) + C3-drill-down + D4-трейс |
| 5. Консолидация «во сне» с heat/decay | ACT-R (B3) + graph_enrich (G) + E9-cycles; «Эббингауз» закрыт ACT-R |
| 6. Не-LLM удешевление | A3 (extractive) + A5 (regex) + принцип no-LLM ariel |
| 7. Пространственная навигация | C8 (уже есть: MOC/wiki/branches) + D3 (MOC-first retrieval) |
| 8. Явные TTL/volatile | B4 (expires_at есть, surface нет) + B5 (защиты свипа) |
| 9. Session-commit → извлечение | B9 → F session-close фаза |
| 10. Автономия через дешёвый gate | E8 — парк/MUSE: гейт инициативы — harness-демон, ariel остаётся keyless |

**Вывод по документу конкурентов**: 47 пунктов разобрано (11 топ + A1–A8 + B1–B10 + C1–C11 + D1–D12 + E1–E16 + F1–F7). Уникальных новинок, меняющих план: **A1-безопасность (единственный 🔴)**, import_chat (F), entity linking (G), eval-фреймворк (H №1), memory_audit-ридер (F). Остальное — подтверждения дизайна, дубли или парк. Наша архитектура (тиры, no-LLM ядро, миграции, staged mutations) пережила разбор без контрпримеров — единственный провал F2 (plaintext).

- 2026-09-04: v9 — детальная секция TencentDB-Agent-Memory (пользователь). Все 12 «что взять» уже записаны в волнах B/C/D/E (маппинг: 1→C1, 2–3→B6/B7, 4→B8, 5→C3, 6→D5, 7→D6, 8→C7, 9→B5, 10→E15, 11→E4, 12→E7). Микро-факты из детальных механик — 4 добавки:

### TencentDB детально — микро-добавки

1. **C1-сцена: cap ≤1500 симв** — конкретная цифра размера сцены (наш skill-cap 4KB; для сцен берём 1500 как soft-target генератора, whole-file не пишет никто после бага #88).
2. **jieba-токенизация (CJK)** — наш FTS5 на default unicode61 (RU/EN ок, проверено продом). CJK-поддержка — только если появится пользователь на китайском/японском; парк.
3. **Circuit breaker на harness-адаптерах** — у их Hermes-плагина есть (5 fails → 60s pause); наш hermes-плагин — НЕТ (проверено: 0 вхождений), CLI-падения только логируются. Внутри ariel breaker стоит (E2), но внешний защите нечего ломать каждый turn при лежащем CLI. → **F-спека интеграции**: 5-fails→60s паттерн в harness-адаптеры (S, микроскопия, но дешёвая).
4. **Их 8 слабостей = наш дифференциатор** (записать как позиционирование): LLM-обязателен vs наши 3 не-LLM пути записи; «86 scene blocks пассивным приёмником» (их личный опыт) vs наши примитивы+тулы+rules; plaintext vs (починить!) F2; вендор-лок vs SQLite; миграция-TODO vs alembic-first; баги LLM-файлов vs lint+лимиты; нет forget/dream/evolve примитивов, графа, bi-temporal, MCP-first — всё это у нас уже built-in. Self-reported цифры (PersonaMem 48→76%, −61% токенов, WideSearch 33→50%) — референс-формат для №11.

**Проверка гипотезы «может что-то уже было»**: да — эта секция подтверждает волны B/C/D/E почти 1:1 (их «взять» = наш черновик). Единственные ~4 микро-добавки выше.

- 2026-09-04: v10 — детальная секция agentmemory (rohitg00). Маппинг «взять»-пунктов на черновик:
  1 (4-tier + synthetic) → B1 + A3 · 2 (dedup+privacy) → A1 🔴 · 3 (recall hygiene) → B2 ·
  4 (Replay viewer) → C11 · 5 (similarTo) → A2 · 6 (provenance) → A4 · 7 (MEMORY.md-бридж) → A8 ·
  8 (RRF k=60/diversification/бюджет 2000) → D7/D8 (k и бюджет совпали 1:1) · 9 (LongMemEval) → топ-№11.
  **Все 9 — уже записаны.** Слабости их = наш дифференциатор (внешний ii-engine с пином vs self-contained;
  54 тула без тиров vs ARIEL_EXPOSE; plaintext; LLM-фичи требуют провайдера; state store непрозрачен;
  заточен под кодинг-агентов — не под личность).
  Новых механик в детальном описании — 3 микро-добавки:

### agentmemory детально — микро-добавки

1. **Keyless = только BM25** — у нас это режим по умолчанию для части установок (hash-fallback embeddings), но БМ25-«всегда включён» формулировка совпадает: search_fts5 живёт независимо от векторной ветки. Подтверждение дизайна, новой работы нет.
2. **Сессийные summary на Stop/SessionEnd** — у нас `session_ended`-хук ЕСТЬ (user_hooks._session_ended + post_session_diff), но их Stop-триггер (после каждого завершённого turn-батча) тоньше нашего session-end; совпадает с B9 session-close фазой → одна и та же фаза F покрывает.
3. **Skill-навыки 17 шт (9 invocable) vs наш wiki_type=skill** — поверхность та же, но у них skills ИНКРЕМЕНТНЫ: их сейчас 0 в проде-графе (wiki_index: skill=0). D2.2-конвейер есть, контента нет — ретро-mine (E6) это починит; + вариант «invocable-навык» (агент дёргает skill как команду) — это SKILL.md-конвенция хоста (MiMoCode/Claude Code), не сервера — harness-side, не наша фича.
4. **Real-time viewer :3113 / WS** — их realtime-порт не нужен: наш dashboard-поллинг + C11-replay закрывают сценарий отладки; WS-стриминг — парк (оверхед без потребителей).

**Резюме**: agentmemory — второй подряд конкурент, чей «взять»-лист 1:1 повторяет уже записанный черновик (волна A и была их пайплайном). Подтверждение: A1-находка (privacy только на L4) стала 🔴 не зря — у них это первая стадия пайплайна.

- 2026-09-04: v11 — детальная секция OpenViking (ByteDance). Маппинг: 1 (L0/L1/L2-tiered) → D2 · 2 (directory-recursive) → D3 · 3 (observable retrieval) → D4 · 4 (session commit extraction) → B9 · 5 (viking:// namespaces) → **Stage 2 URI-keys** (audit-файл #19: «discuss WITH the tool-surface redesign» — прямое совпадение замысла). Все 5 — записаны.
  Новых механик в детальном описании — 2 добавки + 1 усиление:

### OpenViking детально — микро-добавки

1. **Каталоги тоже несут .abstract/.overview** (не только записи) — усиление D2: в наших терминах MOC-хабы (A1.1) уже являются «абстрактом каталога»; добавить overview-секцию в MOC (список подтем + 1-строчный summary каждой) — это делает D3-директорий-спуск дешёвым (решение «куда спускаться» по ~100-токенному MOC, без чтения страниц). → дополнение к D3-вердикту.
2. **`peers/`-неймспейс (межагентная память)** — у нас D1.13 scopes (user/agent) + branches, но «память О ДРУГИХ агентах» и shared-подпространство не моделировались; hivemind-сценарий пока вне продукта → **парк/Stage 2**: при URI-redesign предусмотреть namespace-схему, допускающую peers/ (зарезервировать, не строить).
3. **Их цифры LoCoMo 80–83% vs 24–57% нативных** — референс-цель для №11-eval: наш baseline прогнать против тех же датасетов; если RRF+inject дают <60% — D3/D2-апгрейды приоритизируются цифрами, а не интуицией.

**Резюме**: OpenViking — третий конкурент, чей «взять»-лист покрыт черновиком (D2/D3/D4/B9 + Stage 2 URI). AGPLv3 = код не берём, только идеи — что и делаем. Их бенчмарк-цифры усиливают №11: теперь есть публичные ориентиры для сравнения.

- 2026-09-04: v12 — детальная секция MemoryPalace. Маппинг: 1 (AAAK) → C9 · 2 (mine) → E11 · 3 (wake_up) → E10 · 4 (ttl) → B4 · 5 (auto-save N обменов+pre-compact) → УЖЕ ЕСТЬ (daemon auto-save по threshold + context_threshold re-arm + session.compacted wipe — KPI-подобные триггеры совпали) · 6 (wings/rooms/drawers) → C8.
  Все 6 — записаны. Новых механик в детальном описании — 3 микро-добавки:

### MemoryPalace детально — микро-добавки

1. **hall_keywords → маршрутизация записи по «залам»** — детерминированный keyword-роутинг направления записи (не поиска!) в topic-wing. У нас steering (D1.3) роутит ИНТЕНТЫ поиска; направление записи решает importance-gate, но не тему. → **Phase F (G1-опция)**: topic-классификация записи (A5-regex словарь тем) → wiki-тип/epi_tags-предзаполнение; дёшево, снижает долю «unsorted»-фактов. S.
2. **Tunnels (кросс-wing связи)** — у branches (D1.11) связей между скоупами НЕТ сознательно (изоляция полная). Но сценарий «факт из одного проекта ссылается на общий» реален (shared ssot у Люси). → **Парк/Stage 2**: при URI-redesign зарезервировать cross-namespace edge (по аналогии с peers/ из OpenViking — оба в один пункт Stage 2).
3. **Их ID-схема `drawer_{wing}_{room}_{hash}`** — подтверждение нашего Stage 2 URI-дизайна: человекочитаемый структурный ключ с хеш-суффиксом — ровно то, что планируем для wiki-путей (safe_title + hash уже в update()-фиксе general-3). Ничего не делать, чужой опыт сходится с нашим.
4. **Их LongMemEval R@5 96.6%/96.4%** — самая высокая публичная планка из всех разобранных (agentmemory 95.2, OpenViking LoCoMo 80–83). №11-цель: сначала догнать 95+ на LongMemEval-S, потом сравнивать с их формой.

**Резюме**: MemoryPalace — четвёртый подряд конкурент с «взять»-листом внутри черновика. Их «self-evolving» = скоринг+чистка (F7-урок подтверждён их же кодом: evolve() без decay/shadow-bin/консолидации — беднее нашего forget-примитива). Слабая модель эмбеддингов + отсутствие reranking/RRF — ещё один аргумент, что наш search-стек (RRF+5 источников+ACT-R) впереди; №11 покажет цифрами.

- 2026-09-04: v13 — детальная секция MemoryMuse. Маппинг «взять»: 1 (pinned+slots) → C4 · 2 (private journal) → C5 · 3 (WhisperGate) → E8 · 4 (introspection 24ч) → B10 · 5 (recency-формула) → D11 · 6 (charter) → C6 · 7 (scheduler-циклы) → E9 · 8 (soft-delete) → Shadow Bin (уже есть) · 9 (не брать стек) — согласен.
  Все 9 — записаны. Новых механик в детальном описании — 3 микро-добавки:

### MemoryMuse детально — микро-добавки

1. **max_entries на слой (default 20) + order** — у inject-блоков бюджет общий (2000), но per-слой cap отсутствует: «важные» блоки теоретически могут вытеснить все остальные. → **Phase F (спека инъекции)**: per-kind cap в build_inject_blocks (например important ≤N, gap ≤M) — дёшево, предотвращает монополизацию бюджета одним источником.
2. **inner_layer (inner monologue) — приватный слой вне семантики** — у нас ближайший аналог scratchpad (D1.15), но он УЧАСТВУЕТ в inject (kind=scratchpad, score 0.85). Идея «рабочие заметки, которые не ищутся, а только инжектятся/промоутятся» — флаг на scratchpad-записях (private: не попадает в search, только в inject и promote). → **Phase H** вместе с C5 (один флаг-механизм на оба случая).
3. **Порядок секций промпта (Cortex ВЫШЕ conversation и semantic recall)** — у нас порядок блоков в build_inject_blocks фиксируется списком append'ов; ранжирование «due_reminders > recall» осознанно не моделировано (reminders-слоя нет вообще). Не брать как есть (presence-продукт), но F-спека инъекции должна зафиксировать ЯВНЫЙ порядок kind'ов (сейчас он де-факто в порядке кода) — конфигурируемый словарь order. → **Phase F**, часть D5-контракта.
4. **Их Mnemosyne night-фазы vs наш _nightly** — у нас уже 6 фаз (cls_replay, graph_build, wiki_graph_build, skill_promotion, reflection, skill_reinforce); их decay/strengthen/orphan-sweep/re-embedding/cap-рёбер — это ровно Graph-санитария Phase G (уже записана) + B3-эвикт. Подтверждение: наш nightly — правильное место, структура фаз совпадает.
5. **8 циклов автономии при 3 LLM-классах (nano/mini/gpt-4.1)** — экономика подтверждает E8-вердикт: гейт инициативы (WhisperGate) — это harness-задача с nano-моделью, ariel остаётся keyless. Патент-pending + «license subject to change» — ещё одна причина не заимствовать механики напрямую (только идеи, как с AGPL у OpenViking).

**Резюме**: MemoryMuse — пятый подряд конкурент с «взять»-листом внутри черновика. Ценное из деталей: per-kind капы и явный порядок inject-блоков (усиление F-спеки инъекции), private-флаг объединяющий C5+scratchpad. Стек (Mongo+Qdrant+Memgraph+docker) — антипример local-first.
- 2026-09-04: v14 — ПАЙПЛАЙН ЭЛИ/ЛИЛИ (a-memory-l0-l4-pipeline.md + a-memory-graph-miners.md, /home/murat/cow/knowledge/analysis/) принят как ОСНОВА Phase F и Phase G. Мой v1 поглощён. Диагноз кода проверен 04.09 — все дыры реальны (L2 без сообщений, staging_-обрубки, decay_rate игнорируется промоцией, remember дублирует в граф, 4 параллельных входа). Поправки к их доку: CLACK не найден (сжатие = A3+zlib), add_edge «не вызывается нигде» устарело (B1.3/A1.6 пишут, минеры не наполняют), sentence-transformers 6.0.0 уже в venv (минер #9 возможен сейчас). Phase F = их конвейер + мои гейты/журнал/replay; Phase G = их 8+1 минеров + моя санитария/graph_enrich-оркестратор. Ключевые новые обязательства: канонические ключи (не обрубки), kind-роутинг инвариант/событие в G1, противоречия → memory_conflicts вместо молчаливого UPDATE, wiki = L4.5 [[fact:]]-linking без дублирования, L0-тиры жизни 30/180 + дистилляция освобождает сырьё, журнал co-retrieval пар (новый), пре-чистка JSON-узлов перед минерами.

## Sequencing (user decision 2026-09-04)

**Волновой порядок**: F-фундамент (L0-журнал, гейты, канонические ключи, privacy-стадия) → G-минеры (граф) → H (eval, CLI, доки) → **Stage 2 в конце** (slots/URI/аннотации — поверх устоявшейся поверхности). Не пересекать волны.

**Открытое исследование (запущено 04.09)**: добить конкурентов (Letta, Basic Memory, Cognee, Mem0, Zep/Graphiti) + научные статьи (VikingMem, A-MEM, MemGPT/Letta, HippoRAG, MemoryBank, Generative Agents, LongMemEval) — слить находки в draft до планирования.

- 2026-09-04: v16 — исследование конкурентов (general-4, web): Letta, Basic Memory, Cognee, Mem0, Zep/Graphiti. Полный отчёт — в notes сессии. Извлечения:

### Letta (MemGPT) — добавки

1. **Memory blocks с character limit** — у L4-гейта нет size-бюджета: append-only без compaction-давления. → **F 🔑**: per-блок лимит символов/токенов; nightly consolidation должна «упаковываться под бюджет» (compact-to-budget с эвиктом низкого ACT-R), не только дедупить.
2. **Sleep-time compute на idle/heartbeat** (arXiv 2504.13171 подтверждает) — наш nightly идёт по cron; добавить «dream on idle» (демон видит простой) — дёшево. → F/E9-cycles.
3. **Shared read-only blocks** (один block_id у нескольких агентов) → F: shared-маунт как first-class (ближний к A8-бриджу и peers/).
4. **MemFS: память под git** — wiki-L4.5 уже md-файлы: git-journal бесплатно даёт историю маркдауна дешевле аудит-лога. → H (усиливает C10).
5. `/doctor` + `/palace` viewer — подтверждение H-кандидатов (memory_audit, C11).

### Basic Memory — добавки

1. **Observations/Relations грамматика** `- [category] fact #tag (context)` / `- relates_to [[X]]` — парсимый формат факта БЕЗ LLM. → **F 🔑**: микро-синтаксис для L0-дистиллятора (kind/category/tags из скобок — A5-regex стандартизируется), синоним-канонические ключи живут в `[...]`.
2. **`memory://` URL-навигация** (depth-limited traversal как тул) → **G**: агент ходит по [[fact:]]-ссылкам (`memory://fact/<key>`), а не только flat-search. Потребитель C3-drill-down.
3. **Schema infer/validate/diff** — дрейф онтологии wiki-тегов виден детерминированно. → H.
4. **Rerank fail-fast, disabled by default** (cross-encoder jina-tiny-en, без молчаливого фоллбека) → H: шаблон для нашего D1-кандидата (env-gated, никогда не silent).
5. **doctor file↔DB reconciliation** — индекс-vs-источник (наш wiki_index vs md-файлы) — реальный фейл-режим, который мы не проверяем. → F-diagnose/H.

### Cognee — добавки

1. **Четыре глагола remember/recall/forget/improve** + `forget` как verb (dataset-scoped, incl. граф) → **F**: у гейт-поверхности нет verb-forget (GDPR-стиль удаления с провенансом) — слабое место, закрыть.
2. **Session-first read routing** (session cache → fallthrough в граф, SessionEnd → sync) → **F**: документированная политика чтения «горячая сессия → семантика», ложится на L0/L3/L4.
3. **AUTO_FEEDBACK после каждого answered query** (их was_useful = LLM-driven) + «бенчмаркить с включённой памятью» — честная заметка в наш eval-протокол (№11). → H.
4. **Fail-fast multi-tenant** (`ENABLE_BACKEND_ACCESS_CONTROL` — error вместо silent-fallback) → **H-инвариант**: no-silent-fallback для RRF-источников и user-scoping (усиливает D6).
5. **DATASET_QUEUE_ENABLED** (per-process write-guard) → F: file-lock гигиена L0→гейт записей.

### Mem0 — добавки + ГЛАВНЫЙ ВЫЗОВ

1. **🔴 Mem0 убил UPDATE-гейт (апрель 2026): single-pass ADD-only — один LLM-вызов, ничего не перезаписывается; LoCoMo 71.4→92.5** (старая two-phase ADD/UPDATE/DELETE/NOOP из arXiv 2504.19413 — брошена). Вывод: **конфликт-резолюция принадлежит read-time фьюжену, не write-time**. → **F-перекалибровка**: (а) наш bi-temporal ACCUMULATION уже в правильной парадигме (подтверждено); (б) **приоритет — ридер conflict.py (read-time fusion) ДО вложений в gate-time разрешение**; (в) канонические ключи важны для retrieval-boosting, не для «чей факт победит». Проверить цифрой на №11: gate-time vs read-time.
2. **Entity-matches как 6-й RRF-источник** (spaCy NER + embedding-linking, retrieval-boosting) → **G**: минер #3 (сущности) бесплатно становится поисковым сигналом, не только рёбрами.
3. **top_200 retrieval budget** (single-pass, no agentic loops) → **H**: фиксированный бюджет выборки до consume — тунабель, которого у нас нет.
4. **Query-type-aware temporal ranking** (current/past/future-intent → разная interval-семантика) → **H**: классификатор вопроса поверх get_at_time.
5. **Eval-харнесс отдельно от системы + плашка «platform vs OSS»** (их LoCoMo 92.5 — platform-only, OSS не воспроизводится) → **H №11**: публиковать конфиг компонентов при цифрах; свой харнесс — отдельный артефакт (`memory-benchmarks` у них открыт — взять структуру).

### Zep/Graphiti — добавки

1. **Bi-temporal окна на РЁБРАХ** (t_valid/t_invalid, invalidate-never-delete) — наши интервалы на фактах, минеры дают timeless-рёбра → **G 🔑**: каждый минер пишет validity-window на ребро; superseded-рёбра инвалидируются, остаются исторически queryable.
2. **Episodes = first-class queryable узлы** (provenance-слой, lineage-traversal) → **F**: наш L0-журнал = их episodes; сделать его узлом навигации, не только таблицей.
3. **Evolving entity summaries** (узел = обновляемый summary, не первая встреча) → **G**: nightly recompute entity-node content по последнему интервалу.
4. **Graph-distance rerank** (hop-count/connected-component как сигнал переранжирования, детерминированно) → **H**: дешёвый 6-й сигнал поверх RRF без LLM.
5. **Prescribed + learned ontology** («start simple, evolve as patterns appear») → легитимизирует наш staged-подход: детерминированные минеры первыми, типизированные рёбра — где появляются схемы.
6. **structured_output_mode/SEMAPHORE_LIMIT** — operational-мудрость для наших LLM-вызовов (staging/consolidation), если появятся. → H-заметка.

### Cross-cutting вызовы нашему дизайну (все 5 систем)

1. **Read-time fusion > write-time resolution** (Mem0) — приоритет: conflict.py-ридер раньше gate-time-логики; №11-эксперимент «gate vs read» обязателен.
2. **Size-бюджет — недостающий гейт** (Letta) — compact-to-budget в nightly с ACT-R-эвиктом.
3. **Рёбрам тоже нужны временные интервалы** (Graphiti) — G-минеры пишут t_valid/t_invalid.
4. **No-silent-fallback как инвариант** (Basic Memory + Cognee) — RRF-источники и scoping падают громко.
5. **Eval-харнесс = отдельный артефакт + конфиг-плашка** (Mem0/Cognee) — №11 оформляем standalone.

- 2026-09-04: v17 — исследование научных статей (general-5, web; 7 бумаг/8 arXiv ID, формулы и гипер параметры верифицированы). Полный отчёт — notes сессии. Извлечения по фазам:

### LongMemEval (arXiv:2410.10813, ICLR 2025) —蓝图 для H №1

- **Метрики**: LLM-judge (gpt-4o-2024-08-06, >97% human agreement) + Recall@k/NDCG@k с human-аннотированными evidence-позициями. **KU-scored STRICTLY**: retrieving только нового значения = провал — старое+новое должны появиться. → **F**: наши bi-temporal интервалы обязаны отдавать ПОЛНУЮ историю версий по KU-запросам, не latest-only.
- **5 способностей / 7 типов вопросов**: info extraction (user+assistant), multi-session reasoning, temporal reasoning, knowledge updates, **30 false-premise abstention** — у нас abstention вообще не моделирован → H-eval должен включать ABS.
- **Дизайн-находки**: key expansion фактами **+9.4% recall@k, +5.4% QA** (валидация [[fact:]]-расширения); **key merging > rank merging** (параллельные индексы −20-30% recall, m+1× bloat) — 🔴 вызов; time-aware query expansion **+6.8–11.3%** но требует сильного LLM (слабые генерируют ложные диапазоны → у нас bi-temporal-фильтр опционален/no-op без confident range); Chain-of-Note + JSON формат **+10 pts**; сортировка ретрива по timestamp.
- **BM25 равномерно худший** (vs Stella V5) на всех сеттингах → 🔴 демоут наших BM25-источников в RRF до minority-votes, мерить per-source contribution.
- **Бюджет ретрива**: GPT-4o растёт >20k, Llama-8B коллапсирует >3k → H: per-reader budget curve в отчёте.
- Сплиты: _s ≈115k токенов, _m = 500 сессий/1.5M; 500 вопросов.

### VikingMem (2605.29640, VLDB26) — добавки F + H

- **Entity = materialized view над event log**: `entity := SELECT OP(event.content) ... GROUP BY keys` с библиотекой операторов (LLM_MERGE/SUM/MAX/TIME_COMPRESS) → **F**: наша L4 store-семантика = эта идея; взять параметрическую формулировку вместо per-key логики.
- **EUA patch-обновления**: экстрактор выдаёт SEARCH/REPLACE-патчи, применение edit-distance по top-5 ANN-кандидатам — апдейт без LLM → **F**: дешёвая альтернатива LLM-merge на конфликтах.
- **One-pass schema extraction**: все типы памяти в ОДИН LLM-вызов (экономия ~(k−1)×) → **F**: kind-routing может эмитить все типизированные записи одним проходом.
- **OpenClaw (markdown coarse-grained memory) — худший baseline всей таблицы** → 🔴 **G/C-вызов**: wiki-слой = key/edge substrate (ссылки), НЕ retrieval unit; retrievable value = мелкозернистые факты/L0. Подтверждает «wiki ≠ поисковый словарь».
- **Двухсудейный eval-протокол** (GPT-4o-mini + GPT-4.1-mini) для rank-stability → **H**.
- Их LoCoMo 88.83/90.12, LME_s 66.36/75.80 — референс-планки; dynamic KG (Zep/Mem0-graph) проиграл event/entity+hybrid — подтверждение «детерминированные минеры, не LLM-KG».

### A-MEM (2502.12110, NeurIPS 2025) — G-добавки

- **Rich note embedding**: embed concat(content, keywords, tags, context), не голый контент → **F/G**: наш embedding-минер #9 должен кодировать расширенный текст (tags/aliases/канон-разложения) — иначе врёт.
- **LLM-adjudicated links** поверх top-k эмбеддинг-соседей; ablation multi-hop F1: 9.65 → 21.35 (links only) → **27.02 (full)** → **G-абляция**: один дешёвый LLM-проход подтверждения рёбер над top-k кандидатами в ночной consolidation — замерить, стоит ли нарушение no-LLM (локальная модель?).
- **Online neighbor evolution**: новая заметка триггерит обновление контекста соседей (у нас — только ночной batch) → G: partial online-обновление горячих соседей.
- k=10 retrieval, плато/деградация после k≈10-20 → H: k-sensitivity curve.
- Стоимость: 1216–2520 токенов/ответ vs MemGPT ~16977.

### HippoRAG 1+2 (2405.14831, 2502.14802) — 🔴 G-вызов

- **HR2 центральный вывод: structure-augmented RAG (включая KG+PPR) ПРОИГРЫВАЕТ стандартному dense RAG на базовых фактических задачах** → наш graph-expand — reranker для multi-hop ONLY; **абляция graph-expand OFF на single-hop LongMemEval-подсетах обязательна** (H).
- PPR (Personalized PageRank) — глобальная диффузия: на multi-hop/associative бьёт naive 1-hop expand, на factual проигрывает → кандидат в G: PPR над нашими минер-рёбрами как альтернатива расширению (замерить).
- HR2: passage-graph + PPR **+7% associative** поверх лучшего эмбеддера; passages = first-class graph nodes → **G**: рёбра passage↔fact (наши structural-минеры уже дают), сделать passage-anchored.
- Задачная таксономия для eval: factual vs sense-making vs associative — 3-way split в наш протокол.
- HR1: multi-hop до +20% над SOTA при **10-30× дешевле, 6-13× быстрее** IRCoT.

### MemoryBank (2305.10250) — вердикт по decay

- **R = e^(−t/S)**, S discrete init 1, **S+1 при recall, t=0 при recall** — это грубый ACT-R use-count с экспонентой: S=1 → R≈0.14 уже при t=2 (круче power-law в разы).
- **Вердикт: держим ACT-R power-law**; steal только **deletion gate** (prune при R < threshold) — подкрепляет B3/B5-эвикты. Плюс: иерархические дневные summaries → global (наш nightly-rollup той же формы — подтверждение).

### Generative Agents (2304.03442) — верифицированные формулы → H-протокол

- **Retrieval score = α_r·recency + α_i·importance + α_rel·relevance, α=1, каждый компонент min-max нормализован per-query**; recency = **0.995^(часов с последнего retrieval)** (экспонента по retrieval-времени, не creation). → **F/H**: у ACT-R-члена в multi_source нет per-query нормализации компонент — добавить; 0.995^h с ресетом = base-level learning — согласуется с ACT-R, не противоречит.
- **Reflection trigger: Σ(importance последних событий) > 150** (~2-3 рефлексии/день) → детерминированный on-demand триггер рефлексии в дополнение к cron (F/E9).
- Рефлексии возвращаются в общий stream — наш L3/L4 split богаче, но рефлексии должны искаться как события.
- **Interview-протокол**: 25 вопросов × 5 зон, human-rating vs «imposters» — дешёвый dev-qual харнесс believable-памяти (H); их top failure mode = failed retrieval + memory embellishments.

### MemGPT (2310.08560) — открытый вопрос F

- Self-editing memory как callable функции + interrupts/heartbeats: **агент-контролируемые vs автоматические гейты** — tension, который наш F решает в пользу авто; A/B-тест (H): дать агенту опциональные gate-опы (наш forget/think уже наполовину это) и сравнить качество с чисто-авто.
- FIFO+recursive summarization eviction = онлайн-консолидация без ночного джоба — для чат-нагрузок работает.

### CROSS-CUTTING вызовы (ранжировано, слияние с конкурентной волной v16)

1. **Key merging > parallel RRF** (LME): факты конкатенировать в источник (у нас [[fact:]] inline — есть), аблировать per-source contribution; параллельные пулы −20-30% recall.
2. **BM25 демоут** (LME): dense/miners весят больше; BM25 = minority votes.
3. **Graph = multi-hop reranker only** (HippoRAG2): graph-expand OFF на single-hop — обязательная абляция.
4. **Wiki ≠ retrieval unit** (VikingMem/OpenClaw): markdown-слой — ключи/рёбра, значения — мелкозернистые.
5. **Decay: ACT-R остаётся** (MemoryBank/GA согласуются), добавить per-query min-max нормализацию (GA) + deletion-threshold (MemoryBank).
6. **KU = полная история версий** (LME strict): bi-temporal читает old+new.
7. **LLM-adjudication рёбер** (A-MEM) — единственное место, где стоит рассмотреть локальную LLM в ночной фазе; решит №11-абляция.
- 2026-09-04: v17 — научные статьи (7 бумаг/8 ID). Ключевое: LongMemEval =蓝图 H (метрики+строгий KU+абстеншн); 4 cross-cutting вызова (key-merging>parallel RRF, BM25 демоут, graph=multi-hop only, wiki≠retrieval unit); ACT-R остаётся (MemoryBank/GA согласуются), + per-query нормализация (GA) и deletion-gate; минер #9 кодирует расширенный текст (A-MEM); LLM-adjudication рёбер — вопрос №11-абляции.

- 2026-09-04: v18 — пачка 1 (general-6, 8 источников про контекст/компакцию). Извлечения по фазам:

### Privacy (A1-гейт) — upgrade из LLM-Redactor (2604.12064)

1. **Placeholders вместо delete-strip**: типизированные стабильные плейсхолдеры `⟨EMAIL_1⟩` (same value → same placeholder; reverse map только в process memory, не персистится) — retrieval продолжает работать на де-идентифицированном тексте. → **F 🔑**: наш strip_secrets вырезает навсегда; placeholder+map сохраняет юзабельность.
2. **Трёхслойный детектор**: regex (структурные секреты) + NER (Presidio/spaCy) + локальный LM-классификатор (семантическая чувствительность). Верифицировано: A+B+C = 0.6% PII-ликов; НО **regex-only на прозе почти бесполезен** (person 12.3%, org 25.9% остаточные лики с NER; employee_id 79.8% regex-only). → наш A1-гейт = только regex → ложная безопасность; нужен NER-тир.
3. **Strict-mode**: детект <0.5 confidence → отказ, не passthrough. Инвариант: crash = un-restorable (правильный fail mode).
4. **Предел**: implicit identity («CFO, чья жена…») проходит ВСЕ трансформы (43.6% даже A+B+C) — реляционная идентификация не ловится content-трансформами в принципе. Честный потолок privacy-гейта.

### Компакция/инъекция — вызовы F

5. **Reacquisition cost — недостающая метрика eval** (2608.16370): компрессия GPT-5.5: completion 80%→85% (p=1.0, «не видно») при **retrieval-вызовах 21→63.9 (×3, p=.002)**. Completion-only скоринг слеп. → **H 🔑**: в харнесс добавляется «post-injection tool-call delta». Плюс: **random retention ≈ hindsight oracle** в одном из 6 cell'ов — важно ЧТО сохранено (D-state тип), а не сколь тщательно выбрано → oracle-абляции прежде доверия EMA.
6. **Rubric-компакция** (SelfCompact 2606.23525): tool без rubric → неравномерное срабатывание; **fixed-interval компакция переводит правильные ответы в неправильные в 40.4% переходов**; rubric = активный ингредиент (41.0→46.4%); компакция выгодна iff L/ℓ>10; probe-and-pop (KV-cache переиспользуется, вердикт почти бесплатен); summarizer append не substitute (стабилизация префикса). → **F**: компакция/eviction инъекции = state-aware rubric («суб-задача решена?»), не чистый token-бюджет; наш stable-first+cache:break подтверждён (Prefix-stability = 20-70% экономии, CWL).
7. **CWL: детерминированный LLM-free eviction по typed episode-graph** (expl/act, dependency-рёбра, acyclicity, 4 градуированных уровня strip) — accuracy parity с fresh-сессиями на 80M токенов; «dependencies dominate recency». → **G/F 🔑**: наши минер-рёбра = готовый eviction/injection-порядок (dependency-aware вместо чистого recency); инварианты схемы (typed directions, no cycles, L0-raw never evicted = protected prologue). Оговорка: у них dependencies заявлены агентом (ground truth), у нас inferred — mis-inference = premature eviction, нужен консервативный порог.
8. **ACON (ICML 2026)**: правила компакции = оптимизируемый натурально-языковой артефакт, итерируемый по fail-анализу агента (−26-54% токенов при росте качества; до +46% малым моделям). → **F/H**: memory_compress-rules → prompt-артефакт с outer loop (fail → revise rules), не one-shot.
9. **LLM-Redactor/123ofAI**: audit-trail каждой компакции (range/model/output/raw-refs) + recovery-path; negative evidence first-class (failed_attempts, do_not_retry) — наши L3 не моделируют негатив явно → фид в E6; D_critical (constraint-survival check) — инъект-метрика которой у нас нет.
10. **LCC (2602.21221)**: disposable-LoRA = 16× латентная компиляция контекста ≈ full-context upper bound; **manifold regularization** (random-query loss) обязательна иначе parrot-collapse (N_Q=0 → 0.0); gradient isolation (freeze consumer). → долгосрочная альтернатива zlib-холодному тиру (латентные буферы бьют text-pruning); **вызов CLACK «lossless-or-bust»**: компресия может улучшать recall (3.29 > upper bound 2.89) — но для нас lossless-CLACK остаётся контрактом аудита, латентный тир = опция H-дальнего горизонта. JIT-loading (2511.03728): инъекция несёт канонические ключи, контент по требованию (>6× system-prompt, 10-25× growth) — вызов flat stable-first.

- 2026-09-04: v19 — пачка 2 (general-7, 7 статей про long-term memory / state tracking). Извлечения:

### StateMem (2608.19652, UIUC) — 🔑 прямая спецификация для Graphiti-style validity windows

- **Детерминированный LLM-free Rechecker O(|E|)**: state units (id, content, priority hard/soft, source, deps ∈ {derived_from, coupled_with}); supersession → status=superseded (retained, inactive); dependents → needs_recheck — наша planned Graphiti-схема concretized.
- **StateMemWrapper: 4 precedence rules в read-prompt** (всего 155-токен инструкция, +15..+32 pts от структуры): later-supersedes-earlier; standing rules > instances; **derived values recomputed, never quoted**; retire только по явной supersession/expiry.
- **Цифры**: drift 44.4% подтверждённых фейлов LongMemEval-oracle; multi-session 71.4% vs KU 25.0%; enabling reasoning НЕ помогает (84→76%, p=0.34); **RRF-salience trap**: fusion «loudest wins», k-sweep flat на state-задачах (BM25 20% при любом k∈{5..40}) — k-tuning не лечит state-ошибки; **anti-trap пробы** (штрафуют always-prefer-latest) — lazy-reader эвристики как дешёвые trap-генераторы для eval.
- → **F/G/H**: supersession+dependency propagation (G), precedence-правила в read-путь (F), closed-pool probes + anti-trap + sequence-пробы (H-eval).

### CAMA (2608.19701, HKU) — anti-false-majority для RRF-фьюжена

- **Memory Correlation Bias**: воспоминания с общим source-корнем формируют ложные mostat при независимом подсчёте; **max-based slot presence** e_j = max_i z_ij (коррелированные записи не накачивают evidence mass); **N_eff = exp(log Σ p_j^α / (1−α))** (Hill diversity) + posterior entropy как sufficiency/stop → **принципиальный abstention-сигнал для false-premise eval** (лучше judge-only).
- **Recovery**: Expand(q′) альтернативные запросы / Trace(m_i) — проход по provenance-рёбрам к общим родителям (Trace снижает N_eff → детект корреляции). Верифицировано: false-majority 34.1 → 8.6 (decoupling), LongMemEval CMR 87.2; ΔAcc/kToken 1.14 (HippoRAG 1.07, MADAM-RAG 0.41).
- → **G/H**: dedup evidence mass по source_root в фьюжене (кап per-root), max-presence вместо аддитивного счёта, N_eff-абстеншн.

### 3M (2608.15451) — операторный словарь консолидации

- **Операторы**: Add/Update/Merge/Split/Connect/Compress/Prune + Conflict Detection/Repair (condition-splitting: ОБЕ записи с scope-условиями, не удаление), Generalize/Specialize/Abstract/Infer/Analogy/Find Gap/Verify. Merge хранит **Aliases field** = runtime synonym-словарь. → **F**: именовать наши nightly-фазы этим словарём; condition-splitting — опциональный write-time repair для инвариантов (доказывает: bounded write-time repair viable — смягчает Mem0-вызов).
- **Add gate = semantic novelty test** (вставка только если information content растёт) — дополнение к SHA-256: paraphrase-redundancy check; калибровка Jaccard (mean .274, bounds .000–.478).
- **Find Gap**: gaps знаний → proactive acquisition (наши L3-questions → gap-registry); **derived-флаг** отдельно от observed (origin-inspectable) → kind=derived + Verify-обработка.
- Их staged commit (Extract→ops→Verify, atomic replace, модель не может удалять файлы) — подтверждение наших staged mutations.

### SKILL.state (2608.26263, Google+Purdue, EMNLP) — mutable state projection

- **Σ_{t+1} = Σ_t ⊕ ΔΣ_t** (dict merge, null-удаление), reasoning выбрасывается после validated commit; deterministic validation, invalid patch → rollback-retry (мусорный вывод не портит состояние). Ошибки малых моделей: 68% premature overwrite, 12% syntax → validation-гейт обязателен.
- **Бюджет-мэтч контроль**: structured state 0.94 vs sliding-window 0.18 / LLMLingua 0.22 / summary 0.52 (same tokens) — **статистическая компресия разрушает точные реляционные зависимости**.
- **Ограничение (их признание)**: sufficient-statistic assumption фейлит, когда релевантность раннего наблюдения не была распознана при ingest → **append-only журнал (наш L0) — правильный субстрат; mutable state = ПРОЕКЦИЯ, не замена** (bi-temporal L4 уже полупокрывает). O(1) промпт / O(T) кумулятивно vs O(T²) append-only — конкретная bound для compact-to-budget.
- Верифицировано: T=100: 65,408 токенов vs 1,062,387 (16.2×); noise 50 events/turn: ≥0.97 vs 0.53.

### Прочие (коротко)

- **LCLM (2606.09659)**: chunk-ID + EXPAND(i) agent-loop — улучшение CLACK-холодного тира: архив = compressed skim layer + on-demand raw expansion (RULER +17..20 pts при 16×). Interleaved compressed/uncompressed сегменты в инъекции (не только head). **Reconstruction-only обучение коллапсируется в реконструкцию** → перед hot→warm демоушеном — needle-QA «reconstruction check» на сжатых чанках; zlib-архивы lossless именно поэтому.
- **P5 (2608.19535, ACM AI Leadership 2026)**: **net-benefit accounting** — компакция вычитает собственную стоимость; **mild compression (rate 0.9) = чистый energy LOSS** → warm-tier: **жать жёстко (≥3×) или не жать вовсе** (step-change 30→180д подтверждён против gradual trims); 3-mode controller (skip/safe/aggressive) вместо per-query budget search.
- **P2 (2608.15570)** — cond-mat физика, нерелевантна (ошибка в списке? — abstract-only, marked).

### CROSS-CUTTING (только новое, сверх v16–v18)

**F**: semantic novelty gate (paraphrase-Jaccard перед L4-insert); condition-splitting repair для инвариантов (опция); merge-aliasing (runtime synonym-словарь обновляется оператором Merge); warm-tier: hard-or-none + reconstruction-check перед демоушеном; kind=derived флаг; deterministic validation + rollback-retry для структурных апдейтов.
**G**: typed edges derived_from/coupled_with + unit status (active/superseded/needs_recheck) + O(|E|) recheck propagation; **provenance-root evidence dedup в фьюжене** (cap per source_root, max-presence); read-time Trace по provenance к общим родителям; N_eff как sufficiency-метрика.
**H**: StateMemBench-пробы (closed-pool, superseded value = scored outcome, anti-trap, sequence-пробы) в LongMemEval-протокол; **N_eff+entropy абстеншн** (калиброванный, не judge-only); per-substrate salience floors + k-sweep flatness отчёт (не тюнить k на state-задачах); net-benefit accounting компакции; **precedence-правила в read-промпт** (155 токенов, +15..32 pts — дешёвый verified win); ACT-R напрямую не оспорен ни одной из 7 статей (ближайшая угроза — salience trap, лечится precedence, не формулой decay).
- 2026-09-04: v19 — пачка 2 (StateMem/CAMA/3M/SKILL.state/LCLM/P5). Топ: StateMem deterministic supersession+dependency recheck = спецификация validity windows; CAMA N_eff + max-presence = anti-false-majority и abstention для фьюжена; 4 precedence-правила read-промпта (+15..32 pts за 155 токенов) = дешёвый verified win; SKILL.state: mutable state = ПРОЕКЦИЯ над append-only L0 (их собственное признание ограничений); warm-tier hard-or-none (mild = net-negative); semantic novelty gate + condition-splitting repair (3M смягчает Mem0-вызов для инвариантов).

- 2026-09-04: v20 — батч 1/3 (general-8): chimera, shiroe, gbrain, knowledge_graph, LightMem, ReMe, Acontext, EverOS.

### Батч 1 — уникальное

1. **gbrain — целевая цифра**: LongMemEval-S **strict recall_all@5 = 93.19%** (rerank off) / **95.32%** (Voyage rerank-2.5), retrieval-only, per-row receipts → **H**: наш aim-номер для №11 (MemoryPalace 96.6 — выше, но их стек другой).
2. **gbrain gap-analysis в ридере**: ответ явно флагует unknown/stale/uncited/contradicting → **G 🔑**: «detection without reporting is half a conflict system» — наш conflict-ридер должен ПРОЯВЛЯТЬ, не только фьюзить. Плюс `create_safety` verdict (exists/probable/unknown) как контракт выхода писателей.
3. **shiroe hash-chained append-only log** (`state verify` replay-integrity) → **F**: hash-цепочка на L0-журнал — бесплатный tamper-evidence (у нас append-only без цепочки).
4. **chimera triple cost-cap** (per-cycle / rolling-60m / per-task + `estimate` verb) → **G/F**: spend-gate вокруг ingestion/consolidation (runaway-защита ночного batch — в связке с E9-cycles).
5. **chimera: SQLite + recursive-CTE primary, Kuzu opt-in** (~95% query coverage) → подтверждение: граф-СУБД не нужна, projection-not-primary.
6. **chimera task-escalation memory**: failed task авто-повышает тир при следующей попытке + hot-signature alarm на повторные фейлы → **H**: repeat-failure detector в reacquisition-loop.
7. **EverOS + Acontext: agent-self track** (cases/skills отдельной сущностью от user-памяти) → **G 🔑**: kind-routing не имеет agent-self трека — дать процедурам/кейсам агента отдельный namespace (второй независимый сигнал после E6).
8. **EverOS orthogonal retrieval axes** (user_id/agent_id/app_id/project_id/session_id композабельно) → **F**: дешёвые scoping-фильтры на BM25.
9. **ReMe bounded-delta consolidation**: dream трогает только изменённые файлы, ~5 units/run cap → **F**: cost-bound ночного consolidation (не full-scan каждый раз).
10. **ReMe line-range chunk recall + bounded 1-hop wikilink expansion** → **G**: конкретная read-механика [[fact:]]-neighborhood, уважающая «links ≠ retrieval units».
11. **ReMe proactive surface**: dream-produced interest topics предлагаются агенту pre-turn → **H**: proactive recall отсутствовал в списке фаз.
12. **knowledge_graph dual-weight edge merge** (LLM-relation W1 + co-occurrence W2 → ОДНО ребро с конкатенацией лейблов) → **F**: merge-семантика для минер-рёбер из разных источников.
13. **LightMem KV-cache precomputation for memory** (offline lossless / online lossy — todo) → **H watch**: самая novel идея батча — persist prefill state для горячих memory-read.
14. **LightMem llmlingua-2 pre-compression входящих turn'ов ДО storage** (семантический, у нас zlib только warm) → H, оценить стоимость вызова.
15. **Acontext outcome-triggered learning** (task complete/fail → distillation в skill-файлы; SKILL.md задаёт схему) → **G**: outcome/procedural слой kind-routing + подтверждение «skill is memory».

### Батч 1 — covered/слабости (одной строкой)

shiroe Work-Graph = workflow versioning (covered: memory_history snapshots); knowledge_graph dual-weight — notebook-grade; LightMem online-update no-op placeholder; ReMe LongMemEval 89.4% agentic (reader included — не retrieval-only, несравнимо с gbrain напрямую); Acontext SaaS-first (PG+Redis+RabbitMQ+S3 для self-host); EverOS marketing-heavy, keyword-only; gbrain TS/Postgres стек, нет answer-accuracy run; chimera 0★ solo.

### Cross-cutting батча 1

«No repo has anything resembling our 3-tier journal lifecycle (CLACK), ACT-R decay, N_eff abstention, reacquisition-cost» — конкурентной угрозы нет; strongest borrows: **gbrain gap-reader (G), hash-chain L0 (F), agent-self track (G), outcome-triggered procedural (G), edge merge-semantics (F)**.
- 2026-09-04: v20 — батч 1/3 (8 репо). Топ: gbrain LongMemEval-S strict recall_all@5 = 93.19/95.32% (aim-номер №11) + gap-analysis reader (detection без reporting = полсистемы); hash-chain L0 (tamper-evidence бесплатно); EverOS/Acontext agent-self track (дыра в kind-routing); chimera triple cost-cap + SQLite-CTE- primary граф; ReMe bounded-delta consolidation + proactive surface; dual-weight edge merge.

- 2026-09-04: v21 — батч 2/3 (general-9): MemOS(hijzy), memgraph, **prism**, agent-context-code, Memori, engram, claude-mem, llm-wiki-cli, mnemosyne.

### Батч 2 — уникальное

1. **prism (основа эпистемического графа) — 3 пропущенные механики** → **G 🔑**:
   - **Valence-typed edges**: 10 типов (supports/refutes/supersedes/derives_from/specializes/contrasts_with/implements/generalizes/exemplifies/qualifies), valence решает RESULT BUCKET (primary/supporting/contrasting/qualifying/superseded) — наши минеры структурные, семантической эпистемической типизации нет.
   - **Convergence scoring**: узлы, достигнутые несколькими независимыми seed-путями, ранжируются выше `a·(1+λ·conv)` мультипликативно.
   - **SUPERSEDED bucket**: темпоральная valence ВИДИМО понижает устаревшие факты (не дропает) — комплемент bi-temporal + conflict fusion.
   - Плюс: two-stage edge build (cheap binary pre-filter → async batch classify, checkpoint/resume) — cost-паттерн если когда-нибудь LLM-mine рёбра; spreading activation (sum-pool, 0.7 hop decay, 0.6 reverse-edge penalty).
2. **State-tracking (StateMemBench из пачки 2 статей) получил подтверждение от MemOS**: NL feedback/correction каналы («correct/supplement/replace memory X») — у нас только read-side fusion, write-side correction channel отсутствует → **G**. Плюс **Memory Cubes** (composable KB с per-query `readable_cube_ids`) — изоляция тоньше нашего per-layer scoping → F; **MemScheduler** async ingestion queue (capture decoupled от agent turn) → F; OmniMemEval (14 продуктов × 10 датасетов) — eval-шаблон → H.
3. **claude-mem 93,136★ — РЕАЛЬНО (API-verified)**, но: token-inflated rebrand «Grok Mem» + CMEM crypto-token промо; суть = lossy LLM-compressed observations, БЕЗ lossless L0-аналога. Полезное: **per-result token labeling** (search ≈50-100 tok → timeline → get_observations ≈500-1000, ~10× savings) → **F**: маркировать token-cost прямо в результатах тулов; **`<private>` tag на capture** — дешёвый комплемент transcript-guard + placeholders (усиливает C5).
4. **mnemosyne — «automatic» = Hermes-plugin ONLY** (их домашний harness; для Claude Code/Cursor/Codex — plain MCP). Эмпирическое подтверждение: **autohooks-grade universal capture ≈ уникальна**. Полезное: **MIB bit-vectors 384d→48 bytes, Hamming в SQLite без ANN** (flat R@10, 35ms @10M, 9.4× compression) → H-путь для нашего embedding-минера; eval-гигиена (version-pinned bench rows + judge-mismatch caveats + 100% abstention reporting) → H-протокол; Temporal TripleStore valid_from/as_of = валидация bi-temporal.
5. **llm-wiki-cli — 4th layer «schema & purpose»**: per-project maintenance rules как first-class memory (у нас только importance rules.yaml) → **G**; **citation-safe spans** (heading path отдельно от snippet byte-range, stale-locator detection) → **G**; **atomic changesets** (sparse draft overlay DB, exact inverse patch, same-entity conflict fails closed) — богаче нашего single-mutation rollback → **G**; retention защищает pinned + open-contradiction записи от pruning — прямая связка с conflict fusion.
6. **engram**: **Git Sync** portable compressed chunks + Obsidian export — у нас вообще нет cross-machine portability story → **F/G**; `mem_review`/`mem_judge`/`mem_compare` — agent-facing tools аудита устаревшего и сравнения конфликтующих → **G** (у нас gates есть, agent-review surface нет); stable `topic_key` contract + runtime `mem_suggest_topic_key` → F-мелочь (наши канонические ключи).
7. **Memori (16.4k★)**: `entity_id × process_id` attribution на каждую интеракцию (3 оси — гранулярнее нашего scoping) → **F**; background augmentation taxonomy (attributes/events/facts/people/preferences/relationships/rules/skills — 8 типов) расширяет kind-routing меню → **G**; их «automatic» = SDK monkey-patching LLM-клиентов (не harness-level), full = cloud API. LoCoMo 87% @ 721 tok/query — подтверждение compact-to-budget.
8. **memgraph**: atomic GraphRAG (vector+traversal+prompt assembly в одном Cypher), SHOW SCHEMA INFO (self-describing ontology для агентов) → **F**; TGN/GNN link prediction — future upgrade embedding-минера → H. Wholesale не берём (граф-СУБД не нужна — подтверждено chimera).
9. **agent-context-code — POINTER MISLABEL**: это local semantic CODE SEARCH (tree-sitter AST + LanceDB + BM25 + RRF + reranker), не L2.5 project-memory. Единственное заимствование: **Merkle-DAG incremental indexing** (content-hash tree → только изменённые файлы перепроцессируются) для rebuild'ов wiki/L4.5 проекции → **F**. Нужен новый референс для L2.5-слота.

### Батч 2 — covered/слабости

MemOS upstream — Neo4j+Qdrant для self-host, cloud upsell; memgraph — BSL/MEL лицензия, infra alternative; Memori cloud-first quota gating; engram FTS5-only без векторов; claude-mem monetization noise; mnemosyne absolute recall низкий (20% R@10), bench numbers stale by own admission; LWC 50★ lexical-only.

### Cross-cutting батча 2

**5/9 репо целят в тот же harness-набор (Claude Code, OpenClaw, Hermes, Cursor...) — memory capture стал harness-plugin land-grab; наше autohooks-лидерство (3 harness) держится, только пока покрытие расширяется (F decision point).** prism дал 3 из 3 обещанных механик; pointers claude-mem/mnemosyne/agent-context-code проверены — два подтверждены, один mislabel.
- 2026-09-04: v21 — батч 2/3 (9 репо). Топ: prism — 3 пропущенных механики эпистемического графа (valence-typed edges → result buckets, convergence scoring, SUPERSEDED bucket) → G 🔑; claude-mem 93k★ реальны но lossy+token-модель (наш дифференциатор: lossless L0 + autohooks); mnemosyne «automatic» = Hermes-only (подтверждение уникальности autohooks); MIB bit-vectors 48-byte/Hamming — H-путь embedding-минера; MemOS Memory Cubes + NL correction channel; engram git-sync portability; atomic changesets + citation-safe spans (LWC); cross-cutting: harness-plugin land-grab (5/9 репо в тот же harness-набор — autohooks-лидерство держится пока покрытие расширяется).

- 2026-09-04: v22 — батч 3/3 (general-10): keep, ai-memory, memanto, obsidian-semantic-search, mazemaker, autograph, memory-os, Soul-of-Waifu, LightMem (повтор по запросу).

### Батч 3 — уникальное

1. **ai-memory (5673★, Rust) — Capture VERIFIED: автоматический** (lifecycle hooks → sanitized observations, session-end консолидация в wiki-страницы, zero-LLM дефолт FTS5+entities RRF) = autohooks-эквивалент на **20+ harnesses** → валидация нашего направления захвата; **competition: их покрытие шире** → F decision point остаётся. Уникальное: **typed handoff protocol** (handoff = first-class объект: owned, claim-once, кросс-harness эстафета, не конвенция) → **G**; **per-repo `[capture]` allowlist/exclude** + типизированная privacy-граница ДО записи → **F** (питает A1-гейт); **`experience/` pass** — абстракции, видимые только поверх траекторий (кросс-сессионный слой) → **G**. Слабость: инвариант «db rebuildable from wiki» — вики гейтит всё.
2. **keep**: **Declared-inverse edge tags** (`_inverse` на ключ в словаре писателя → двунаправленные рёбра `speaker:X`↔`said`) — рёбра из словаря, не из контента → **F** (дополнение минерам); **standing queries при чтении** — попадание в заметку поджигает `.meta/*` запросы (обязательства всплывают в момент извлечения) → **F** (дешёвый хук read-path, у нас standing queries есть, read-поджига нет); **git changelog ingest** (коммиты как заметки с рёбрами к файлам) → **F/G** детерминированный минер. Слабость: >3kB суммаризация (наш L0 честнее для eval).
3. **memanto — MIB-вердикт: covered** (проверка кода: тот же 128-byte binary + Hamming механизм, что наш rag/quantize.py; «стырила» только формат competitor-diff отчёта). Уникальное: **expiry policies** — retention-таблица по типам + именованные правила (first-match-wins, pins); expired остаются recallable с меткой `[EXPIRED]`, restorable, с provenance правила → **F 🔑**: уточняет наш deletion gate (мягкое истечение вместо жёсткого удаления — родня Shadow Bin); **estate-quality-over-time метрики** (contradiction rate, staleness, precision@month-6) + честная оговорка «бенчмарк-числа некросс-сравнимы» → **H**.
4. **mazemaker — трёхфазный dream engine** → **F/G 🔑**: NREM (spreading activation: +0.05 со-сработавшим, −0.01 неактивным, prune <0.05), REM (**мосты изолированных узлов** к похожим несвязанным, weight=sim×0.3), Insight (BFS-комьюнити → материализованные абстракции) — у нас в nightly нет edge-decay prune и REM-бриджинга как отдельных проходов (наш graph_enrich получает фазовую структуру); **negative-control protocol**: каждый механизм обязан иметь off-switch бенчмарк, который должен УПАСТЬ (shuffled-edge 1.00→0.27; post-dream 0.00→0.43) → **H 🔑**: переносимый стандарт доказательности — ablation-сьют для минеров/dream-фаз. Слабость: AGPL, open-core gating.
5. **autograph — schema-as-code**: один schema.json (типы карточек, папки, статусы, **per-type decay-ставки**) — машиночитаемый контракт для всех агентов-писателей; наш kind-routing зашит в код → **F**: вынести в декларативный контракт; **vault health score** (broken_links/orphans/desc_coverage/stale%) — непрерывный lint-метрик → **H** дёшево. Ebbinghaus с access-count ≈ ACT-R (ещё одно подтверждение); identity-dedup (email/handle) ≈ синоним-ключи.
6. **memory-os**: **Ground-Truth instruction layer** (SOUL.md/rulebook.md — явная «внедрённая память авторитетна», иначе агент перезапрашивает тулы для проверки уже-инжекченного) → **F 🔑**: дешёвый фикс инъекции (prompt-слой с обозначением authority); **semantic dedup cosine >0.92 → merge** (недельный сканер; у нас только SHA-256 exact — второй независимый сигнал после 3M/A2) → **F**; trust scoring с use-feedback → G (вероятно покрыто EMA — детали unverified). Слабость: Qdrant+Redis+ARQ Docker стек.
7. **Soul-of-Waifu**: почти всё covered (self-healing overwrite+correction-log = planned conflict-fusion reader; depth presets ≈ compact-to-budget; near-dup merge = A2). Единственное: **first-person diary как артефакт консолидации** → **G** опционально. Consumer-app, без eval.
8. **LightMem (повтор, уточнение)**: **lossy pre-compression gate** (LLMLingua-2/entropy) ДО LLM-извлечения — рычаг стоимости на пути hot→extraction → **F**; **topic segmentation как единица извлечения** (не turn/window) с шарингом precomp-результатов → **F**; **sleep-time offline update queue** со score_threshold (батчевые idle-апдейты между записью и ночью) → **G**; их eval-колонки tokens/calls/runtime = наш reacquisition-cost (покрыто).

### Cross-cutting батча 3

- **Три репо независимо сходятся на semantic dedup** (cosine-порог merge: memory-os, 3M, A2-план) — следующий шаг после exact-dedup, консенсус рынка.
- **ai-memory подтвердил autohooks-жизнеспособность на 20+ harnesses** — но их покрытие шире нашего: F decision point (какие harness'ы следующими).
- **negative-control protocol (mazemaker) + eval-гигиена (mnemosyne, memanto) + estate-quality metrics (memanto)** — три независимых источника сходятся на стандарте доказательности для H.
- MIB: origin-вопрос закрыт — механизм общий, «стырил» только diff-отчёт формат.
- 2026-09-04: v22 — батч 3/3 (9 репо) — РАЗВЕДКА ЗАВЕРШЕНА. Топ: mazemaker трёхфазный dream engine (NREM decay/REM мосты/Insight абстракции) + negative-control protocol (off-switch ablations = стандарт доказательности) → F/G/H 🔑; ai-memory autohooks-эквивалент на 20+ harnesses (typed handoff protocol, [capture] allowlist, experience/ pass) — валидация + competition; keep declared-inverse edges + standing-queries-at-read; memanto MIB=covered (проверено кодом) + expiry policies с [EXPIRED]-меткой + estate-quality-over-time; memory-os Ground-Truth instruction layer + semantic dedup cosine>0.92 (третий независимый сигнал); autograph schema-as-code + vault health score; LightMem lossy pre-comp gate + topic segmentation.

- 2026-09-04: v23 — Memanto paper (2604.22085, читать сама — ключевой источник MIB). explore-1 извлёк точные секции. Полный отчёт — notes.

### Memanto/Moorcheh — что мы НЕ вытянули (и что вытянули)

**Наши 3 гэпа по слоям:**
- MIB-глубина: только концепт «32× compression, no measurable loss», бит-селекция не раскрыта в этой статье (за формулами → 2601.11557). Наш quantize.py вероятно тот же класс (sign-binarization). Приоритет НИЗКИЙ.
- **EDM** (information-theoretic distance вместо cosine/Hamming: «scores by ability to reduce uncertainty in query context») — формулы НЕТ в этой статье; применим как re-ranker по shortlist (у нас brute-force, ANN-ограничений нет). Средний приоритет, зависит от 2601.11557.
- **ITS** (детерминированный [0,1] score + threshold gating): формулы нет; дешёвый суррогат — min-max нормализация Hamming в [0,1] + threshold 0.05. Дешёво.

**Главные рычаги из их ablation (LongMemEval/LoCoMo, верифицированные дельты):**
1. **Recall Expansion = крупнейший одиночный выигрыш**: k=10→40 + threshold 0.15→0.10 = **+20.4/+6.6 pp**. Итоговая конфигурация: **k до 100, ITS threshold 0.05, gating вместо fixed-k** = ещё +5.8/+3.4. Кумулятивно k 10→100 = **+28.4 pp**.
2. **Recall > Precision принцип**: «LLM — более способный фильтр, чем любой pre-computed retrieval structure, ценой лишних токенов» (обосновано LongMemEval-цифрой: точность растёт до 20K retrieved tokens; lost-in-the-middle зависит от позиции, не количества). → **H/F 🔑**: наш RRF отдаёт top-10-стайл — прямое указание поднять бюджет ретрива до 40-100 и отдать фильтрацию LLM.
3. Prompt Optimization почти ничего не даёт (+2.2/+0.1) — не тратить время на промпт-тюнинг ретрива.
4. **Inference model upgrade** = +4.8 pp на LME (Sonnet→Gemini 3) — читающая модель важнее промптов.

**13 категорий vs наши**: 9 совпадают. У них есть event/learning/error/artifact, у нас rule/todo/question/hypothesis. 🔑 **Каждый тип несёт priority/decay-сигнал** (fact=stable, commitment=time-critical, goal=until achieved, event=episodic decaying, context=highly temporal, learning=accumulating) — type-filtered retrieval. → **F**: добавить decay/priority-сигнал в TypePolicy (есть decay_rate — добавить retrieval-приоритет). error-тип = ближайший к E6 negative memory (у нас error_pattern в D1.8).

**Conflict Resolution**: same-type + same-namespace semantic matching → **уведомление агенту с 3 опциями: supersede / retain / annotate** (обе + conflict flag); до явного разрешения противоречие НЕ персистится. Цель — constraint drift/memory poisoning. → **F**: наша conflict-схема (ридер ещё не построен) = этот контракт: 3 опции + non-persistence до резолюции.

**Temporal Versioning**: as-of / changed-since / current-only query-модальности, non-destructive supersession (superseded_by колонка) — SQLite-совместимо, валидирует наш bi-temporal; changed-since модальность у нас отсутствует → добавить к get_intervals.

**Daily Intelligence**: автогенерация ежедневных session summaries + contradiction reports + interactive conflict prompts как **локальные Markdown-файлы** (= human-readable audit) — пересечение с A8-бриджем и C1-сценами.

**Memory Tax таблица** (верифицирована): Memanto 0 LLM/write, <10ms ingest; Mem0 ≈500ms; Mem0g ≈2s; Zep ≈3s. 10K ops/день: $0.50 vs $2.32/$1.70 → $662/год/агент. Complexity 0/4 (no graph DB, no LLM ingest, no multi-query, no recursive) vs Hindsight 4/4. → наш стек (0 LLM/write, SQLite) идеологически = Memanto; **обе статьи (их и HippoRAG2) сходятся: simpler architecture + optimized retrieval > graph complexity**.

**Ablation-методология**: 5-стадийный progressive ablation (baseline → recall → prompts → max-recall → model) — шаблон для нашего №11 (каждый компонент оценивается изолированно, дельты видны).

**Параметры**: budget 100 chunks, ITS threshold 0.05, 1 query/question (no multi-query/recursive), модель эмбеддингов НЕ названа; k=60 плато, inflection k=40; +4× token cost k=10→40 оправдан. Код: github.com/moorcheh-ai/memanto-evaluation (открытый!) — сверить нашу реализацию с их eval-скриптами при построении №11.

- 2026-09-04: v24 — вики-батка (general-13): iwe, obsidian-mind (4.6k★), agent-second-brain (autograph-движок), obsidian-second-brain (4.3k★), Ar9av/obsidian-wiki, llm-wiki, agent-context-code (подтверждён: код-поиск, НЕ L2.5). 14 genuinely-new механик самоорганизации.

### Wiki-самоорганизация — топ-находки → G/H

1. **Bookkeeping-файлы исключать из граф-алгоритмов** (Ar9av/obsidian-wiki) → **G 🔑**: их бенчмарк показал — index.md (ссылается на всё) fool'ит path/centrality-запросы; **наши MOC-хубы доминируют BFS/louvain так же** — исключить/занизить вес auto-indexes в A1.4/A1.6. Измеренный эффект: 4.4× быстрее, 44%→83% correctness на структурных вопросах (подтверждение graph-first инвестиции).
2. **Rewrite-not-append ingest + auto-reconcile** (eugeniughelbur 4.3k★): один источник обновляет 5–15 существующих страниц; /reconcile резолвит противоречия с документированием «почему»; supersession-aware search → **G/H**: наш docs_to_wiki только добавляет — переход на rewrite-семантику.
3. **Redirect-stub self-merge**: near-dup пары мержатся (dry-run default), удалённая страница = **redirect stub, никогда не удаляется** → **H**: сохраняет backlink integrity с первого дня (в связке с A2-similarTo).
4. **OKM freshness policy** («метаболизм» — компаньон Google OKF): каждый факт обязан быть *timeless / dated / pointer*; fast-changing факты НЕ копируются, линкуются с as-of штампом; enforced linter'ом → **H**: предотвращение staleness на записи (наш status=stale — after-the-fact).
5. **Ebbinghaus decay tiers** (agent-second-brain/autograph): strength `1+ln(access_count)`, 5 тиров core→active→warm→cold→archive, **touching promotes back up** — наш статичный lifecycle (active/stale/archived) становится динамическим → **G**.
6. **Random recall archived cards** рядом с контекстом («creative collisions») → **G**: эмерджентная навигация, которой у нас нет (ε-random в духе E-greedy).
7. **Blast-radius write guards** (iwe 1.6k★): каждая мутация декларирует сколько docs/blocks может задеть; mismatch → abort → **H**: write-gate для wiki-мутаций (обязателен поверх MCP).
8. **Write-time link validation** (obsidian-mind): «note without links is a bug» — PostToolUse хук БЛОКИРУЕТ md-записи без wikilinks/frontmatter → **H**: линковка enforced на записи, не swept потом.
9. **OKF frontmatter contract + schema inference**: per-type документные схемы, **infer из стора**, механическая валидация; find по frontmatter → **H** (в связке с autograph schema-as-code из v22).
10. **Declared-scope cross-repo memory** (obsidian-mind): reach (project/platform/general) объявляется НА ЗАПИСИ, не угадывается при чтении; **corrections supersede, not overwrite** (стор становится довереннее со временем); каждый read логируется → **H** (scope-field на фактах + supersede-семантика).
11. **Injection budget с pointer degradation** (obsidian-mind): бюджет по манифесту; за потолком дешёвые секции деградируют до *именованных pointer'ов* («silent loss worse than bloat») → **H**: token-budget для MOC/recall_protocol.
12. **Frontmatter Bases**: DB-views из frontmatter, incl. **Stale Actives** (untouched 14d) → **G**: авто-views как комплемент louvain.
13. **Nightly 5-phase maintenance + bounded recall with abstention + retrieval eval** (eugeniughelbur): close-day → reconcile → synthesize (пишет synthesis-страницы сам) → heal orphans → rebuild index; **≤4 notes/~900 chars, injects NOTHING при low-confidence, все решения логируются**; recall@k + MRR на natural questions → **H**: подтверждение N_eff-абстеншна + наш eval-протокол.
14. **Bi-temporal [[fact:]] metadata**: learned_at + valid_at на линках → **G** (тонкость к planned [[fact:]]).

### Батч 3 — covered/слабости

llm-wiki governance-heavy без граф-алгоритмов (orthogonal); agent-context-code повторно подтверждён code-search (L2.5-донор по-прежнему не найден); iwe propose-select-approve для docs_to_wiki (H, minor); Ar9av compile-not-accumulate + alias dedup (H minor).

### Cross-cutting батча вики

**Четыре источника независимо сходятся**: (1) auto-indexes/MOC опасны в граф-алгоритмах (Ar9av измерил) — наш A1.1 родился до этого урока; (2) rewrite-not-append > append-only для wiki-ingest; (3) redirect-stubs вместо удаления; (4) freshness должна проверяться на записи (linter), не после. **obsidian-second-brain (4.3k★) — ближайший референс нашего nightly-блока**: 5 фаз + bounded abstention + встроенный retrieval eval.

- 2026-09-04: v25 — движки/граф-фреймворки (general-12): cocoindex, cocoindex-code, txtai, semantica, code-graph-rag, open-index, autocontext. Все 7 — новые поколения (2 созданы 2026, три >11k★) — зрелость паттернов «инкрементальный движок» и «детерминированный KG без LLM» подтверждает направление a-memory.

### Батч движков — топ-находки → F/G/H

1. **autocontext (1.3k★) — outcome-gated promotion = недостающий дизайн G2-гейта** → **F/G 🔑**: выученный контекст НЕ раздаётся, пока не пройдёт matched screening → adaptive confirmation → held-out eval → **false-promotion budget** → causal attribution → atomic activation; rejected-свидетельства сохраняются для scoped retest. Вместо чистого min_weight — evidence-gated промоция (кандидат живёт в staging пока пробы не подтвердят пользу; бюджет ложных промоций ограничен). Плюс cross-run knowledge (playbook.md + hints.md + trace.jsonl — следующий прогон читает уроки прошлого = подтверждение scratchpad/promote + L0-replay) и ablation-attribution с отказом от edit-size-корреляции как каузальной (методология №11).
2. **cocoindex (11.5k★): memo = hash(вход) + hash(КОДА)** — кэш инвалидируется и при смене данных, и при смене функции → **F**: независимо подтверждает replay-config-hash дизайн (добавить в verdict-колонку). **Delta-only graph_enrich**: lineage-propagated delta через affected records, retire stale rows, не трогая неизменённое → **G**: минеры пересчитываются только для затронутых узлов вместо полного ночного ребилда. Rust-продукт — парк.
3. **cocoindex-code (2.7k★): snowflake-arctic-embed-xs** — локальный embedding по умолчанию через sentence-transformers (наш стек!) → **G**: готовый кандидат модели для минера #9 (крошечная, без ключей). Hook-driven инкрементальный реиндекс (SessionStart+PostToolUse) — подтверждение auto_save-ingress паттерна.
4. **txtai (12.9k★): граф из kNN-подграфа поиска** (graph-from-search-results, не из сущностей) + centrality/communities/path built-in → **G**: дешёвый генератор рёбер similar_to — top-k соседей per node вместо порога косинуса по всей базе; topic-кластеры валидируют louvain-MOC. Sparse+dense UNION внутрииндексный (элегантнее нашего 5-source RRF, но SQLite-only swap запрещён); HippoRAG2-caution применима. Фреймворк — парк.
5. **semantica (11.9k★): decision как first-class нода с read-поверхностью** — trace_decision_chain (каузальная родословная) / find_similar_decisions (прецеденты) / analyze_decision_impact (карта влияния) → **G 🔑**: наш causal-граф (E17a) умеет ЗАПИСЬ, но не имеет read-поверхности — готовый API nightly + memory_audit. **PROV-O стандарт** (W3C) для provenance вместо ad-hoc → H/typed_export; **point-in-time graph snapshots** — дёшево в SQLite, закрывает time-travel → G. Rete/Datalog/forward-chaining без LLM — парк до Stage 2.
6. **code-graph-rag (5k★): static+runtime overlay** — `cgr trace` мержит фактические вызовы (тест-ран/eBPF) в статический tree-sitter-граф («dispatch, который статике не виден») → **G**: архитектурный паттерн подтверждён чужим продуктом (наша пара: structural-miner статика + co-retrieval факты). **ast-grep pluggable tier** — graceful degradation для минеров (слабый язык → пониженный вес, не отказ). NL→Cypher через LLM — парк (no-LLM запрет).
7. **open-index (105★, создан 2026-08) — БЛИЖАЙШИЙ архитектурный родственник** (SQLite+FTS5, четыре примитива doc_type/schema/entity/connector): **zero-result search analytics** — провальные запросы = «что смоделировать следующим» → **G/H 🔑 ВСТРАИВАТЬ**: дешёвый минер key-expansion (LME +9.4% recall) поверх уже существующих recall_events; **storage: file|index per doc_type** (кураторские = git-tracked JSON, машинные = DB-owned) — независимое подтверждение нашего wiki-md/L4-KV разделения (v14); per-field boost, Stop-hook write-back (параллель scratchpad-promote). Остальное украсть-идею.

- 2026-09-04: v26 — библиотеки-кандидаты на встраивание (general-11): 7 проверок embed/park/skip.

### Библиотеки — вердикты встраивания

| # | Библиотека | Вердикт | Точка интеграции |
|---|-----------|---------|------------------|
| 1 | **spaCy 3.8.16 (+en_core_web_sm 12MB)** | **ВСТРАИВАТЬ** | privacy-gate NER-тир (regex-only на прозе: person 12.3%/org 25.9% остаточных ликов; sm закрывает большинство) + entity-минер #3 (doc.ents → co_mentions, один process-wide load). Лицензии: ядро MIT, модели CC BY-SA (атрибуция при дистрибуции). trf 436MB — парк. |
| 2 | **fractional-indexing (httpie)** | **ВСТРАИВАТЬ (vendored, CC0)** | `order_key TEXT` на wiki-страницах/канонических ключах/L0 — между-вставка O(1) вместо full-reorder. Single-file + tests, byte-compatible с rocicorp. PyPI 0.1.3 отстаёт от README 4.0.0 — vendor файл, сняв supply-chain риск. Лучший find волны. |
| 3 | turbovec (16.7k★, MIT, ICLR 2026, Rust-ядро) | **PARK** (не пре-альфа, но 5 мес + wrong regime: выигрывает на 100K+ fp32, у нас <1M × 128-byte) | **Скальпели в quantize.py**: length-renormalization scalar (`‖v‖/⟨u,x̂⟩` на encode — убирает систематическое занижение IP после квантования, бесплатный recall-буст) + per-coordinate calibration по ~1024 строкам (до +2.2pp R@1). Пара десятков строк numpy. |
| 4 | msgspec (BSD-3) | PARK | Только если micro-bench покажет pydantic >20% CPU на L0-записи (SQLite insert дороже — маловероятно). Несовместим с pydantic-моделями = дрейф схем. |
| 5 | SQLModel | **НЕТ** | 🚩 репо пересоздан 2026-02: 10★/1 contributor/0 releases (исторический tiangolo 14k+★ больше не там). Под капотом SQLAlchemy ORM. |
| 6 | SQLAlchemy 2.0 | НЕТ (уже dep alembic) | 10k-строк raw SQL → ORM = регрессия (теряем FTS5-контроль). Core как typed query-builder — точечно на новых модулях. |
| 7 | PageIndex (35.5k★, MIT) | НЕТ (сервис) | Это LLM-агентный спуск по дереву-оглавлению (LLM-вызов на query), не структура данных. **Механика переносима целиком**: wiki-L4.5 drill-down = TOC-дерево, которое строим сами (parent/child + node-summary off-line), retrieval = детерминированный спуск — «PageIndex без LLM-налога», бьёт similarity-search на структурированных вики. |

Порядок: №2 (час) → №1 (день + бенч утечек person/org до/после) → идеи №7 в бэклог → №3 bench-условие.
- 2026-09-04: v25 — движки (7 репо): autocontext outcome-gated promotion = недостающий дизайн G2-гейта (false-promotion budget); cocoindex memo hash(вход)+hash(КОД) подтверждает replay-config-hash; delta-only graph_enrich; semantica decision-read-поверхность (trace/impact/precedent) для пустого causal-графа + PROV-O + point-in-time snapshots; txtai kNN-подграф = дешёвый similar_to генератор; open-index zero-result-минер ВСТРАИВАТЬ (recall_events уже есть) + file|index split подтверждён; code-graph-rag static+runtime overlay.
- 2026-09-04: v26 — библиотеки (7): ВСТРАИВАТЬ spaCy sm (privacy NER-тир + entity-минер; regex-only = ложная безопасность) + fractional-indexing (CC0, vendor, order_key O(1)-вставки); PARK turbovec (скальпели: length-renorm + TQ+ калибровка в quantize.py — 2 строки numpy, recall-буст) и msgspec; НЕТ SQLModel (репо пересоздан, 10★ 🚩) и PageIndex (LLM-сервис, механика TOC-спуска украдена для wiki-L4.5).

- 2026-09-04: v27 — ITS/EDM объяснение найдено пользователем (после долгих поисков; в Memanto-статье формул не было). Формулы и разбор применимости к SQLite-стеку:

### EDM/ITS — формулы и наш путь реализации

**EDM (Efficient Distance Metric)** — концептуально:
```
EDM(m|q) = H(Y|q) − H(Y|q, m)   # прирост информации о Y при добавлении памяти m к запросу q
```
Блок высоко scored если: восполняет недостающий факт / однозначно определяет сущность-событие-инструкцию / разрешает противоречие / завершает цепочку рассуждений / делает вероятный ответ определённее. **Семантически похожий блок может получить НИЗКИЙ score если повторяет уже имеющееся** (косинус задает «какое воспоминание похоже?», EDM — «какое расскажет больше всего?»).

**Практическая форма** (их же): `EDM(m|q,S) = α·R(m,q) + β·N(m,S) + γ·G(m,q,S) − δ·K(m)` где R=relevance, N=неохваченная контекстом информация, G=предполагаемый ответ/улучшение reasoning, K=избыточность/стоимость.

**ITS** (это НЕ стандарт — внутреннее имя Moorcheh; вероятная форма):
```
ITS = I(C;Q) / H(Q)   # mutual information(чанк, запрос) / энтропия(запрос)
```
1 = чанк полностью исчерпывающе отвечает; 0 = ноль полезной информации. **Детерминизм**: score зависит только от (чанк, запрос), НЕ от модели/индекса/данных — «дай все где ценность ≥0.8» воспроизводимо vs «топ-5 похожих» меняется.

### Применимость к a-memory (SQLite brute-force scan — EDM/ITS реализуемы без ANN)

**Члены формулы — почти всё уже есть в стапе:**
- **R (relevance)** = наш FTS5-rank + Hamming-score (есть).
- **N (novelty/неохваченное)** = вычислимо: BM25-термы запроса, уже покрытые выбранным контекстом S, vs термы чанка (set-операции над токенами — дёшево).
- **G (gain/логика)** = частично A-MEM/ITS-gating; без LLM — эвристики (semantic-dedup cosine уже даёт «повторяет ли»; завершённость цепочки — минер #6 led_to-маркеры).
- **K (redundancy/cost)** = наш semantic-dedup (cosine >0.92, третий сигнал консенсуса) + memory_compress-стоимость.

**Дешёвый суррогат EDM для shortlist** (SQLite-scan даёт full control — EDM можно вычислить по top-100 candidates без ANN-ограничений):
1. Первый проход: RRF (k=60) → top-100 candidates (recall-first, LongMemEval-принцип).
2. Второй проход (EDM-фьюжен): переранжировать по `α·R + β·N + γ·G − δ·K` — где N/K детерминированы (set-операции), R из существующих скорингов, α/β/γ/δ калибруются на №11-eval (LongMemEval-протокол с oracle-абляциями).
3. ITS-нормализация: min-max финального скора в [0,1] per-query + threshold gating (их 0.05-порог) → **детерминизм: same query → same results** (их ключевое свойство для reproducible агентов).

**Порядок внедрения (F→H):**
1. **F**: threshold-gating вместо fixed-k (LME/ablation показали: recall-first главный рычаг +28.4pp).
2. **H №11**: реализовать EDM-фьюжен как ablation-arm (baseline RRF vs +N/K члены vs полный EDM) — измерить на LongMemEval, калибровать α/β/γ/δ.
3. **ITS-детерминизм** — флаг `deterministic_retrieval` (для регламентированных сценариев; по умолчанию off).

**Важная оговорка**: их цифры (89.8% LME) — платформенные (Moorcheh cloud, proprietary engine). Наш путь — воспроизвести ПРИНЦИП (uncertainty-reduction + threshold gating) на открытом стеке и доказать цифрами №11. Ссылка 2601.11557 остаётся точкой для EDM-формулы (в Memanto её нет).
- 2026-09-04: v27 — ITS/EDM формулы найдены пользователем (в Memanto их не было). EDM = H(Y|q)−H(Y|q,m) прирост информации; практическая форма α·R+β·N+γ·G−δ·K; ITS = I(C;Q)/H(Q) ∈ [0,1], детерминизм = score зависит только от (чанк, запрос). Путь: F threshold-gating вместо fixed-k; H №11 EDM-фьюжен как ablation-arm (α/β/γ/δ калибруются); члены R/N/G/K почти все уже в стапе (FTS-rank, semantic-dedup=K, set-операции=N). Оговорка: их цифры платформенные, наш путь — воспроизвести принцип на открытом стеке.

- 2026-09-04: v28 — батч A (general-14): Memanto-экосистема + memory-type статьи (12 бумаг).

### Батч A — топ-находки

1. **LEANN (2506.08276) ⭐ — recompute-don't-store для холодного тира**: индекс ≤50× меньше, ~5% от исходного размера, SOTA accuracy, латентность сопоставима — **эмбеддинги пересчитываются на лету, не хранятся**. → **F 🔑**: убивает вопрос «архивировать ли квантованные эмбеддинги в CLACK» — холодный тир хранит ТОЛЬКО сырой текст, re-embed при запросе; MIB-quantize остаётся для hot/warm только.
2. **RaBitQ (2405.12497, SIGMOD 2024) — MIB-upgrade**: error-bounded 1-bit/dim квантизация с ДОКАЗАННОЙ границей ошибки — 384-dim → 48 bytes (vs наши 128) c bitwise/SIMD distance. → **G**: наша 128-byte Hamming без error-bound; RaBitQ строго лучше обоснован и меньше.
3. **ENGRAM (2511.12960) — procedural как ТРЕТИЙ роут-тип**: our kind-routing invariants→L4 / events→L3; добавить how-to/procedure как роутимый kind (ложится на wiki L4.5) → **F 🔑**. Цифры: SOTA LoCoMo; **+15 pts LongMemEval при ~1% токенов** — «single router + plain dense-per-type + set-merge». 🔴 **Вызов**: наш 5-source RRF обязан доказать себя в 5-stage ablation против dense-per-kind (ENGRAM-style); не выиграет — режем источники.
4. **APEX-MEM (2604.14362) — fuse-then-summarize на read**: после 3-option конфликт-фьюжена сжимать разрешённое evidence в ОДИН contextual summary перед инъекцией → **G**. Планка: **88.88% LoCoMo / 86.2% LongMemEval** (ACL 2026). Независимая валидация append-only + read-time resolution + temporal grounding (наш точный bet).
5. **LLM-Independent Adaptive RAG (2505.04253) — pre-gate над RRF**: 27 внешних query-features (7 групп) решают «ретривить ли вообще» БЕЗ LLM — матчит LLM-based adaptive методы на 6 QA-датасетах → **F**: наша пайпа фаерит все 5 источников всегда; feature-gated skip = дешёвый тир над ними (cost + меньше шума) + питает N_eff/abstention.
6. **SHIMI (2504.06135)**: Merkle-DAG sync + CRDT conflict resolution + Bloom-фильтры как sync-prefilter → **G/парк**: только если multi-device a-memory появится на roadmap (наш conflict contract — single-store).
7. **SSR (2605.30120)**: SAE sparse codes → inverted index (без кластеринга), 15× быстрее индексация vs ColBERTv2, ~2× ниже латентность, лучше BEIR → **H**: альтернативный дизайн embedding-минера на существующей BM25-инфре.
8. **REWA (2512.00378) — теория для MIB**: rate-distortion bound как yardstick «достаточно ли 128 bytes» (witness-overlap = mutual information; O(Δ⁻² log N) оптимум для rank preservation) → **H** (анализ, без эмпирики).
9. **Merlin (2605.09990) — dedup перф-бюджет**: xxHash3-64 + SIMD flat set, 8.7 GB/s, 13.9–71% reduction → **F**: yardsticks для нашего dedup-гейта (impl открыт: corbenicai/merlin-community).
10. **LoCoMo дельта**: датасет шипит event-summarization + temporal/causal задачи помимо QA → добавить event-summarization в №11.

### Cross-cutting батчи A

**LEANN+RaBitQ вместе ставят под вопрос хранение эмбеддингов вообще**: cold — не хранить (recompute), hot/warm — error-bounded 48B вместо 128B. **ENGRAM+AdaptiveRAG** толкают «less machinery, gated» → 5-stage ablation обязана включить arms: «RRF 5-source vs dense-per-kind (ENGRAM) vs gated-retrieval (AdaptiveRAG)». Если RRF не выиграет — режем.
- 2026-09-04: v28 — батч A (12 бумаг, Memanto-экосистема+memory-types). Топ: LEANN recompute-don-t-store (холодный тир без эмбеддингов, ≤50× shrink); RaBitQ 48-byte error-bounded (MIB-upgrade); ENGRAM procedural как 3-й роут-тип + вызов 5-source RRF (dense-per-kind +15pts @1% токенов — ablation обязан включить arms RRF-vs-dense-per-kind-vs-gated); APEX-MEM fuse-then-summarize (88.88/86.2 планка); Adaptive RAG 27 query-features без LLM как pre-gate; Merlin dedup yardsticks.

- 2026-09-04: v29 — батч D (general-17, memory-systems, 12 бумаг). Формул MIB/EDM/ITS в 2601.11557 НЕТ (подтверждено пользователем; explore-2 отменён — суррогат-путь из v27 остаётся).

### Батч D — топ-находки

1. **LycheeMemory V2 (2608.12990, HIT) — segment-level consolidation**: батч обменов в семантически-связные сегменты (surprise/cohesion boundary detection), ОДИН LLM-вызов кодирования на финализированный сегмент (−86% construction tokens); **bounded disambiguation carry-forward** (entity-alias буфер между прогонами консолидации — канонические ключи стабильны без re-read истории) → **F 🔑**: nightly консолидирует L0 батчами на семантических границах, не per-message. Цифры: 89.22 LoCoMo / 92.20 LME-S. Признание: preference/persona вопросы требуют dedicated profile modeling (их слабость = наш L4 user-profile).
2. **Memora (2602.03315, Microsoft, ICML'26) — primary abstraction vs cue anchors**: каноническая identity decoupled от value; **non-exclusive anchor keys** дают many-to-many доступ, авто-prune при orphan → **G**: orphan-anchor GC для derived-ключей минеров. **Merge-threshold evidence: 0.8→0.6 similarity = 3.4× больше мержей при НУЛЕВОМ приросте accuracy** (update ratio стабилен 16.5-22.2% при росте стора) → **F 🔑**: прямое обоснование консервативного ITS threshold. RAG и KG retrieval = частные случаи одного retrieval framework (теория для multi-route). 86.3 LoCoMo / 87.4 LME.
3. **EverMemOS (2601.02163) — foresight signals с validity intervals**: forward-looking инференсы (планы, temporary states) хранятся с [t_start, t_end], read-time фильтр t_now ∈ interval → **F/G**: расширяет validity windows на ПРОСПЕКТИВНЫЕ факты (temporary states получают expiry, не только рёбра). **Sufficiency-check JSON** {sufficient, key_info_found, missing_information} + 2-3 targeted queries по named gaps → **H**: контракт для self-healing conflict reader. 93.05 LoCoMo (+9.2% rel.) / 83.0 LME.
4. **Mnemis (2602.15313, ACL'26) — System-2 Global Selection**: иерархический граф даёт top-down ИСЧЕРПЫВАЮЩИЙ traversal для aggregate-вопросов («list all X»), где similarity retrieval СТРУКТУРНО фейлится → **G 🔑**: hierarchical rollup (topic→scene→facts) как ОТДЕЛЬНЫЙ маршрут — у нашего RRF+EDM нет exhaustive-route. 93.9 LoCoMo / 91.6 LME-S.
5. **AdaMem (2603.16496) — question-conditioned routing**: resolve target participant first, graph-walk только когда вопрос реляционный → **G**: дешёвый question-classifier гейтит граф-проход (пара к ITS threshold gating). SOTA LoCoMo/PersonaMem.
6. **D3ST (2201.08904) — canonical keys как natural-language descriptions**, не terse ID → **F**: лучше match/merge accuracy + zero-shot generalization (принимаем к каноническим ключам).
7. SSET (NAACL'24): semantic AND structural agreement при edge-typing (подавление false negatives) → **G** маргинально. CompassMem: navigation-as-retrieval → H опция. ViLoMem: «grow-and-refine» naming. Memory survey: 6 операций — покрыто нашим 7-операторным суперсетом.
Cross-cutting: **LoCoMo SOTA band теперь 86–94** (GPT-4.1-mini класс); **три статьи независимо: гранулярность/батчинг консолидации — доминирующий cost lever**, не то ЧТО хранится.

- 2026-09-04: v30 — батч E (general-18, 9 бумаг; 3 SKIP по флагам — читаю сам).

### Батч E — топ-находки

1. **xMemory (2602.02007) — СИЛЬНЕЙШЕЕ внешнее валидация wiki L4.5 + consolidation-операторов**:
   - **Decouple-before-aggregate**: атомарные memory-компоненты (facts/constraints/updates) извлекаются ДО группировки; retrieval units изолируют дельту между near-duplicate историями → **G**: L4 extraction эмитит компоненты, wiki агрегирует над компонентами (не над raw spans).
   - **Uncertainty-gated top-down expansion**: compact backbone расширяется до raw только пока marginal predictive-entropy reduction > 0, стоп при нуле → **F**: заменяет fixed top-k на evidence-utility budget (согласуется с EDM/ITS).
   - **Retroactive restructuring**: ночной split/merge групп; frozen structure = 0% reassignment F1 38.59 vs full 44.91% ratio → 43.98 → **G**: наши consolidation-операторы.
   - Верифицировано: LoCoMo Qwen3-8B 34.48/43.98 @ 4,711 tok/query; group cap 12 (avg 4.48). Оговорка: component extraction LLM-based (натяжка с детерминированными минерами).
2. **RoMem (2604.11544, EMNLP'26) — policy-слой для bi-temporal L4** → **G/F 🔑**: **relation-volatility gate** (per-fact volatility из семантики отношения: «президент» ротируется быстро, «родился» стабильно) — decay/ревизия per volatility class вместо uniform recency; **geometric shadowing**: superseded факты НИКОГДА не удаляются — поворачиваются по фазе, темпорально-корректные outrank противоречия (усиливает ITS-демоушен). Детерминированный прокси: predicate-type volatility таблица. ICEWS05-15 72.6 MRR SOTA.
3. **Infini Memory (2606.10677) — topic document как revision unit**: наблюдения staged в буфер, периодически консолидируются in-place с fact revision ВНУТРИ дока → **G**: контракт «buffer-stage → consolidate» = интерфейс nightly-оператора (near-1:1 с нашим L0→L4.5). Iterative agentic read — конфликт с single-pass deterministic (парк).
4. **Aeon (2601.15311) — Semantic Lookaside Buffer**: сессионный hot-fact cache ПЕРЕД RRF (sub-5µs hits) → **F**: genuinely new для hot path. Sidecar blob arena + generational GC — deferred до SQLite-paper (она у меня в чтении).
5. **BookRAG (2512.03413) — BookIndex**: ToC-hierarchy + entity→node backlinks (graph-navigational retrieval над topic docs) → **G** wiki-L4.5; **information-foraging query router** (classify query type → tailored workflow) — read-path аналог write-side kind-routing → **F**. Цифр в abstract нет.
6. **SuperLocalMemory V3.3**: **Ebbinghaus forgetting coupled to storage fidelity** (decay = progressive embedding quantization, never delete) → **G**: L4 aging — старые факты теряют точность эмбеддинга, не существование (ложится на bi-temporal); **spreading activation как 4-й retrieval channel** → F кандидат. Оговорки: single author, Elastic License, unverified, V3.3 regression.
7. **LatentGraphMem**: fixed-budget symbolic subgraph interface как observability/eval surface (H: dump точно того, что retrieval выставил). PRISM Edit/SCM: skip/параметрика.
Cross-cutting: **«deterministic proxy для learned gate» — повторяющийся паттерн адаптации** (3 статьи зависят от обученных компонент); 3/9 — trained components (детерминированный a-memory не блокируется, адаптируется).

- 2026-09-04: v31 — батч B (general-15, графы/KG, 12 бумаг; 2 full-HTML: GAAMA, Mandol).

### Батч B — топ-находки → G

1. **GAAMA (2603.27910, full HTML) — два измеренных урока для минеров**:
   - **Mega-hub dampening ≠ exclusion**: person-узлы накапливают 400-500+ рёбер; dampening (θ/deg, θ=50) дал лишь marginal relief — **независимо подтверждает MOC-hub EXCLUSION** (v24 Ar9av); их topic-concept fix даёт ~30× sparser граф.
   - **PPR calibration**: edge-type-aware веса w_base(t), per-source normalization; additive score = cosine + w_ppr·PPR, **w_ppr=0.1**; PPR=1.0 scored WORSE semantic-only → **G**: граф = tiebreaker/neighborhood expander, никогда не override (подтверждает HippoRAG2-only posture). GRAFT (post-retrieval repair loop — диагностика провала → хирургическая вставка недостающих рёбер) превращает минеров из write-once в feedback-corrected, но требует LLM-вызов → парк.
   - Верифицировано: LoCoMo-10 79.1%, +4.2pp; graph-augmented только +1.2pp над semantic-only (тонко — согласуется с «graph underperforms dense»).
2. **Mandol (2606.29778, cs.DB, full HTML) — SOTA LoCoMo+LME, retrieval полностью LLM-free** (валидация deterministic-минеров):
   - **MAD-denoising порог**: τ = median(S) − κ·MAD(S) — distribution-free, робастный к outlier-скам вместо fixed cutoff → **G 🔑** (замена fixed miner-score cutoffs).
   - **Conflict arbitration**: конфликт = «same entities, inconsistent descriptions» ИЛИ «два abstract-узла трассируются к одному basic memory ID» (наши provenance-мосты = детектор конфликтов!); арбитраж по relevance × freshness × source confidence.
   - 5.4× retrieval / 4.8× insertion speedup @ 10 QPS.
3. **D-Mem (2603.18631) — quality-gated escalation**: дешёвый dense-first; мульти-хоп граф-reranker ТОЛЬКО при провале confidence-гейта; **восстанавливает 96.7% Full-Deliberation F1 при меньшем cost** → **G 🔑**: дизайн-паттерн, примиряющий HippoRAG2-only stance с сохранением графа (graph = gated fallback, не default).
4. **ElephantBroker (2603.25097, user-flagged) — 4-state evidence verification**: unverified → verified → stale → invalid (state machine на фактах/рёбрах; окна становятся ПЕРЕХОДАМИ, а не датами) → **G** (усиление Graphiti validity windows); 9-stage consolidation: strengthen-useful/decay-noise периодический проход (time-decayed reinforcement вместо статичных miner-весов). Оговорка: ноль внешних бенчей (2,200 internal tests) — inspiration, не evidence.
5. **Youtu-GraphRAG (2508.19855) — dually-perceived communities**: Louvain-структурные + embedding-similarity контента подграфа (semantic merge/split pass после louvain) → **G**: прямой upgrade чисто-структурных A1.6-комьюнити; 90.71% token-saving, +16.62% accuracy.
6. **Cognis (2604.19771) — pre-extraction versioning**: перед записью обновлённой памяти retrieve пересекающихся существующих + record version/supersede links → **G**: расширяет mutation ledger на РЁБРА (supersede-рёбра при истечении validity windows, не удаление); query-time freshness boost. SOTA LoCoMo+LME.
7. **MAGMA (2601.03236) — orthogonal typed views**: 8 минер-сигналов как независимо-toggleable edge views; per-query subset (temporal Q → session-proximity + validity only) вместо blended — эффект достижим фильтрацией одной typed edge table (4 физ. графа — оверинжиниринг) → **G**.
8. **AtomicRAG (2604.20844)** — валидация: атомарные self-contained facts + bare «relationship exists» рёбра (triple-extraction errors ломают reasoning paths); atom reassembly per query perspective. GraFine: SPX/GSR alternation (semantics-aware ADD + graph-aware PRUNE under time budget, no LLM) + **Topological Recall метрика** → G/H.
Cross-cutting: **3 статьи сходятся — минеры должны быть feedback-corrected post-retrieval, не write-once** (Mandol/GAAMA-GRAFT/ElephantBroker); numbers quality: только GAAMA/D-Mem/Mandol/Youtu публикуют цифры.

- 2026-09-04: v32 — батч C (general-16, retrieval/RAG, 12 бумаг). 2 ID-mismatch пойманы (2302.13253 ≠ LLMLingua (настоящий 2310.05736), LLMLingua-2 = 2403.12968, LongRAG = 2406.04224/2410.18050) — ledger поправлен.

### Батч C — топ-находки → F/H

1. **PrecisionMemBench (2605.11325) — precision-aware benchmark**: precision / noise isolation / session latency / **belief mutability** decoupled от answer quality; **whole-store dump = perfect recall, precision ≤0.22** (квантификация dump-everything failure) → **H 🔑**: precision+noise-isolation колонки рядом с LongMemEval. **Tenure** (их система): resolve scope+retrieval **детерминированно ДО inference**, inject typed beliefs as ambient instruction = production-валидация «deterministic first, LLM as filter» (ITS thesis).
2. **MOSS (2607.04391) — 1 год продакшена с нулём LLM в retrieval loop** (символьно, репродуцируемо, каждый шаг логируется) = сильнейшее public existence proof ITS-тезиса → **F** (validation). **Corpus-derived concept vocabulary** (569 concepts inductively, 322k аннотаций, без внешней онтологии) — паттерн: wiki/scratchpad-майнинг бутстрапит собственную таксономию. Масштаб: 44M токенов, 110k сегментов, 4 infra generation.
3. **SYNAPSE (2601.02744) — lateral inhibition**: spreading activation propagates relevance, **inhibition подавляет коррелированных соседей** → **F 🔑**: конкретный diversity-оператор для EDM re-rank (прямой ответ на wave-3 challenge «RRF loudest-wins / correlated evidence»). Triple hybrid = embeddings ⊕ activation-based graph traversal. LoCoMo SOTA temporal+multi-hop.
4. **2507.19715 — semantic compression = submodular-coverage selection** (representative set поверх top-k; facility-location дёшево при k≤100) → **F**: принципиальный diversity-aware фьюжен вместо ad-hoc MMR на EDM-стадии (второй оператор против correlated evidence).
5. **LGESQL (2106.01093) — edges-as-nodes trick**: line graph делает свойства рёбер скорабельными тем же ranker'ом → **F**: stale/low-confidence рёбра демоутятся без special-casing.
6. **H-MEM (2507.22925) — positional index encoding**: память несёт указатели на sub-memories следующего слоя; layer-by-layer routing без exhaustive similarity → **F**: coarse-to-fine candidate generation, чтобы дёшево попасть в k≤100 бюджет across 5 источников. Оговорка: hierarchy как read-time routing (write-time reorg конфликтует с Mem0 ADD-only уроком).
7. **MemTree (2410.14052) — incremental tree adaptation** (новое маршрутизируется в агрегированные узлы по embedding similarity, без offline rebuild) → **F**: кандидат для wiki-кластеризации/L4 topic nodes если flat retrieval сатурация. MiniRAG (2501.06713): semantic-aware heterogeneous graph + topology retrieval для SLM-режима (25% storage) — niche.
8. **PlugMem (2603.03296) — propositional + prescriptive knowledge как unit**: добавить **prescriptive node kind** в kind-routing (сегодня: invariants/events/facts) → **F** (рядом с D2.2). Verified: unchanged module бьёт baselines на 3 гетерогенных бенчах.
Cross-cutting: **MOSS + Tenure независимо подтверждают deterministic-retrieval-before-LLM** (ITS thesis — два production источника); **SYNAPSE inhibition + submodular coverage = два именованных diversity-оператора** для EDM-стадии.
- 2026-09-04: v31 — батч B (графы/KG, 12 бумаг): GAAMA измерил PPR calibration (w_ppr=0.1 additive, 1.0 хуже dense) + mega-hub dampening-фейл (exclusion подтверждена); D-Mem quality-gated escalation (dense-first, graph fallback = примирение HippoRAG2-stance с графом, 96.7% recovery); Mandol MAD-порог + provenance-based conflict detection, LLM-free retrieval SOTA; ElephantBroker 4-state evidence machine; Cognis supersede-рёбра; Youtu dually-perceived communities. Cross: минеры = feedback-corrected post-retrieval, не write-once (3 статьи).
- 2026-09-04: v32 — батч C (retrieval, 12 бумаг; 2 ID-mismatch пойманы: LLMLingua=2310.05736, LLMLingua-2=2403.12968, LongRAG=2406.04224): PrecisionMemBench precision≤0.22 при dump (H-метрики: precision/noise-isolation/belief-mutability); MOSS 1 год продакшена zero-LLM retrieval (existence proof ITS-тезиса) + corpus-derived vocabulary; SYNAPSE lateral inhibition + submodular coverage = 2 именованных diversity-оператора для EDM; Tenure deterministic-before-inference; H-MEM positional index для k≤100; LGESQL edges-as-nodes; PlugMem prescriptive node kind.

- 2026-09-04: v33 — 5 флагнутых статей прочитаны лично (abstract-уровень; full-тексты при планировании №11).

### Мои 5 статей — вердикты

1. **2608.24060 — scrydb «SQLite is Enough»** (Timo Breuer, MIT, код открыт): **академическое подтверждение нашего всего retrieval-стека** — lexical (FTS5) + semantic (sqlite-vec) + hybrid rank-fusion в SQLite, оценено на IR-бенчах с latency-трейд-оффами. → **F 🔑**: ближайший референс нашей архитектуры; **embed candidates: рассмотреть sqlite-vec extension** как опцию вместо/рядом с Hamming-scan (тот же SQLite, native vector ops, MIT). «SQLite is Enough» = название, валидирующее весь local-first выбор.
2. **2601.10080 — CDT: Codified Decision Trees (персон-логика из сюжетных линий)**: behavioral profiles = дерево условных правил (внутренние узлы = validated scene conditions, листья = grounded behavioral statements), **детерминированный retrieval правил на исполнении**; induction: candidate rules → validation → hierarchical specialization. Опередил human-written profiles и prior induction на 85 персонах. → **G/F**: паттерн применим к нашему personality/agent-facts слою — правила персон как кодифицированное дерево вместо неструктурированного профиля; deterministic rule retrieval пары с standing queries. Но: persona-эволюция остаётся MUSE-территорией (D3.x) — берём только механику codified rules, не применение к личности. (Ref: hivemind.md у Люси — прямое применение!)
3. **2512.22280 — Valori: Deterministic Memory Substrate**: float-арифметика даёт **недетерминизм между архитектурами** (x86 vs ARM): identical inputs → разные memory states/retrieval. Valori: fixed-point Q16.16 + memory as replayable state machine → **bit-identical states/snapshots/search results across platforms**. → **H/F**: наш retrieval-трейс (D4) и replay (F3) зависят от этой проблемы — SQLite сам по себе детерминирован на чтение, но float32-эмбеддинги + векторные op'ы не гарантируют bit-identical при переносе между машинами. Valori (open-source) — референс если захотим bit-exact replay across VPS-переездов. Сейчас: пометить как известное ограничение (наши бэкапы x86-only — ок).
4. **2605.09990 — Merlin (уже в v28 от батча A, это первоисточник)**: local-first, SIMD-friendly open-addressing flat hash + xxHash3-64, byte-exact dedup, 13.9–71% reduction, 8.7 GB/s; **MCP-интеграция описана прямо в статье** (zero-network-interception deployment для IDE/agents). → подтверждение: наш dedup-гейт может подняться до их performance-уровня бесплатно (impl открыт, corbenicai/merlin-community).
5. **2604.14362 — APEX-MEM (первоисточник, уже в v28)**: property graph с domain-agnostic ontology, **append-only storage preserving full temporal evolution**, multi-tool retrieval agent резолвит конфликты at query time → compact summary. ACL 2026, 88.88/86.2. Дополнение к v28: у них ontology domain-agnostic (наша kind-routing типизация — конкурентный подход), и их retrieval-agent — это multi-тул (дороже нашего single-pass EDM-плана; их compact summary = наш фьюжен+инъекция).

### Сводка моих 5

| Статья | Вердикт | Фаза |
|---|---|---|
| scrydb (SQLite is Enough) | 🟢 референс + sqlite-vec кандидат | F/H |
| CDT (персон-деревья) | 🟢 механика codified rules | G (не persona!) |
| Valori (deterministic substrate) | 🟡 известное ограничение | H (replay-заметка) |
| Merlin (dedup) | ✅ первоисточник v28 находки | F |
| APEX-MEM | ✅ первоисточник v28 находки | G |

(2601.11557: формул нет — подтверждено пользователем; суррогат-путь EDM/ITS из v27 принят.)
- 2026-09-04: v33 — 5 флагнутых статей прочитаны лично: scrydb «SQLite is Enough» (академическое подтверждение всего retrieval-стека + sqlite-vec как embed-кандидат), CDT codified decision trees (механика правил персон для G, persona остаётся MUSE), Valori deterministic substrate (float32 недетерминизм x86/ARM — заметка для replay), Merlin первоисточник (MCP-интеграция прямо в статье), APEX-MEM первоисточник (ACL 2026, domain-agnostic ontology).

- 2026-09-04: v34 — EDM.md (транскрипт Aurelle, 467 строк): реконструкция формул MIB/EDM/ITS + аудит статьи 2601.11557. ВОПРОС «ВЫТЯНУЛИ ЛИ МЫ ВСЁ» ЗАКРЫТ: формул в статьях действительно нет (реконструкция = модель для иллюстрации), и там где есть измерения — **наш простой quantize.py ≈ их fancy MIB**.

### Формулы (реконструкция Aurelle — модели, не публикации)

1. **MIB цель**: не локальная ошибка ‖x−x̂‖², а **сохранение порядка релевантности** s(x,y)>s(x,z) ⟹ ŝ(b(x),b(y))>ŝ(b(x),b(z)); возможная формулировка max[Σ I(B_i;Y) − β·Σ I(B_i;B_j)] (биты полезные для поиска, штраф на дубли). Энтропия бита H(B_i)≤1, пик при p=0.5; бит с p=0.99 почти ничего не сообщает.
2. **EDM**: база = XOR+popcount (u·v = m−2·d_H, s_H = 1−d_H/m); информационная версия — **взвешенный Hamming** d_wH = Σ w_i·1[b_i≠c_i], w_i = I(B_i;Y) или log(1/P(B_i=b_i)) — редкий информативный бит весит больше тривиального. ⚠️ это модель, статья EDM-формулу не утверждает.
3. **ITS**: Gain(x|q) = H(Y|q)−H(Y|q,x) = I(Y;X|Q); обобщённо ITS(q,x) = σ(α·I(Y;X|Q) − β·d_EDM + γ·r(x) + δ·m(q,x)), нормализация per-query (g−g_min)/(g_max−g_min), retrieval R(q) = {x: ITS ≥ τ} — **переменное количество документов по сложности запроса**. ITS = встроенный reranker (EDM → кандидаты, ITS → порядок) — убирает дорогой внешний reranker. ⚠️ нужна модель релевантности Y (обучение/калибровка) — механизм не раскрыт.

### Аудит статьи 2601.11557 (Aurelle, честно)

- Confounds сравнения: Pinecone тестили С внешним Cohere reranker, Moorcheh — со встроенным ITS; exhaustive scan vs HNSW; разная инфраструктура; **нет абляции MIB-vs-sign, EDM-vs-Hamming, ITS-vs-reranker в равных условиях**.
- 🔑 **КРИТИЧНО ДЛЯ НАС: авторы прямо пишут — BBQ, information-theoretic бинаризация и простая sign-бинаризация иногда дают СОПОСТАВИМОЕ качество; итог сильнее зависит от индексации и scoring**. → Наш quantize.py (sign) НЕ хуже fancy-MIB; гоняться за глубиной MIB не нужно. Ценность архитектуры = bitwise scan + ITS-порог, что у нас уже в плане (v27).
- Честные цифры MAIR: LegalQuAD 66.73% NDCG@10, LeCaRDv2 66.17, ConvFinQA 74.30; distance-only 9.6ms; end-to-end 219.4ms vs 1448.6 (Pinecone+Cohere). Задержка Moorcheh растёт с корпусом (exhaustive scan) — на малых масштабах (наш случай) не проблема.

### Новые указатели из поиска Aurelle (в бэклог G/H)

- **TOKI: Bitemporal Operator Algebra for Contradiction Resolution in LLM-Agent Persistent Memory** — алгебра разрешения противоречий поверх bi-temporal — прямой комплемент нашего conflict-fusion (G).
- **PROJECTMEM: Local-First, Event-Sourced Memory and Judgment Layer** — родня нашего L0.
- **Reliable Post-Retrieval Assembly: Separating Evidence Extraction from Policy Execution** — сепарация extraction/policy = наш gate-дизайн.
- **Presentation, Not Mechanism: Render Confound in Deprecation-Aware Memory Evaluation** — ловушка eval (deprecation-aware) → H.
- **Covariance Structure and Coordinate Heterogeneity Govern Binary Quantization** — почему sign-бинаризация работает/ломается → H (обоснование quantize).

### Вердикт по EDM/ITS-дорожке (финал)

1. quantize.py оставляем как есть (sign ≈ fancy — подтверждено их же данными); опционально взвешенный Hamming (w_i = log(1/P)) — 5 строк, дёшево.
2. ITS-нормализация per-query + threshold gating — главный рычаг (подтверждён Memanto-абляцией +28.4pp), реализуем без модели релевантности (min-max по кандидатам).
3. EDM deep-theoretic (взаимная информация с Y) — требует обученной модели релевантности = против no-LLM принципа; парк до появления labeled-данных из №11.
4. TOKI/PROJECTMEM/render-confound — в бэклог G/H указателями.

- 2026-09-04: v35 — deep-dive general-19 (4 статьи full-HTML, формулы дословно). Поправки к брифу: «PrecisionMemBench» = **Tenure** (72 кейса, не 89×13); MOSS бенчмарков НЕ имеет вообще; H-MEM не упоминает RAPTOR.

### SYNAPSE (2601.02744v2) — формулы spread activation + inhibition

- **Propagation (fan effect, ACT-R-родня)**: u_i⁽ᵗ⁺¹⁾ = (1−δ)·a_i⁽ᵗ⁾ + Σ S·w_ji·a_j⁽ᵗ⁾/fan(j); w_ji = e^(−ρ|τi−τj|) (temporal ρ=0.01) или sim (semantic).
- **Lateral inhibition** (формула дословно): û_i = max(0, u_i − β·Σ_{k∈T_M}(u_k − u_i)·𝕀[u_k > u_i]), T_M = M top-potential; **β=0.15, M=7** — давит когерентные distractor-кластеры (CAMA-проблема).
- **Triple Hybrid**: S(v_i) = λ₁·sim + λ₂·a_i⁽ᵀ⁾ + λ₃·PageRank; λ={0.5,0.3,0.2}; top-k=30.
- **FOK-gate**: τ_gate=0.12 на activation топ-нодa → детерминированный reject ДО LLM (FRR<2.5%).
- Гиперы: S=0.8, δ=0.5, γ=5.0, T=3 (самый чувствительный), τ_dup=0.92, K=15 top-incoming, GC activation<0.01×10 окон → архив, |V|≤10000.
- **LoCoMo (GPT-4o-mini) avg 40.5 F1** (beat Zep 39.7, A-Mem 33.3; +7.2 к A-Mem); **абляция**: −decay δ=0 → Temp 50.1→**14.2** (крах!); −fan → MH 35.7→30.2; −inhibition → Open 25.9→22.4; vectors-only 25.2.
- → **G/F порт**: inhibit-оператор с β≈0.15/M=7 как pre-EDM шаг (коррелированные distractor-кластеры) + τ_gate≈0.12 reject до LLM.

### MOSS (2607.04391v1) — 1 год продакшена: как устроен zero-LLM loop

- Масштаб: 110,183 сегмента ≈ **44M токенов**, ~600 файлов, SQLite ~5GB, 163k файлов каталогизировано, 4 поколения инфраструктуры.
- **Concept vocabulary 569**: (1) hand-injected seed-скелет, (2) большинство — **LLM keyword extraction индуктивно из корпуса** (Word2Vec/FastText реализованы и ОТБРОШЕНЫ по качеству; внешние таксономии тоже), (3) named entities. Codebook thematic analysis: коды эмерджентны из данных, применяются консистентно (322,662 аннотаций). Темы НЕ прекомпьютятся — собираются в query time группировкой концептов (**«query is sovereign»**).
- **Zero-LLM loop**: Intent [агент, стохастично] → QueryProfiler (структурные веса под интент: timestamps/valence-activation/concepts) → **Execution: детерминированный SQL (recursive CTE по 11 графам ~5M типизированных рёбер: co-occurrence, temporal adjacency, affective resonance (valence×activation, Russell circumplex), thematic cohesion), NO LLM** → Evaluation ≤15 reformulations. «Non-determinism confined to the edges; the deterministic core is traversed but never iterated».
- ⚠️ Бенчмарков НЕТ (A/B объявлен будущим) — «map usually suffices» (2-3 сегмента вместо ~50 чанков), affective-индексация одна из самых используемых на практике. Limitations: детерминизм только в execution, не formulation; single-user; нет защиты от memory-poisoning.
- → **F порт**: инверсия MOSS — **веса EDM (α,β,γ,δ) собираются в query time из интента** («relevance = свойство вопроса, не записи»), реляционные колонки в core_memory (timestamp/valence/concept) + лимит reformulations ~15 как цикл-гейт. affective resonance (valence×activation) — наш candidate для эмоционального слоя Эли.

### Tenure (2605.11325) — precision-first контракт

- **Таблица 1: 72/72 vs 8/72**; mean precision 1.0 vs **0.12**; recall 1.0 vs 1.0; drift 0.0 vs 0.43-0.50. Ключевой кейс: Redis-запрос → BM25 ровно 1 belief (score 11.71), cosine — все 12 в банде 0.667-0.799 (spread 0.132!) → **ситокосинусный band — враг precision**.
- **Deterministic-before-inference**: Stage 1 strip code/URLs → Stage 2 alias-weighted BM25 compound query (canonical+aliases, **14× boost**, fuzzy maxEdits=1 + prefixLength=2 блокирует mango→Mongo) → Stage 3 **hard scope filter POST-search** (два равных BM25 разводятся скоупом); pinned tier unconditional; counter-signal aliases (superseded-имена живут как aliases с негативной ролью, ceiling 25/белиф); promotion inferred→active = min reinforcement + min age.
- → **F порт**: «precision-first» двухступень — hard scope/status filter ПОСЛЕ RRF как discriminator (не weight); counter-signal алиасы в ITS; async extraction с validator/merger (4 ветки insert/reinforce/flag/skip).

### H-MEM (2507.22925v1) — positional index routing

- **4 слоя** Domain→Category→Memory Trace→Episode; **position encoding**: v_i⁽ᴸ⁾ = [semantic e_i, p_(i−1) self, p_i1..p_iK sub-memories] — similarity ТОЛЬКО по e, routing по указателям.
- **Алгоритм**: M_k⁽ˡ⁾ = ∪_{x∈M_k⁽ˡ⁻¹⁾} TopK_{y∈Child(x)} sim(q,y) — рекурсивный спуск; сложность **O((a+k·300)·D)** vs flat O(a·10⁶·D). Update: forgetting curve × feedback-weight (approval↑/none→decay/rebuttal↓).
- LoCoMo vs A-MEM (6 бэкбонов): **+14.98 F1 / +12.77 BLEU avg; MH +21.25; Adversarial +16.71**; efficiency: 7.34×10⁹→4.38×10⁷ ops (167×), <100ms vs >400ms.
- → **G/F порт**: positional-pointer структура L4-факт→ключевые→сессии с рекурсивным TopK-спуском — бюджет k≤100 без скана всего стора; feedback-модуляция листьев (наша importance — уже похоже).

### Cross-cutting deep-dive

**Все 4 сходятся: deterministic/structural gate ДО LLM** (SYNAPSE τ_gate, Tenure hard-filter, MOSS SQL-core, H-MEM pointer-routing) — ITS threshold-gating строить как **post-RRF hard discriminator, не weight**. **Correlated-evidence подтверждена извне дважды** (Tenure cosine-band 0.132 spread precision 0.12; SYNAPSE inhibition β=0.15) — EDM re-rank'у нужен inhibit-член по кластерам. ⚠️ 3 фактические поправки брифа зафиксированы (Tenure/72 кейса, MOSS без бенчей, H-MEM без RAPTOR).

- 2026-09-04: v35 — deep-dive general-19 (4 статьи full-HTML, формулы дословно). Поправки брифа: «PrecisionMemBench» = **Tenure** (72 кейса: 60 static + 12 session, НЕ 89×13); MOSS бенчмарков НЕ имеет; H-MEM RAPTOR не упоминает.

### SYNAPSE (2601.02744v2) — spread activation формулы дословно

- **Propagation**: u_i⁽ᵗ⁺¹⁾ = (1−δ)·a_i⁽ᵗ⁾ + Σ S·w_ji·a_j⁽ᵗ⁾/fan(j); w_ji = e^(−ρ|τi−τj|) temporal (ρ=0.01) или sim.
- **Lateral inhibition** (формула): û_i = max(0, u_i − β·Σ_{k∈T_M}(u_k−u_i)·𝕀[u_k>u_i]), T_M = M top-potential; **β=0.15, M=7**.
- **Triple Hybrid**: S = λ₁·sim + λ₂·a + λ₃·PageRank (λ={0.5,0.3,0.2}, top-k=30); **FOK-gate τ=0.12** → детерминированный reject до LLM (FRR<2.5%).
- Гиперы: S=0.8, δ=0.5, γ=5.0, T=3 (самый чувствительный), τ_dup=0.92, K=15, GC <0.01×10 окон, |V|≤10000.
- LoCoMo avg **40.5 F1** (Zep 39.7, A-Mem 33.3); абляция: −decay → Temp **50.1→14.2 крах**; −fan → MH 30.2; −inhibition → Open 22.4; vectors-only 25.2.
- → **G/F**: inhibit-оператор (β=0.15, M=7) как pre-EDM шаг против correlated distractor-кластеров + τ_gate reject до LLM.

### MOSS (2607.04391v1) — 1 год продакшена, анатомия zero-LLM loop

- 110,183 сегмента ≈ **44M токенов**; SQLite ~5GB; 4 поколения инфраструктуры.
- **Concept vocabulary 569**: seed-скелет + **LLM keyword extraction индуктивно из корпуса** (Word2Vec/FastText отброшены по качеству; внешние таксономии тоже); codebook thematic analysis; **темы НЕ прекомпьютятся — собираются в query time («query is sovereign»)**.
- **Loop**: Intent [агент, стохастично] → QueryProfiler (структурные веса под интент) → **Execution: детерминированный SQL, recursive CTE по 11 графам ~5M типизированных рёбер (co-occurrence, temporal adjacency, affective resonance = valence×activation Russell circumplex, thematic cohesion), NO LLM** → ≤15 reformulations. «Non-determinism confined to the edges; deterministic core is traversed but never iterated».
- ⚠️ Бенчей НЕТ (A/B — future); «map usually suffices» (2-3 сегмента vs ~50 чанков); affective-индексация — одна из самых используемых на практике. Limitations: single-user, нет memory-poisoning защиты.
- → **F**: **веса EDM (α,β,γ,δ) собираются в query time из интента** («relevance = свойство вопроса, не записи»); affective resonance — кандидат эмоционального слоя Эли; reformulation-лимит.

### Tenure (2605.11325) — precision-first контракт

- **Таблица 1: 72/72 vs 8/72**; precision 1.0 vs **0.12**; recall равный 1.0; drift 0.0 vs 0.43-0.50. Кейс: Redis-запрос → BM25 ровно 1 belief (11.71), **cosine — band из 12 белифов 0.667-0.799 (spread 0.132!)** — ситокосинусный band = враг precision.
- **Deterministic-before-inference**: Stage1 strip → Stage2 alias-weighted BM25 (canonical+aliases **14× boost**, fuzzy maxEdits=1 + prefixLength=2 блокирует mango→Mongo) → Stage3 **hard scope filter POST-search** (равные BM25 разводятся скоупом); pinned unconditional; counter-signal aliases (superseded-имена с негативной ролью, ceiling 25/белиф); promotion = min reinforcement + min age.
- → **F**: hard scope/status filter ПОСЛЕ RRF как discriminator (не weight); counter-signal алиасы в ITS.

### H-MEM (2507.22925v1) — positional index routing

- 4 слоя Domain→Category→Trace→Episode; **v_i⁽ᴸ⁾ = [semantic e_i, p_(i−1) self, p_i1..p_iK children]** — similarity только по e, routing по указателям.
- **Алгоритм**: M_k⁽ˡ⁾ = ∪_{x∈M_k⁽ˡ⁻¹⁾} TopK_{y∈Child(x)} sim(q,y); **O((a+k·300)·D)** vs flat O(a·10⁶·D); forgetting curve × feedback-weight (approval↑/none→decay/rebuttal↓).
- LoCoMo vs A-MEM: **+14.98 F1 / +12.77 BLEU; MH +21.25; Adv +16.71**; ops 7.34×10⁹→4.38×10⁷ (167×), <100ms.
- → **G/F**: positional-pointer L4-факт→ключевые→сессии с рекурсивным TopK-спуском = бюджет k≤100 без скана стора.

Cross-cutting: **все 4 = deterministic/structural gate ДО LLM** (τ_gate, hard-filter, SQL-core, pointer-routing) → ITS threshold-gating строить как **post-RRF hard discriminator, не weight**. Correlated-evidence подтверждена извне дважды (Tenure band 0.132; SYNAPSE inhibition).

- 2026-09-04: v36 — deep-dive general-20 (4 статьи full-HTML).

### Mnemis (2602.15313v2, ACL'26) — Dual-Route Retrieval

- **Иерархия**: bottom-up LLM категории по 3 инвариантам: **Minimum Concept Abstraction** + **Many-to-Many mapping** (child → несколько категорий) + **Compression Efficiency Constraint** (категория ≥n детей, |слой i+1| ≤ |слой i|; нарушение = терминирование ingestion).
- **System-2 Global Selection**: старт сверху, спуск послойно **без top-k constraint**, early stopping когда все дети ветки релевантны; категория несёт **агрегатную summary потомков** (скан категории = чтение precomputed cluster-summary). Слабости: temporal-вопросы (S2), multi-hop (S1).
- **Таблица LoCoMo (GPT-4.1-mini)**: Full Context 80.6 · Mem0 66.3 · Zep 61.6 · EverMemOS 92.3 · **Mnemis 93.3; k=30 → 93.9**. **LME-S: 91.6** (SSU 98.6, SSA 100, SSP 100, TR 86.5, KU 93.6). Routing ablation: S1-RAG 73.8 · S1-Graph 81.6 · S1 89.1 · S2-only 87.7 · **S1+S2 93.3** (прирост от global selection, не reranker).
- Cost: hierarchical graph построение = 1.39e7 токенов (vs base 3.87e7).
- → **G 🔑**: категорийная иерархия с инвариантами (compression constraint — защита от вырождения) + **S2 scan-маршрут для enumerative/multi-hop** (второй read-путь рядом с EDM/ITS; покрывает structural слабость similarity — «list all X»).

### Memora (2602.03315, ICML'26, Microsoft) — Harmonic Memory, create-or-update

- **Primary abstraction**: m=(a,v); консолидация: R=TopK(sim(a_i,a_m)) → фильтр U={sim≥γ} → LLM-селектор m*=J(a_i,U) → **Update(m*,a_i,v_i) | Create(a_i,v_i)**; update может рефайнить абстракцию.
- **Cue anchors**: F_c(a_i,v_i) = 1-3 якоря «[Main Entity]+[Key Aspect]» (2-4 слова, атомарные), many-to-many; **orphan-prune**: «Any cue anchor that loses all associations is automatically pruned».
- **Retrieval = MDP** (Algorithm 1): s_t=(q,W,F,budget), Stop при b≤0; GRPO-обучение политики (Eq11-13).
- **Теорема D.1**: **RAG = degenerate special case (identity cues, no abstraction); KG retrieval = special case (symbolic cues + graph expansion)** — формальное обоснование multi-route дизайна.
- **Таблица LoCoMo (LLM-judge)**: Memora(P) **0.863** > Nemori 0.794 > Mem0 0.653 (FC 0.825). **LME-S: 87.4%**. Build-up ablation: Mem0 0.653 → +abstraction 0.795 → +update 0.801 → +cue anchors 0.849 → +policy 0.863; записей **344 vs 651** (Mem0).
- **T17 полная**: No update 0.795/0% · **γ=0.8: 0.801, 21% ratio** · γ=0.6: 0.799, **68.2% ratio** → 3.4× апдейтов при нуле gain = over-consolidation.
- HP: γ=0.80; construction 1322s → 739.9s при index-offset (−45%).
- → **F 🔑**: канонические ключи = create-or-update с двумя ступенями (cosine ≥ γ=0.80 → верификация перед merge) + orphan-prune synonym-индекса; **высокий consolidation-порог (T17 = прямое доказательство против агрессивного слияния)**.

### LycheeMemory V2 (2608.12990, HIT) — boundary detection формулы

- **Segmentation (Eq1-4, embedding-only, LLM не вызывается в середине)**: s_t = 1 − max(sim(e_t,c_k), sim(e_t,h_k)) (surprise против centroid И последнего обмена); d_t = max(0, Coh(S_k) − Coh(S_k∪{x_t})); **p_t = σ(b + w_s·φ(s_t) + w_c·d_t + w_l·L_t + w_n·N_t)**; cut если p_t > δ=0.50 или token cap (300/600/900, max 10 обменов).
- **Alias buffer (Eq5-7)**: ρ_{k+1} = [d_k ; Recent(R_{k−m:k})] — bounded carry-forward (не растёт с историей); правило: «Use only to resolve references; never extract facts from it». τ-типы записей: fact/preference/event/constraint/**procedure**/**failure_pattern**/tool_affordance.
- **Retrieval**: planner (единственный generative вызов) → 4 канала → **RRF(d)=Σ 1/(κ+rank_j)** → cross-encoder per route → diversity.
- Таблицы: LoCoMo **89.22**; LME-S **92.20** (KU 97.44!); **−86% construction tokens** (механика: encoding calls T → |S|, avg 5.8 turns/segment).
- **Ablation**: eager per-turn 81.88 (−7.3pp, +645.8K tok); fixed-window 82.40; **w/o fusion/rerank/div 66.62** — fusion/rerank/diversity = крупнейший вклад.
- → **G/F**: p_t-скора сегментации с константами Table 10 = LLM-free оператор группировки L0-журнала; ρ-буфер; construction-tokens как first-class eval-метрика.

### EverMemOS (2601.02163) — self-organizing, foresight

- **MemCell c=(E,F,P,M)**: Episode (third-person, coreference resolved) + Atomic Facts + **P (Foresight): prospections с validity [t_start,t_end]** («temporary flu» vs «permanent graduation» distinction), read-time фильтр t_now ∈ interval, expired discard.
- **Консолидация**: sim > τ → assimilate + incremental update, иначе новый scene; **τ=0.70 (LoCoMo) / 0.50 (LME), max time gap 7/30 дней**; 13-49 scenes/conv.
- **Retrieval**: RRF over atomic facts → **MemScene score = max relevance among constituents** → top-10 scenes → Episodes → rerank → Foresight filtering → **sufficiency-check → 2-3 targeted follow-up queries по named gaps** (Pivot/Entity Association/Temporal Calculation/Concept Expansion/Constraint Relaxation).
- **Sufficiency JSON (strict)**: `{is_sufficient, reasoning, key_information_found: ["Fact (Source: Doc N)"], missing_information: ["Specific gap"]}`.
- Таблицы: LoCoMo **93.05** (Zep 85.22, MemOS 80.76, Mem0 64.20); LME-S **83.00**. Boundary ablation: **semantic segmentation 89.16 > oracle-session 87.66 > fixed-token-512 87.55 > fixed-token-1024 84.52** — сегментация бьёт даже oracle-сессии!
- → **F/G/H**: foresight-факты с validity на ПРОСПЕКТИВНЫЕ состояния; sufficiency JSON как read-time gate для targeted re-query; сегментация как оператор группировки L0.

Cross-cutting v35-v36: **консолидационные пороги «высокие» везде** (Memora γ=0.80 + T17 против 0.6; EverMemOS τ=0.70; Lychee δ=0.50) — outcome-gated promotion подтверждён консервативным порогом; **Lychee boundary detection = единственный воспроизводимый LLM-free сегментатор с полными константами** — наш оператор группировки L0; **два read-маршрута** (gap-driven EverMemOS для атомарных, scan-driven Mnemis для enumerative) оба дешевле top-k перебора.
- 2026-09-04: v35/v36 — deep-dive 8 статей full-HTML (формулы дословно): SYNAPSE lateral inhibition β=0.15/M=7 + FOK-gate 0.12 (LoCoMo 40.5, −decay крах Temp 50.1→14.2); MOSS 44M токенов zero-LLM loop (query is sovereign, веса из интента, 11 графов SQL-CTE, affective resonance); Tenure 72/72 vs 8/72 precision 1.0-vs-0.12 (cosine band spread 0.132 = враг precision; counter-signal aliases); H-MEM positional routing O((a+k·300)·D) +14.98 F1; Mnemis S1+S2 dual-route 93.3/91.6 (S2 scan-маршрут для enumerative — второй read-путь); Memora create-or-update γ=0.80 + T17 (0.6→3.4× merges zero gain) + теорема RAG/KG = degenerate cases; Lychee boundary detection Eq1-4 с константами (LLM-free сегментатор L0! бьёт oracle-session 89.16-vs-87.66, −86% tokens); EverMemOS foresight validity intervals + sufficiency JSON strict (τ=0.70).

- 2026-09-04: v37 — сверка чата Эли↔draft: 4 механики из чата НЕ вошли в draft (найдены grep'ом по ключевым словам). Плюс EDM-статья с VPS = та же 2601.11557 (дубликат транскрипта Aurelle — v34 уже покрыл, включая «формул нет» и «sign ≈ fancy MIB»).

### Из чата Эли — 4 пробела (дописать в Phase F/G спеку)

1. **Watermark / идемпотентность L0** («каждый raw обработан ровно один раз») → **F 🔑**: в draft есть идемпотентность replay (F3) и дедуп (A1), но watermark-механика ингеста (курсор по raw-записям: какой уже дистиллирован) не прописана — это диспетчеризация между L0 и ночным дистиллятором. Реализация: колонка processed_at/status в l0_journal + ночная выборка WHERE status='received'. Критично для «дистилляция освобождает сырьё» (без watermark не понять, что освобождать).
2. **L2-обогащение из L0** («L2 группирует L0 по временным окнам → темы, state_deltas; сейчас саммари сессий висит без сырья, а сырьё есть!») → **F**: у нас L2-sessions хранит summary без сообщений (проверено); чат Эли предлагает ночной проход, который биндит L0-записи к сессиям (по времени) и пересобирает summary/state_deltas из фактического сырья. Это заполняет дыру «сессия без содержания» и даёт минеру #4 (same_session) настоящие группировки.
3. **Behavior-аннотации тулов из tool_use-частотности** («tool_use → частотность, ошибки, паттерны → behavior-аннотации тулов») → **G/H**: минер триплетов query→tool→outcome даст статистику по каждому тулу (error-rate, средний результат) → авто-аннотации для MCP behavior hints (Stage 2-пункт №1 получает data-фундамент).
4. **EVOLUTION-дампы** как отдельный тип сырья классификатора L0 → **F**: в чате классификатор распознаёт `[EVOLUTION]`-маркер (self-обучающийся проход); в draft классификатор tool_result/recall/message/import есть, EVOLUTION-маркера нет — добавить в классификатор сырья (детерминированный префикс, как ariel_recall).

### Гидратация/«горячий путь» — сверка (в draft входят частично)

- «Дистилляция не на горячем пути — горячий путь = L0 append + L1» → входит в F-принципы (v14 «единый приёмник»), но явно не сформулировано как отдельный принцип → добавить строку в F-спеку.
- «Гидратация вниз» (recall по L4 → L3 → L0 «почему мы так решили») → покрыто C3-drill-down, но термин «гидратация» связывает с rehydrate (D3.5) — при спеке F назвать drill-down «гидратация вниз» для преемственности с их доком.
- Триплеты query→tool→outcome через tool_use_id-связку → покрыто (v14, поток «тулы»).
- 6 принципов линии (один вход, дистилляция off-hot-path, идемпотентность, провенанс, фильтры L4, двунаправленность) → в draft рассыпаны; собрать в одну «Principles»-секцию F-спеки.

### EDM-статья (VPS) — вердикт

Это та же 2601.11557 «From HNSW to Information-Theoretic Binarization», что уже разобрана в v34 через транскрипт Aurelle (EDM.md в workspace — тот же разговор в другом формате). Ключевые факты v34 остаются в силе: формул MIB/EDM/ITS в статье нет (реконструкция Aurelle — модель для иллюстрации); **sign-бинаризация ≈ fancy MIB по их же данным** (наш quantize.py не хуже); конфаунды сравнения (Pinecone с Cohere rerank vs встроенный ITS, exhaustive vs HNSW); честные цифры MAIR (LegalQuAD 66.73% NDCG@10, distance-only 9.6ms, end-to-end 219ms vs 1449ms).
- 2026-09-04: v37 — сверка чата Эли↔draft: 4 пробела дописаны (watermark-идемпотентность ингеста L0, L2-обогащение из L0 по временным окнам, behavior-аннотации тулов из триплетов, EVOLUTION-маркер классификатора) + сборка Principles-секции F-спеки из 6 принципов линии. EDM-статья с VPS = дубликат 2601.11557 (v34 уже покрыл).
