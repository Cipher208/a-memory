"""Dual-route retrieval (Phase G Task 6): question-type router + S2 exhaustive + D-Mem escalation.

Маршруты (classify_query по маркерам и длине):
- factual    → RRF/EDM без graph-expand (HippoRAG2: graph-augmented проигрывает
               dense на single-hop) + ITS gating;
- enumerative («все/список/перечисли/list all») → S2-exhaustive (Mnemis):
               категория (wiki_type / node_type) → полный сбор детей БЕЗ top-k;
- multi-hop  → RRF/EDM; при низком dense-confidence (< 0.3) — D-Mem escalation:
               второй проход с включённым graph-источником (graph-rerank).

RRF — recall-first генератор; EDM/ITS — post-процессор (rag/edm.py).

Phase G Task 7: RETRIEVAL_MODE (env) / retrieval.mode (config.yaml) переключает
arm абляции (rag/ablation.py): 'rrf' | 'dense_per_kind' | 'gated' | 'full'.
Дефолт 'full' — существующее поведение не меняется.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from rag.ablation import dense_per_kind_search, gated_search, retrieval_mode
from rag.edm import DMEM_MIN_CONFIDENCE, FOK_TAU, dense_confidence, edm_rerank, make_s2_hit

logger = logging.getLogger(__name__)

_ENUMERATIVE_RE = re.compile(r"(?:\bвсе(?:х|м|е|ё)?\b|\bсписок\b|перечисл\w*|list\s+all|\benumerate\b)", re.IGNORECASE)
_MULTIHOP_RE = re.compile(r"(почему|из-за|привело|влияет|цепочк|поэтому|следств)", re.IGNORECASE)

_S2_CATEGORY_RE = re.compile(r"(?:все(?:\s+|х)|список\s+(?:все\w*\s+)?|list\s+all\s+|перечисл\w*\s+(?:все\w*\s+)?)([а-яёa-z0-9_]+)")

# S2 compression constraint (Task C6, Mnemis): категория с < n детьми не
# проходит — ветка терминируется (|слой i+1| ≤ |слой i| соблюдён структурно:
# дети всегда подмножество родительского каталога).
S2_MIN_CHILDREN = 2


def classify_query(query: str) -> str:
    """Question-type router: factual | enumerative | multi-hop.

    enumerative — маркеры полноты («все/список/перечисли/list all»);
    multi-hop — каузальные маркеры или длинный составной вопрос (≥ 10 слов);
    иначе factual.
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
    """S2-exhaustive (Mnemis): категория → полный сбор детей БЕЗ top-k.

    Иерархический спуск: wiki.list_all → фильтр по категории (wiki_type или
    заголовок); эпи-граф — полный сбор узлов совпавшего node_type. Категория —
    слово после маркера полноты («перечисли все правила» → rules); без
    совпадений — весь активный каталог (exhaustive fallback).

    C6 compression constraint: собранная категория с < S2_MIN_CHILDREN детьми
    терминируется (возвращается пусто) — вырожденные ветки не всплывают как
    «списки», потомку доверять не на чем.
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
        # Mnemis: категория с < n детьми не проходит — терминирование слоя.
        logger.debug("s2_exhaustive: category %r degenerate (%d < %d children) — terminated", category, len(hits), S2_MIN_CHILDREN)
        return []
    return hits


def _graph_cm(rag: Any, cm: Any | None) -> Any | None:
    """Cm для G-члена: явный аргумент, иначе cm рага (если у него есть .get)."""
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
    """Router dispatch: classify_query → маршрут → EDM/ITS post-процессор.

    rag — MultiSourceRAG (5-source RRF) или совместимый: search(query, ...,
    include_graph=bool). factual: graph-expand OFF; multi-hop: эскалация при
    low dense-confidence; enumerative: S2 с откатом на factual-путь.
    """
    # Task 7 ablation arms: 'full' (дефолт) — текущий dual-route ниже;
    # 'rrf'/'dense_per_kind'/'gated' — альтернативные армы для №11-eval.
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
        # нет cm (двойники без БД) → деградация к полному пути ниже

    qtype = classify_query(query)
    if qtype == "enumerative":
        hits = await s2_exhaustive(getattr(rag, "wiki", None), _graph_cm(rag, cm), query, user_id=user_id, layer=layer)
        if hits:
            return hits
        # категория не опознана → откат на dense/EDM (recall-first)

    # D-Mem: dense-first для factual И multi-hop (graph-augmented проигрывает
    # dense); эскалация — только gated: низкий dense-confidence → graph-rerank.
    pool = await rag.search(query, user_id=user_id, limit=100, include_graph=False)
    graph_cm = _graph_cm(rag, cm)
    hits = await edm_rerank(pool, query, cm=graph_cm, user_id=user_id, layer=layer)

    if qtype == "multi-hop" and await dense_confidence(pool, query) < DMEM_MIN_CONFIDENCE:
        pool2 = await rag.search(query, user_id=user_id, limit=100, include_graph=True)
        esc = await edm_rerank(pool2, query, cm=graph_cm, user_id=user_id, layer=layer)
        seen = {h.get("id") for h in hits}
        hits = [*hits, *(e for e in esc if e.get("id") not in seen)]

    # FOK-gate (Task C6, SYNAPSE τ=FOK_TAU): гейт по СЫРОЙ активации (до
    # minmax) — minmax на дегенеративном пуле даёт слабому 1.0; если
    # raw_activation нет (легаси), fallback на score.
    top = hits[0] if hits else {}
    activation = float(top.get("raw_activation") if top.get("raw_activation") is not None else top.get("score") or 0.0)
    if hits and activation < FOK_TAU:
        logger.debug("route_query: FOK-gate reject (raw activation %.3f < τ=%.2f)", activation, FOK_TAU)
        return []

    return [{**h, "kind": str(h.get("source") or "relevant")} for h in hits[:limit]]
