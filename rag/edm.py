"""EDM re-rank + ITS gating (Phase G Task 6): dual-route post-processor.

RRF fusion (search_rrf / 5-source) remains the recall-first candidate generator;
EDM/ITS re-rank the top-100 rather than replace recall:

EDM(m|q,S) = α·R(m,q) + β·N(m,S) + γ·G(m,q,S) − δ·K(m,S)
  R — normalized RRF-score (SYNAPSE-inhibition → per-query min-max → [0,1]);
  N — novelty: the share of query tokens that m ADDS to the already selected
      (marginal coverage via set operations) — boosts the complementary block;
  G — 0.3 if the block is connected by a led_to edge to an already selected one (chain completion);
  K — max cosine against the already selected (semantic-dedup).
α/γ/δ = 1.0, β = 0.8 — starting values; calibration — Phase H No.11.

Final EDM-score is per-query min-max → [0,1]; ITS threshold 0.05: blocks below
it are not returned; k ≤ 100 (ITS_K_CAP).
"""

from __future__ import annotations

import math
import re
from typing import Any

from lifecycle.graph_sanitation import INHIBITION_BETA, INHIBITION_TOP_M
from rag.multi_source import _ID_OFFSET_GRAPH, _ID_OFFSET_WIKI

EDM_ALPHA = 1.0
EDM_BETA_NOVELTY = 0.8
EDM_GAMMA = 1.0
EDM_DELTA = 1.0
ITS_THRESHOLD = 0.05
ITS_K_CAP = 100
CHAIN_BONUS = 0.3
DMEM_MIN_CONFIDENCE = 0.3
FOK_TAU = 0.12  # SYNAPSE FOK-gate (C6): top candidate below τ — refuse before the LLM (target FRR < 2.5%)
CAMA_NEFF_MIN = 1.5  # CAMA (C6): below — evidence is effectively from a single source → abstain

_TOKEN_RE = re.compile(r"[а-яёa-z0-9]+")


def tokens(text: str | None) -> set[str]:
    """Word tokens (len ≥ 3) — set operations for novelty/coverage."""
    return {t for t in _TOKEN_RE.findall((text or "").lower()) if len(t) >= 3}


def inhibit_scores(scores: list[float], beta: float = INHIBITION_BETA, top_m: int = INHIBITION_TOP_M) -> list[float]:
    """Lateral inhibition pre-step (SYNAPSE, formula G5) over the candidates.

    û_i = max(0, u_i − β·Σ_{k∈T_M}(u_k−u_i)·𝕀[u_k>u_i]), up to M=7 neighbors strictly
    stronger. In-memory version of lifecycle.graph_sanitation.lateral_inhibition:
    same formula, but without writing to epi_edges — re-ranking only.
    """
    out: list[float] = []
    for i, u in enumerate(scores):
        stronger = sorted((v for j, v in enumerate(scores) if j != i and v > u), reverse=True)[:top_m]
        out.append(max(0.0, u - beta * sum(v - u for v in stronger)) if stronger else u)
    return out


