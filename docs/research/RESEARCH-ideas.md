# Research: Ideas for mcp-ariel-memory

> Сборник идей из референс-репозиториев для развития mcp-ariel-memory.
> Статус: поиск идей, не разработка.

---

## Заметки из предыдущих сессий

### 2026-07-10 — консолидированные идеи из 15+ репозиториев

**Priority 1 — easy wins (low effort, high value):** *(все 6 — реализованы в v1.8.1)*
- [x] 1. Session quality scoring (icarus): 5-component (depth, decision, recall_usage, linked_entries, user_engagement)
- [x] 2. Recall telemetry: track recalled vs actually used → usage_rate metric (signal = fact of dream call; `recall_events`)
- [x] 3. Auto training_value classification via DECISION_RE + OUTCOME_RE regex
- [x] 4. Daily brief tool: pending work + recent activity + suggested action in one call (`daily_brief`, tier `brief`)
- [x] 5. 6 analytical perspectives for wiki_summarize (pluton): practical/epistemic/psychological/social/temporal/metacognitive
- [x] 6. Auto-maintained INDEX.md for wiki categories (wiki lint `missing_index` auto-fix)

**Priority 2 — moderate effort:**
- [x] 7. Wiki lint tool с schema enforcement (6 checks: frontmatter, required fields, broken wikilinks, missing index, page length, unknown tags) — из memory-wiki-plugin
- [x] 8. Ref chain linking (review_of + revises) между wiki entries
- [x] 9. Promotion pipeline: detect preference signals → candidates → wiki pages
- [x] 10. Secret detection on wiki ingest (GitHub PATs, API keys, PEM headers)
- [x] 11. SPLIT/MERGE/RETIRE organic wiki operations

**Priority 3 — major features:**
- [x] 12. Dream cycle consolidation pipeline (pluton): mine→curate→compress→inject (inject step)
- [x] 13. CONTEXT.md compression: auto-generated ~3000 char summary for system prompt (`memory_context_inject` writes per-layer `CONTEXT.md`)

### Оценка репозиториев (из сессии 2026-07-10)

| Репозиторий | Что даёт | Установable? |
|---|---|---|
| hermes-memory-wiki-kit | FTS5 search, wiki_graph, wiki_health, 9 tools, auto-capture | Нет (plugin) |
| icarus-plugin | LLM extraction (OpenRouter), multi-source injection, CREATIVE.md fix | Нет (plugin) |
| pluton | Dream Memory, 6 perspectives, organic wiki ops, auto INDEX.md | Нет (plugin) |
| memory-wiki-plugin | Schema enforcement, promotion pipeline, secret detection, 55 tests | Нет (plugin) |
| Acontext | Skill memory, progressive disclosure, auto task detection | Нет (needs PG+S3+Redis+RabbitMQ) |
| agent-config | /recall protocol, shared skills SSOT | Нет (10+ repos ecosystem) |

### Ключевые концепции

- **Acontext "skill = memory"**: Wiki entries как Markdown skill files, progressive disclosure
- **/recall protocol**: 4-axis context recovery: session spine → conscious markers → expand → semantic → day-axis
- **hermes-memory-wiki-kit signal scoring**: corrections=5, decisions=4, preferences=4, config=2, URLs=1. Auto-capture at score>8
- **icarus multi-source**: fabric + Qdrant + sessions FTS5 + facts FTS5. Writes to CREATIVE.md (не MEMORY.md!)

---

## Источники

| Репозиторий | Описание | Статус |
|---|---|---|
| MemOS | Persistent memory for AI agents | general-1 ✓ |
| GPTCache | Semantic cache for LLM queries | general-1 ✓ |
| Selective_Context | Context compression | general-1 ✓ |
| agent-second-brain | Voice notes → knowledge base + daily report | general-2 ✓ |
| omem | Shared memory between agents | general-2 ✓ |
| Nocturne Memory | Long-term memory for MCP agents | general-2 ✓ |
| codebase-memory-mcp | Code memory for AI agents | general-3 ✓ |
| Command-Center-AI | Eternal context engine | general-3 ✓ |
| MoltBrain | AI brain architecture | general-3 ✓ |
| memorymuse | Self-aware AI with persistent memory | general-4 ✓ |
| llm-context-compressor | Context compression | general-4 ✓ |
| agent-context-code | Code context for agents | general-4 ✓ |

