# Scoring

Unified scoring for RAG search results.

## Scorer

```python
from rag.scoring import Scorer, ScoringWeights

scorer = Scorer(mode="rrf", weights=ScoringWeights(relevance=1.0, novelty=0.5, type_boost=0.3))

results = scorer.rank_sync(query, candidates, user_id)
```

## CorpusStats

Novelty calculation based on retrieval history:

```python
from rag.scoring import CorpusStats

stats = CorpusStats(total_retrievals=100, doc_retrieval_counts={1: 5, 2: 3})
prior = stats.prior(doc_id=1)  # 0.05
```

## Properties (Hypothesis-verified)

- `final_score = w_rel * rel + w_nov * novelty + w_tb * type_boost`
- Results always sorted by `final_score` descending
- `prior() ∈ [0, 1]` for any doc_id
- `update_weights()` clamps to `[0, 2]`
