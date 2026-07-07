# ROADMAP

> Future development plans for mcp-ariel-memory after v1.0.0.

---

## 1. Publishing & Distribution

- [ ] **PyPI publication** — `twine upload dist/*`, requires `~/.pypirc` with API token
- [ ] **GitHub Release** — create tag `v1.0.0` + release with CHANGELOG
- [ ] **MCP Registry update** — fix description: "37 tools" → "19 tools"
- [ ] **Docker Hub** — publish `ariel-memory` image
- [ ] **npm version** — verify `mcp-ariel-memory@1.0.0` is current, bump if needed

## 2. Architecture & Performance

- [x] **Float blobs migration** — `keep_float_blobs` config + migration v7 (drop column)
- [x] **Expand batched reads** — fetchmany(1000) in forgetting, scheduler
- [x] **Connection pooling** — cache_size=64MB, temp_store=MEMORY
- [ ] **Alembic schema migrations** — replace raw SQL migrations with Alembic for versioned, reversible schema changes. Run migrations in CI before Docker build.
- [ ] **ONNX embeddings** — export BAAI/bge-small-en-v1.5 to ONNX + INT8 quantization via HuggingFace optimum. 4-5x faster inference, lower RAM. Blocks GIL on CPU currently.
- [ ] **HNSW index** — integrate hnswlib for binary search (replaces O(n) scan). Config: `binary.index: "hnsw"` with fallback to brute-force. Requires migration for index storage.
- [ ] **FTS5 improvements** — add triggers for auto-sync content (currently manual INSERT)
- [ ] **Extended indexes** — add indexes for `temporal_events`, `temporal_links`

## 3. RAG & Search

- [x] **Supervised thresholds** — per-dimension MIB thresholds from labeled pairs (implemented)
- [ ] **Hybrid reranker** — add cross-encoder reranking after RRF
- [ ] **Embedding cache** — implement LRU cache for repeated queries
- [ ] **Auto-strategy** — make `auto` strategy smarter (based on query analytics)
- [ ] **Query expansion** — add synonyms for FTS5

## 4. Wiki Restructuring

- [x] **Unify wiki system** — merge `file_wiki.py`, `user_wiki.py`, `agent_wiki.py` into one `WikiManager` with layer-based separation. Current 3 separate classes duplicate logic. Goal: single FTS5 index, shared sync logic, configurable per-layer behavior.

## 4. Bug Fixes & Cleanup

- [x] **Deprecated API cleanup** — removed `search_rrf()`, `search_binary()` (A7)
- [x] **Tool rename** — `memory_search_rrf` → `memory_search` (after MCP Registry update)
- [ ] **CLI improvements** — add `--version` flag, improve help messages
- [ ] **Dashboard** — add live updates (WebSocket), improve UI

## 5. Testing & CI

- [x] **Coverage** — add `--cov` in CI, reach 80% coverage (current: 73%)
- [ ] **Coverage 80%** — raise coverage from 73% to 80%: test server.py, tools_ops.py, backup_cron.py, saga.py, wiki/manager.py
- [x] **Property-based testing** — 25 Hypothesis tests (similarity, scoring, quantize, secrets, ring buffer)
- [x] **CI pipeline** — lint (ruff), typecheck (mypy), quality (skylos), security (gitleaks + pip-audit), test matrix (3.10-3.13)
- [x] **Concurrency control** — cancel-in-progress on CI workflows
- [x] **Pip caching** — all CI jobs use pip cache
- [ ] **Load testing** — add k6/Artillery tests for production simulation
- [ ] **Fuzz testing** — add fuzz tests for parsing and validation
- [ ] **Cross-platform testing** — add Windows to CI matrix (currently Linux only)
- [ ] **Benchmark tracking** — store benchmark results in CI, track regressions

## 6. Documentation

- [x] **API Reference** — auto-generate from docstrings (MkDocstrings)
- [x] **MkDocs site** — Material Theme, deployed to GitHub Pages
- [x] **Contributing guide** — CONTRIBUTING.md with contributor instructions
- [ ] **Architecture diagrams** — add mermaid diagrams to docs
- [ ] **Examples** — add usage examples for Claude Desktop, Hermes, etc.

## 7. Security

- [x] **Secret scanning** — GitHub secret scanning + push protection enabled
- [x] **Dependency audit** — pip-audit in CI, CVE scanning
- [x] **CodeQL** — default setup (AST analysis for SQL injection, path traversal)
- [x] **gitleaks** — CI workflow for secret scanning
- [x] **Docker hardening** — non-root user (UID 1000)
- [x] **CORS hardening** — restrict to localhost, configurable via config.yaml
- [x] **Issue forms** — YAML forms for bug reports and feature requests
- [ ] **RBAC** — add role-based model for multi-tenant deployments
- [x] **Input validation** — add Pydantic schemas on MCP tools
- [ ] **Key rotation** — zero-downtime master key rotation with re-encryption

## 8. Integrations

- [ ] **LangChain** — add LangChain integration (memory retriever)
- [ ] **AutoGen** — add AutoGen framework support
- [ ] **CrewAI** — add CrewAI support
- [ ] **LlamaIndex** — add LlamaIndex integration

## 9. Hooks & Extensibility

- [ ] **Plugin system** — implement plugins for custom hooks
- [ ] **Webhook support** — add webhook callbacks for external services
- [ ] **GraphQL API** — add GraphQL endpoint

## 10. Operations & Observability

### 10.1 Monitoring & Alerting

- [ ] **Prometheus metrics** — add Prometheus-compatible metrics endpoint (`/metrics`)
- [ ] **Telegram webhook** — send alerts to Telegram channel via bot API
- [ ] **Discord webhook** — send alerts to Discord channel via webhook URL

