"""Retrieval ablation arms (Phase G Task 7) — для №11-eval сравнения retrieval-стратегий.

RETRIEVAL_MODE (env) / retrieval.mode (config.yaml), дефолт 'full':
- 'rrf'            — статус-кво: один 5-source RRF-поиск без EDM/ITS и роутинга;
- 'dense_per_kind' — ENGRAM-упрощение: один поиск per memory-kind (kind_for_text
                     запроса или все kinds) через FTS5+Hamming (+ kind-скоуп
                     core_memory), set-merge без RRF-фьюжена;
- 'gated'          — Adaptive RAG (упрощённо): query-features (длина запроса,
                     вопросительное слово, маркеры «list all», entity-имена из
                     словаря синонимов) решают, какие источники фаерить;
- 'full'           — dual-route (classify_query → S2/EDM/ITS/D-Mem) — дефолт,
                     существующее поведение Task 6, прод не меняет.

Это НЕ полноценный ENGRAM (без spaCy/13 типов) — упрощения достаточно для
абляции. rag_chunks.memory_kind: ingestor пока не тегирует kind, NULL → 'fact';
для честной абляции eval-harness может бэкфиллить колонку.
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
# маркеры полноты — та же семантика, что _ENUMERATIVE_RE в dual_route
# (копия, а не импорт: dual_route импортирует этот модуль — цикл недопустим)
_ENUMERATIVE_RE = re.compile(r"(?:\bвсе(?:х|м|е|ё)?\b|\bсписок\b|перечисл\w*|list\s+all|\benumerate\b)", re.IGNORECASE)
_QUESTION_WORDS = ("почему", "как", "зачем", "why", "how")
_SHORT_QUERY_WORDS = 4


def retrieval_mode() -> str:
    """Активный arm: env RETRIEVAL_MODE → config retrieval.mode → 'full'.

    Невалидное значение (env или yaml) деградирует к дефолту 'full' — армы
    абляции никогда не ломают прод.
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
    """Query-features gated-арма (упрощённый Adaptive RAG вместо 27 фич).

    length — слов; is_question — «?»/вопросное слово; has_entity — токен из
    словаря синонимов (канон сущностей); is_enumerative — маркеры «list all».
    """
    q = (query or "").strip()
    tl = q.lower()
    toks = {t for t in _TOKEN_RE.findall(tl) if len(t) >= 3}
    syn = synonyms if synonyms is not None else load_synonyms()
    vocab = set(syn) | {v for vs in syn.values() for v in vs}
    return {
        "length": len(q.split()),
        "is_question": tl.endswith("?") or any(t in _QUESTION_WORDS for t in toks),
        "has_entity": bool(toks & vocab),
        "is_enumerative": bool(_ENUMERATIVE_RE.search(tl)),
    }


def gate_sources(feat: dict[str, Any]) -> dict[str, bool]:
    """Матрица «фичи запроса → какие источники фаерить» (упрощённый Adaptive RAG).

    enumerative («list all») → каталог wiki + typed-хранилища, dense-rag выключен;
    короткий невопросный запрос → быстрый путь rag+core;
    entity-имя в запросе → +граф (co_mentions/канон сущностей);
    длинный/вопросный → полный fan-out.
    """
    if feat["is_enumerative"]:
        return {"rag": False, "wiki": True, "episodic": False, "core": True, "graph": True}
    if feat["length"] <= _SHORT_QUERY_WORDS and not feat["is_question"]:
        return {"rag": True, "wiki": False, "episodic": False, "core": True, "graph": False}
    if feat["has_entity"]:
        return {"rag": True, "wiki": True, "episodic": False, "core": True, "graph": True}
    return {"rag": True, "wiki": True, "episodic": True, "core": True, "graph": True}


async def gated_search(rag: Any, query: str, *, user_id: str = "default", limit: int = 10) -> list[dict[str, Any]]:
    """Gated arm: query-features → включение/выключение источников 5-source RAG.

    include_*-флаги MultiSourceRAG.search используются как есть (не модифицируются);
    фьюжен — обычный RRF по фаернутым источникам; EDM/ITS не применяется
    (его вклад изолирует arm 'full').
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
    """Dense-per-kind arm (ENGRAM-упрощение): один поиск per memory-kind, set-merge.

    По умолчанию kind один — kind_for_text(query) (роутинг запроса → тип памяти);
    kinds=[...] перечисляет несколько. Каждый kind-поиск: L4 core
    (core_memory.memory_kind, токен-LIKE) + rag-корпус (FTS5 + Hamming по
    bin_embedding, kind через rag_chunks.memory_kind, NULL → 'fact'). Результаты
    объединяются set-merge'ом (дедуп по (title, content-prefix)) БЕЗ RRF-фьюжена.
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
    """Per-kind поиск: L4 core (memory_kind) + rag-корпус (FTS5/Hamming, kind-скоуп)."""
    toks = [t for t in _TOKEN_RE.findall((query or "").lower()) if len(t) >= 3]
    if not toks:
        return []
    hits: list[dict[str, Any]] = []

    # 1) L4 core — типизированное хранилище фактов (memory_kind заполнен всегда)
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

    # 2) rag-корпус: kind страницы через rag_chunks.memory_kind (NULL → 'fact')
    try:
        conn = await cm.get(DB_NAME)
        cur = await conn.execute("SELECT DISTINCT page_id FROM rag_chunks WHERE COALESCE(memory_kind, 'fact') = ?", (kind,))
        kind_pages = {int(r["page_id"]) for r in await cur.fetchall()}
    except Exception:
        return hits  # rag-таблиц нет (init_rag_db не вызывался) → только core-ветка
    if not kind_pages:
        return hits

    from rag.search import search_binary, search_fts5

    # FTS5 (lexical-dense): search_fts5 сам деградирует в LIKE при отсутствии FTS5
    try:
        fts_hits = await search_fts5(cm, query, user_id, limit * 3, True, layer=layer)
        hits.extend({**h, "memory_kind": kind} for h in fts_hits if h.get("id") in kind_pages)
    except Exception:
        logger.debug("dense_per_kind: fts branch skipped", exc_info=True)

    # Hamming (dense по bin_embedding) — исчерпывающий скан, фильтр по kind-страницам
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
