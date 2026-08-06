# Changelog

## v1.5.0 (2026-07-07)

### Features
- **SHA-256 dedup**: Prevents duplicate observations within 5-minute window
- **Circuit breaker**: Prevents cascading LLM/embedding failures (3 errors → open → 30s recovery)
- **Token budget**: Limits context injection to 2000 tokens with CJK-aware estimation
- **Privacy filter**: Strips API keys, secrets, and private tags before storage

### Testing
- Dedup: 5 test cases (first call, duplicate, different value, different session, TTL expiry)
- Circuit breaker: 8 test cases (threshold, recovery, half-open, context manager, registry, metrics, callbacks)
- Token budget: 3 test cases (short text, long text, CJK estimation)
- Privacy filter: 7 test cases (OpenAI, GitHub, Slack, Bearer, private tags, empty text, multiple secrets)

### Documentation
- Migration roadmap from agentmemory added to ROADMAP.md

## v1.4.0 (2026-07-06)

### Testing
- Test suite optimization: 364→250 tests
- Parametrized 6 test files (user/agent, ru/en, auto_strategy)
- Property-based expansion: 25→79 Hypothesis tests
- Logic verification: ImportanceScoring, TypedDecay, SagaCompensation, SearchRelevance, RAGPipeline, WikiCRUD, ConnectionPool, ImportanceGate, RateLimiter
- Stateful machines: MemoryManager, Saga multi-step, Hooks execution order
- Chaos fixtures: database_locked, api_timeout, keyboard_interrupt, corrupt_db
- Coverage tests for typed_export, backup, audit_trail, rate_limiting, agent_hooks, wiki, backup_cron, saga
- Deleted 10 duplicate test files
- Coverage: 73% (Codecov)

## v1.3.1 (2026-07-06)

### Fixed
- aiosqlite 0.22.0 hang — pinned version in CI
- CI test hang — rewrote e2e tests with temp databases
- pytest_sessionfinish hook for process termination

### Testing
- 18 e2e tests covering all 25 MCP tools

## v1.3.0 (2026-07-05)

### Security
- SQL injection findings: all false positives (parameterized queries)

### Architecture
- RAG engine split: engine.py → engine.py + search.py + chunking.py
- Tool rename: memory_search_rrf → memory_search

### Fixed
- N+1 query in _search_rrf
- Embedding dedup
- Router simplification
- DB_NAME constant extraction
- Saga complexity reduction

### Testing
- 499 tests, coverage 77%

## v1.0.0 (2026-07-01)

### Features
- 19 unified MCP tools with layer parameter
- 4-layer memory architecture (L1-L4)
- Typed memory (13 categories)
- RAG search (FTS5 + MIB + hybrid)
- Knowledge graphs (epistemic + temporal)
- Wiki system (14 content types)
- Saga pattern (retry, idempotency, compensation)
- Envelope encryption (libsodium)
- Platform-aware async (aiosqlite / asyncio.to_thread)
- 24 hooks with importance gating
- Automatic backups with jitter
- Rate limiting
- Dashboard
- Health endpoints

### Testing
- 372 tests passing
- 25 property-based Hypothesis tests
- CI on Python 3.10-3.13
- Type checking (mypy)
- Linting (ruff)
- Secret scanning (gitleaks)
