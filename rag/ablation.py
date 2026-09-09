"""Retrieval ablation arms (Phase G Task 7) — for №11-eval comparison of retrieval strategies.

RETRIEVAL_MODE (env) / retrieval.mode (config.yaml), default 'full':
- 'rrf'            — status quo: a single 5-source RRF search without EDM/ITS and routing;
- 'dense_per_kind' — ENGRAM simplification: one search per memory-kind (kind_for_text
                     of the query or all kinds) via FTS5+Hamming (+ kind-scoped
                     core_memory), set-merge without RRF fusion;
- 'gated'          — Adaptive RAG (simplified): query features (query length,
                     question word, «list all» markers, entity names from the
                     synonyms dictionary) decide which sources to fire;
- 'full'           — dual-route (classify_query → S2/EDM/ITS/D-Mem) — default,
                     existing Task 6 behavior, production unchanged.

This is NOT full ENGRAM (no spaCy/13 types) — the simplification suffices for
the ablation. rag_chunks.memory_kind: the ingestor does not tag kind yet, NULL → 'fact';
for a fair ablation the eval harness may backfill the column.
"""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING, Any

from config import config
from rag.synonyms import load_synonyms
from shared.constants import DB_NAME
from shared.memory_types import kind_for_text

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

RETRIEVAL_MODES = ("rrf", "dense_per_kind", "gated", "full")
DEFAULT_MODE = "full"
ENV_RETRIEVAL_MODE = "RETRIEVAL_MODE"

_TOKEN_RE = re.compile(r"[а-яёa-z0-9]+")
# exhaustiveness markers — same semantics as _ENUMERATIVE_RE in dual_route
# (a copy, not an import: dual_route imports this module — a cycle is unacceptable)
_ENUMERATIVE_RE = re.compile(r"(?:\bвсе(?:х|м|е|ё)?\b|\bсписок\b|перечисл\w*|list\s+all|\benumerate\b)", re.IGNORECASE)
_QUESTION_WORDS = ("почему", "как", "зачем", "why", "how")
_SHORT_QUERY_WORDS = 4


def retrieval_mode() -> str:
    """Active arm: env RETRIEVAL_MODE → config retrieval.mode → 'full'.

    An invalid value (env or yaml) degrades to the default 'full' — ablation
    arms never break production.
    """
    env = os.environ.get(ENV_RETRIEVAL_MODE, "").strip().lower()
    if env in RETRIEVAL_MODES:
        return env
    mode = str(config.get("retrieval", "mode", default=DEFAULT_MODE) or DEFAULT_MODE).strip().lower()
    if mode not in RETRIEVAL_MODES:
        logger.warning("Unknown retrieval.mode %r — falling back to %r", mode, DEFAULT_MODE)
        return DEFAULT_MODE
    return mode


