"""Graph miners (Phase G): deterministic edge factories over existing data.

Miners read existing data (epi_tags, node contents, L0 user-message rows) and
write edges into epi_edges tagged `heuristic:<name>` (rollback: DELETE WHERE
tags LIKE '%heuristic:%'). All inserts are INSERT OR IGNORE against the
epi_edges PK — re-runs are no-ops. No LLM calls anywhere.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from shared.connection import AsyncConnectionManager
from shared.constants import DB_NAME

Miner = Callable[[AsyncConnectionManager, str], Awaitable[dict[str, int]]]

_TOKEN_RE = re.compile(r"[а-яёa-z0-9]+")
# Служебные слова без топик-сигнала (RU+EN); len>=4 дополнительно отсекает мусор.
_STOP_TOKENS = {"и", "но", "в", "на", "с", "для", "это", "что", "the", "a", "an", "is", "are", "of", "to"}

_SESSION_GAP = 1800.0  # L0-строки ближе 30 мин — одна сессия
_NODE_WINDOW = 300.0  # узел в сессии, если created_at в ±5 мин от строки L0
_BIND_SHARED = 2  # или ≥2 общих канон-токенов с текстами сессии


async def _insert_edge(conn: Any, a: int, b: int, relation: str, weight: float, heuristic: str) -> int:
    """INSERT OR IGNORE into epi_edges; returns rows actually written (re-run → 0)."""
    cur = await conn.execute(
        "INSERT OR IGNORE INTO epi_edges (source_id, target_id, relation, weight, created_at, tags)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (a, b, relation, weight, time.time(), json.dumps([f"heuristic:{heuristic}"])),
    )
    return int(cur.rowcount or 0)


def _canon(w: str, syn: dict[str, list[str]]) -> str:
    """Каноническая форма токена: класс синонимов в обе стороны (postgres/postgresql/psql → postgres)."""
    return min({w, *syn.get(w, []), *(k for k, vs in syn.items() if w in vs)})


def _canon_tokens(text: str, syn: dict[str, list[str]] | None = None) -> set[str]:
    """Редкие токены текста: [а-яёa-z0-9]+ lowercase, len>=4, не стоп-слова, канонизированные."""
    if syn is None:
        from rag.synonyms import load_synonyms

        syn = load_synonyms()
    return {_canon(w, syn) for w in _TOKEN_RE.findall(text.lower()) if len(w) >= 4 and w not in _STOP_TOKENS}


async def miner_tags(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """#1: общие epi_tags → `tagged`, weight = min(0.3 + 0.1*shared, 0.6)."""
    conn = await cm.get(DB_NAME)
    rows = await (
        await conn.execute(
            """
            SELECT t1.node_id AS a, t2.node_id AS b, COUNT(DISTINCT t1.tag) AS shared
            FROM epi_tags t1
            JOIN epi_tags t2 ON t1.tag = t2.tag AND t1.node_id < t2.node_id
            JOIN epi_nodes n1 ON n1.node_id = t1.node_id AND n1.layer = ?
            JOIN epi_nodes n2 ON n2.node_id = t2.node_id AND n2.layer = ?
            GROUP BY t1.node_id, t2.node_id HAVING shared > 0
            """,
            (layer, layer),
        )
    ).fetchall()
    edges = 0
    for a, b, shared in rows:
        edges += await _insert_edge(conn, int(a), int(b), "tagged", min(0.3 + 0.1 * int(shared), 0.6), "tags")
    await conn.commit()
    return {"edges": edges}