---

## agent-second-brain

Telegram-ассистент: голосовые заметки → транскрипция → классификация → задачи + Obsidian vault + daily report. Забывание по шкале Эbbinghaus (5 уровней: Core → Active → Warm → Cold → Archive).

### Ключевые фичи

**1. Graduated Touch Protocol**
Вместо сброса памяти в "active" при чтении — продвижение на один уровень за раз: `archive → cold → warm → active`. Повторные чтения = сильная память.

```python
# memory-engine.py cmd_touch()
if current_tier == "archive":
    target_days = (warm_threshold + cold_threshold) // 2
    new_tier = "cold"
elif current_tier == "cold":
    target_days = (active_threshold + warm_threshold) // 2
    new_tier = "warm"
```

**Для mcp-ariel-memory**: Реализовать постепенное продвижение вместо бинарного "обращался/не обращался".

**2. Creative Recall**
Случайная выборка из холодных/архивных записей для дивергентного мышления — "прочитай эти карточки и найди неожиданные связи".

**Для mcp-ariel-memory**: Добавить инструмент `creative_recall` — рандомный сэмпл из cold tier.

**3. Context Budget Calculator**
Отслеживание токенов: цель <25KB (6,000 токенов) для always-loaded контекста. Каждый 1KB = ~250 токенов на ход экономии.

**Для mcp-ariel-memory**: Добавить трекинг потребления токенов по уровням.

**4. Vault Health Scoring (100 баллов)**
Авто-детект осиротевших записей, починка сломанных ссылок, генерация MOC (Map of Content).

**Для mcp-ariel-memory**: Добавить диагностику хранилища — осиротевшие воспоминания, предложение связей.

**5. 3-Phase Daily Pipeline**
Capture → Execute → Reflect. Каждая фаза = чистый JSON для следующей.

**Для mcp-ariel-memory**: Структурировать write pipeline как явные фазы с JSON-чекпоинтами.

---

## omem (ourmem)

Production MCP memory server. Rust + TypeScript. 11-stage hybrid retrieval, 7-decision reconciliation, 3-tier sharing (Personal/Team/Org), Weibull decay.

### Ключевые фичи

**1. 11-Stage Retrieval Pipeline**
```
parallel_search → rrf_fusion → rrf_normalize → min_score_filter → topk_cap
→ cross_encoder → bm25_floor → decay_boost → importance_weight → length_norm
→ hard_cutoff → mmr_diversity
```

Ключевые новшества:
- **BM25 Floor** (Stage 7): Если BM25 score ≥ 0.75, floor = pre_rerank_score × 0.95 — защищает точные совпадения от штрафа rerankera
- **Length Normalization** (Stage 10): Штрафует многословные записи через `log₂(len/500)` — длинные записи не доминируют
- **Weibull Decay Boost** (Stage 8): β=0.8 (Core), β=1.0 (Working), β=1.3 (Peripheral)
- **MMR Diversity** (Stage 12): Jaccard dedup с порогом 0.85

**Для mcp-ariel-memory**: BM25 floor + length normalization — дешёвые улучшения качества поиска.

**2. 7-Decision Reconciliation Engine**
| Решение | Эффект |
|---|---|
| CREATE | Новая память |
| MERGE | Обновить существующую |
| SKIP | Дубликат, игнорировать |
| SUPERSEDE | Новая заменяет старую |
| SUPPORT | Увеличить confidence +0.1 |
| CONTEXTUALIZE | Новая с отношением к существующей |
| CONTRADICT | Конфликт (temporal → SUPERSEDE) |

**Для mcp-ariel-memory**: Реализовать хотя бы CREATE/MERGE/SKIP/SUPERSEDE.

