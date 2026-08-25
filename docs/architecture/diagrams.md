# Architecture

## System Overview

```mermaid
graph TB
    Client[MCP Client<br/>LLM Agent] -->|stdio/HTTP| Server[mcp_server<br/>MCPServer mcp 2.x]

    subgraph Server
        Tools[Tools Layer<br/>35 tools / 5 primitives exposed] --> Hooks[Hooks Pipeline<br/>19 hooks]
        Hooks --> Memory[Memory Layer]
    end

    subgraph Memory Layer
        L1[L1: ReflexBuffer<br/>ring 50] --> L2[L2: SessionStore<br/>sessions]
        L2 --> L3[L3: EpisodicMemory<br/>episodes]
        L3 --> L4[L4: CoreMemory<br/>key-value 5000]
    end

    Memory --> RAG[RAG Engine<br/>FTS5 + MIB]
    Memory --> Wiki[Wiki System<br/>FTS5 Index]
    Memory --> Graph[Knowledge Graphs<br/>epistemic + temporal]
    Memory --> Saga[Saga Engine<br/>persistence + compensation]
```

## Memory Consolidation Flow

```mermaid
sequenceDiagram
    participant Agent
    participant T as think / dream
    participant ST as DreamBuffer staging
    participant SW as Hourly Sweep
    participant L3 as L3 EpisodicMemory
    participant L4 as L4 CoreMemory

    Agent->>T: text / query
    T->>L3: episodes (importance-routed)
    T->>ST: dream() digests staged
    Note over SW: runs hourly per layer
    SW->>ST: drain staging → consolidate_staging per user
    SW->>L3: consolidation sweep (dedup by layer)
    SW->>L4: promote recurring episodes to core facts
    Note over SW: staging leftovers >24h dropped
```

## RAG Search Pipeline

```mermaid
flowchart LR
    Query[User Query] --> Router{RetrievalRouter}
    Router -->|short/exact| FTS[FTS5 Search]
    Router -->|long/semantic| Hybrid[Hybrid Search]

    FTS --> RRF[RRF Scoring]
    Hybrid --> FTS
    Hybrid --> MIB[MIB Binary Search]
    Hybrid --> RRF

    RRF --> Scorer[ImportanceScorer<br/>relevance + novelty + type_boost]
    MIB --> Scorer

    Scorer --> Results[Ranked Results]
```

## Security Architecture

```mermaid
graph LR
    Client -->|Bearer Token| Auth[Auth Middleware]
    Auth --> RateLimit[Rate Limiter<br/>100 req/min]
    RateLimit --> Tools[Tools Layer]

    Tools --> Encrypt[Envelope Encryption<br/>libsodium secretbox]

    subgraph Key Resolution
        KR1[OS Keychain] --> KR2[.env file]
        KR2 --> KR3[config.yaml]
        KR3 --> KR4[env var]
        KR4 --> KR5[auto-generate]
    end

    Encrypt --> KR1
```

## Saga Pattern

```mermaid
stateDiagram-v2
    [*] --> Pending
    Pending --> Running: execute
    Running --> Completed: success
    Running --> Failed: error
    Running --> Compensating: compensate
    Compensating --> Failed: compensation done
    Completed --> [*]
    Failed --> [*]
```

## CI Pipeline

```mermaid
flowchart LR
    Push[Push/PR] --> Lint[ruff check]
    Push --> TypeCheck[mypy]
    Push --> Quality[skylos]
    Push --> Audit[pip-audit]
    Push --> Security[gitleaks]
    Push --> Test[pytest 3.10-3.13]
    Push --> Coverage[pytest-cov]
    Push --> Build[python -m build]

    Test --> Publish[PyPI?]
    Build --> Publish
```
