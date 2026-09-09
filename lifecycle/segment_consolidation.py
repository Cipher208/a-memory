"""LycheeMemory V2 boundary detection (draft v37, Eq1-4) — L0 segmentation.

LLM-free segmenter of the L0 journal: records are grouped into semantically
coherent segments at surprise/cohesion boundaries:

- Eq1: s_t = 1 − max(sim(e_t, c_k), sim(e_t, h_k)) — surprise against the
  segment centroid AND the last element;
- Eq3: d_t = max(0, Coh(S_k) − Coh(S_k ∪ {x_t})) — cohesion drop when a
  record is added;
- Eq2 (σ-form): p_t = σ(b + w_s·s_t + w_c·d_t + w_l·L_t) — boundary probability;
  cut at p_t > δ=0.50 or token cap (segment ≤ cap tokens / ≤ max_turns records).

Similarity is token-Jaccard (deterministic, no dependence on the hash fallback
of embeddings; the same choice as in graph_enrich REM). Constants (b, w_s, w_c, w_l)
approximate Table 10 of the paper; δ=0.50 and the caps 300/600/900 are taken verbatim.
Fixed-width partitioning gives 82.40 (fixed-window ablation), boundary detection —
89.22 LoCoMo: the boundaries are not noise.
"""

from __future__ import annotations

import math
from typing import Any

from shared.constants import DB_NAME

DELTA = 0.50  # boundary threshold (paper Table 10)
SEGMENT_TOKEN_CAP = 900  # segment cap (300/600/900 in the paper; the large one for L0)
MAX_TURNS = 10  # max records per segment
# σ-constants (approximation of Table 10: surprise dominates, cohesion drop second)
SIG_B, W_S, W_C, W_L = -2.0, 2.0, 1.0, 0.5


def _jaccard(a: set[str], b: set[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _cohesion(segments_tokens: list[set[str]]) -> float:
    """Return Coh(S), the mean pairwise token-Jaccard similarity of the segment (0 for <2 items)."""
    n = len(segments_tokens)
    if n < 2:
        return 0.0
    total = 0.0
    pairs = 0
    for i in range(n):
        for j in range(i + 1, n):
            total += _jaccard(segments_tokens[i], segments_tokens[j])
            pairs += 1
    return total / pairs


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def detect_boundaries(records: list[tuple[int, str]], token_cap: int = SEGMENT_TOKEN_CAP) -> list[list[int]]:
    """Apply Eq1-4 to [l0_id, text] records -> list of segments (l0_id lists, order preserved)."""
    if not records:
        return []
    from rag.edm import tokens

    segs: list[list[int]] = []
    cur_ids: list[int] = []
    cur_tokens: list[set[str]] = []
    cur_tokens_total = 0
    for rid, text in records:
        toks = tokens(text)
        est = len(text) // 4 or 1  # rough token estimate for the cap
        if cur_ids:
            # Eq1: surprise against the centroid and the last element
            centroid: set[str] = set().union(*cur_tokens) if cur_tokens else set()
            sim_last = _jaccard(toks, cur_tokens[-1])
            sim_centroid = _jaccard(toks, centroid)
            s_t = 1.0 - max(sim_last, sim_centroid)
            # Eq3: cohesion drop
            coh_before = _cohesion(cur_tokens)
            coh_after = _cohesion([*cur_tokens, toks])
            d_t = max(0.0, coh_before - coh_after)
            p_t = _sigmoid(SIG_B + W_S * s_t + W_C * d_t + W_L * (1 if len(cur_ids) >= MAX_TURNS else 0))
            if p_t > DELTA or cur_tokens_total + est > token_cap or len(cur_ids) >= MAX_TURNS:
                segs.append(cur_ids)
                cur_ids, cur_tokens, cur_tokens_total = [], [], 0
        cur_ids.append(rid)
        cur_tokens.append(toks)
        cur_tokens_total += est
    if cur_ids:
        segs.append(cur_ids)
    return segs


async def segment_l0(since_hours: float = 24.0, layer: str = "user") -> dict[str, Any]:
    """Segment recent L0 records (nightly operator). Returns statistics.

    Segmentation is currently report-only (a map of coherent batches for
    consolidation); the "one LLM call per segment" stage is not needed in
    a-memory: the distiller is already LLM-free, and the nightly graph_enrich
    consumes the segments as a grouping.
    """
    from shared.connection import connection_manager

    conn = await connection_manager.get(DB_NAME)
    cutoff = __import__("time").time() - since_hours * 3600
    rows = await (
        await conn.execute(
            "SELECT id, text FROM l0_journal WHERE ts >= ? AND layer=? ORDER BY id ASC LIMIT 500",
            (cutoff, layer),
        )
    ).fetchall()
    records = [(int(r["id"]), str(r["text"])) for r in rows]
    segments = detect_boundaries(records)
    sizes = [len(s) for s in segments]
    return {
        "records": len(records),
        "segments": len(segments),
        "avg_segment": round(sum(sizes) / len(sizes), 2) if sizes else 0.0,
        "largest": max(sizes) if sizes else 0,
    }