**3. Dual-Stream Write**
```
Messages → Session Store (sync, <50ms) → Background Task (async LLM extraction)
```
Сырые данные сохраняются сразу, LLM-обработка асинхронно.

**Для mcp-ariel-memory**: Разделить fast-path хранение от slow-path извлечения знаний.

**4. Admission Control (5-dimensional scoring)**
```
composite = 0.1·utility + 0.1·confidence + 0.1·novelty + 0.1·recency + 0.6·type_prior
```
Category priors: Profile=0.95, Preferences=0.90, Patterns=0.85, Cases=0.80, Entities=0.75, Events=0.45

**Для mcp-ariel-memory**: Фильтровать перед сохранением — не хранить всё подряд.

**5. Weibull Decay Model**
```python
recency = exp(-λ · t^β)
λ = ln(2) / (half_life × exp(1.5 × importance))
β = 0.8 (Core) | 1.0 (Working) | 1.3 (Peripheral)
```

**Для mcp-ariel-memory**: Заменить линейный decay на Weibull. β-параметр по уровням — элегантное решение.

**6. Noise Filter (3-layer)**
1. Regex patterns (приветствия, благодарности)
2. Vector prototype matching (cosine ≥ 0.82)
3. Feedback learning (до 200 векторов шума)

**Для mcp-ariel-memory**: Добавить фильтрацию шума с обучением от обратной связи.

**7. Multi-Level Content (l0/l1/l2)**
Каждая запись:
- `content`: Оригинал
- `l0_abstract`: Однострочное саммари
- `l1_overview`: Структурированный markdown (2-5 строк)
- `l2_content`: Полный нарратив

**Для mcp-ariel-memory**: Хранить прогрессивные саммари для разных уровней доступа.

**8. Space-Based Sharing with Provenance**
Физические копии (не ссылки) с полной отслеживаемостью lineage:
```json
{
  "shared_from_space": "personal/alice-uuid",
  "shared_from_memory": "original-memory-uuid",
  "source_version": 3
}
```

**Для mcp-ariel-memory**: Добавить provenance tracking при шаринге между агентами.

---

## Nocturne Memory

Python MCP сервер для персональной памяти AI. Графовая модель (Node → Memory → Edge → Path), SQLite/PostgreSQL, FTS5, namespace изоляция.

### Ключевые фичи

**1. Graph Data Model**
```
Node (UUID, версия-независимый)
  └── Memory (версии контента, deprecated, migrated_to)
Edge (parent→child, priority + disclosure)
Path (URI cache: domain://path → edge)
```

Ключевой инсайт: **Node — версия-независимая сущность**. Обновление контента создаёт новую Memory-строку, граф не трогается.

**Для mcp-ariel-memory**: Разделить идентификацию сущности от версий контента.

**2. Version Chain with Migration Tracking**
```python
class Memory:
    migrated_to = Column(Integer)  # Указывает на преемника
```
При обновлении старая запись: `deprecated=True, migrated_to=new_id`.

**Для mcp-ariel-memory**: Реализовать цепочки версий — откат, аудит, история.

**3. Glossary Keywords (豆辞典)**
Ключевые слова связаны с нодами и всплывают при поиске. Доменная лексика.

**Для mcp-ariel-memory**: Добавить систему глоссария — доменные термины для улучшения ретриеваля.

**4. Weighted Random Recall**
```python
staleness_days = max((now - last).total_seconds() / 86400.0, 0.5)
mult = max(0.5 ** max(0, priority), 1e-12)
weights.append(staleness_days * mult)
```
Старые + приоритетные записи всплывают чаще.

**Для mcp-ariel-memory**: Заменить чистый рандом на взвешенный (staleness × priority).

**5. Orphan Detection and Recovery**
Диагностика: stale nodes, crowded nodes (>10 детей), orphaned nodes, duplicate aliases.

**Для mcp-ariel-memory**: Добавить инструменты диагностики — осиротевшие, переполненные, дублирующие записи.

**6. Access Logging with Context**
Отслеживание не только "когда", но и "почему" была обращена память.

**Для mcp-ariel-memory**: Данные для расчёта decay и аналитики использования.

