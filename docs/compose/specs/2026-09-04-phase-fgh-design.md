# Phase F/G/H Design — a-memory L0 pipeline, self-wiring graph, eval & infra

> **Design doc** (distilled from research draft `2026-09-04-phase-fgh-draft.md` v37, 1068 строк;
> доки Эли/Лили `a-memory-l0-l4-pipeline.md` + `a-memory-graph-miners.md`; 33 репо + 44 статьи/блога).
> Status: DRAFT-FOR-REVIEW → верификация делегатом → compose:plan.
> Sequencing (user, 2026-09-04): **F-фундамент → G-минеры → H → Stage 2 в конце**. Волны не пересекать.
> Phase A–D/E закрыты; Stage 2 разблокирован, но идёт последним.

## [S0] Контекст и принятые решения

- L0 = сырой append-only журнал (user decision); EDM/ITS формулы в публикациях отсутствуют — реализуем принцип (uncertainty-reduction scoring + threshold-gating) через детерминированный суррогат; sign-бинаризация ≈ fancy MIB (подтверждено 2601.11557 их же данными).
- ENGRAM-абляция (№11): RRF-5-source vs dense-per-kind vs gated-retrieval — решение абляцией, не вкусом. При ENGRAM-win меняется только генератор кандидатов recall-пути (include_* флаги + per-kind индексы); L0/гейты/сторы/граф-минеры не затрагиваются (Memora Thm D.1: RAG/KG/dense-per-kind = частные случаи одного framework).
- Privacy-гейт идёт в F-фундамент сразу (🔴 A1: L3 сейчас без фильтра); spaCy NER-тир — сразу после фундамента; атрибуция в NOTICE, PyPI-релиз = download-at-install.
- E6 negative memory — в Phase F (продюсер: lessons mining из session-close).
- Шифрование at-rest: envelope есть только для saga/auth; данные plaintext — claim в pyproject убрать (хвост H) или SQLCipher (решение отдельное, не блокер F/G/H).
- OpenViking peers/, MemoryPalace tunnels, cross-namespace edges — резервируются при Stage 2 URI-redesign, не строятся.

## Phase F — Конвейер L0→L4

### [S1] L0 журнал (`l0_journal`)

- Схема: `id, ts, event, source_msg_id, layer, user_id, text, raw_type, status, decisions JSON, processed_at, order_key` (+ hash-chain для tamper-evidence, shiroe-паттерн).
- raw_type классификатор (без LLM, первое совпадение): MCP type-field (tool_result/tool_use/text) → префиксы (`[ariel recall]`, `[EVOLUTION]`) → роль+tool_calls → fallback plain text. Запись в include: import (E6/A6).
- Статусы: received → skipped / gated_out / saved_l3 / staged / promoted_l4 / replayed.
- decisions JSON — след каждого гейта: [{gate, verdict, score, reason}].
- Пишется первым, best-effort (сбой не блокирует поток); instant write-to-search (no-index, Memanto D6).

### [S2] Гейты (каждый пишет вердикт в L0)

- **G0 входной**: transcript-guard (`_looks_like_dump`, в проде) + SHA-256 dedup (5-мин окно, из memory_remember) + **privacy: strip→typed placeholders** (⟨EMAIL_1⟩, same value→same placeholder, reverse map не персистится, strict-mode refusal <0.5) + **spaCy NER-тир** (en_core_web_sm 12MB; PERSON/ORG/GPE/LOC; regex-only на прозе = 12-26% ликов) + per-repo capture allowlist/exclude.
- **G1 importance + дистиллятор**: атомизация на клаузы (союзы/точки, «и/но/причём») → типизация (kind_for_text + A5-regex: «запомни:», «я решил», «не делай») → importance (EMA + rules D1.9) → **канонический ключ** (семантический хедер через синоним-словари: «decision:граф_без_llm»; повтор → UPDATE updated_at + recency-boost, не дубль) → **семантический novelty-gate** (paraphrase-Jaccard; калибровка mean .274) → topic-классификация (hall_keywords-паттерн → wiki-тип/epi_tags предзаполнение).
- **Kind-роутинг (ядро F)**: инварианты (fact/decision/rule/instruction/commitment/goal/relationship; low decay_rate/never_archive) → L4; события (observation/question/context; высокий decay) → L3. Policy.decay_rate учитывается промоцией (сейчас игнорируется).
- **Конфликты**: rag/conflict.py на входе L4 → memory_conflicts + 3-опционный контракт агенту (supersede/retain/annotate), non-persistence до резолюции; **read-time fusion приоритетнее gate-time** (Mem0 ADD-only, LoCoMo +21pp); condition-splitting repair — опция для инвариантов (3M).
- **G2 promotion**: outcome-gated (autocontext: matched screening → confirmation → held-out eval → false-promotion budget → atomic activation) + консервативные пороги (γ=0.80; T17: 0.6 = 3.4× мержей zero gain) + transcript-guard + отложенный candidate живёт в staging.
- **Compact-to-budget size-гейт** (Letta): per-блок лимит символов/токенов; nightly consolidation **упаковывается под бюджет** с эвиктом низкого ACT-R (не только дедуп) — append-only без compaction-давления = недостающий гейт.
- **Procedural/agent-self как 3-й роут-kind** (ENGRAM + EverOS/Acontext + PlugMem): how-to/procedure записи агента — отдельный routed kind (ложится на wiki L4.5 + agent-self namespace), не смешивается с user-инвариантами.
- **Priority-сигнал в TypePolicy** (Memanto): decay_rate есть, добавить **retrieval-priority** per-тип (fact=stable, commitment=time-critical, goal=until-achieved, event=episodic, context=highly-temporal, learning=accumulating, error=guard) → type-filtered retrieval.
- **Segment-level consolidation** (Lychee Eq1-4): boundary detection embedding-only p_t = σ(...) (константы Table 10: δ=0.50, 300/600/900 tok, 10 exchanges), alias carry-forward buffer ρ_{k+1} = [d_k; Recent] (fixed budget, «resolve references only, never extract»); −86% construction tokens у источника.
- **Session-close фаза** (B9/EverMemOS): post_session_diff → preferences/experience extraction через A5-regex → staging.
- **E6 negative memory**: lessons mining (failures/do-not-retry из session-close) → anti_patterns записи; **первый продюсер** — из батч A.
- **Foresight validity**: forward-looking факты (планы, temporary states) с [t_start, t_end], read-time фильтр t_now (EverMemOS).

### [S3] L2-обогащение и повторные операции

