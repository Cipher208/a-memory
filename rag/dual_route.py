"""Dual-route retrieval (Phase G Task 6): question-type router + S2 exhaustive + D-Mem escalation.

Routes (classify_query by markers and length):
- factual    → RRF/EDM without graph-expand (HippoRAG2: graph-augmented loses
               to dense on single-hop) + ITS gating;
- enumerative (RU markers 'vse/spisok/perechisli' + 'list all', see _ENUMERATIVE_RE)
              → S2-exhaustive (Mnemis):
               category (wiki_type / node_type) → full child collection WITHOUT top-k;
- multi-hop  → RRF/EDM; on low dense-confidence (< 0.3) — D-Mem escalation:
               second pass with the graph source enabled (graph-rerank).

RRF — recall-first generator; EDM/ITS — post-processor (rag/edm.py).

Phase G Task 7: RETRIEVAL_MODE (env) / retrieval.mode (config.yaml) switches
the ablation arm (rag/ablation.py): 'rrf' | 'dense_per_kind' | 'gated' | 'full'.
Default 'full' — existing behavior is unchanged.
"""

from __future__ import annotations

import logging
import re

from config import config
from typing import Any

from rag.ablation import dense_per_kind_search, gated_search, pre_gate_flags, retrieval_mode
from rag.edm import DMEM_MIN_CONFIDENCE, FOK_TAU, dense_confidence, edm_rerank, make_s2_hit

logger = logging.getLogger(__name__)

_ENUMERATIVE_RE = re.compile(r"(?:\bвсе(?:х|м|е|ё)?\b|\bсписок\b|перечисл\w*|list\s+all|\benumerate\b)", re.IGNORECASE)
_MULTIHOP_RE = re.compile(r"(почему|из-за|привело|влияет|цепочк|поэтому|следств)", re.IGNORECASE)

_S2_CATEGORY_RE = re.compile(r"(?:все(?:\s+|х)|список\s+(?:все\w*\s+)?|list\s+all\s+|перечисл\w*\s+(?:все\w*\s+)?)([а-яёa-z0-9_]+)")

# S2 compression constraint (Task C6, Mnemis): a category with < n children
# fails — the branch is terminated (|layer i+1| ≤ |layer i| holds structurally:
# children are always a subset of the parent catalog).
S2_MIN_CHILDREN = 2

# Tenure counter-signal aliases (S17 item 7): demote a hit whose content
# contains a superseded name without the current one (renaming). Not a drop —
# the hit stays in the output with a lowered score.
_COUNTER_FACTOR = 0.7