---

## Сравнительная таблица

| Паттерн | agent-second-brain | omem | Nocturne | Возможность для mcp-ariel-memory |
|---|---|---|---|---|
| Decay | Линейный (0.015/день) | Weibull (β по уровням) | Нет (только staleness) | Заменить линейный на Weibull |
| Уровни | 5 (core→archive) | 3 (Core/Working/Peripheral) | По приоритету | Adopt tier-specific decay rates |
| Поиск | Сканирование файлов | 11-stage hybrid | Только FTS5 | Добавить BM25 floor + length norm |
| Dedup/Reconciliation | Нет | 7-decision engine | Version chain | Реализовать MERGE/SKIP/SUPERSEDE |
| Граф | Wiki-links | Flat per-space | Node/Edge/Path graph | Hybrid: graph + wiki-links |
| Random recall | Чистый рандом | Нет | Staleness × priority | Weighted random recall |
| Noise filter | Нет | 3-layer + feedback learning | Нет | Добавить noise filter с обучением |
| Versioning | Нет | Version field + staleness | Version chain + rollback | Version chains для аудита |

---

## ТОП-5 идей по приоритету

1. **Weibull Decay с tier-specific β** (omem) — Core помнятся долго (β=0.8), peripheral забываются быстро (β=1.3). Главное улучшение качества памяти.

2. **7-Decision Reconciliation** (omem) — Минимум: CREATE/MERGE/SKIP/SUPERSEDE. Предотвращает раздувание памяти и поддерживает консистентность.

3. **Graduated Tier Promotion** (agent-second-brain) — Не прыгать в "core" при обращении. Продвигать по одному уровню. Spaced repetition = сильные воспоминания.

4. **BM25 Floor Protection** (omem) — Если BM25 ≥ 0.75, защищать от штрафа rerankera. Дешёвое улучшение качества поиска.

5. **Version Chains с Rollback** (Nocturne) — Хранить deprecated записи с `migrated_to` указателями. Аудит, откат, исторический анализ.

---

## Дополнительные идеи

- **Obsidian export** (R1 из roadmap) — реализовать на основе agent-second-brain vault-системы
- **Daily report** — автоматический дайджест памяти (из agent-second-brain)
- **Voice → memory pipeline** — голосовые заметки через Telegram (из agent-second-brain)
- **Shared memory spaces** — Peronal/Team/Org изоляция (из omem)
- **Namespace isolation** — мульти-персональная память (из Nocturne)
- **Dashboard с diff/rollback** — визуальный ревью памяти (из Nocturne)
- **Semantic cache** — кэширование повторяющихся LLM-запросов (из GPTCache)
- **Progressive summaries** — l0/l1/l2 уровни детализации (из omem)
- **Noise learning** — обучение от обратной связи что считать шумом (из omem)
- **Token budget tracking** — потребление токенов по уровням (из agent-second-brain)

---

## MemOS

"Memory Operating System" для LLMs. +43.7% accuracy vs OpenAI Memory. Multi-modal, graph-backed, Dream pipeline.

### Ключевые фичи

**1. Dream Pipeline (фоновая консолидация)**
5-стадийный pipeline, работает когда пользователь отсутствует:
```
Signal Store (накопление) → Motive Formation (LLM решает что консолидировать)
→ Direct Recall (поиск связанных) → Consolidation Reasoning (LLM "мечтает")
→ Persistence (сохранение insight)
```

Ключевой инсайт: **Hypothetical Deduction Gate** — Dream actions сохраняются ТОЛЬКО если содержат `hypothetical_question` (какой вопрос помогает ответить). Zero-confidence → skip. Предотвращает низкокачественную консолидацию.

**Для mcp-ariel-memory**: Реализовать Dream pipeline — после N новых memories запускать фоновую консолидацию с LLM. Автоматическое создание insights вместо ручной реорганизации.

**2. DreamMemoryLifecycle**
`last_hit_at`, `hit_count`, `usefulness_score`, `invalidated_by_feedback`. Архивирование: no hits in 7 days → archive, low usefulness → archive.

