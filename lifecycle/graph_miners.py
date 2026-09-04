"""Graph miners (Phase G): deterministic edge factories over existing data.

Miners read existing data (epi_tags, node contents, L0 user-message rows) and
write edges into epi_edges tagged `heuristic:<name>` (rollback: DELETE WHERE
tags LIKE '%heuristic:%'). All inserts are INSERT OR IGNORE against the
epi_edges PK — re-runs are no-ops. No LLM calls anywhere.
"""

from __future__ import annotations

import contextlib
import hashlib
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


async def _layer_nodes(conn: Any, layer: str) -> list[tuple[int, str]]:
    """Узлы слоя без мусорного JSON/tool_use_id-контента (фильтр как в graph_enrich)."""
    rows = await (
        await conn.execute(
            "SELECT node_id, content FROM epi_nodes WHERE layer=?"
            " AND content NOT LIKE '[{%' AND content NOT LIKE '%tool_use_id%'",
            (layer,),
        )
    ).fetchall()
    return [(int(r["node_id"]), str(r["content"])) for r in rows]


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
    """#3: словарь синонимов (канон-классы, обе стороны) + spaCy NER (латиница ORG/GPE) → `co_mentions` 0.4."""
    conn = await cm.get(DB_NAME)
    nodes = await _layer_nodes(conn, layer)
    if len(nodes) < 2:
        return {"edges": 0}
    from rag.synonyms import load_synonyms

    syn = load_synonyms()
    nlp = _get_ner()
    ents = [_entities(str(c), syn, nlp) for _, c in nodes]
    edges = 0
    for i in range(len(nodes)):
        if not ents[i]:
            continue
        for j in range(i + 1, len(nodes)):
            if ents[i] & ents[j]:
                edges += await _insert_edge(conn, nodes[i][0], nodes[j][0], "co_mentions", 0.4, "entities")
    await conn.commit()
    return {"edges": edges}


def _entities(text: str, syn: dict[str, list[str]], nlp: Any = None) -> set[str]:
    """Сущности текста: канон-классы словаря синонимов + spaCy ORG/GPE (латиница).

    Канонизация через _canon — полный класс в обе стороны: «Лили»/«Lily»/
    «лисёныш» схлопываются в одну сущность.
    """
    vocab = set(syn) | {v for vs in syn.values() for v in vs}
    found = {_canon(w, syn) for w in _TOKEN_RE.findall(text.lower()) if w in vocab}
    if nlp is not None:
        with contextlib.suppress(Exception):
            found |= {ent.text.lower() for ent in nlp(text).ents if ent.label_ in _NER_LABELS}
    return found


_NER_LABELS = {"ORG", "GPE"}
_ner = None


def _get_ner() -> Any:
    """Lazy spaCy NER; None если модель не установлена — словарного слоя достаточно."""
    global _ner
    if _ner is None:
        try:
            from mcp_server.utils.privacy import _get_nlp

            _ner = _get_nlp()
        except Exception:
            _ner = False
    return _ner or None


# Задача G3: журнал co-retrieval. hits из FTS5 — это rag_pages.id, из графа —
# epi_nodes.node_id: разные пространства. Компромисс — пишем пары ЛЮБЫХ hit-id
# с префиксом типа ('f:5', 'g:12'); минер #7 отбирает только g:-пары.
_G_PREFIX = "g:"
_F_PREFIX = "f:"


async def ensure_co_pairs(cm: AsyncConnectionManager) -> None:
    """Idempotent schema for the co-retrieval journal (как ConflictResolver.ensure)."""
    await cm.execute_script(
        DB_NAME,
        """
        CREATE TABLE IF NOT EXISTS recall_co_pairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            node_a TEXT NOT NULL,
            node_b TEXT NOT NULL,
            query_hash TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_co_pairs_ab ON recall_co_pairs(node_a, node_b);
        """,
    )


def _hit_ref(hit: dict[str, Any]) -> str | None:
    """hit-id → типизированная ссылка ('g:<node_id>' / 'f:<page_id>'); None → не журналируется."""
    hid = hit.get("id")
    if not isinstance(hid, int) or hid == 0:
        return None
    if hid < -3_000_000:  # rag.multi_source._ID_OFFSET_GRAPH: графовое пространство (отрицательные)
        return f"{_G_PREFIX}{-hid - 3_000_000}"
    return f"{_F_PREFIX}{hid}"