def minmax(values: list[float]) -> list[float]:
    """Per-query min-max into [0,1]. Degenerate case (all equal) → all 1.0 (the gate cuts nothing)."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-12:
        return [1.0] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def _minmax_floor(scores: list[float], positive: list[float]) -> list[float]:
    """Zero-floor min-max: negatives (the K penalty outweighed) are clamped to 0, positive → s/max.

    Negative tails do not compress the scale — the top block stays 1.0,
    evidence-free junk goes to 0 and is cut by the ITS threshold.
    """
    hi = max(positive)
    if hi <= 0:
        return [0.0] * len(scores)
    return [max(0.0, s) / hi for s in scores]


def graph_node_id(cand: dict[str, Any]) -> int | None:
    """node_id for graph/graph_expand candidates (id = −node_id − 3_000_000), else None."""
    rid = cand.get("id")
    if isinstance(rid, int) and rid <= -_ID_OFFSET_GRAPH:
        return -rid - _ID_OFFSET_GRAPH
    return None


def make_s2_hit(entry_id: int, title: str, content: str, wiki_type: str, score: float) -> dict[str, Any]:
    """Build an S2-route result in search-hit format (negative wiki-id)."""
    return {
        "id": -int(entry_id) - _ID_OFFSET_WIKI,
        "title": title,
        "content": content,
        "wiki_type": f"wiki:{wiki_type}",
        "score": float(score),
        "source": "s2_exhaustive",
    }


async def _led_to_neighbors(cm: Any, node_ids: list[int], layer: str, user_id: str) -> dict[int, set[int]]:
    """led_to-neighborhoods of the candidates from epi_edges (active edges only, G5 window).

    Symmetric: chain completion is «a block connected by led_to to an already selected one».
    """
    if cm is None or not node_ids:
        return {}
    try:
        from lifecycle.graph_sanitation import active_edges_clause
        from shared.constants import DB_NAME

        conn = await cm.get(DB_NAME)
        ph = ",".join("?" * len(node_ids))
        clause, params = active_edges_clause("e")
        cur = await conn.execute(
            "SELECT e.source_id, e.target_id FROM epi_edges e"
            " JOIN epi_nodes n ON n.node_id = e.source_id"
            " WHERE e.relation='led_to' AND n.layer=? AND n.user_id=?"
            f" AND (e.source_id IN ({ph}) OR e.target_id IN ({ph})) AND {clause}",
            (layer, user_id, *node_ids, *node_ids, *params),
        )
        rows = await cur.fetchall()
    except Exception:
        return {}  # G-member unavailable → degrade to R/N/K, do not crash
    neigh: dict[int, set[int]] = {}
    for r in rows:
        neigh.setdefault(int(r["source_id"]), set()).add(int(r["target_id"]))
        neigh.setdefault(int(r["target_id"]), set()).add(int(r["source_id"]))
    return neigh


async def _embed(texts: list[str]) -> list[list[float]]:
    from shared.embeddings import embed_texts

    try:
        return await embed_texts(texts)
    except Exception:
        return []  # embeddings unavailable → K-member disabled (dedup degrade)


def neff_hill(finals: list[float], alpha: float = 2.0) -> float:
    """CAMA N_eff (Task C6): N_eff = exp(log(Σ p_j^α)/(1−α)) — Hill diversity.

    p_j = final_j / Σ final (final ≥ 0 after the zero-floor). α=2 → N_eff = 1/Σp²
    (inverse Simpson index): monoculture → 1.0, k equal sources → k.
    α=1 is the degenerate case of the formula → 0.0 (abstain).
    """
    total = sum(f for f in finals if f > 0)
    if total <= 0 or abs(alpha - 1.0) < 1e-9:
        return 0.0
    p = [max(0.0, f) / total for f in finals]
    return math.exp(math.log(sum(pj**alpha for pj in p)) / (1 - alpha))


async def edm_rerank(
    cands: list[dict[str, Any]],
    query: str,
    *,
    cm: Any | None = None,
    user_id: str = "default",
    layer: str = "user",
    alpha: float = EDM_ALPHA,
    beta: float = EDM_BETA_NOVELTY,
    gamma: float = EDM_GAMMA,
    delta: float = EDM_DELTA,
    threshold: float = ITS_THRESHOLD,
    k_cap: int = ITS_K_CAP,
    deterministic: bool | None = None,
) -> list[dict[str, Any]]:
    """EDM re-rank + ITS gating. The 5-source RRF pool is fed as-is (recall-first).

    Greedy MMR: at each step, the argmax of EDM(m | S) among the remaining is picked —
    N and K are recomputed against the already selected; the result is per-query min-max → [0,1],
    blocks below threshold are cut off, capped at k_cap.

    S17 deterministic_retrieval (default off, config retrieval.deterministic):
    regulated scenarios get a pure min-max order without inhibition
    or the K-member (embeddings are machine-dependent) — a reproducible result for
    the same pool.
    """
    if not cands:
        return []
    if deterministic is None:
        from config import config

        deterministic = bool(config.get("retrieval", "deterministic", default=False))
    pool = cands[:k_cap]
    raw = [float(c.get("score") or 0.0) for c in pool]
    rrf = minmax(raw if deterministic else inhibit_scores(raw))
    qtok = tokens(query)
    texts = [f"{c.get('content') or ''} {c.get('title') or ''}" for c in pool]
    # K-member — semantic dedup on CONTENT (different titles do not make blocks different)
    ktexts = [str(c.get("content") or "") for c in pool]
    node_ids = [graph_node_id(c) for c in pool]
    led = await _led_to_neighbors(cm, [n for n in node_ids if n is not None], layer, user_id)
    vecs = await _embed(ktexts) if (len(pool) > 1 and not deterministic) else []

    def _toks(i: int) -> set[str]:
        return tokens(texts[i])

    remaining = list(range(len(pool)))
    selected: list[int] = []
    selected_nodes: set[int] = set()
    covered: set[str] = set()
    edm_scores = [0.0] * len(pool)
    # CAMA max-presence (Task C6): e_j = max_i z_ij — correlated records
    # (shared content/chain node) do not pump up evidence; one is counted.
    presence: dict[int, int] = {i: i for i in range(len(pool))}
    while remaining:
        best_i, best_s = remaining[0], -1e18
        for i in remaining:
            novelty = (len((_toks(i) & qtok) - covered) / len(qtok)) if qtok else 0.0
            nid = node_ids[i]
            chained = nid is not None and bool(led.get(nid, set()) & selected_nodes)
            chain = CHAIN_BONUS if chained else 0.0
            dup = 0.0
            if vecs and selected and not chained:
                from shared.embeddings import similarity

                # K-member — the dedup penalty, clamped to [0,1]: a negative
                # cosine = "not a duplicate" (hash embeddings produce negative
                # cosines); chain-linked pairs are not silenced by dedup (the G-member is
                # chain completion, not repetition).
                dup = max([0.0, *(similarity(vecs[i], vecs[j]) for j in selected)])
            s = alpha * rrf[i] + beta * novelty + gamma * chain - delta * dup
            if s > best_s:
                best_i, best_s = i, s
        edm_scores[best_i] = best_s
        selected.append(best_i)
        remaining.remove(best_i)
        covered |= _toks(best_i) & qtok
        nid = node_ids[best_i]
        if nid is not None:
            selected_nodes.add(nid)

    # CAMA max-presence: e_j = max_i z_ij → records with identical content
    # are correlated; their evidence merges into the group leader (not summed).
    if vecs:
        from shared.embeddings import similarity

        for i in range(len(pool)):
            for j in range(i):
                if similarity(vecs[i], vecs[j]) >= 1.0 - 1e-6:
                    presence[i] = presence[j]
                    break

    # zero-floor: negative tails (the K penalty outweighed) are clamped to 0 and
    # do not compress the min-max scale — the top block stays 1.0
    pos = [s for s in edm_scores if s > 0]
    final = [max(0.0, s) for s in _minmax_floor(edm_scores, pos)] if pos else [0.0] * len(edm_scores)

    # CAMA N_eff: the effective number of distinguishable sources (max-presence field).
    # DECISION (audit 05.09): abstain stays metadata for the eval offsite —
    # the answer-level consumer (refusing to answer) is decided at Stage 2 after
    # the ablation: forcing abstention before a numeric verdict is premature.
    presence_finals = [0.0] * len(pool)
    for i, f in enumerate(final):
        presence_finals[presence[i]] = max(presence_finals[presence[i]], f)
    n_eff = neff_hill(presence_finals)
    abstain = len(pos) > 0 and n_eff < CAMA_NEFF_MIN

    # Raw activation (the inhibited RRF BEFORE minmax): minmax on a degenerate
    # pool gives the weakest 1.0 — the FOK gate must look at the raw signal.
    # S17 deterministic mode: inhibition is bypassed — activation = raw RRF.
    inhibited = raw if deterministic else inhibit_scores(raw)

    out: list[dict[str, Any]] = []
    for i in sorted(range(len(pool)), key=lambda i: -final[i]):
        if final[i] < threshold:
            continue
        hit = dict(pool[i])
        hit["score"] = final[i]
        hit["raw_activation"] = round(inhibited[i], 4)
        hit["abstain"] = abstain
        hit["n_eff"] = round(n_eff, 3)
        out.append(hit)
        if len(out) >= k_cap:
            break
    return out


async def dense_confidence(cands: list[dict[str, Any]], query: str) -> float:
    """D-Mem dense-confidence: the share of query tokens covered by the top-10 candidates.

    Lexical proxy [0,1]: deterministic even without a model (hash embeddings give
    a noisy cosine). Upgrade path — e5 cosine query↔candidates once a model
    is available; the 0.3 threshold stays the same.
    """
    qtok = tokens(query)
    if not qtok or not cands:
        return 0.0
    covered: set[str] = set()
    for c in cands[:10]:
        covered |= tokens(f"{c.get('content') or ''} {c.get('title') or ''}")
    return len(covered & qtok) / len(qtok)