**Для mcp-ariel-memory**: Lifecycle tracking для каждой memory — hit tracking, usefulness scoring, auto decay.

**3. Multi-Cube Knowledge Base**
Изолированные memory cubes с контролируемым шарингом и динамической композицией.

**Для mcp-ariel-memory**: Multi-tenant memory cubes — per-user/per-agent/per-project изоляция.

**4. Modular Search Pipeline**
`search_pipeline.py`, `rerank_pipeline.py`, `filter_pipeline.py`, `enhancement_pipeline.py` — каждая стадия pluggable.

**Для mcp-ariel-memory**: Разбить текущий монолитный search на явные stages: query parsing → vector → FTS5 → rerank → filter → post-process.

**5. Memory Feedback API**
Нatural-language обратная связь для коррекции memories.

**Для mcp-ariel-memory**: Пользователь корректирует memories на естественном языке.

---

## GPTCache

Семантический кэш для LLM. 10x дешевле, 100x быстрее. Плагинная архитектура.

### Ключевые фичи

**1. Pluggable Pipeline Architecture**
Каждая stage — swappable function:
```
cache_enable → pre_embedding → embedding → data_manager → similarity_evaluation → post_process
```

**Для mcp-ariel-memory**: Сделать retrieval pipeline плагинным — каждая stage заменяется.

**2. 7 Similarity Evaluation Strategies**
- SearchDistanceEvaluation — raw vector distance
- OnnxModelEvaluation — trained duplicate detection
- KReciprocalEvaluation — reciprocal rank fusion
- CohereRerankEvaluation — external reranker
- SequenceMatchEvaluation — weighted multi-signal
- TimeEvaluation — time-decayed similarity
- SbertCrossencoderEvaluation — cross-encoder

**Для mcp-ariel-memory**: Multi-signal reranking — combine semantic + temporal + frequency.

**3. LLM Semantic Verification Gate**
После поиска cache hit — lightweight LLM call для верификации что cached answer реально отвечает на вопрос.

**Для mcp-ariel-memory**: Верификация retrieval results — LLM проверяет что найденная memory реально отвечает на query.

**4. Temperature-Based Cache Bypass**
Вызывающий контрольлирует freshness vs cost. Temperature=2 → всегда bypass cache.

**Для mcp-ariel-memory**: Контроль "recency bias" — можно запросить свежие memories или все.

**5. Time-Decayed Similarity**
`final_score = semantic_similarity * time_decay(created_at)`. Свежие memories получают boost.

**Для mcp-ariel-memory**: Factor recency в ranking — старые memories нуждаются в большем semantic relevance.

---

## Selective_Context

Компрессия контекста через self-information (surprisal). 2x больше контекста в фиксированном window.

### Ключевые фичи

**1. Self-Information Scoring**
```python
self_info = [-logprob for logprob in logprobs]
```
Каждый token/phrase/sentence оценивается по surprisal. High self-info = неожиданный/новый = важный.

**Для mcp-ariel-memory**: Self-information как importance signal для memories — principled, model-based, не heuristic.

**2. Multi-Level Lexical Units**
3 уровня гранулярности: sentence → phrase (spaCy noun chunks) → token.

**Для mcp-ariel-memory**: Multi-granularity chunking — phrases вместо фиксированных чанков.

**3. Threshold-Based Masking**
`ppl_threshold = np.percentile(self_info, mask_ratio * 100)` — маскирование units ниже порога.

**Для mcp-ariel-memory**: Компрессия memories при превышении context budget — keep most informative.

**4. Conversation-Aware Processing**
Ролевые префиксы с высоким self-info (100.0) для сохранения turn boundaries.

**Для mcp-ariel-memory**: Preserve conversation structure при компрессии.

---

## codebase-memory-mcp

Чистый-C движок графа знаний кода. Tree-sitter AST, 14 MCP инструментов, 155 языков. Multi-pass pipeline.

### Ключевые фичи

**1. RAM-First Pipeline с LZ4**
Всё индексирование в памяти с LZ4 HC компрессией, затем дамп в SQLite. Нулевой footprint после индексации.

