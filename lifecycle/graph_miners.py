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
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from shared.connection import AsyncConnectionManager
from shared.constants import DB_NAME

logger = logging.getLogger(__name__)

Miner = Callable[[AsyncConnectionManager, str], Awaitable[dict[str, int]]]

_TOKEN_RE = re.compile(r"[а-яёa-z0-9]+")
# Служебные слова без топик-сигнала (RU+EN); len>=4 дополнительно отсекает мусор.
_STOP_TOKENS = {"и", "но", "в", "на", "с", "для", "это", "что", "the", "a", "an", "is", "are", "of", "to"}

_SESSION_GAP = 1800.0  # L0-строки ближе 30 мин — одна сессия
_NODE_WINDOW = 300.0  # узел в сессии, если created_at в ±5 мин от строки L0
_BIND_SHARED = 2  # или ≥2 общих канон-токенов с текстами сессии


async def _insert_edge(conn: Any, a: int, b: int, relation: str, weight: float, heuristic: str) -> int:
    """UPSERT into epi_edges; returns rows actually written (re-run → 0).

    Аудит 05.09 (P1): INSERT OR IGNORE замораживал вес — повторный прогон
    минера не усиливал связь. Теперь upsert берёт max(weight) (сильнейшее
    свидетельство живёт), created_at обновляется. После вставки heuristic-ребра
    применяется lateral inhibition (G5, SYNAPSE): слабое ребро гасится
    кластером более сильных соседей узла.
    """
    cur = await conn.execute(
        """INSERT INTO epi_edges (source_id, target_id, relation, weight, created_at, tags) VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (source_id, target_id, relation)
           DO UPDATE SET weight = MAX(weight, excluded.weight), created_at = excluded.created_at""",
        (a, b, relation, weight, time.time(), json.dumps([f"heuristic:{heuristic}"])),
    )
    written = int(cur.rowcount or 0)
    if written:
        from lifecycle.graph_sanitation import lateral_inhibition

        with contextlib.suppress(Exception):  # ингибиция не должна ронять минер
            await lateral_inhibition(conn, a)
            await lateral_inhibition(conn, b)
    return written


def _canon(w: str, syn: dict[str, list[str]]) -> str:
    """Каноническая форма токена: делегирует в rag.synonyms.canonical_form (двусторонний разворот класса)."""
    from rag.synonyms import canonical_form

    return canonical_form(w, syn)


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
            "SELECT node_id, content FROM epi_nodes WHERE layer=? AND content NOT LIKE '[{%' AND content NOT LIKE '%tool_use_id%'",
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
    """#2: ≥2 общих редких токена и Jaccard ≥ порога → `topic_overlap`, weight = Jaccard.

    Порог = max(0.3, mad_threshold(jaccards)) — MAD-порог (G2 sanitation) поднимает
    cutoff только когда распределение действительно смещено вверх; floor 0.3
    сохраняет историческое поведение на разреженных слоях.
    """
    from lifecycle.graph_sanitation import mad_threshold

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
    cands: list[tuple[int, int, float]] = []
    for i, a in enumerate(ids):
        ta = toks[a]
        if not ta:
            continue
        for b in ids[i + 1 :]:
            shared = ta & toks[b]
            jaccard = len(shared) / len(ta | toks[b])
            if len(shared) >= 2:
                cands.append((a, b, jaccard))
    tau = max(0.3, mad_threshold([c[2] for c in cands])) if cands else 0.3
    edges = 0
    for a, b, jaccard in cands:
        if jaccard >= tau:
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

    nodes = await (await conn.execute("SELECT node_id, content, created_at FROM epi_nodes WHERE layer=?", (layer,))).fetchall()
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
# с префиксом типа ('f:5', 'g:12'); минер #7 минерит g:-пары напрямую, а f:-пары
# через маппинг rag_pages.path → wiki-узел (node_type='wiki_page',
# lifecycle/wiki_graph_builder.py). Смешанные g/f-пары не минерятся.
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


