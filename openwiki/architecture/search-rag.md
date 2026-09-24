---
type: "Reference"
title: "Search and RAG"
openwiki_generated: true
verified:
  - by: openwiki/0.6.0
    at: 2026-09-24T20:32:03.830Z
sources:
  - id: openwiki-source-3362a44760d5bb1a3a6551d2
    resource: repo://rag/ablation.py
  - id: openwiki-source-22b053057ca5712218010cb1
    resource: repo://rag/chunking.py
  - id: openwiki-source-8753daeb174206f0162334ff
    resource: repo://rag/conflict.py
  - id: openwiki-source-e415a2357821a22daea23da3
    resource: repo://rag/dual_route.py
  - id: openwiki-source-43ccd78d309d32796dfad9c6
    resource: repo://rag/router.py
generated: { by: "opencode", at: "2026-09-24T20:32:03.830Z" }
---

# Search and RAG

Retrieval is a gated multi-route pipeline: queries are classified, routed
across sparse, dense, and graph sources, then scored, fused, and filtered
for conflicts before results return.

## Pipeline (`rag/`)

- `router.py` + `dual_route.py` (`route_query`, `classify_query`) choose the
  retrieval route per query; `multi_source.py` fans out across sources.
- `search.py` / `searcher.py` execute sparse (FTS5 BM25) and dense passes;
  `engine.py` orchestrates.
- `scoring.py` fuses and ranks hits; `edm.py` applies inhibition;
  `actr.py` (`actr_activation`) boosts by recency and access count.
- `ablation.py` gates sources per query (`gated_search`, `gate_sources`,
  `retrieval_mode`) so narrow queries skip expensive routes.
- `conflict.py` (`ConflictResolver`, BM25 pair similarity + char n-gram
  Jaccard) filters contradictory hits.

## Ingest

- `ingestor.py` writes items; `chunking.py` splits text (default 500 chars,
  100 overlap); `synonyms.py` expands queries; `quantize.py` compresses
  vectors.

## Vector backend

- Dense vectors come from the shared local e5 service
  (`embeddings_service.py`, `intfloat/multilingual-e5-small`, loopback
  `:8710`), reached via `shared/embeddings.py` — no cloud embedding API is
  ever called.