def _apply_counter_signals(hits: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Demote hits by superseded names (config rag.counter_signals).

    A hit is demoted if its content contains the old name and does NOT contain
    the current one; a query explicitly asking about the old name is not
    penalized (a query in bijection with the old record is relevant to it).
    """
    try:
        from rag.synonyms import load_counter_signals

        signals = load_counter_signals()
    except Exception:
        return hits
    if not signals:
        return hits
    ql = query.lower()
    out = []
    for h in hits:
        content = str(h.get("content") or h.get("value") or "").lower()
        factor = 1.0
        for old, cur in signals.items():
            if old in content and cur not in content and old not in ql:
                factor *= _COUNTER_FACTOR
        if factor < 1.0 and isinstance(h.get("score"), (int, float)):
            h = {**h, "score": float(h["score"]) * factor, "counter_signal": True}
        out.append(h)
    return out


def classify_query(query: str) -> str:
    """Question-type router: factual | enumerative | multi-hop.

    enumerative — exhaustiveness markers (RU 'vse/spisok/perechisli' + 'list all',
    see _ENUMERATIVE_RE);
    multi-hop — causal markers or a long compound question (≥ 10 words);
    otherwise factual.
    """
    q = (query or "").strip().lower()
    if not q:
        return "factual"
    if _ENUMERATIVE_RE.search(q):
        return "enumerative"
    if _MULTIHOP_RE.search(q) or len(q.split()) >= 10:
        return "multi-hop"
    return "factual"


async def s2_exhaustive(
    wiki: Any | None,
    cm: Any | None,
    query: str,
    *,
    user_id: str = "default",
    layer: str = "user",
) -> list[dict[str, Any]]:
    """S2-exhaustive (Mnemis): category → full child collection WITHOUT top-k.

    Hierarchical descent: wiki.list_all → filter by category (wiki_type or
    title); epi-graph — full collection of nodes of the matched node_type. The
    category is the word after the exhaustiveness marker (query 'perechisli vse
    pravila' → category 'rules'); with
    no matches — the entire active catalog (exhaustive fallback).

    C6 compression constraint: a collected category with < S2_MIN_CHILDREN
    children is terminated (returns empty) — degenerate branches do not
    surface as «lists»: there is no basis to trust the child set.
    """
    category = ""
    m = _S2_CATEGORY_RE.search((query or "").lower())
    if m:
        category = m.group(1).rstrip(".,?!:;")
    hits: list[dict[str, Any]] = []

    if wiki is not None and hasattr(wiki, "list_all"):
        try:
            rows = await wiki.list_all(limit=100000)
        except Exception:
            rows = []
        for r in rows:
            wt = str(r.get("wiki_type") or "")
            if category and category not in wt and category not in str(r.get("title") or "").lower():
                continue
            hits.append(make_s2_hit(int(r.get("entry_id") or 0), str(r.get("title") or ""), str(r.get("content") or ""), wt, 0.5))

    if cm is not None and category:
        try:
            from shared.constants import DB_NAME

            conn = await cm.get(DB_NAME)
            cur = await conn.execute(
                "SELECT node_id, content, node_type, confidence FROM epi_nodes WHERE layer=? AND user_id=? AND node_type=?",
                (layer, user_id, category),
            )
            for r in await cur.fetchall():
                hits.append(
                    {
                        "id": -int(r["node_id"]) - 3_000_000,
                        "title": f"Graph Node {r['node_id']} ({r['node_type']})",
                        "content": r["content"],
                        "score": float(r["confidence"]),
                        "source": "s2_exhaustive",
                        "wiki_type": r["node_type"],
                    }
                )
        except Exception:
            logger.debug("s2_exhaustive: graph branch skipped", exc_info=True)

    if not hits:
        logger.debug("s2_exhaustive: no children for category %r", category)
    if category and 0 < len(hits) < S2_MIN_CHILDREN:
        # Mnemis: a category with < n children fails — layer termination.
        logger.debug("s2_exhaustive: category %r degenerate (%d < %d children) — terminated", category, len(hits), S2_MIN_CHILDREN)
        return []
    return hits


def _graph_cm(rag: Any, cm: Any | None) -> Any | None:
    """Cm for the G-member: explicit argument, otherwise the rag's cm (if it has .get)."""
    if cm is not None:
        return cm
    candidate = getattr(rag, "cm", None)
    if candidate is not None and hasattr(candidate, "get"):
        return candidate
    return None


async def route_query(
    rag: Any,
    query: str,
    *,
    user_id: str = "default",
    limit: int = 10,
    layer: str = "user",
    cm: Any | None = None,
) -> list[dict[str, Any]]:
    """Router dispatch: classify_query → route → EDM/ITS post-processor.

    rag — MultiSourceRAG (5-source RRF) or compatible: search(query, ...,
    include_graph=bool). factual: graph-expand OFF; multi-hop: escalation on
    low dense-confidence; enumerative: S2 with fallback to the factual path.
    """
    # Task 7 ablation arms: 'full' (default) — the current dual-route below;
    # 'rrf'/'dense_per_kind'/'gated' — alternative arms for №11-eval.
    mode = retrieval_mode()
    if mode == "rrf":
        pool = await rag.search(query, user_id=user_id, limit=limit, include_graph=True)
        return [{**h, "kind": str(h.get("source") or "relevant")} for h in pool]
    if mode == "gated":
        return await gated_search(rag, query, user_id=user_id, limit=limit)
    if mode == "dense_per_kind":
        graph_cm = _graph_cm(rag, cm)
        if graph_cm is not None:
            return await dense_per_kind_search(graph_cm, query, user_id=user_id, layer=layer, limit=limit)
        # no cm (twins without a DB) → degrade to the full path below

    qtype = classify_query(query)
    if qtype == "enumerative":
        hits = await s2_exhaustive(getattr(rag, "wiki", None), _graph_cm(rag, cm), query, user_id=user_id, layer=layer)
        if hits:
            return hits
        # category not recognized → fall back to dense/EDM (recall-first)

    # D-Mem: dense-first for factual AND multi-hop (graph-augmented loses to
    # dense); escalation is gated-only: low dense-confidence → graph-rerank.
    # S17 pre-gate: {} when retrieval.pregate is off (status quo), otherwise a
    # trimmed fan-out by query features — cost down + noise down, N_eff reflects the trimming.
    pool = await rag.search(query, user_id=user_id, limit=100, include_graph=False, **pre_gate_flags(query))
    # CLACK exp3: q-fields boost (score += w·|qt∩qf|) before EDM — config-only flag.
    if bool(config.get("retrieval", "qfields", "enabled", default=False)):
        from lifecycle.qfields import apply_qfield_boost

        pool = await apply_qfield_boost(_graph_cm(rag, cm), pool, query)
    graph_cm = _graph_cm(rag, cm)
    hits = await edm_rerank(pool, query, cm=graph_cm, user_id=user_id, layer=layer)

    if qtype == "multi-hop" and await dense_confidence(pool, query) < DMEM_MIN_CONFIDENCE:
        pool2 = await rag.search(query, user_id=user_id, limit=100, include_graph=True)
        esc = await edm_rerank(pool2, query, cm=graph_cm, user_id=user_id, layer=layer)
        seen = {h.get("id") for h in hits}
        hits = [*hits, *(e for e in esc if e.get("id") not in seen)]

    # FOK-gate (Task C6, SYNAPSE τ=FOK_TAU): gate on the RAW activation (before
    # minmax) — minmax on a degenerate pool gives the weakest hit 1.0; if
    # raw_activation is absent (legacy), fall back to score.
    top = hits[0] if hits else {}
    raw_val = top.get("raw_activation")
    score_val = top.get("score")
    activation = float(raw_val if raw_val is not None else (score_val if score_val is not None else 0.0))
    if hits and activation < FOK_TAU:
        logger.debug("route_query: FOK-gate reject (raw activation %.3f < τ=%.2f)", activation, FOK_TAU)
        return []

    # Tenure counter-signal aliases (S17 item 7): superseded names with a negative
    # role (demotion, not drop) — after ITS, before output.
    hits = _apply_counter_signals(hits, query)

    return [{**h, "kind": str(h.get("source") or "relevant")} for h in hits[:limit]]