async def miner_wiki_fact_links(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """Ночные провенанс-мосты wiki↔L4 (S5, аудит 05.09).

    [[fact:key]] в wiki_index.content → ребро между wiki_page-узлом и
    fact-узлом записи.

    fact-узел ищется как в miner_provenance: node_type='fact', content == value.
    Запись без узла или страница без wiki-узла пропускаются.
    """
    conn = await cm.get(DB_NAME)
    import re as _re

    links = await (
        await conn.execute(
            "SELECT w.file_path, w.content FROM wiki_index w WHERE w.layer=? AND w.content LIKE '%[[fact:%'",
            (layer,),
        )
    ).fetchall()
    if not links:
        return {"edges": 0}

    def _keys(text: str) -> list[str]:
        return _re.findall(r"\[\[fact:([^\]]+)\]\]", text)

    edges = 0
    for file_path, content in links:
        keys = _keys(str(content))
        if not keys:
            continue
        page = await (
            await conn.execute(
                "SELECT n.node_id FROM epi_nodes n WHERE n.layer=? AND n.node_type='wiki_page' AND n.content=? LIMIT 1",
                (layer, file_path),
            )
        ).fetchone()
        if page is None:
            continue
        for key in keys:
            # [[fact:backup_enc]] → ключ может быть с kind-префиксом
            # (fact:backup_enc) или без — пробуем оба, как пишут в wiki.
            fact_row = None
            for cand in (key, f"fact:{key}"):
                fact_row = await (
                    await conn.execute(
                        "SELECT value FROM core_memory WHERE layer=? AND key=? LIMIT 1",
                        (layer, cand),
                    )
                ).fetchone()
                if fact_row is not None:
                    break
            if fact_row is None:
                continue
            fact = await (
                await conn.execute(
                    "SELECT node_id FROM epi_nodes WHERE layer=? AND user_id=(SELECT user_id FROM epi_nodes WHERE node_id=?) AND node_type='fact' AND content=? LIMIT 1",
                    (layer, int(page["node_id"]), str(fact_row["value"])),
                )
            ).fetchone()
            if fact is None:
                continue
            edges += await _insert_edge(conn, int(page["node_id"]), int(fact["node_id"]), "wiki_fact_link", 0.5, "provenance")
    await conn.commit()
    return {"edges": edges}


async def miner_co_retrieval(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """#7: co-retrieval journal, count>=2 → `co_recalled` edges (g:-пары + f:-пары).

    g:<node_id> — прямое ребро между узлами слоя. f:<page_id> — wiki/rag-страница:
    маппинг через rag_pages.path → epi_nodes.content (node_type='wiki_page',
    создаёт lifecycle/wiki_graph_builder.py). Страница без wiki-узла пропускается.
    """
    await ensure_co_pairs(cm)
    conn = await cm.get(DB_NAME)
    rows = await (
        await conn.execute(
            "SELECT node_a, node_b, COUNT(*) AS c FROM recall_co_pairs WHERE node_a LIKE ? AND node_b LIKE ? GROUP BY node_a, node_b HAVING c >= 2",
            (f"{_G_PREFIX}%", f"{_G_PREFIX}%"),
        )
    ).fetchall()
    frows = await (
        await conn.execute(
            "SELECT node_a, node_b, COUNT(*) AS c FROM recall_co_pairs WHERE node_a LIKE ? AND node_b LIKE ? GROUP BY node_a, node_b HAVING c >= 2",
            (f"{_F_PREFIX}%", f"{_F_PREFIX}%"),
        )
    ).fetchall()
    edges = 0
    for a, b, c in rows:
        na, nb = int(str(a)[2:]), int(str(b)[2:])
        existing = await (await conn.execute("SELECT 1 FROM epi_nodes WHERE node_id IN (?, ?) AND layer=?", (na, nb, layer))).fetchall()
        if len(existing) < 2:  # узлы не из этого слоя/удалены — ребро не строим
            continue
        edges += await _insert_edge(conn, min(na, nb), max(na, nb), "co_recalled", min(0.3 + 0.1 * int(c), 0.6), "co_retrieval")
    edges += await _f_pair_edges(conn, layer, frows)
    await conn.commit()
    return {"edges": edges}


async def _f_pair_edges(conn: Any, layer: str, rows: list[Any]) -> int:
    """f:-пары → co_recalled рёбра между wiki-узлами (rag_pages.path → epi_nodes).

    Маппинг: rag_pages.path == epi_nodes.content при node_type='wiki_page'
    (инвариант wiki_graph_builder._ensure_node). Пара минерится только когда
    ОБЕ страницы имеют wiki-узел этого слоя — иначе ребро некуда повесить.
    """
    edges = 0
    node_cache: dict[str, int | None] = {}
    for a, b, c in rows:
        ids: list[int] = []
        for page_id in (str(a)[2:], str(b)[2:]):
            if page_id not in node_cache:
                row = await (
                    await conn.execute(
                        "SELECT n.node_id FROM rag_pages p JOIN epi_nodes n"
                        " ON n.content = p.path AND n.layer = ? AND n.node_type = 'wiki_page'"
                        " WHERE p.id = ? LIMIT 1",
                        (layer, int(page_id)),
                    )
                ).fetchone()
                node_cache[page_id] = int(row["node_id"]) if row else None
            node_id = node_cache[page_id]
            if node_id is None:
                ids = []
                break
            ids.append(node_id)
        if len(ids) < 2:
            continue
        edges += await _insert_edge(conn, min(ids), max(ids), "co_recalled", min(0.3 + 0.1 * int(c), 0.6), "co_retrieval")
    return edges


_EMBED_JACCARD = 0.7
_EMBED_TOPK = 15  # не более 15 рёбер semantic_overlap на узел от этого минера
_SEMANTIC_WEIGHT = 0.5

# #6: маркер-словарь причинно-следственного перехода (план G4b, Step 4).
_MARKERS = re.compile(r"починила|исправила|теперь работает|сломалось|переделали|решено|закрыто")
_MARKER_MIN, _MARKER_MAX = 300.0, 30 * 86400.0  # дельта ts в [5 мин, 30 дней]


async def miner_markers(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """#6: пары узлов с общим канон-токеном, ts-дельта в окне, в позднем — маркер → `led_to` 0.3.

    Направление A→B (A раньше, B с маркером «починила/сломалось/…»): ранний узел
    про X, поздний — исход по X. Без общего токена или вне окна ребра нет.
    """
    conn = await cm.get(DB_NAME)
    from rag.synonyms import load_synonyms

    syn = load_synonyms()
    nodes = await (await conn.execute("SELECT node_id, content, created_at FROM epi_nodes WHERE layer=?", (layer,))).fetchall()
    parsed = [(int(r["node_id"]), _canon_tokens(str(r["content"]), syn), _MARKERS.search(str(r["content"])), float(r["created_at"])) for r in nodes]
    edges = 0
    for i, (a, ta, _, ta_ts) in enumerate(parsed):
        if not ta:
            continue
        for b, tb, m, tb_ts in parsed[i + 1 :]:
            if not m or not ta & tb:
                continue
            lo, hi = (a, b) if ta_ts <= tb_ts else (b, a)  # ребро из раннего в поздний (маркерный)
            delta = abs(tb_ts - ta_ts)
            if _MARKER_MIN <= delta <= _MARKER_MAX:
                edges += await _insert_edge(conn, lo, hi, "led_to", 0.3, "marker")
    await conn.commit()
    return {"edges": edges}


async def miner_structural(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """#8: структурные инварианты — co-citation, belief propagation, louvain-мосты.

    - co-citation: два узла слоя цитируются третьим (не-эвристические рёбра) →
      `co_cited` 0.3 (эвристические рёбра-цитаты исключены: график не замыкается сам на себя).
    - belief propagation: confidence(B) += 0.1·conf(A)·w для входящих рёбер с
      conf(A) ≥ 0.8 — одноразовый буст (только узлы с дефолтной 0.5, не рекурсивный).
    - community bridge: пары внутри louvain-сообщества БЕЗ прямого ребра, но с
      общим epi_tag → `community_bridge` 0.2.
    """
    conn = await cm.get(DB_NAME)
    edges = 0

    # --- co-citation ---
    rows = await (
        await conn.execute(
            """
            SELECT e1.target_id AS a, e2.target_id AS b, COUNT(DISTINCT e1.source_id) AS c
            FROM epi_edges e1
            JOIN epi_edges e2 ON e1.source_id = e2.source_id AND e1.target_id < e2.target_id
            JOIN epi_nodes n1 ON n1.node_id = e1.target_id AND n1.layer = ?
            JOIN epi_nodes n2 ON n2.node_id = e2.target_id AND n2.layer = ?
            WHERE e1.tags = '[]' AND e2.tags = '[]'
            GROUP BY e1.target_id, e2.target_id HAVING c > 0
            """,
            (layer, layer),
        )
    ).fetchall()
    for a, b, c in rows:
        edges += await _insert_edge(conn, int(a), int(b), "co_cited", min(0.3 + 0.05 * (int(c) - 1), 0.6), "co_citation")

    # --- belief propagation: одноразовый буст целей рёбер от conf(A) >= 0.8 ---
    boosted = 0
    rows = await (
        await conn.execute(
            """
            SELECT DISTINCT e.target_id
            FROM epi_edges e
            JOIN epi_nodes s ON s.node_id = e.source_id AND s.layer = ?
            JOIN epi_nodes t ON t.node_id = e.target_id AND t.layer = ?
            WHERE s.confidence >= 0.8 AND t.confidence = 0.5
            """,
            (layer, layer),
        )
    ).fetchall()
    for (target,) in rows:
        gains = await (
            await conn.execute(
                "SELECT MAX(s.confidence * e.weight) FROM epi_edges e"
                " JOIN epi_nodes s ON s.node_id = e.source_id AND s.layer = ?"
                " WHERE e.target_id = ? AND s.confidence >= 0.8",
                (layer, target),
            )
        ).fetchone()
        gain = 0.1 * float(gains[0] if gains is not None and gains[0] is not None else 0.0)
        if gain > 0:
            await conn.execute("UPDATE epi_nodes SET confidence = confidence + ? WHERE node_id = ?", (gain, int(target)))
            boosted += 1

    # --- louvain-мосты: пары в одном сообществе, без прямого ребра, с общим тегом ---
    communities = await _node_communities(conn, layer)
    if communities:
        tagged: dict[int, set[str]] = {}
        for r in await (await conn.execute("SELECT node_id, tag FROM epi_tags")).fetchall():
            tagged.setdefault(int(r["node_id"]), set()).add(str(r["tag"]))
        linked: set[tuple[int, int]] = {
            (int(r["source_id"]), int(r["target_id"])) for r in await (await conn.execute("SELECT source_id, target_id FROM epi_edges")).fetchall()
        }
        for members in communities:
            ms = sorted(members)
            for i, a in enumerate(ms):
                for b in ms[i + 1 :]:
                    if (a, b) in linked or (b, a) in linked or not tagged.get(a, set()) & tagged.get(b, set()):
                        continue
                    if await _insert_edge(conn, a, b, "community_bridge", 0.2, "community_bridge"):
                        edges += 1
                        linked.add((a, b))

    await conn.commit()
    return {"edges": edges, "boosted": boosted}


async def _node_communities(conn: Any, layer: str) -> list[set[int]]:
    """louvain-сообщества узлов слоя по их рёбрам (A1.6, networkx); [] при пустом графе.

    G5 hub exclusion: MOC-хабы/auto-indexes исключены из графа сообществ —
    иначе один MOC склеивает всё в одно сообщество.
    """
    try:
        import networkx as nx  # type: ignore[import-untyped]
    except ImportError:
        return []
    from lifecycle.graph_sanitation import HUB_EXCLUSION_PARAMS

    rows = await (
        await conn.execute(
            "SELECT e.source_id, e.target_id FROM epi_edges e"
            " JOIN epi_nodes s ON s.node_id = e.source_id AND s.layer = ?"
            " JOIN epi_nodes t ON t.node_id = e.target_id AND t.layer = ?",
            (layer, layer),
        )
    ).fetchall()
    excluded = {
        int(r["node_id"])
        for r in await (
            await conn.execute(
                f"SELECT node_id FROM epi_nodes WHERE layer=? AND node_type IN ({', '.join('?' * len(HUB_EXCLUSION_PARAMS))})",
                (layer, *HUB_EXCLUSION_PARAMS),
            )
        ).fetchall()
    }
    graph = nx.Graph()
    graph.add_nodes_from(
        int(r["node_id"])
        for r in await (await conn.execute("SELECT node_id FROM epi_nodes WHERE layer=?", (layer,))).fetchall()
        if int(r["node_id"]) not in excluded
    )
    for r in rows:
        s, t = int(r["source_id"]), int(r["target_id"])
        if s not in excluded and t not in excluded:
            graph.add_edge(s, t)
    return [set(c) for c in nx.community.louvain_communities(graph, seed=42) if len(c) >= 2]


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


async def _find_or_add_node(conn: Any, layer: str, user_id: str, node_type: str, content: str) -> int:
    """find_or_add по (layer, user_id, node_type, content) — как record_causal._node."""
    row = await (
        await conn.execute(
            "SELECT node_id FROM epi_nodes WHERE layer=? AND user_id=? AND node_type=? AND content=? LIMIT 1",
            (layer, user_id, node_type, content),
        )
    ).fetchone()
    if row:
        return int(row["node_id"])
    cur = await conn.execute(
        "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES (?, ?, ?, ?, '[]', 0.5, ?)",
        (layer, user_id, content, node_type, time.time()),
    )
    return int(cur.lastrowid or 0)


async def miner_tool_triplets(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """#10: l0_journal tool_use+tool_result пары (по tool_use_id) → триплеты query→tool→outcome.

    Узлы: query (текст из tool_use.input), action 'tool:<name>', outcome — сводка
    результата; is_error у tool_result → outcome узел node_type='error_outcome'.
    Рёбра query_tool / tool_outcome, weight=0.5, tags heuristic:triplets
    (idempotent: INSERT OR IGNORE + find_or_add). Висячие/битые блоки скипаются.
    """
    from lifecycle.tool_stats import _SNIP, scan_tool_pairs, tool_query_text, tool_result_text

    conn = await cm.get(DB_NAME)
    pairs, _ = await scan_tool_pairs(conn, 0.0, layer=layer)
    edges = 0
    for use, result in pairs:
        query = tool_query_text(use.get("input"))
        outcome = tool_result_text(result.get("content"))[:_SNIP]
        if not query or not outcome:
            continue
        uid = str(use.get("_uid"))
        q_id = await _find_or_add_node(conn, layer, uid, "query", query)
        a_id = await _find_or_add_node(conn, layer, uid, "action", f"tool:{use.get('name') or 'unknown'}")
        o_type = "error_outcome" if result.get("is_error") else "outcome"
        o_id = await _find_or_add_node(conn, layer, uid, o_type, outcome)
        edges += await _insert_edge(conn, q_id, a_id, "query_tool", 0.5, "triplets")
        edges += await _insert_edge(conn, a_id, o_id, "tool_outcome", 0.5, "triplets")
    await conn.commit()
    return {"edges": edges}


async def wire_new_node(cm: AsyncConnectionManager, layer: str, node_id: int, content: str, tags: list[str] | None = None) -> int:
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


async def ensure_zero_result(cm: AsyncConnectionManager) -> None:
    """S17 #6: idempotent schema журнала провальных запросов (open-index-минер)."""
    await cm.execute_script(
        DB_NAME,
        """
        CREATE TABLE IF NOT EXISTS recall_zero_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            layer TEXT NOT NULL DEFAULT 'user',
            user_id TEXT NOT NULL DEFAULT 'default',
            query TEXT NOT NULL,
            query_hash TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_zero_results_layer ON recall_zero_results(layer, user_id, ts);
        """,
    )


async def log_zero_result(cm: AsyncConnectionManager, layer: str, user_id: str, query: str) -> None:
    """Провальный запрос (0 хитов) → минер-сигнал «что смоделировать следующим»."""
    await ensure_zero_result(cm)
    conn = await cm.get(DB_NAME)
    qhash = hashlib.sha1(query.encode("utf-8", "ignore")).hexdigest()[:16]
    await conn.execute(
        "INSERT INTO recall_zero_results (ts, layer, user_id, query, query_hash) VALUES (?, ?, ?, ?, ?)",
        (time.time(), layer, user_id, query[:500], qhash),
    )
    await conn.commit()


async def miner_zero_results(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """#12 zero-result минер (S17 #6, open-index): повторные провальные запросы → вопрос-узлы.

    Один и тот же запрос (query_hash, per-user) падал ≥2 раз → find_or_add
    question-узла с текстом запроса: граф получает явный маркер зазора
    «что смоделировать следующим» (open-index: +9.4% recall на аналогичном
    сигнале). find_or_add идемпотентен — узел-гэп не плодится ночами.
    Возвращаемый счётчик — question-узлы поверх журнала (MINERS-контракт —
    единый ключ edges, graph_enrich reports как есть).
    """
    await ensure_zero_result(cm)
    conn = await cm.get(DB_NAME)
    rows = await (
        await conn.execute(
            "SELECT query, user_id, COUNT(*) c FROM recall_zero_results WHERE layer=? GROUP BY query_hash, user_id HAVING c >= 2 LIMIT 20",
            (layer,),
        )
    ).fetchall()
    if not rows:
        return {"edges": 0}
    surfaced = 0
    try:
        from graph.epistemic import EpistemicGraph

        g = EpistemicGraph(cm=cm, layer=layer)
        for r in rows:
            await g.find_or_add_entity(str(r["user_id"]), str(r["query"])[:300], "question")
            surfaced += 1
    except Exception:
        logger.debug("zero-result surfacing failed", exc_info=True)
        return {"edges": 0}
    return {"edges": surfaced}


MINERS: dict[str, Miner] = {
    "tags": miner_tags,
    "tokens": miner_tokens,
    "entities": miner_entities,
    "sessions": miner_sessions,
    "provenance": miner_provenance,
    "co_retrieval": miner_co_retrieval,
    "embedding": miner_embedding,
    "markers": miner_markers,
    "structural": miner_structural,
    "triplets": miner_tool_triplets,
    "wiki_fact_links": miner_wiki_fact_links,
    "zero_results": miner_zero_results,
}