async def miner_tokens(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """#2: ≥2 общих редких токена и Jaccard ≥0.3 → `topic_overlap`, weight = Jaccard."""
    conn = await cm.get(DB_NAME)
    nodes = await (await conn.execute("SELECT node_id, content FROM epi_nodes WHERE layer=?", (layer,))).fetchall()
    syn: dict[str, list[str]] | None = None
    toks: dict[int, set[str]] = {}
    for r in nodes:
        if syn is None:
            from rag.synonyms import load_synonyms

            syn = load_synonyms()
        toks[int(r["node_id"])] = _canon_tokens(str(r["content"]), syn)
    ids = sorted(toks)
    edges = 0
    for i, a in enumerate(ids):
        ta = toks[a]
        if not ta:
            continue
        for b in ids[i + 1 :]:
            shared = ta & toks[b]
            jaccard = len(shared) / len(ta | toks[b])
            if len(shared) >= 2 and jaccard >= 0.3:
                edges += await _insert_edge(conn, a, b, "topic_overlap", jaccard, "tokens")
    await conn.commit()
    return {"edges": edges}


async def miner_sessions(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """#4: факты одной сессии → `same_session`, weight = 0.3.

    Кластеризация L0 user-message по близкому ts (или общему source_msg_id);
    узел привязан к кластеру по ts-окну от строк L0 либо по ≥2 общим
    канон-токенам с текстами кластера (синоним-канонизация).
    """
    conn = await cm.get(DB_NAME)
    l0 = await (
        await conn.execute(
            "SELECT ts, source_msg_id, text FROM l0_journal WHERE layer=? AND raw_type='user-message' ORDER BY ts",
            (layer,),
        )
    ).fetchall()
    clusters: list[dict[str, Any]] = []
    for r in l0:
        ts, smid = float(r["ts"]), r["source_msg_id"]
        if clusters and ts - clusters[-1]["max_ts"] <= _SESSION_GAP:
            c = clusters[-1]
        else:
            c = {"max_ts": ts, "rows": [], "smids": set(), "toks": set()}
            clusters.append(c)
        c["max_ts"] = max(c["max_ts"], ts)
        c["rows"].append(ts)
        if smid is not None:
            c["smids"].add(int(smid))
        c["toks"] |= _canon_tokens(str(r["text"]))
    merged: list[dict[str, Any]] = []
    for c in clusters:
        hit = next((m for m in merged if m["smids"] & c["smids"]), None)
        if hit is not None:
            hit["rows"] += c["rows"]
            hit["smids"] |= c["smids"]
            hit["toks"] |= c["toks"]
        else:
            merged.append(c)

    nodes = await (
        await conn.execute("SELECT node_id, content, created_at FROM epi_nodes WHERE layer=?", (layer,))
    ).fetchall()
    assigned: dict[int, set[int]] = {}  # node_id → индексы кластеров
    for idx, c in enumerate(merged):
        for r in nodes:
            nid, ts = int(r["node_id"]), float(r["created_at"])
            near = any(abs(ts - t) <= _NODE_WINDOW for t in c["rows"])
            if near or len(_canon_tokens(str(r["content"])) & c["toks"]) >= _BIND_SHARED:
                assigned.setdefault(nid, set()).add(idx)

    edges = 0
    for idx in range(len(merged)):
        members = sorted(nid for nid, cs in assigned.items() if idx in cs)
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                edges += await _insert_edge(conn, a, b, "same_session", 0.3, "sessions")
    await conn.commit()
    return {"edges": edges}


async def miner_entities(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """#3: NER co-mentions → `co_mentions` edges."""
    return {"edges": 0}


async def miner_provenance(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """#5: core fact → episode → wiki metadata.parents → `sourced_from` edges."""
    return {"edges": 0}


async def miner_co_retrieval(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """#7: co-retrieval journal, count>=2 → `co_recalled` edges."""
    return {"edges": 0}


async def miner_embedding(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """#9: embedding similarity >= 0.7 → `semantic_overlap` edges."""
    return {"edges": 0}


MINERS: dict[str, Miner] = {
    "tags": miner_tags,
    "tokens": miner_tokens,
    "entities": miner_entities,
    "sessions": miner_sessions,
    "provenance": miner_provenance,
    "co_retrieval": miner_co_retrieval,
    "embedding": miner_embedding,
}