### 10.2 Health & Readiness

- [x] **Healthcheck endpoint** — `GET /health` with status, version, uptime, DB connectivity
- [x] **Readiness probe** — `GET /ready` with DB + migrations status
- [x] **Liveness probe** — `GET /alive` heartbeat
- [ ] **OpenTelemetry** — add OpenTelemetry tracing for distributed observability

### 10.3 Graceful Shutdown & Signal Handling

- [x] **Signal handling** — handle `SIGTERM` and `SIGINT` for graceful shutdown
- [x] **Stop background tasks** — cancel saga watchdog, backup cron, read_only_replica
- [ ] **Drain connections** — wait for in-flight requests to complete before exit

## 11. Typed Memory & Importance

- [x] **Typed Memory** — 13 categories with per-type retention, decay, and boost
- [x] **Memory kinds** — instruction, fact, decision, goal, preference, commitment, relationship, observation, rule, todo, question, hypothesis, context
- [x] **Type-aware forgetting** — instruction/rule/commitment never decay/archive
- [x] **Type-aware consolidation** — low importance instruction/rule/commitment still promote
- [x] **Type-aware hooks** — importance gate uses type policy
- [x] **Type-aware RAG boost** — search results boosted by query-type matching
- [x] **Typed export CLI** — export, reclassify, backfill bulk operations
- [x] **Importance v2** — 8-signal scorer (base, length, question, tech, emotional, novelty, retrieval, noise)
- [x] **Importance scheduler** — background daemon for periodic re-scoring
- [x] **Importance middleware** — uses ImportanceScorer instead of naive heuristic
- [ ] **Adaptive threshold** — replace fixed ImportanceGate threshold with EMA (exponential moving average) of recent message importance scores. Current 0.3 threshold is static; EMA would adapt to conversation patterns (high-signal technical discussions vs low-signal casual chat).

## 12. Saga & Reliability

- [x] **Saga retry** — exponential backoff with configurable retry_attempts/retry_on
- [x] **Saga idempotency** — idempotency_key_fn + saga_step_log prevents duplicate effects
- [x] **Saga encryption** — atomic encrypted state writes with legacy rotation
- [x] **Saga compensation** — saves state before compensation, archives deleted entries
- [x] **Saga cleanup** — handles stuck/failed/manual_review_required states

## 13. RAG Improvements

- [x] **Unified search** — `search(query, strategy=...)` with fts/mib/hybrid/auto
- [x] **BM25 conflict similarity** — char-trigram Jaccard fallback
- [x] **Type-aware search boost** — boost_for_query based on memory_kind
- [x] **Batched embedding reads** — fetchmany(1000) instead of fetchall
- [x] **epi_tags table** — indexed JOIN for fast tag lookups (1850 ops/s)
- [x] **rag_chunks index** — (page_id, chunk_index) for JOINs (3537 ops/s)
- [x] **Performance benchmarks** — FTS 1817, MIB 215, hybrid 178 ops/s

## 14. Tutorials & Launch Demo

- [x] **Launch demo script** — `demo.py` creates test data and shows features
- [ ] **Getting started guide** — step-by-step tutorial for new users
- [ ] **Quick start with Docker** — one-command Docker setup with sample data
- [ ] **Hermes integration guide** — how to deploy on Hermes agent

## 15. Operational Tasks

- [x] **Remove deprecated features** — search_rrf, search_binary removed
- [ ] **Cleanup** — remove unused tables, indexes, functions
- [ ] **Optimization** — optimize memory usage, CPU, I/O
- [ ] **Scalability** — test with 1M+ records

## 16. Hermes Memory Integration

- [ ] **Archive config** — expose `archive_threshold_days`, `archive_min_importance`, `forgetting.decay_rate` via MCP tools for agent self-configuration
- [ ] **Memory health check** — add `memory_stats` tool to check archive size, forgetting schedule, typed memory distribution
- [ ] **Auto-archival** — implement cron-based archival in Hermes (currently only `archive_threshold_days` is configurable from config)

---

**Completed:** 38/65 items
**Last updated:** 2026-07-05

## 17. Migration from agentmemory

Features to port from agentmemory (rohitg00/agentmemory) for full replacement.

| Пункт | Фича | Описание | Приоритет |
|-------|------|----------|-----------|
| R1 | Obsidian export | Экспорт памяти в Obsidian markdown с wikilinks | nice-to-have |
| R2 | Mesh sync | P2P синхронизация между инстансами | nice-to-have |
| R3 | Cross-agent sync | Память доступна из Claude Code, Cursor, OpenCode | nice-to-have |
| R4 | Git snapshots | Версионирование памяти через git commit/diff/rollback | nice-to-have |

### Migration phases

**Phase 1 (simple, 3-4 days):**
- SHA-256 dedup (5min TTL)
- Circuit breaker (3 errors → open → 30s)
- Token budget (2000 tokens on context_inject)
- Privacy filter (strip secrets before save)

**Phase 2 (medium, 5-7 days):**
- Slot system (8 pinned memory units)
- Lesson confidence (strengthen/decay)
- Faceted tagging (dimension:value with AND/OR)

**Phase 3 (complex, 10-14 days):**
- Hybrid search RRF with graph traversal
- Knowledge graph extraction from sessions
- Action graphs with dependencies
- Lease system for multi-agent

**Phase 4 (hooks, 5-7 days):**
- sync_turn hook (background capture)
- on_memory_write hook (mirror MEMORY.md)
- Diagnostics tool
- Export tool

---

**Completed:** 38/65 items (+4 roadmap)
**Last updated:** 2026-07-07
