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

- **Primary (F-наследие)**: RRF k=60 5-source (BM25 demote до minority) → **EDM re-rank** α·R+β·N+γ·G−δ·K → **ITS threshold-gating** (min-max [0,1] per query, threshold ~0.05, k≤100) → детерминизм (same query → same results, MOSS 1 год продакшена валидирует).
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

### [S12] Библиотеки

- **ВСТРАИВАТЬ**: spaCy + en_core_web_sm (privacy NER + entity-miner; атрибуция NOTICE, PyPI download-at-install); fractional-indexing (CC0, vendor; order_key для wiki/L0/канон-ключей); zero-result-минер (open-index идея, поверх recall_events).
- **ПАРК**: turbovec (16.7k★, ICLR'26 — но 5 мес + wrong regime; скальпели: length-renorm + TQ+ calibration в quantize.py — 2 строки numpy); msgspec (после micro-bench L0); SQLite-paper (2608.24060 scrydb — референс, sqlite-vec кандидат).
- **ИДЕИ**: PageIndex TOC-спуск без LLM для wiki-L4.5; PROV-O для provenance (typed_export); point-in-time graph snapshots; Merkle-DAG incremental reindex для wiki-проекции; spread activation как 4-й retrieval channel (SuperLocalMemory).

### [S13] Инфраструктура (остальное)

- memory_audit (расширение memory_diagnose контент-чеками: конфликты-ридер, дубли, stale, file↔DB reconciliation); health: /ready + alembic-head check (1 строка); ariel-cli (ls/tree/find/grep + typed_export surface); A8 MEMORY.md-бридж (двунаправленный, drain-marker); import_chat (claude/chatgpt/jsonl → L0 → гейты, origin=import); Roadmap.md freeze (сделано); admin-тулы манифест (8 шт); autocontext outcome-gated promotion в staging; cycles-таблица daemon (E9: 60s/1ч/3ч/24ч) + **chimera triple cost-cap** (per-cycle/rolling-60m/per-task + estimate-verb) как spend-gate ночного batch; Mermaid-канвас (после G-наполнения); Sessions Replay viewer (после F1); spaCy-атрибуция; crypt-claim fix (убрать/реализовать); **semantica decision read-поверхность** (trace_decision_chain / find_similar_decisions / analyze_decision_impact) для causal-графа E17a; **gbrain gap-reader** — memory_audit флагует unknown/stale/uncited/contradicting в ответах (detection без reporting = полсистемы) + `create_safety` verdict (exists/probable/unknown) как контракт выхода писателей.

### [S14] Stage 2 (последняя волна)

- Сведение 65 тулов + slots + URI-keys (включая peers/ tunnels резерв) + MCP behavior-аннотации (readOnlyHint/destructiveHint/idempotentHint — fork поддерживает ToolAnnotations; data-фундамент из триплет-минера) + переименования (wake_up alias) + admin-тулы манифест. **Начинается только после F/G/H.**

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
- **Минорный хвост осознанно-отложенного (distillate)**: B2 is_current-view; B4 ttl_minutes на тул-поверхности; B7 heat sum+1; B10 recurring→staging; C1 генератор сцен; C4 pinned; C5 private-флаг; C6 Layer Charter; C9 compact-render; D2 .abstract-тир; D3 MOC-first роутер; D4 retrieval-трейс в L0; D7 session-diversity; D12 BFS-upgrade `_from_graph`; E6-ретро skill-mine; changed-since модальность; Aeon lookaside buffer; Tenure hard-scope filter post-RRF; CWL dependency-aware инъекция; no-silent-fallback инвариант; reconstruction-check hot→warm; APEX fuse-then-summarize; Basic Memory observation-синтаксис. Каждый помечен в research draft с вердиктом; попадание в волны — на планировании.