**Для mcp-ariel-memory**: Использовать in-memory SQLite для операций индексации, затем компактировать на диск.

**2. Function Registry с каскадным resolution**
Каскад стратегий: exact QN → import-aware → fuzzy bare name. Confidence scoring 0.0–1.0.

**Для mcp-ariel-memory**: Применить к сущностям памяти — "этот факт упоминает 'auth system' — это то же что 'authentication module' из сессии 3?" Score it.

**3. Louvain Community Detection**
Автообнаружение функциональных модулей через кластеризацию рёбер вызовов.

**Для mcp-ariel-memory**: Запустить community detection на графе памяти — автообнаружение кластеров тем (авторизация, JWT, token refresh).

**4. MinHash + LSH Near-Clone Detection**
`SIMILAR_TO` рёбра с Jaccard scoring. `SEMANTICALLY_RELATED` рёбра (score ≥ 0.80).

**Для mcp-ariel-memory**: Обнаружение дублей/перекрытий между сессиями. При сохранении — проверка на похожесть.

**5. Multi-Pass Pipeline**
```
structure → definitions → calls → usages → semantic → similarity → semantic_edges
→ gitdiff → route_nodes → infrascan → cross_repo → tests
```
Каждый pass изолирован и добавляет свой тип знаний.

**Для mcp-ariel-memory**: Отдельные pass для извлечения сущностей, построения связей, temporal linking, конфликтов.

**6. Shared Graph Artifact**
`.codebase-memory/graph.db.zst` — сжатый снапшот в репозитории. Команды.skip reindex.

**Для mcp-ariel-memory**: Ship compressed memory snapshot с проектом. Новые агенты загружаются из снапшота.

---

## Command-Center-AI — Eternal Context Engine (ECE)

Локальный AI memory layer. Obsidian Markdown + LanceDB vector search. Решает проблему context window compaction.

### Ключевые фичи

**1. Rule-Based Memory Classifier (~1ms)**
```python
LONG_TERM_PATTERNS = [
    (r"\b(always|never|prefers?|decided|from now on|rule:)\b", "decision", 0.9),
    (r"\b(i am|i'm|my name|my identity)\b", "identity", 0.8),
    (r"\b(0-tolerance|mandatory|non-negotiable|core directive)\b", "directive", 0.95),
]
```
Без LLM, ~1ms, покрывает 90% случаев. Force overrides: "remember this" → always long_term.

**Для mcp-ariel-memory**: Заменить LLM-based классификацию на regex patterns. Быстро, дёшево, надёжно.

**2. Bootstrap / Anti-Compaction Recovery**
```python
SESSION_HANDOFF.md  # что было сделано, что дальше
ACTIVE_CONTEXT.md  # текущий working set
FRESHNESS.md  # timestamp каждого файла
bootstrap_agent(reason="session_start")  # полное восстановление
```

**Для mcp-ariel-memory**: Реализовать `record_handoff` в конце сессии и `bootstrap_agent` в начале. Критично для multi-session.

**3. Store Protection**
Rate limiter: 30 writes/min. Dedup: 0.92 similarity threshold → skip near-identical.

**Для mcp-ariel-memory**: Rate limit per agent + dedup перед сохранением. Без этого memory раздувается быстро.

**4. Query Expansion from Vault Vocabulary**
```python
# Embed query, find N nearest tokens in vault vocabulary
# Append them to query for both vector and BM25 paths
expansion_terms = [self.vocab_tokens[i] for i in top_indices]
return query + " " + " ".join(expansion_terms)
```

**Для mcp-ariel-memory**: Построить vocabulary index из всех хранимых memories. При поиске расширять запрос терминами из vault.

**5. Nightly Maintenance**
Cache purge, short-term TTL expiry (30 дней), freshness report, doctor check.

**Для mcp-ariel-memory**: Автоочистка сессионных memories, TTL expiry, проверка здоровья.

**6. Privacy Namespaces**
`vault/agents/{agent_id}/` — per-agent изоляция. Cloud AIs не видят除非 explicitly requested.