def query_features(query: str, synonyms: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Full query-feature vector (S17 AdaptiveRAG pre-gate): 27 features, 7 groups, LLM-free.

    Groups (checked against v28 #5 — length/type/dates/question form):
      1. shape      (4): length, chars, is_short, is_long
      2. question   (4): is_question, has_question_mark, is_imperative, is_negated
      3. entity     (4): has_entity, n_capitalized, has_numbers, has_code
      4. temporal   (4): has_date, has_year, has_relative_time, has_deadline
      5. lexical    (3): unique_ratio, stopword_ratio, avg_word_len
      6. intent     (4): is_enumerative, is_comparison, is_followup, is_greeting
      7. hint       (4): has_url, has_quote, wants_wiki, wants_episodic

    The first 4 keys (length/is_question/has_entity/is_enumerative) are the historical
    contract of the gated arm; the semantics never changed. Feeds gate_sources; in the
    full arm the pool after pre-gating is smaller → CAMA N_eff/abstention reflect the trimmed fan-out.
    """
    q = (query or "").strip()
    tl = q.lower()
    raw_toks = _TOKEN_RE.findall(tl)
    toks = {t for t in raw_toks if len(t) >= 3}
    syn = synonyms if synonyms is not None else load_synonyms()
    vocab = set(syn) | {v for vs in syn.values() for v in vs}
    # 1. shape
    length = len(q.split())
    # 2. question-form
    is_question = tl.endswith("?") or any(t in _QUESTION_WORDS for t in toks)
    first = raw_toks[0] if raw_toks else ""
    is_imperative = first in _IMPERATIVE_WORDS
    is_negated = bool(toks & _NEGATION_WORDS)
    # 3. entity
    n_capitalized = sum(1 for t in re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", q)[1:] if t[:1].isupper())
    has_numbers = any(ch.isdigit() for ch in q)
    has_code = any(m in q for m in _CODE_MARKERS)
    # 4. temporal
    has_date = bool(_DATE_RE.search(q))
    has_year = bool(re.search(r"\b(19|20)\d{2}\b", q))
    has_relative_time = any(m in tl for m in _RELATIVE_TIME_MARKERS)
    has_deadline = any(m in tl for m in _DEADLINE_MARKERS)
    # 5. lexical
    unique_ratio = (len(toks) / len(raw_toks)) if raw_toks else 0.0
    stopword_ratio = (sum(1 for t in raw_toks if t in _STOPWORDS) / len(raw_toks)) if raw_toks else 0.0
    avg_word_len = (sum(len(t) for t in raw_toks) / len(raw_toks)) if raw_toks else 0.0
    # 6. intent
    is_comparison = any(m in tl for m in (" vs ", " или ", " сравн", "compare", "versus"))
    is_followup = tl.startswith(("а ", "и ", "но ")) or bool(toks & {"тоже", "also"}) or "what about" in tl
    is_greeting = any(tl.startswith(g) for g in ("привет", "здравствуй", "доброе утро", "hi", "hello", "hey"))
    # 7. source-hints
    has_url = "http" in tl or "www." in tl
    has_quote = '"' in q or "«" in q
    wants_wiki = any(m in tl for m in ("документаци", "справк", "официальн", "что такое", "как работает", "docs", "documentation"))
    wants_episodic = any(m in tl for m in ("помнишь", "напомни", "в прошлый раз", "мы уже", "где мы", "did we", "remember when", "перестал работать"))
    return {
        # 1. shape
        "length": length,
        "chars": len(q),
        "is_short": length <= _SHORT_QUERY_WORDS,
        "is_long": length >= 12,
        # 2. question
        "is_question": is_question,
        "has_question_mark": "?" in q,
        "is_imperative": is_imperative,
        "is_negated": is_negated,
        # 3. entity
        "has_entity": bool(toks & vocab),
        "n_capitalized": n_capitalized,
        "has_numbers": has_numbers,
        "has_code": has_code,
        # 4. temporal
        "has_date": has_date,
        "has_year": has_year,
        "has_relative_time": has_relative_time,
        "has_deadline": has_deadline,
        # 5. lexical
        "unique_ratio": round(unique_ratio, 3),
        "stopword_ratio": round(stopword_ratio, 3),
        "avg_word_len": round(avg_word_len, 2),
        # 6. intent
        "is_enumerative": bool(_ENUMERATIVE_RE.search(tl)),
        "is_comparison": is_comparison,
        "is_followup": is_followup,
        "is_greeting": is_greeting,
        # 7. hints
        "has_url": has_url,
        "has_quote": has_quote,
        "wants_wiki": wants_wiki,
        "wants_episodic": wants_episodic,
    }


# S17 pre-gate lexicons (LLM-free, token/substring level)
_IMPERATIVE_WORDS = frozenset(
    {
        "сделай",
        "найди",
        "покажи",
        "расскажи",
        "напиши",
        "исправь",
        "добавь",
        "удали",
        "проверь",
        "show",
        "find",
        "list",
        "fix",
        "add",
        "remove",
        "write",
        "check",
        "run",
    }
)
_NEGATION_WORDS = frozenset({"не", "нет", "no", "not", "never"})
_CODE_MARKERS = ("```", "def ", "import ", "Traceback", "npm ", "git ", "SELECT ", ".py", ".json", ".yaml", ".ts", "()", "[]")
_DATE_RE = re.compile(r"\d{1,2}[./-]\d{1,2}")
_RELATIVE_TIME_MARKERS = (
    "сегодня",
    "вчера",
    "завтра",
    "неделю назад",
    "месяц назад",
    "прошлый",
    "последний",
    "today",
    "yesterday",
    "recently",
    "last week",
)
_DEADLINE_MARKERS = ("дедлайн", "deadline", "к концу", "к пятнице", "к понедельнику", "до конца", "by friday", "by monday", "eod")
_STOPWORDS = frozenset(
    {"и", "в", "не", "на", "что", "с", "а", "по", "как", "это", "для", "из", "у", "же", "the", "a", "an", "of", "to", "in", "is", "and", "or", "it"}
)


def pre_gate_flags(query: str) -> dict[str, bool]:
    """S17 pre-gate for the full arm: {} when disabled, otherwise the include-flags for gate_sources.

    A cheap skip tier on top of RRF (cost down + noise down): the gate decides
    by features which sources to fire, BEFORE the fan-out. Default off
    (config retrieval.pregate) — enabled in production after the №11 ablation;
    the trimmed pool itself feeds N_eff/abstention.
    """
    from config import config

    if not bool(config.get("retrieval", "pregate", default=False)):
        return {}
    flags = gate_sources(query_features(query))
    return {
        "include_rag": flags["rag"],
        "include_wiki": flags["wiki"],
        "include_episodic": flags["episodic"],
        "include_core": flags["core"],
    }


def gate_sources(feat: dict[str, Any]) -> dict[str, bool]:
    """Matrix «query features → which sources to fire» (simplified Adaptive RAG).

    enumerative («list all») → wiki catalog + typed stores, dense-rag off;
    a short non-question query → fast path rag+core;
    an entity name in the query → +graph (co_mentions/entity canon);
    long/question → full fan-out.
    """
    if feat["is_enumerative"]:
        return {"rag": False, "wiki": True, "episodic": False, "core": True, "graph": True}
    if feat["length"] <= _SHORT_QUERY_WORDS and not feat["is_question"]:
        return {"rag": True, "wiki": False, "episodic": False, "core": True, "graph": False}
    if feat["has_entity"]:
        return {"rag": True, "wiki": True, "episodic": False, "core": True, "graph": True}
    # S17 additions (after the historical rules — the matrix contract is stable):
    if feat.get("has_code") or feat.get("has_url"):
        # code/URL query — FTS corpus and documentation, episodic/graph not relevant
        return {"rag": True, "wiki": True, "episodic": False, "core": True, "graph": False}
    if feat.get("wants_episodic") or (feat.get("has_relative_time") and feat["is_question"]):
        # "what were we doing / when did it stop working" — a biographical query
        return {"rag": True, "wiki": False, "episodic": True, "core": True, "graph": False}
    return {"rag": True, "wiki": True, "episodic": True, "core": True, "graph": True}


async def gated_search(rag: Any, query: str, *, user_id: str = "default", limit: int = 10) -> list[dict[str, Any]]:
    """Gated arm: query features → enabling/disabling sources of the 5-source RAG.

    The include_* flags of MultiSourceRAG.search are used as-is (not modified);
    fusion is plain RRF over the fired sources; EDM/ITS is not applied
    (arm 'full' isolates its contribution).
    """
    flags = gate_sources(query_features(query))
    hits = await rag.search(
        query,
        user_id=user_id,
        limit=limit,
        include_rag=flags["rag"],
        include_wiki=flags["wiki"],
        include_episodic=flags["episodic"],
        include_core=flags["core"],
        include_graph=flags["graph"],
    )
    return [{**h, "kind": str(h.get("source") or "relevant")} for h in hits]


async def dense_per_kind_search(
    cm: Any,
    query: str,
    *,
    user_id: str = "default",
    layer: str = "user",
    kinds: Sequence[str] | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Dense-per-kind arm (ENGRAM simplification): one search per memory-kind, set-merge.

    By default the kind is single — kind_for_text(query) (query routing → memory type);
    kinds=[...] lists several. Each kind search: L4 core
    (core_memory.memory_kind, token-LIKE) + the rag corpus (FTS5 + Hamming over
    bin_embedding, kind via rag_chunks.memory_kind, NULL → 'fact'). Results are
    merged with a set-merge (dedup by (title, content-prefix)) WITHOUT RRF fusion.
    """
    if kinds is None:
        kinds = [kind_for_text(query).value]
    seen: set[tuple[str, str]] = set()
    merged: list[dict[str, Any]] = []
    for kind in kinds:
        for hit in await _kind_hits(cm, kind, query, user_id=user_id, layer=layer, limit=limit):
            key = (str(hit.get("title") or ""), str(hit.get("content") or "")[:200])
            if key in seen:
                continue
            seen.add(key)
            merged.append({**hit, "kind": "relevant"})
    merged.sort(key=lambda h: -(h.get("score") or 0.0))
    return merged[:limit]


async def _kind_hits(cm: Any, kind: str, query: str, *, user_id: str, layer: str, limit: int) -> list[dict[str, Any]]:
    """Per-kind search: L4 core (memory_kind) + rag corpus (FTS5/Hamming, kind-scoped)."""
    toks = [t for t in _TOKEN_RE.findall((query or "").lower()) if len(t) >= 3]
    if not toks:
        return []
    hits: list[dict[str, Any]] = []

    # 1) L4 core — typed fact store (memory_kind is always populated)
    try:
        conn = await cm.get(DB_NAME)
        like = " OR ".join(["(key LIKE ? OR value LIKE ?)"] * len(toks))
        params: list[Any] = [layer, user_id, kind]
        for t in toks:
            params.extend([f"%{t}%", f"%{t}%"])
        cur = await conn.execute(
            "SELECT entry_id, key, value, importance FROM core_memory"
            f" WHERE layer=? AND user_id=? AND memory_kind=? AND ({like})"
            " ORDER BY importance DESC LIMIT ?",
            (*params, limit),
        )
        hits = [
            {
                "id": int(r["entry_id"]),
                "title": str(r["key"]),
                "content": str(r["value"]),
                "score": float(r["importance"]),
                "source": "core_kind",
                "memory_kind": kind,
            }
            for r in await cur.fetchall()
        ]
    except Exception:
        logger.debug("dense_per_kind: core branch skipped", exc_info=True)

    # 2) rag corpus: page kind via rag_chunks.memory_kind (NULL → 'fact')
    try:
        conn = await cm.get(DB_NAME)
        cur = await conn.execute("SELECT DISTINCT page_id FROM rag_chunks WHERE COALESCE(memory_kind, 'fact') = ?", (kind,))
        kind_pages = {int(r["page_id"]) for r in await cur.fetchall()}
    except Exception:
        return hits  # no rag tables (init_rag_db never called) → core branch only
    if not kind_pages:
        return hits

    from rag.search import search_binary, search_fts5

    # FTS5 (lexical-dense): search_fts5 itself degrades to LIKE when FTS5 is unavailable
    try:
        fts_hits = await search_fts5(cm, query, user_id, limit * 3, True, layer=layer)
        hits.extend({**h, "memory_kind": kind} for h in fts_hits if h.get("id") in kind_pages)
    except Exception:
        logger.debug("dense_per_kind: fts branch skipped", exc_info=True)

    # Hamming (dense over bin_embedding) — exhaustive scan, filtered by kind pages
    try:

        def _bin_for(emb: list[float]) -> bytes:
            from rag.quantize import embed_to_binary

            return embed_to_binary(emb, threshold=0.0, dim=len(emb))

        binary_dim = int(config.get("binary", "dim", default=384))
        bin_hits = await search_binary(cm, query, user_id, 100_000, _bin_for, binary_dim, layer=layer)
        hits.extend({**h, "memory_kind": kind} for h in bin_hits if h.get("page_id") in kind_pages)
    except Exception:
        logger.debug("dense_per_kind: hamming branch skipped", exc_info=True)

    return hits