- L2 sessions хранит summary без сообщений (проверено кодом) → ночной проход биндит L0-записи к сессиям по времени, пересобирает summary/state_deltas из фактического сырья (кормит минер #4 real-группировками).
- Compat: пометки на существующих wiki_index тестовых строках (Test/Count/g/h — user verdict отложен), memory_forget удалён (65 тулов), wiki_index дедуп сделан.

### [S4] Replay и watermark

- **Watermark**: processed_at/status в l0_journal — каждый raw дистиллируется ровно один раз; ночная выборка WHERE status='received'. Без watermark «дистилляция освобождает сырьё» не работает.
- **Replay**: `replay --since -7d --gate g1` — переигрывание гейтов по окну (порог/rules изменились → переиграть; идемпотентность = source_msg_id + gate + config-hash; cocoindex memo = hash(вход)+hash(КОД) — тот же принцип).
- **dispatch_log → view над L0** (не миграция, пере-вычисление).

### [S5] Wiki = L4.5 knowledge layer

- Намеренная запись (рефлексия/доки/дизайн), НЕ дистилляция из L0; **не retrieval unit** (VikingMem: OpenClaw markdown — худший baseline).
- **[[fact:ключ]] linking**: факт помнит wiki_id; bi-temporal metadata (learned_at + valid_at) на линках.
- **Ночные провенанс-мосты** wiki↔L4↔L0 (минер #5) — бесплатно дают рёбра графа.
- **Гидратация вниз**: recall со страницы → L4 → L0 («почему мы так решили» — до исходного сырья; = C3 drill-down).
- Wiki-самоорганизация (из v24/v26): rewrite-not-append ingest + auto-reconcile; redirect-stub при merge (никогда не удалять); OKM freshness linter на записи (timeless/dated/pointer); write-time link validation («note without links is a bug»); blast-radius guards; OKF/autograph schema-as-code (frontmatter contract + schema infer/validate/diff); Stale-Actives auto-views.
- MOC-хубы **исключить/занизить** из BFS/louvain (Ar9av: 44→83% correctness) — bookkeeping exclusion.
- **Memanto expiry policies** (v22 🔑): мягкое истечение вместо жёсткого удаления — expired остаются recallable с меткой `[EXPIRED]`, restorable, provenance именованного правила; retention-таблица per-тип (first-match-wins, pins). Уточняет S6-политику и deletion gate.

### [S6a] Principles линии (6, из чата Эли — сборка по v37)

1. **Один вход**: все 4 потока (MCP-тулы / авто-хуки / консолидация / внешние дампы) проходят через L0-приёмник — сортировочную станцию; remember перестаёт дублировать контент в граф (только провенанс-ссылка); graph_add/agent-хуки под контроль (важность + провенанс обязательны); think-роутинг (>2000 → wiki и т.д.) проходит через L0.
2. **Дистилляция off-hot-path**: горячий путь = L0 append + L1; всё остальное (дистилляция, минеры, консолидация) — ночные/фоновые проходы.
3. **Идемпотентность**: watermark — каждый raw обрабатывается ровно один раз.
4. **Провенанс**: каждая L3/L4 запись помнит source_raw_id → drill-down до исходного сырья.
5. **Фильтры L4**: только атомарные инварианты с ключом; сырой JSON автоматически не проходит (стена стоит сама).
6. **Двунаправленность**: bottom-up дистилляция + top-down гидратация (drill-down «почему мы так решили»).

### [S6b] L0-классификатор сырья (5 шагов, из чата Эли)

1. Структурный парс MCP-обёртки (`type`-поле): tool_result (+tool_use_id, content, isError) / tool_use (+name, input) / text.
2. Префиксные маркеры не-MCP дампов: `[ariel recall]`/`[ariel memory]`/`[ariel proposals]` → recall-дамп; `[EVOLUTION]` → evolution-дамп; `[ariel ...]` любой → память-дамп.
3. Роль + tool_calls: assistant с tool_calls → agent-action; role=user → user-message.
4. Fallback: нераспознанное → plain text в дистиллятор как есть.
5. Связка тулов: tool_use_id ↔ tool id = готовая пара ключей для триплета query→tool→outcome (без угадывания порядка в сессии).

Таблица маршрутов: tool_use → L0+ждёт пару (триплет query→tool); tool_result → L0+связка (→outcome ребро); recall-дамп → L0+счётчик (co-retrieval статистика → `co_recalled`); user-message/plain → L0 → дистиллятор (атомы → L3/L4). Бонус tool_use: частотность/ошибки/паттерны → **behavior-аннотации тулов** (data-фундамент для Stage 2 MCP hints).


### [S6] L0-разрастание (политика)

- Три тира: горячий 0–30д (полное) → тёплый 30–180д (A3-extractive + zlib) → холодный (>180д или дистилляция + N дней без recall → CLACK-архив, lossless LLM-readable; из L0 удаляется).
- Дедуп SHA-256 блока (повторный вывод = ссылка); дистилляция освобождает сырьё; source_raw_id переживает архивацию.
- B5-защиты на свипе: min-остаток, стоп при >80% expired, cleaner_summary в L0/аудит; warm-tier: жать жёстко или не жать (mild = net-negative, P5).
- Оценка: 2MB/день → ~16MB/год (сжатие ×15, дедуп ×3).

## Phase G — Self-wiring граф

### [S7] Фундамент (прежде минеров)

- add_edge вызовы в правильных местах (builder'ы пишут, но минеров нет — наполнение нулевое); **журнал co-retrieval пар** — НОВЫЙ (расширить audit_trail: recall_useful + target_id), иначе минер #7 не считает.
- **Пре-чистка**: замусоренные JSON-узлы графа → в L0 (восстановить из бэкапов *.bak-pre-*), иначе минеры свяжут мусор с мусором.
- 3-тир L0-жизнь/CLACK cold — см. S6.

### [S8] Минеры (детерминированные; ребро тегировано `heuristic:<name>`)

Порядок (план Эли/Лили, уточнён research):
1. **#1 Общие теги** → `tagged` (вес = число общих) — epi_tags есть.
2. **#2 Редкие токены + синонимы** → `topic_overlap` (Jaccard) — FTS5+synonyms есть.
3. **#4 Сессионная близость** → `same_session` — таймстампы есть.
4. **#5 Провенанс-мосты** → `sourced_from` (эпизод→wiki→факт) — source_id/wiki_id есть.
5. **#9 Эмбеддинг-минер** → `semantic_overlap` (Jaccard MIB ≥0.7, top-k, вес 0.5-0.6) — ST 6.0.0 в venv; **кодирует content+tags+aliases** (A-MEM); arctic-embed-xs кандидат; MIB-128B → RaBitQ 48B error-bounded апгрейд.
6. **#7 Co-retrieval** → `co_recalled` (вес = частота/N) — после журнала. *(Порядок #9↔#7 сознательно изменён против draft v14: эмбеддинг-минер стартует раньше, т.к. модель уже в venv — без новой инфраструктуры; co-retrieval ждёт новый журнал.)*
7. **#3 Сущности** → `co_mentions` + канонизация (словарь: Лили/Lily/лисёныш; spaCy NER) + **entity-matches как 6-й RRF-сигнал** (Mem0 retrieval-boosting).
8. **#6 Маркеры результата** → `led_to` heuristic («починила», «сломалось»).
9. **#8 Структурные инварианты** — co-citation, louvain-расширение, belief propagation.
10. **Триплет-минер** → `query→tool→outcome` рёбра (Эли/Лили поток «тулы»): tool_use_id ↔ tool id = готовая пара; частотность/ошибки/паттерны per-tool → **behavior-аннотации** (data-фундамент Stage 2 MCP hints).

Инкрементальный режим (при записи) + ночной batch; **вес = доверие источнику** (эвристика 0.3–0.6, ручные 0.8+); откат: DELETE WHERE edge_tags LIKE '%heuristic:%'.

### [S9] Санитария и policy-слой

- **Lateral inhibition** (SYNAPSE): û_i = max(0, u_i − β·Σ(u_k−u_i)·𝕀[u_k>u_i]), β=0.15, M=7 — против correlated distractor-кластеров; без inhibition Open-domain падает 25.9→22.4. **FOK-gate τ=0.12 на activation топ-нодa** → детерминированный reject до LLM (FRR<2.5%).
- **Validity windows на рёбрах** (Graphiti/StateMem): derived_from/coupled_with typed edges + unit status (active/superseded/needs_recheck) + O(|E|) deterministic recheck propagation.
- **MAD-пороги** (Mandol): τ = median(S) − κ·MAD(S) вместо fixed cutoffs.
- **Volatility-классы** (RoMem): per-fact decay по семантике отношения (президент ротируется, рождение — нет); deterministic proxy = predicate-type таблица.
- **Valence-typed edges** (prism): 10 типов → result buckets (primary/supporting/contrasting/qualifying/superseded).
- **Hub exclusion**: MOC-хубы/авто-индексы вне centrality/BFS (Ar9av: иначе fools 44→83%).
- **Трёхфазный dream** в graph_enrich: NREM (spreading activation +0.05/−0.01, prune <0.05) → REM (мосты изолированных узлов, sim×0.3) → Insight (BFS-комьюнити → материализованные абстракции) + negative-control протокол (off-switch бенчи обязаны падать).
- **CAMA anti-false-majority**: max-presence e_j=max_i z_ij + N_eff (Hill diversity) в фьюжене — correlated evidence не накачивает mass; N_eff = abstention-сигнал.
- Cap на узел, min-weight, decay только heuristic-рёбер (ручные не трогаются).

### [S10] Dual-route retrieval

- **Primary (F-наследие)**: RRF k=60 5-source (BM25 demote до minority) → **EDM re-rank** α·R+β·N+γ·G−δ·K → **ITS threshold-gating** (min-max [0,1] per query, threshold ~0.05, k≤100) → детерминизм (same query → same results, MOSS 1 год продакшена валидирует; **флаг `deterministic_retrieval`** для регламентированных сценариев, default off — v27).
- **Counter-signal алиасы** (Tenure): superseded-имена живут как aliases с негативной ролью в ранжировании (ceiling 25/белиф) — не drop, а понижение.
- **S2 exhaustive route** (Mnemis): «list all X» — иерархический top-down scan (категория несёт агрегатную summary потомков); similarity структурно фейлится на enumerative; **compression constraint** (категория ≥n детей, |слой i+1|≤|слой i|).
- **Adaptive pre-gate**: 27 query-features без LLM решают, фаерить ли полный ретрив (Adaptive RAG).
- **D-Mem escalation**: dense-first; граф-reranker только при провале confidence-гейта (96.7% recovery при меньшем cost) — примиряет HippoRAG2-верdict с графом.
- **Question-type router** (AdaMem/BookRAG): классификатор вопроса → tailored маршрут.
- **ENGRAM-абляция** в №11: arms A=RRF-5-source / B=dense-per-kind / C=gated / D=A+EDM+ITS. Decision rule: A≥B,C — держим; B>A — дефолт dense-per-kind (конфиг, не архитектура; Memora D.1 — все схемы частные случаи).
- **Correlated evidence fix**: max-presence (CAMA) + lateral inhibition (SYNAPSE) + submodular coverage (2507.19715).

## Phase H — Eval и инфраструктура

### [S11] Eval harness (№1 приоритет)

- Датасеты: LongMemEval-S (strict KU: old+new обязаны вернуться; abstention 30 false-premise; 5 способностей) + LoCoMo (вкл. event-summarization задачи) + PersonaMem-формат.
- Метрики: LLM-judge (97% agreement) + Recall@k/NDCG@k (human evidence positions) + **precision/noise-isolation** (Tenure: dump = precision 0.12 при recall 1.0) + **reacquisition-cost** (completion слеп: 80→85% при ×3 retrieval) + **construction-tokens** (write-cost first-class) + **drift score** + N_eff-abstention (калиброванный) + k-sensitivity + **estate-quality-over-time** (contradiction rate, staleness, precision@6mo) + 3-way task split (factual/sense-making/associative) + **двухсудейный** rank-stability протокол.
- Ablation arms (см. S10) + oracle-retention абляции (random ≈ oracle в одном env) + negative-control protocol (mazemaker: off-switch должен падать) + judge rubric anchored 0-5 (LCC) + честная плашка «какие компоненты включены» (Mem0/Cognee урок) + **LLM-adjudication arm** (A-MEM: локальная LLM подтверждает top-k эмбеддинг-рёбра; links-only +12 F1 multi-hop — единственный кандидат на нарушение no-LLM, решает абляция) + **StateMemBench-пробы**: closed-pool (superseded value = scored outcome), anti-trap (anti-prefer-latest), sequence-пробы (derived recomputed, never quoted), per-substrate salience floors, k-sweep flatness отчёт (не тюнить k на state-задачах).
- Референс-планки: gbrain 93.19/95.32 strict-R@5; MemoryPalace 96.6; Memanto 89.8 LME / 87.1 LoCoMo (platform-only, Moorcheh cloud); Memora 87.4; APEX-MEM 88.88/86.2; LoCoMo SOTA band 86-94.
- Eval harness = отдельный артефакт с открытыми конфигами (Mem0 `memory-benchmarks` паттерн).
- **Cross-encoder rerank** (v2 #2): парк до №11 — включается как arm только если бенчмарк покажет провал RRF (Basic Memory fail-fast disabled-by-default паттерн).

### [S12] Библиотеки

- **ВСТРАИВАТЬ**: spaCy + en_core_web_sm (privacy NER + entity-miner; атрибуция NOTICE, PyPI download-at-install); fractional-indexing (CC0, vendor; order_key для wiki/L0/канон-ключей); zero-result-минер (open-index идея, поверх recall_events).
- **ПАРК**: turbovec (16.7k★, ICLR'26 — но 5 мес + wrong regime; скальпели: length-renorm + TQ+ calibration в quantize.py — 2 строки numpy); msgspec (после micro-bench L0); SQLite-paper (2608.24060 scrydb — референс, sqlite-vec кандидат).
- **ИДЕИ**: PageIndex TOC-спуск без LLM для wiki-L4.5; PROV-O для provenance (typed_export); point-in-time graph snapshots; Merkle-DAG incremental reindex для wiki-проекции; spread activation как 4-й retrieval channel (SuperLocalMemory).

### [S13] Инфраструктура (остальное)

- memory_audit (расширение memory_diagnose контент-чеками: конфликты-ридер, дубли, stale, file↔DB reconciliation); health: /ready + alembic-head check (1 строка); ariel-cli (ls/tree/find/grep + typed_export surface); A8 MEMORY.md-бридж (двунаправленный, drain-marker); import_chat (claude/chatgpt/jsonl → L0 → гейты, origin=import); Roadmap.md freeze (сделано); admin-тулы манифест (8 шт); autocontext outcome-gated promotion в staging; cycles-таблица daemon (E9: 60s/1ч/3ч/24ч) + **chimera triple cost-cap** (per-cycle/rolling-60m/per-task + estimate-verb) как spend-gate ночного batch; Mermaid-канвас (после G-наполнения); Sessions Replay viewer (после F1); spaCy-атрибуция; crypt-claim fix (убрать/реализовать); **semantica decision read-поверхность** (trace_decision_chain / find_similar_decisions / analyze_decision_impact) для causal-графа E17a; **gbrain gap-reader** — memory_audit флагует unknown/stale/uncited/contradicting в ответах (detection без reporting = полсистемы) + `create_safety` verdict (exists/probable/unknown) как контракт выхода писателей.

### [S14] Stage 2 (последняя волна)

- Сведение 65 тулов + slots + URI-keys (включая peers/ tunnels резерв) + MCP behavior-аннотации (readOnlyHint/destructiveHint/idempotentHint — fork поддерживает ToolAnnotations; data-фундамент из триплет-минера) + переименования (wake_up alias) + admin-тулы манифест. **Начинается только после F/G/H.**

**→ Stage 2-B SHIPPED 2026-09-07 (`mcp_server/annotations.py`): единая карта 65/65 тулов (registry cross-check тест), ToolHints dataclass с MCP-консервативным default (не-картное = не read-only, destructive=True — промоутем write-тул к безопасным быть не может); read-тулы ~30 шт (read_only+idempotent, destructive=False), destructive явно помечены (forget/wiki_delete/cleanup/heal/data/api_key/saga); server.py прокидывает annotations в mcp.tool() — на wire Tool.annotations заполнены (тест через list_tools). Ограничение честно: memory_history/proposals/backup — action-миксы, помечены по худшему действию.**

**→ Stage 2 стартовал 2026-09-07, План A (URI-keys) SHIPPED (`shared/uris.py`): схема `ariel://<layer>/<store>/<key>` (fact/wiki/graph/node/episode/l0) поверх существующих ключей, ноль миграций; `parse_uri` (мусор/резервы peer/ отвергнуты) + `resolve_uri` (user_id обязателен в вызове — изоляция: чужое по URI не резолвится; peer → ValueError reserved). Интеграция: search-items и wiki_read несут `uri`, `drill_down(entry_id | ariel-URI, user_id)` принимает URI. Тесты 9 (parse-матрица, roundtrip, изоляция user_id/layer, peer ValueError, search/wiki_read/drill_down интеграция). Gate 1494/0, mypy 231 clean. Следом: План B (behavior-аннотации), C (slots+consolidation), D (Stage2-eval).

**→ Stage 2 План C v2 УТВЕРЖДЁН (design 2026-09-07, код не начат): сведение поверхности 57→~13 схем через мета-слой.** Мотивация (верифицирована live-count): примитивы задумывались для минимальной поверхности, live-комбо 7 тиров = 57 видимых схем на агента (registry 65; 8 сирот — только с `all`). Решение владельца: «выпадающий список» — мета-тул на тир. Механика: (1) `mcp_server/slots.py` — SLOTS: dict[tool→slot], 12 слотов (core 7 вкл. wake_up / recall 5 / context 4 / episodes 4 / sessions 3 / graph 4 / wiki 9 / insight 10 (+procedure, scratchpad) / write 9 (+skill_promote из сирот) / review 4 (+daily_brief — тир brief растворяется) / admin 7 = НОВЫЙ тир (api_key, backup, cleanup, data, lucidity_purge, saga, sync_replica)); cross-check тест против registry (сироты = 0 инвариант — исполним). (2) Мета-слой: 6 диспетчеров (context/insight/write/wiki/review/admin) **генерируются из EXTRA_TIERS программно** (тир-словарь = единственный источник); на проводе схема `action: string + args: object` (probe на живом SDK подтверждил), точные параметры — через `action='list'` каталог (per-action behavior из карты Plan B); первый вызов = каталог, далее одношагово. Мета-слой под флагом `ARIEL_META=1` (default off — плоская поверхность 65 не меняется, eval Plan D не тронут). (3) wake_up (E10) регистрируется примитивом (PRIMITIVE_TOOLS, слот core) — draft features/wake_up.py (recap+inject критика на общем бюджете, 56 строк). (4) Пресеты ARIEL_EXPOSE: agent/operator/full. (5) Манифест docs/tools/exposure.md (слоты/тиры/пресеты/грамматика) + починка врёной «65 tools» строки в reference.md (live-комбо даёт 57, не 65). Модель видит: 7 примитивов + 6 мета-тулов = 13 схем при всех тирах. Риски приняты: параметры не видны до каталога (лечение каталогом), первый вызов двухшаговый за сессию, мета-аннотации консервативны по худшему действию членов.

**→ Stage 2 План C SHIPPED 2026-09-07 (`8e6f9d2`+`8d281f0`+`65bced1`+`2b0b2ed`+`98f6741`+`242250f`, push 040fb34..242250f, gate 1523/0 + mypy 10-dir clean).** Всё выше — вживую: registry 66 (65+wake_up); slots.py + 5 cross-check тестов (включая дубль-инвариант по группам); EXTRA_TIERS: admin-7 добавлен, skill_promote→write, brief растворён в review, сироты=0 тестом; `resolve_exposure` расширен пресетами agent/operator/full с fixpoint-раскрытием (первая версия с однопроходным раскрытием поймана тестом operator⊄agent — переписана на цикл); wake_up зарегистрирован примитивом (surface по умолчанию 6→7) + annotations read_only; мета-слой `mcp_server/meta_tools.py`: фабрика `_make_dispatcher` (SDK отверг underscore-параметры в сигнатуре — закрытие через factory-call), `_scope_tool` оборачивает цель лениво на каждый dispatch (user_id-биндинг переживает мета-прыжок), mypy no-any-return закрыт явной dict-нормализацией; live-проверка: ARIEL_EXPOSE=all+ARIEL_META=1 → **13 схем** (7 примитивов + 6 диспетчеров), без ARIEL_META → 66 плоско. Пресеты измерены: agent=59, operator=66. Манифест docs/tools/exposure.md; reference.md переписан (66, admin-тир, ex-brief). Live-конфиги агентов НЕ тронуты (переезд на agent+ARIEL_META=1 — отдельное решение владельца; строка комбинации легаси теперь резолвится в 59). Уроки: pre-commit ruff-pinned v0.16.1 ≠ свежий ruff (формат-драфт схлопывал commit — форматить pinned-версией до git add); счётчики registry хардкодились в 3 местах тестов (65→66) — live-реестр прав, статические числа устаревают молча.

## [S15] Волновой план

- **F1**: L0-журнал + watermark + классификатор + G0 (privacy) — фундамент.
- **F2**: G1-дистиллятор (kind-роутинг, канонические ключи, novelty-gate, конфликты) + G2 (outcome-gated promotion) + replay.
- **F3**: session-close + L2-обогащение + E6 lessons mining + retention/CLACK + спека инъекции (per-kind caps, порядок, maxChars, precedence rules).
- **G1**: фундамент графа (add_edge wiring, co-retrieval журнал, пре-чистка) + минеры #1/#2/#4 (сотни рёбер за вечер).
- **G2**: #5 провенанс + #9 эмбеддинг-минер + санитария (inhibition, validity, MAD, valence) + graph_enrich трёхфазный.
- **G3**: #7 co-retrieval + #3 сущности (spaCy) + #6 маркеры + #8 инварианты + dual-route (S2 exhaustive, D-Mem escalation, question-router).
- **H1**: eval harness (№11) + arms + negative controls.
- **H2**: memory_audit + import_chat + ariel-cli + A8-бридж + admin manifest + crypt-claim fix.
- **H3**: Mermaid + PROV-O + snapshots + Juice-опции (LCC latent, KV-precompute — watch).
- **Stage 2**: сведение тулов + URI + аннотации (после всего).

## [S16] Открытые вопросы и осознанно-отложенное

- CLACK-формат (Лили-концепт) — реализация холодного тира: F или H по объёму (решить при планировании F3).
- Тестовые строки wiki_index (Test/Count/g/h) — писатель живой; чистить при следующей прод-чистке.
- JSON-дампы эпизодов Эли (~120) — удалить по явному OK (L3-память).
- Счётчик тулов: **65** (после удаления memory_forget в tails-волне; 66 был временно с memory_stash).
- privacy strict-mode (<0.5 confidence refusal) — включать ли по умолчанию (опасность ложных отказов для легитимных сообщений; LLM-Redactor default: on).
- **memory_forget удалён vs Cognee forget-verb**: verb-forget (dataset-scoped GDPR-стиль) ≠ удалённый тул (single-key L4); verb-forget реализуется примитивом `forget` (scope-параметры) — tension разрешён, отдельный тул не возвращать.

### [S17] Stage 1 tail — потерянные 🔑-пункты драфта (сверка драфт→диздок→план→код, 2026-09-05)

> Сверка v38-драфта против диздока/плана C1-C8/кода нашла 7 🔑-обязательств, потерянных
> при конверсии (застряли между стадиями без владельца). Все — F-фундамент, ноль
> межзависимостей. **Закрываются немедленно как окончание Stage 1**, до Stage 2.

1. **`deterministic_retrieval` флаг** (v27 п.3): флаг в config — retrieval отдаёт детерминированный порядок (ITS min-max без ингибиции/дрожаний) для регламентированных сценариев. Default off. Реализация: bypass inhibit/EMA-членов в edm_rerank при флаге.
2. **ENGRAM procedural kind** (v28 #3): процедурный kind («как сделать X», how-to) — 14-й MemoryKind с own policy (never_archive=True, decay 0) + kind_for_text-маркеры («сделай так», «порядок действий», «инструкция по») → роутится в L4-namespace агента (agent-self track). Data-фундамент для Stage 2 behavior-аннотаций.
3. **AdaptiveRAG pre-gate** (v28 #5): 27 query-features (7 групп, LLM-free) решают «фаерить ли все источники» — дешёвый skip-тир поверх RRF (cost down + шум down), результат питает N_eff/abstention. Реализация: feature-вектор запроса (длина, тип, наличие дат, вопросительная форма...) → простой гейт.
4. **A2 advisory `similarTo` в ответе save** (волна A2): ConflictResolver ловит near-dup на записи → сейчас пишет memory_conflicts молча; вернуть advisory в ответе save (поля `similar_to: [key…]`), агент сам решает переформулировать.
5. **SHA-256 дедуп L0-блоков** (механизмы разрастания п.2): колонка content_hash в l0_journal (или in-capture дедуп) — повторный вывод команды хранится один раз, ссылки на первую запись. Дедуп сейчас только тул-уровень (_DedupCache, TTL 300с).
6. **Zero-result минер** (open-index, v25): провальные запросы (0 хитов) журналируются как минер-сигнал «что смоделировать следующим» (LME +9.4% recall) — поверх recall_events/co_pairs инфраструктуры.
7. **Counter-signal алиасы** (Tenure): superseded-имена в ранжировании с негативной ролью (не drop) — связка с conflict resolution + канон-ключами.

Плюс **EMA-гейт** (F2-обязательство): adaptive_threshold выпал из auto_save_text при переработках — порог статичный 0.5. Вернуть EMA-член в гейт (вернув репутацию «без изменений логики»).

**Дополнение из первоисточника a-memory-start-FGH.md (сверка 2026-09-05):**

8. **Drill-down переживает архивацию** (принцип 5 линии): diagnostics.drill_down ищет сырье только в l0_journal — после tier_l0 строка уезжает в l0_cold_archive и ссылка ведёт в тупик. Фикс: drill_down фолбэк в l0_cold_archive по source_raw_id. **→ SHIPPED 2026-09-06 (`283e48f`): фолбэк SELECT по id + `archived: true`-флаг; нет нигде → прежний тупик-контракт сохранён.**
9. **Подтверждающий слой эмбеддинг-минера** (роль 2 из стартового дока): keyword-связь (теги/токены) + высокое векторное сходство → вес ребра растёт (0.4→0.6); сигналы противоречат → вес падает/ребро отбрасывается. Сейчас минер #9 пишёт semantic_overlap независимо, кросс-валидации нет. **→ SHIPPED 2026-09-06 (`8571541`): `graph.embedding_crosscheck` default OFF — ON: keyword-согласие (общие канон-токены ИЛИ общие теги) → вес ребра 0.6; лексического следа нет → ребро отброшено (на hash-векторах «векторно похоже, лексически никак» = шум).**
10. **Anomalous-vector мусор-детектор** (бонус-трюк): L3-дампы дают аномальные векторы — эмбеддинг-минер попутно флагует «мусорные узлы, кандидаты на чистку» (двойная польза с пре-чисткой). Сейчас не реализовано. **→ SHIPPED 2026-09-06 (`8571541`): бит-вырожденные векторы (0 бит) → тег `anomaly:junk_vector`, отчёт `anomalies: N`; anomaly-теги в rich-embed не попадают (самозагрязнения нет). Density-эвристики НЕ делались: без валидационных данных — только детерминированный критерий вырожденности.**
11. **HDBSCAN-кластеризация эмбеддингов** (роль 3): сверка с louvain-комьюнити → community-хабы. Не реализовано (louvain есть, эмбеддинг-кластеров нет). **→ SHIPPED 2026-09-06 (`376b45b`): `lifecycle/embedding_clusters.py` — HDBSCAN (sklearn, precomputed Hamming на 384 MIB-битах) + louvain-доля согласия; отчёт graph_enrich miners.embedding.clusters за флагом `graph.embed_clusters` default OFF; <12 векторов → skipped. Отчёт-only, рёбра не меняет; смысл при dense-эмбеддингах.**

### [S18] Stage 2 — пополнение (сверка 2026-09-05, сверх S14/S16-distillate)

- GA per-query min-max для ACT-R-члена в multi_source (v17 #5).
- maxChars per-memory при инъекции (D6, S-эффорт).
- Channel-гранулярность metadata (A4: user_explicit/episode_promotion/... → +tool/import/shared).
- `changed_since`-модальность в get_intervals (Memanto temporal versioning).
- Orphan-anchor GC для derived-ключей минеров (Memora cue-anchor prune).
- Semantic dedup cosine>0.92 merge как 3-й сигнал после exact-SHA + novelty-Jaccard (memory-os/3M консенсус).
- B2 recall hygiene: is_current-view / superseded-флаги, чтобы recall не отдавал закрытые интервалы (bi-temporal читает old+new только для KU-запросов — остальное фильтрует).
- 3-option конфликт-контракт агенту (supersede/retain/annotate): ConflictResolver.resolve есть, agent-facing surface нет — тул или расширение memory_recall.
- Gap-registry (3M Find Gap): L3-questions → ночной registry → proactive acquisition.
- **B6 UPDATE>MERGE>CREATE + лимит активных сущностей** (TencentDB): был в драфте как «Phase G после entity linking, needs eval-цифры» — в диздок не перенесён. Стадия: после №11 (heuristic размера, false-merge дороже разрастания). **→ POST-EVAL SHIPPED 2026-09-06 (`03ab2c8`), цифры с 3 живых инстансов: false-merge НЕ подтвердился (0 групп дублей person/org/entity) — UPDATE/MERGE не делаем, find_or_add_entity exact-dedup достаточен; разрастание ПОДТВЕРДИЛОСЬ — multi-topic dump собрал 109 co_mentions из 137 рёбер инстанса (hermes), один синоним-класс («memory») соединил его со всем подряд → degree-cap `_CO_MENTIONS_TOPK=12` на узел (паттерн _EMBED_TOPK минера #9).**
- **Offload тяжёлых tool-логов в refs/*.md** (TencentDB v2): в контексте только Mermaid-канвас с node_id (Mermaid есть; offload-механика — Stage 2 мелочь). **→ SHIPPED 2026-09-06 (`24597db`): `features/offload.py` — лог >4000 симв → work_notes/refs-<date>-<tool>.md (wiki-страница, FTS работает), компактный ref-указатель наружу; графовый узел НЕ пишется bypass'ом — wiki_graph_builder создаёт wiki_page-узел штатно (F-T9 AST-инвариант поймал первую версию с add_node — гейт работает).**
- **Circuit breaker на harness-адаптерах** (TencentDB v9: 5 fails → 60s pause): hermes-плагин breaker'а не имеет (проверено) — Stage 2 мелочь, адаптеры Hermes/cow/MiMo.

### [S19] Сверка исходников (2026-09-05, a-memory-start-FGH / l0-l4-pipeline / graph-miners / spacy-integration)

> Построчная сверка пяти исходных доков против диздока/кода. Найденные пропуски —
> в S17 (немедленно) или сюда (Stage 2). Помечены [S17↑] если перенесены.

**Из start-FGH / l0-l4-pipeline (линия L0–L4):**
- **wiki_id на факте** (обратная ссылка L4→wiki: «факт помнит wiki_id источника»): реализован только минер wiki_fact_links (ребро), колонки/метаданных wiki_id на core_memory нет → Stage 2: metadata.wiki_ids при [[fact:]]-линковке.
- **Recall со страницы** (page → связанные факты, гидратация вниз): нет прямой read-поверхности — потребители ходят через graph_node. Stage 2 (пара к wiki_id).
- **Дедуп capture по content_hash** [S17↑ п.5].

**Из graph-miners (граф):**
- **Подтверждающий слой минера #9** [S17↑ доп. 9]: голосование keyword+embedding → weight 0.4→0.6.
- **Anomalous-vector мусор-детектор** [S17↑ доп. 10].
- **HDBSCAN-кластеризация** [S17↑ доп. 11].
- **Стемминг в минере #2** (Эли: «стемминг уже есть» — по коду его нет; _TOKEN_RE голый regex → topic_overlap теряет RU-морфологию): Stage 2 — лёгкий стеммер ( Porter RU) в _canon_tokens. **→ SHIPPED 2026-09-06 (`f2b0487`): pymorphy3-леммы (не Porter) в `_canon_tokens` и канон-ключах за флагом `rag.lemmatize` default true; guard'ы: score <0.3 (заимствования вне словаря: «деплой»→«деплый») и общий префикс <4 → сырой токен; лемма <4 симв. отбрасывается; drift-риск ключей задокументирован в config.yaml (старые ключи не пере-хэшируются). Тест-фикстуры адаптированы: «сборка/сборку» больше не анти-кейс — это заявленный эффект.**
- **Retrieval-фильтр по heuristic-тегам** («recall может фильтровать по источнику ребра»): фильтр рёбер по provenance при graph-expand — Stage 2 (низкий приоритет).

**Из spacy-integration (4 варианта углубления, все вне диздока — фиксируются сюда):**
1. **ru_core_news_sm на privacy-гейт** (~12МБ): русский NER вместо en-модели на кириллице; расширение маскировки на произвольные русские PERSON/ORG без ручного словаря. Garbage-guard обязателен (ru-NER шумит на коротких текстах). ~~15 минут работы — ближайший шаг~~ **SHIPPED 2026-09-06 (`2a91ff7`, S19.1): PER ≥2 токенов (однотокенный = шум «Кисонька»), ORG/LOC однотокенные легитимны, en-NER только на латинице, breaker ru_ner_model 3/60s, `rag.ru_ner` default on, тесты test_privacy_ru_ner.py 6.**
2. **textcat-типизация клауз** (усиление kind_for_text): обучающие данные УЖЕ ЕСТЬ (core_memory.memory_kind + decay_rate ≤0.005 → самоклейб «инвариант vs событие»); argmax + порог уверенности, ниже порога → fallback keyword-мапы; eval на H-harness (NDCG/drift). ~~Обучающие данные УЖЕ ЕСТЬ~~ **ПИЛОТ SHIPPED 2026-09-06 (`9d17e07`), вердикт НИЖЕ. Инвентаризация данных опровергла масштаб: все 5 инстансов L4 = 448, fact 81%, non-fact 87 (decision 2, todo 1) — 14 классов не на чем.**
3. **Лемматизация канон-ключей**: ru-леммы схлопывают формы («уволилась/увольнение» → одна основа) — меньше ручных синонимов.
4. **Морфо-фичи для ImportanceScorer** (POS/модальность) — низкий приоритет.
- **Паттерн интеграции** (инвариант): lazy-load + circuit-breaker (переиспользовать _embedding_breaker); fallback на правила при сбое; config-флаг; eval до/после.

**S19.2 textcat-пилот — вердикт (2026-09-06, `9d17e07`):**

Схема: 2-классовая модель (stable = kind с decay ≤0.005 / ephemeral — ровно «инвариант vs событие» Эли, тот же порог что route_kind). Данные: self-labels всех 5 live-инстансов (310 строк, stable 48 / ephemeral 262), oversampling minority; ru_core_news_sm tok2vec заморожен, учится только textcat; интеграция — ТОЛЬКО keyword-miss ветка: FACT (decay 0.010 → L3) с argmax 'stable' ≥ 0.9 промоутится в L4, keyword-матчи и уверенность ниже порога не трогаются, флаг `rag.textcat` default OFF, breaker textcat_model 3/60s.

| конфигурация | 5-fold CV | stable precision | stable recall |
|---|---|---|---|
| keyword-мапы (статус-кво) | acc 0.871 | **0.636** | 0.352 |
| keyword + textcat (th=0.9) | acc 0.848 | 0.485 | **0.484** |
| textcat-only (dev n=62, th=0.9) | acc 0.839 | 0.750 | 0.300 |

Вердикт: **прод НЕ включаем** — модель добавляет recall (+13пп) но роняет precision (−15пп): половина L4-промоушенов была бы ложной, а L4-загрязнение дороже пропущенного факта. Keyword-мапы остаются единственным роутером. Шипится инфраструктура: `shared/textcat.py` (classify + route_promote_stable + breaker), `shared/textcat_data.py` (сборщик self-labels), `scripts/train_textcat.py` (train + threshold sweep + метрики в train_metrics.json), дистиллер-хук за флагом, тесты 11. Путь включения: переобучение при L4 non-fact ≥ 300 (сейчас 87), затем 5-fold P ≥ 0.75 → флаг ON владельцем. OOD-наблюдение: на мусоре ('abc') модель уверена в ephemeral — порог защищает только stable-сторону, что и нужно по контракту.

**Из EDM.md (транскрипт Aurelle, сверка 2026-09-05):** драфт v34 покрывает документ полностью — формул MIB/EDM/ITS в статье нет (реконструкция = модель), sign ≈ fancy MIB, конфаунды сравнения (Pinecone+Cohere vs встроенный ITS, exhaustive vs HNSW), цифры MAIR. Указатели раздела 7 доехали в драфт, но не в диздок — фиксируются:
- **TOKI** (bitemporal operator algebra для contradiction resolution) — G-комплемент conflict-fusion;
- **PROJECTMEM** (local-first event-sourced memory + judgment layer) — родня L0, референс;
- **Reliable Post-Retrieval Assembly** (separating evidence extraction from policy execution) — валидация гейт-дизайна;
- **Render Confound in Deprecation-Aware Memory Evaluation** — ловушка eval №11;
- **Covariance Structure... Binary Quantization** — обоснование/границы sign-бинаризации quantize.py;
- **REWA / Information Theory of Similarity (2512.00378)** — rate-distortion yardstick для «достаточно ли 128 bytes».

**Из Автохуки.md (исходная спека автоматической памяти, сверка):** реализовано полностью — «триггеры не таймеры» (autohooks daemon poll = transport, saves server-side), хуки session_started/session_ended/new_message/memory_pressure/auto_context/post_session_diff, fire-async + mem-передача + хендлеры-исполнители (registry takes_mem/is_async), server-side importance-gate вместо эвристик демона. Единственный непокрытый пункт:
- **`context_assembly` хук** (pre-response сборка контекста системой, «я не думаю об инжекте») — инъекция происходит harness-side; ariel-часть готова (memory_context_inject + session-start inject + auto_context post-recall). Доработка — контракт harness-адаптеров (Stage 2, cow/Hermes/MiMo). **→ SHIPPED 2026-09-06 (`24597db`): `autohooks context --text <msg>` — одна CLI-точка pre-response сборки (top-5 релевантных recall_protocol + recent L1, бюджет-capped, md/json); контракт+виринг для трёх адаптеров — docs/hooks/context-assembly.md (правила вставки cache-friendly: <relevant-memories> со стрипом истории, L1-prepend, cache:break). Правки адаптеров — на следующий деплой (владелец: агентов не рестартить).**
- **Минорный хвост осознанно-отложенного (distillate)**: B2 is_current-view; B4 ttl_minutes на тул-поверхности; B7 heat sum+1; B10 recurring→staging; C1 генератор сцен; C4 pinned; C5 private-флаг; C6 Layer Charter; C9 compact-render; D2 .abstract-тир; D3 MOC-first роутер; D4 retrieval-трейс в L0; D7 session-diversity; D12 BFS-upgrade `_from_graph`; E6-ретро skill-mine; changed-since модальность; Aeon lookaside buffer; Tenure hard-scope filter post-RRF; CWL dependency-aware инъекция; no-silent-fallback инвариант; reconstruction-check hot→warm; APEX fuse-then-summarize; Basic Memory observation-синтаксис. Каждый помечен в research draft с вердиктом; попадание в волны — на планировании.

### [S20] Stage 2 — вердикты (журнал абляций и решённых пунктов, 2026-09-06)

**№11 ENGRAM-абляция, прогон 1 (MINI_DATASET n=10, proxy-judge, commit f93f6c3):**
5-source RRF побеждает dense-per-kind решительно — вызов драфта отработан, источники НЕ режутся.

| arm | accuracy | ndcg@5 | recall@5 | construction_tokens | verdict |
|---|---|---|---|---|---|
| rrf | 1.000 | 0.389 | 0.600 | 1257 | статус-кво без EDM/ITS |
| dense_per_kind | 0.200 | 0.000 | 0.000 | 125 | PROVален: ENGRAM-упрощение без wiki/episodic-путей теряет всё |
| gated | 1.000 | 0.430 | 0.600 | 1150 | pre-gate −107 tok (−8.5%) без потери качества |
| full | 1.000 | 0.729 | 1.000 | 1008 | WINNER: EDM/ITS + роутинг, и короче всех |

- Negative-control: shuffled 0.300 < real 1.000 на rrf/full — PASS (judge чувствителен).
- dense_per_kind 0.20 acc — ожидаемо: арм ищет только L4/rag по kind_for_text(query), wiki/episodic недоступны (это конструкция арма, не баг). ENGRAM-схема «+15 pts @ 1% токенов» не воспроизводится на многослойной памяти без dense-модели; наш hash-fallback эмбеддинг-тир не даёт dense-качества.
- gated −8.5% construction_tokens при acc=1.0 — pre-gate кандидат на включение в проде ПОСЛЕ прогона на LongMemEval-S (HF сейчас недоступен оффлайн — прогон откладывается до сети; MINI-вердикт прелиминарный).
- dense_per_kind остаётся армом №11-eval, НЕ прод-путём.

**Прогон Plan A/C — as-shipped (2026-09-06, eb6f959..5c9a67d):**

| пункт | план | commit | verdict |
|---|---|---|---|
| GA min-max ACT-R | A.1 | ead6b20 | per-query `_minmax_actr` [1.0,1.3] в multi_source: топ-факт ×1.3, худший нейтральный 1.0 (floor=нейтральный, не усреднение) |
| maxChars per-block | A.2 | b95a8f1 | `_cap(400)` на всех 9 build-точках inject; `inject.max_chars` default-ON (единственное исключение из default-off) |
| semantic dedup | A.3 | 99279e5 | cos>0.92 same-kind LIMIT 50, `memory.semantic_dedup` default-OFF; позиция — ДО conflict-check (иначе парафрай рождает конфликт-пару из дубликата — тест поймал) |
| B2 is_current | C.1 | a0424be | глобальное скрытие earlier: C4-версионированный ключ `::vN` ИЛИ same-key scope=later (private-later не закрывает); `include_superseded=True` возвращает скрытые; каждый item несёт `is_current`; плоский SQL на earlier-строку вместо двух из плана (план-SQL имел key=?/LIKE-коллизию) |
| changed_since | C.2 | 5c9a67d | дельта-поллинг `get_intervals` по valid_from >= порога (Memanto); None — вся цепочка как раньше |

План-отклонения: тест B2 усилен (12 филлеров — later гарантированно off-page, план-версия при limit=10 пару не роняла); фикстура `hermetic_core` (не `hermetic_cm`); mypy требует tuple(params) в execute.

**Прогон Plan B/D — as-shipped (2026-09-06, ead728c..ebd7af4):**

| пункт | план | commit | verdict |
|---|---|---|---|
| channel-гранулярность | B.1 | ead728c | `shared/channels.py`: KNOWN_CHANNELS (9) + channel_of (prefix до `:`, unknown → other) + canonical_source; без миграции — конвенция |
| 3-option конфликт-контракт | B.2 | 85a288b | memory_proposals action=conflict (тулов по-прежнему 65): supersede/retain через ConflictResolver.resolve (группа закрыта, проигравший архивирован); annotate — аннотация в metadata СТАРОЙ стороны конфликта; ключ — детерминированная `_canonical_key(content, kind_for_text)`-связка дистиллера (плановый cmem.search fuzzy — нет metadata у items и не та запись) |
| orphan-anchor GC | B.3 | 06d3d7b | `_orphan_anchor_gc` в graph_enrich: episode:% якоря (fact/question) без рёбер старше 7д; вызов ПЕРЕД dream (REM мостит изолированные якоря первыми — episode-токены дают Jaccard 1.0); отчёт `orphan_gc: N`; noop-тест адаптирован |
| counter-signal → канон-ключи | B.4 | 081a446 | `_canonical_key` разворачивает superseded-имя в current ДО синоним-канонизации → переименование падает в один ключ, C4 строит superseded-цепочку сам; dual_route-пессимизация не задета |
| gap-registry | D.1 | ff3ceb0+f0e240b | `lifecycle/gap_registry.py`: question-эпизоды (SQL по ВСЕМ пользователям, tags LIKE — search_by_tag per-user) + zero-result хвосты (≥2 повторов) → memory_gaps (gap_hash, идемпотентно check-then-write); graph_enrich фаза + отчёт `gap_registry`; zero-result ветка молчит до первого ensure минера |
| harness breaker | D.2 | 87d91bf | `shared/harness_breaker.py`: harness_call per-agent 5 fails → 60s open (breaker_registry-backed), HarnessUnavailableError; адаптеры вне репо подключают следующим визитом |
| retrieval-фильтр provenance | D.3 | ebd7af4 | `_expand_graph(..., edge_exclude=None)`: рёбра с тегом heuristic:<name> не разворачиваются при graph-expand; None = статус-кво (LIKE по JSON-массиву tags) |

**Прогон Plan E — as-shipped (2026-09-06, f0bad78..74f13e9):**

| пункт | план | commit | verdict |
|---|---|---|---|
| wiki_id backlink | E.1 | f0bad78 | минер wiki_fact_links мёржит page **node_id** в `metadata.wiki_ids` (idempotent set, no-op save при повторе — LEDGER не раздувается); существующие metadata-ключи (scope, source_raw_id) выживают; node_id, не wiki_index.entry_id — узловое пространство рёбер и read-поверхности |
| recall со страницы | E.2 | 74f13e9 | `wiki_read` → `related_facts` + `related_count` (обратный проход по wiki_fact_link-рёбрам, join fact-узел.content == core_memory.value, private исключены); пустой список = норма; тулов 65 стабильно |

Волна S18-19 ЗАКРЫТА полностью: A (4) + C (2+диздок) + B (4) + D (3) + E (2) = 15 пунктов + диздок-вердикты; gate 1452/0, mypy 224 clean, ruff/format clean.

**№11 ENGRAM-абляция, прогон 2 (LongMemEval-S n=50 stride, proxy-judge, 2026-09-06):**
Датасет: официальный xiaowu0162/longmemeval (S-файл 265МБ → локальный кэш; карточка битая для load_dataset — файлы без расширений). 50 вопросов stride-выборкой (все 6 категорий: multi-session 14, temporal 13, KU 8, ss-user 7, ss-assistant 5, ss-pref 3), корпус = union haystack их сессий (2443 сессии). Хэш-эмбеддинги (ARIEL_HASH_EMBEDDINGS=1, консистентно с прогоном 1).

| arm | accuracy | strict | recall@5 | ndcg@5 | precision | construction_tokens | время |
|---|---|---|---|---|---|---|---|
| rrf | 0.500 | 0.200 | 0.010 | 0.500 | 0.142 | 879 501 | 104с |
| dense_per_kind | 0.420 | 0.040 | 0.000 | 0.187 | 0.038 | 790 784 | 58с |
| gated | 0.500 | 0.200 | 0.010 | 0.500 | 0.142 | 879 501 | 109с |
| full | **0.600** | **0.320** | **0.041** | **0.572** | **0.282** | 1 664 494 | 652с |
| full_pregate | 0.600 | 0.320 | 0.041 | 0.572 | 0.282 | 1 664 494 | 650с |
| rrf_shuffled (neg-ctrl) | 0.420 | 0.080 | 0.010 | 0.368 | 0.116 | 879 501 | 103с |
| full_shuffled (neg-ctrl) | 0.540 | 0.180 | 0.041 | 0.359 | 0.200 | 1 664 494 | 647с |

Вердикты:
- **full подтверждает победу** (второй датасет): acc 0.600 vs 0.500 rrf, strict 0.320 vs 0.200, recall@5 4.1% vs 1.0%, precision 0.282 vs 0.142 — EDM/ITS + роутинг лучше статус-кво и на длинном корпусе. dense_per_kind мёртв и здесь (recall 0.000).
- **VERDICT ПРОТИВ pre-gate в проде**: gated ≡ rrf байт-в-байт (гейт-матрица gate_sources для английских вопросных запросов — «длинный/вопросный → полный fan-out»; MINI-экономика −8.5% была на русских коротких/перечислительных запросах) и full_pregate ≡ full (pre-gate урезает пул fan-out, но после EDM-rerank финальные топ-хиты те же — экономия есть в стоимости fan-out, НЕ в construction_tokens; на S нулевая). retrieval.pregate остаётся default-off; кандидат пересматривается только при русской рабочей нагрузке с короткими/перечислительными запросами.
- **Честная плашка judge**: proxy-judge на английском слабодискриминативен — full_shuffled 0.540 против real 0.600 (на MINI падение было 1.000→0.300). Discriminative-метрика — strict (0.320 vs 0.180). Абсолютные acc значения оптимистичны; сравнение армов валидно (все на одном judge).
- recall@5 низкий у всех (≤4.1%): evidence-сессии LongMemEval-S тонут в 2443-сессионном корпусе без dense-модели — известное ограничение hash-эмбеддингов (прогон 1, dense_per_kind-вердикт).

Инфра урока: aiosqlite worker-поток при закрытии инстанса роняет следующий run_eval в том же процессе — один run_eval на процесс + os._exit(0) (тот же паттерн, что conftest.py::pytest_sessionfinish). /tmp 80% quota от tmp-баз прогонов — ручная чистка ariel-eval-* между волнами (backup_cron._cleanup_tmp уже чистит pytest-of-*).

**№11 прогон 3 — dense-эмбеддинги (LongMemEval-S n=50, e5-small 384d, 2026-09-06):**

| arm | accuracy | strict | recall@5 | ndcg@5 | precision | construction_tokens | время |
|---|---|---|---|---|---|---|---|
| full_hash (прогон 2) | 0.600 | 0.320 | **0.041** | **0.572** | **0.282** | 1 664 494 | 652с |
| full_dense | 0.600 | **0.340** | 0.031 | 0.558 | 0.252 | 1 375 219 | 2291с |
| full_dense_shuffled (neg-ctrl) | 0.540 | 0.180 | 0.031 | 0.335 | 0.184 | 1 375 219 | 2877с |

Вердикт: **dense НЕ обгоняет hash на этой выборке — статус-кво (hash) сохраняется**. strict +2пп (0.320→0.340, единственный рост), но recall@5 упал (0.041→0.031), precision/ndcg ниже, время ×3.5 (2291с: dense-кодирование 2443 сессий на CPU). Anti-контроль валиден (0.540 < 0.600). Объяснение гипотеза: e5-small без fine-tune на retrieval-задачах этого домена не бьёт «лексический якорь» RRF-микса (FTS+tags+core точные совпадения) — dense добавляет семантику там, где лексика уже сработала, и добавляет шум там, где не сработала; recall-провал прогонов 1-2 скорее ограничен выборкой 50 (evidence-сессии в tail) + сплитом S, чем качеством векторов. Практические следствия: (1) dense_per_kind-арм и semantic-dedup/crosscheck/HDBSCAN-флаги остаются OFF до появления большего корпуса или dense-aware переобучения порогов; (2) e5-small модель остаётся в venv (кэш HF), hash-fallback остаётся прод-путём эмбеддингов; (3) fail-fast runner scripts/run_lme_dense.py — повтор прогона одной командой. Инфра: env-переменная ARIEL_HASH_EMBEDDINGS=1 ставится хуком ariel-inject.ts всем дочерним процессам сессии (осознанный live-выбор) — runner снимает её принудительно; aiosqlite-гонка требует один run_eval на процесс (как раньше).

**Сверка прогона 2 с published baselines (arXiv 2410.10813, ICLR 2025, HTML v2):**

| система | настройка | accuracy |
|---|---|---|
| **ariel-memory (arm full, наш)** | LongMemEval-S, полный S-корпус, hash-эмбеддинги, **без LLM-ридера** (token-overlap judge) | **0.600 proxy / 0.320 strict** |
| GPT-4o long-context | _S, полный 115k контекст | 0.606 |
| GPT-4o + Chain-of-Note | _S | 0.640 |
| Llama 3.1 70B long-context | _S | 0.334 |
| ChatGPT memory (GPT-4o) | history 10× короче S | 0.577 |
| Coze (GPT-4o) | history 10× короче S | 0.330 |
| Oracle GPT-4o (только evidence) | _S | 0.870 |

Вывод: retrieval-стек ariel (multi-source RRF + роутинг + EDM/ITS + K-gate) на честном LongMemEval-S в лиге GPT-4o long-context (0.606) и выше коммерческих memory-надстроек ChatGPT (0.577)/Coze (0.330) — при нулевых LLM-затратах в рантайме поиска и без генерации связного ответа (eval retrieval-поверхности). Оговорки: judge разные (у статьи GPT-4o-судья 97% human-согласия; наш proxy оптимистичен — shuffled 0.54, discriminative-метрика strict 0.32 и она выше Llama 3.1 70B full-context 0.334 в их judge); срез 50/500 вопросов; recall-рычаг — dense-эмбеддинг (наш recall@5 4.1% на hash-фолбэке; у статьи лучшая конфигурация R@5 0.73 Stella). Встречное вложение: dense-модель даст больше, чем новые гейты.