**Для mcp-ariel-memory**: Multi-agent изоляция — каждый агент видит только свою память + shared.

---

## MoltBrain

Long-term memory для AI агентов. SQLite + ChromaDB. PostToolUse hooks для авто-захвата.

### Ключевые фичи

**1. 3-Layer Search Workflow**
```
1. search(query) → лёгкий индекс с ID (~50-100 токенов/результат)
2. timeline(anchor=ID) → контекст вокруг интересных результатов
3. get_observations([IDs]) → полные детали ТОЛЬКО для отфильтрованных
```

**Для mcp-ariel-memory**: Гениально для экономии токенов. Не возвращать полные memories при поиске — сначала индекс, потом контекст, потом детали.

**2. PostToolUse Auto-Capture**
```typescript
// Авто-захват вывода инструментов — без явного "store"
observationHandler.execute(input: { sessionId, toolName, toolInput, toolResponse, cwd })
```

**Для mcp-ariel-memory**: Авто-захват из использования инструментов. Агент читает файл → результат автоматически становится memory.

**3. Observation Schema**
```typescript
interface ObservationInput {
  type: 'discovery' | 'decision' | 'code_change' | ...;
  facts: string[];       # отдельные факты
  narrative: string;     # свободный текст
  concepts: string[];    # теги концепций
  files_read: string[];
  files_modified: string[];
}
```

**Для mcp-ariel-memory**: Структурированный schema с отдельными facts + narrative + concepts. Facts индивидуально searchable, concepts для кластеризации.

**4. Context Cache с Invalidation**
Кэш по (project + session + query + limit), TTL 10 минут. При обновлении memory — инвалидация.

**Для mcp-ariel-memory**: Кэширование сгенерированного контекста с умной инвалидацией.

**5. Session Lifecycle Hooks**
```
SessionStart → inject context from memory
PostToolUse → capture output, extract facts
Stop → generate summary
```

**Для mcp-ariel-memory**: Три хука: начало (инжект), во время (авто-захват), конец (саммари).

---

## TencentDB Agent Memory

Слоистая память для OpenClaw/Hermes агентов. Символическая память (Mermaid canvas) + L0→L3 pyramid. -61% токенов, +51% pass rate.

### Ключевые фичи

**1. Symbolic Memory (Mermaid Canvas)**
Вместо многословных логов — компактный Mermaid syntax. Node ID ссылаются на полный текст.

**Для mcp-ariel-memory**: Символьная компрессия —摘要记忆追踪 в компактном graph representation.

**2. Progressive Disclosure**
Верхние уровни (personas, canvases) в Markdown для людей; нижние (raw text) в БД для retrieval.

**Для mcp-ariel-memory**: Возвращать саммари сначала, drill down по требованию.

**3. Full Traceability**
Каждая абстракция ссылается на ground-truth evidence. "Persona → Scenario → Atom → Conversation".

**Для mcp-ariel-memory**: Гарантировать трассируемость — каждая memory abstraction связана с источником.

---

## PRISM (Epistemic Graph RAG)

Типизированный эпистемический граф поверх vector store. 10 типов рёбер, spreading activation.

### Ключевые фичи

**1. Epistemic Edge Types**
```
supports, refutes, supersedes, derives_from, specializes,
contradicts, qualifies, temporal_precedes, causal_enables, contextualizes
```

**Для mcp-ariel-memory**: Memories могут поддерживать, опровергать, заменять друг друга. Эпистемические связи вместо простого "related to".

**2. Spreading Activation**
Query → seed nodes → activation через typed edges → nodes с множественными путями = highest rank.

**Для mcp-ariel-memory**: Активация распространяется через связи памяти. Memories с несколькими независимыми путями = высший приоритет.

**3. Result Bucketing**
```
PRIMARY, SUPPORTING, CONTRASTING, QUALIFYING, SUPERSEDED
```

**Для mcp-ariel-memory**: Организовать результаты по эпистемической роли.

---

## Neuroca (NeuroCognitive Architecture)