async def log_co_pairs(cm: AsyncConnectionManager, query: str, hits: list[dict[str, Any]]) -> int:
    """Записать пары (node_a, node_b) всех хитов успешного recall. Возвращает число пар."""
    refs = [r for r in (_hit_ref(h) for h in hits) if r]
    await ensure_co_pairs(cm)  # даже при <2 ref: recall_events-стиль ensure всегда
    if len(refs) < 2:
        return 0
    conn = await cm.get(DB_NAME)
    qhash = hashlib.sha1(query.encode("utf-8", "ignore")).hexdigest()[:16]
    ts = time.time()
    written = 0
    for i, a in enumerate(refs):
        for b in refs[i + 1 :]:
            lo, hi = sorted((a, b))
            cur = await conn.execute(
                "INSERT INTO recall_co_pairs (ts, node_a, node_b, query_hash) VALUES (?, ?, ?, ?)",
                (ts, lo, hi, qhash),
            )
            written += int(cur.rowcount or 0)
    await conn.commit()
    return written


async def miner_provenance(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """#5: metadata.parents 'episode:N' → узел эпизода → `sourced_from` ребро на факт-узел.

    Факт-узел ищется по точному content == core_memory.value (создаёт его
    mcp fact-add); узел эпизода find_or_add по content 'episode:N'.
    Wiki [[fact:]]-связей пока нет — только прямые parents.
    """
    conn = await cm.get(DB_NAME)
    rows = await (await conn.execute("SELECT user_id, value, metadata FROM core_memory WHERE layer=?", (layer,))).fetchall()
    edges = 0
    for r in rows:
        try:
            meta = json.loads(r["metadata"]) if r["metadata"] else {}
        except (TypeError, ValueError):
            continue
        parents = meta.get("parents", []) if isinstance(meta, dict) else []
        ep_refs = [str(p) for p in parents if str(p).startswith("episode:")]
        if not ep_refs:
            continue
        fact = await (
            await conn.execute(
                "SELECT node_id FROM epi_nodes WHERE layer=? AND user_id=? AND node_type='fact' AND content=? LIMIT 1",
                (layer, r["user_id"], r["value"]),
            )
        ).fetchone()
        if fact is None:
            continue
        fact_id = int(fact["node_id"])
        for ref in ep_refs:
            ep = await (
                await conn.execute(
                    "SELECT node_id FROM epi_nodes WHERE layer=? AND user_id=? AND node_type='episode' AND content=? LIMIT 1",
                    (layer, r["user_id"], ref),
                )
            ).fetchone()
            if ep is None:
                cur = await conn.execute(
                    "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at)"
                    " VALUES (?, ?, ?, 'episode', '[]', 0.5, ?)",
                    (layer, r["user_id"], ref, time.time()),
                )
                ep_id = int(cur.lastrowid or 0)
            else:
                ep_id = int(ep["node_id"])
            edges += await _insert_edge(conn, ep_id, fact_id, "sourced_from", 0.5, "provenance")
    await conn.commit()
    return {"edges": edges}


async def miner_co_retrieval(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """#7: co-retrieval journal, count>=2 → `co_recalled` edges (только g:-пары)."""
    await ensure_co_pairs(cm)
    conn = await cm.get(DB_NAME)
    rows = await (
        await conn.execute(
            "SELECT node_a, node_b, COUNT(*) AS c FROM recall_co_pairs"
            " WHERE node_a LIKE ? AND node_b LIKE ?"
            " GROUP BY node_a, node_b HAVING c >= 2",
            (f"{_G_PREFIX}%", f"{_G_PREFIX}%"),
        )
    ).fetchall()
    edges = 0
    for a, b, c in rows:
        na, nb = int(str(a)[2:]), int(str(b)[2:])
        existing = await (
            await conn.execute(
                "SELECT 1 FROM epi_nodes WHERE node_id IN (?, ?) AND layer=?", (na, nb, layer)
            )
        ).fetchall()
        if len(existing) < 2:  # узлы не из этого слоя/удалены — ребро не строим
            continue
        edges += await _insert_edge(conn, min(na, nb), max(na, nb), "co_recalled", min(0.3 + 0.1 * int(c), 0.6), "co_retrieval")
    await conn.commit()
    return {"edges": edges}


_EMBED_JACCARD = 0.7
_EMBED_TOPK = 15  # не более 15 рёбер semantic_overlap на узел от этого минера
_SEMANTIC_WEIGHT = 0.5


def _bits_int(b: bytes) -> int:
    return int.from_bytes(b, "big")


def _bit_jaccard(a: int, b: int) -> float:
    inter = (a & b).bit_count()
    if inter == 0:
        return 0.0
    return inter / (a | b).bit_count()


async def miner_embedding(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """#9: rich embedding (content+tags) → MIB-биты → попарный Jaccard ≥0.7 → `semantic_overlap`.

    A-MEM rich embedding: кодируется «content + теги из epi_tags» с
    синоним-канонизацией токенов (_canon из T2). Мусорный фильтр — как в
    graph_enrich ([{…-JSON / tool_use_id). O(n²) на текущих масштабах ок
    (~200 узлов = 20k пар); top-k=15 на узел.
    """
    conn = await cm.get(DB_NAME)
    nodes = await _layer_nodes(conn, layer)
    if len(nodes) < 2:
        return {"edges": 0}
    from rag.quantize import embed_to_binary
    from rag.synonyms import load_synonyms
    from shared.embeddings import embed_texts

    syn = load_synonyms()
    tag_rows = await (
        await conn.execute(
            f"SELECT node_id, tag FROM epi_tags WHERE node_id IN ({','.join('?' * len(nodes))})",
            tuple(nid for nid, _ in nodes),
        )
    ).fetchall()
    tags: dict[int, list[str]] = {}
    for r in tag_rows:
        tags.setdefault(int(r["node_id"]), []).append(_canon(str(r["tag"]), syn))

    try:
        # A-MEM rich embedding: f"{content} {tags}"; канонизация (_canon из T2) —
        # на тегах, чтобы варианты имени/технологии попадали в один кэш-ключ смысла.
        # Ключ кэша = raw content — переиспользует векторы, посеянные ingestor'ом.
        vecs = await embed_texts([f"{c} {' '.join(sorted(tags.get(nid, [])))}" for nid, c in nodes])
        bits = [_bits_int(embed_to_binary(v, dim=len(v))) for v in vecs]
    except Exception:
        return {"edges": 0}  # эмбеддинг-бэкенд недоступен (нет numpy/модели) — минер пропускается

    cands: list[tuple[float, int, int]] = []
    for i in range(len(nodes)):
        if not bits[i]:
            continue
        for j in range(i + 1, len(nodes)):
            jacc = _bit_jaccard(bits[i], bits[j])
            if jacc >= _EMBED_JACCARD:
                cands.append((jacc, i, j))
    edges = 0
    degree: dict[int, int] = {}
    for _, i, j in sorted(cands, reverse=True):
        a, b = nodes[i][0], nodes[j][0]
        if degree.get(a, 0) >= _EMBED_TOPK or degree.get(b, 0) >= _EMBED_TOPK:
            continue  # top-k=15 на узел
        edges += await _insert_edge(conn, a, b, "semantic_overlap", _SEMANTIC_WEIGHT, "embedding")
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1
    await conn.commit()
    return {"edges": edges}


async def wire_new_node(
    cm: AsyncConnectionManager, layer: str, node_id: int, content: str, tags: list[str] | None = None
) -> int:
    """Инкрементальный режим (G4): рёбра НОВОГО узла vs существующие — сразу при записи.

    Лёгкие сигналы: общие теги (tagged), ≥2 общих канон-токенов + Jaccard ≥0.3
    (topic_overlap), общая сущность словаря/NER (co_mentions). Тяжёлое
    (embedding/sessions) остаётся ночному graph_enrich. Возвращает число рёбер.
    """
    conn = await cm.get(DB_NAME)
    from rag.synonyms import load_synonyms

    syn = load_synonyms()
    my_toks = _canon_tokens(content, syn)
    my_ents = _entities(content, syn)
    my_tags = set(tags or [])
    if not my_toks and not my_ents and not my_tags:
        return 0
    others = await _layer_nodes(conn, layer)
    other_tags: dict[int, set[str]] = {}
    for r in await (await conn.execute("SELECT node_id, tag FROM epi_tags WHERE node_id != ?", (node_id,))).fetchall():
        other_tags.setdefault(int(r["node_id"]), set()).add(str(r["tag"]))
    nlp = _get_ner()
    edges = 0
    for oid, ocontent in others:
        if oid == node_id:
            continue
        otoks = _canon_tokens(ocontent, syn)
        union = my_toks | otoks
        if len(my_toks & otoks) >= 2 and union and len(my_toks & otoks) / len(union) >= 0.3:
            edges += await _insert_edge(conn, min(node_id, oid), max(node_id, oid), "topic_overlap", len(my_toks & otoks) / len(union), "tokens")
        if my_ents and my_ents & _entities(ocontent, syn, nlp):
            edges += await _insert_edge(conn, min(node_id, oid), max(node_id, oid), "co_mentions", 0.4, "entities")
        shared = my_tags & other_tags.get(oid, set())
        if shared:
            edges += await _insert_edge(conn, min(node_id, oid), max(node_id, oid), "tagged", min(0.3 + 0.1 * len(shared), 0.6), "tags")
    await conn.commit()
    return edges


MINERS: dict[str, Miner] = {
    "tags": miner_tags,
    "tokens": miner_tokens,
    "entities": miner_entities,
    "sessions": miner_sessions,
    "provenance": miner_provenance,
    "co_retrieval": miner_co_retrieval,
    "embedding": miner_embedding,
}
