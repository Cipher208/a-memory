# RAG Engine

Unified search across memory layers.

## Strategies

| Strategy | Description | Best For |
|----------|-------------|----------|
| `fts` | Full-text search via FTS5 | Short queries, keywords |
| `mib` | Binary embedding similarity | Semantic search |
| `hybrid` | FTS5 + MIB with scoring | General purpose |
| `auto` | Adaptive (fts for short, hybrid for long) | Default |

Search is scoped per `(layer, user_id)`. Queries are tokenized: multi-word queries match ANY word, and core results are ranked by matched-word count, then importance.

## Usage

```python
from rag.engine import RAGEngine

engine = RAGEngine()

# Search
results = await engine.search(query="database architecture", user_id="u1", strategy="hybrid", limit=10)

# Ingest
await engine.ingest(content="PostgreSQL is used for production...", user_id="u1", page_id=1)
```

## Scoring

Results scored by `Scorer` with weights:

- **Relevance**: RRF score + optional binary score
- **Novelty**: ITS-inspired surprise (rare = more novel)
- **Type boost**: bonus for relevant wiki types