Multi-tiered memory (STM/MTM/LTM), биологически-вдохновлённый. Консолидация, decay, importance scoring.

### Ключевые фичи

**1. Memory Consolidation**
Автоматическое продвижение STM → MTM → LTM на основе частоты обращения и важности.

**Для mcp-ariel-memory**: Автоматическая консолидация — короткая память продвигается в долгосрочную.

**2. Dynamic Backend Selection**
Система выбирает storage backend (in-memory для STM, SQLite/Vector DB для MTM/LTM) автоматически.

**Для mcp-ariel-memory**: Разные storage для разных уровней жизни памяти.

---

## context-mode

MCP сервер для контекст-песочницы. Сохраняет raw tool data вне контекста, индексирует в FTS5. 315KB → 5.4KB (98% reduction).

### Ключевые фичи

**1. Context Sandboxing**
Сырые данные вне context window. Индекс и retrieval только нужного.

**Для mcp-ariel-memory**: Memory sandboxing — не заливать все relevant memories в контекст, а индексировать и возвращать только нужное.

**2. "Think in code" paradigm**
Агент пишет скрипты для анализа данных вместо чтения файлов в контекст.

**Для mcp-ariel-memory**: Сократить context bloat от retrieval.

---

## Сводная таблица: ВСЕ репозитории

| Репозиторий | Ключевая идея | Приоритет |
|---|---|---|
| omem | 11-stage retrieval + 7-decision reconciliation | ★★★★★ |
| MemOS | Dream pipeline + lifecycle tracking | ★★★★★ |
| Nocturne | Node/Memory versioning + disclosure routing | ★★★★★ |
| agent-second-brain | Graduated tier promotion + creative recall | ★★★★☆ |
| ECE | Rule-based classifier + bootstrap/handoff | ★★★★☆ |
| MoltBrain | 3-layer search + PostToolUse auto-capture | ★★★★☆ |
| TencentDB | Symbolic Mermaid memory + progressive disclosure | ★★★★☆ |
| GPTCache | LLM verification gate + time-decayed similarity | ★★★★☆ |
| Selective_Context | Self-information scoring for importance | ★★★☆☆ |
| PRISM | Epistemic edge types + spreading activation | ★★★☆☆ |
| codebase-memory-mcp | Multi-pass pipeline + Louvain communities | ★★★☆☆ |
| Neuroca | STM→LTM consolidation + dynamic backends | ★★★☆☆ |
| context-mode | Context sandboxing | ★★☆☆☆ |

---

## ФИНАЛЬНЫЙ ТОП-15 идей

| # | Идея | Откуда | Влияние |
|---|---|---|---|
| 1 | **Dream Pipeline (фоновая консолидация)** | MemOS | Автоматическое создание insights из кластеров memories |
| 2 | **7-Decision Reconciliation** | omem | Предотвращает раздувание, поддерживает консистентность |
| 3 | **Weibull Decay с tier-specific β** | omem | Главное улучшение качества decay |
| 4 | **BM25 Floor Protection** | omem | Дешёвое улучшение качества поиска |
| 5 | **Epistemic Edge Types** | PRISM | Memories поддерживают/опровергают друг друга |
| 6 | **Rule-Based Memory Classifier** | ECE | Замена LLM на regex, ~1ms |
| 7 | **3-Layer Search Workflow** | MoltBrain | 10x экономия токенов |
| 8 | **Graduated Tier Promotion** | agent-second-brain | Spaced repetition для memories |
| 9 | **Bootstrap/Handoff Protocol** | ECE | Выживание после compaction |
| 10 | **Version Chains с Rollback** | Nocturne | Аудит, откат, история |
| 11 | **Self-Information Scoring** | Selective_Context | Principled importance signal |
| 12 | **LLM Semantic Verification** | GPTCache | Верификация retrieval results |
| 13 | **PostToolUse Auto-Capture** | MoltBrain | Авто-захват без усилий |
| 14 | **Noise Filter с feedback learning** | omem | Обучение от коррекций |
| 15 | **Dual-Stream Write** | omem | Fast sync + async extraction |
