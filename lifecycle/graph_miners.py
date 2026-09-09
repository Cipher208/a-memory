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
# Non-topic service words (RU+EN); len>=4 additionally filters noise.
_STOP_TOKENS = {"и", "но", "в", "на", "с", "для", "это", "что", "the", "a", "an", "is", "are", "of", "to"}

_SESSION_GAP = 1800.0  # L0 rows within 30 min belong to one session
_NODE_WINDOW = 300.0  # node is in-session if created_at is within ±5 min of an L0 row
_BIND_SHARED = 2  # or >=2 shared canon-tokens with session texts


async def _insert_edge(conn: Any, a: int, b: int, relation: str, weight: float, heuristic: str) -> int:
    """UPSERT into epi_edges; returns rows actually written (re-run → 0).

    Audit 05.09 (P1): INSERT OR IGNORE froze the weight — a miner re-run
    never strengthened a link. The upsert now takes max(weight) (strongest
    evidence wins) and refreshes created_at. After a heuristic edge is
    inserted, lateral inhibition applies (G5, SYNAPSE): a weak edge is
    suppressed by the cluster of stronger neighbors around the node.
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

        with contextlib.suppress(Exception):  # inhibition must never crash the miner
            await lateral_inhibition(conn, a)
            await lateral_inhibition(conn, b)
    return written


def _canon(w: str, syn: dict[str, list[str]]) -> str:
    """Canonical form of a token: delegates to rag.synonyms.canonical_form (two-way class expansion)."""
    from rag.synonyms import canonical_form

    return canonical_form(w, syn)


def _canon_tokens(text: str, syn: dict[str, list[str]] | None = None) -> set[str]:
    """Rare tokens of a text: [_TOKEN_RE] tokens lowercase, len>=4, not stop-words, canonicalized.

    S19 tail (Eli missed it — no stemming existed): `rag.lemmatize` → pymorphy3
    RU lemmas collapse inflection ('zarplaty' → 'zarplata'), so topic_overlap
    stops losing morphology. The ingestor cache key is seeded from raw content —
    lemmatization here does not shift existing vectors.
    """
    if syn is None:
        from rag.synonyms import load_synonyms

        syn = load_synonyms()
    lemmatize = _lemmatize_enabled()
    out: set[str] = set()
    for w in _TOKEN_RE.findall(text.lower()):
        if len(w) < 4 or w in _STOP_TOKENS:
            continue
        if lemmatize:
            from shared.morph import normal_form

            lemma = normal_form(w)
            if lemma and len(lemma) >= 4:  # a short lemma loses the meaning; stay on the token
                w = lemma
        out.add(_canon(w, syn))
    return out


def _lemmatize_enabled() -> bool:
    from config import config

    return bool(config.get("rag", "lemmatize", default=False))


async def _layer_nodes(conn: Any, layer: str) -> list[tuple[int, str]]:
    """Nodes of the layer, without junk JSON / tool_use_id content (same filter as graph_enrich)."""
    rows = await (
        await conn.execute(
            "SELECT node_id, content FROM epi_nodes WHERE layer=? AND content NOT LIKE '[{%' AND content NOT LIKE '%tool_use_id%'",
            (layer,),
        )
    ).fetchall()
    return [(int(r["node_id"]), str(r["content"])) for r in rows]


async def miner_tags(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """#1: shared epi_tags → `tagged`, weight = min(0.3 + 0.1*shared, 0.6)."""
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
    """#2: >=2 shared rare tokens and Jaccard >= threshold → `topic_overlap`, weight = Jaccard.

    Threshold = max(0.3, mad_threshold(jaccards)) — the MAD threshold (G2
    sanitation) raises the cutoff only when the distribution is genuinely
    shifted upward; the 0.3 floor preserves historical behavior on sparse layers.
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
    """#4: facts of one session → `same_session`, weight = 0.3.

    L0 user-message rows are clustered by close ts (or shared source_msg_id);
    a node binds to a cluster via the ts window from L0 rows or via >=2 shared
    canon-tokens with cluster texts (synonym canonicalization).
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
    assigned: dict[int, set[int]] = {}  # node_id → cluster indexes
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
    """#3: synonym dictionary (canon classes, both sides) + spaCy NER (Latin ORG/GPE) → `co_mentions` 0.4.

    S17 B6 post-eval (2026-09-06, numbers in the design doc): no false-merge
    found (0 duplicates across 3 instances), but hub growth is real — a
    multi-topic dump with 11 canon classes gathered 109 of 137 co_mentions
    (hermes). The `_CO_MENTIONS_TOPK` per-node degree cap trims the hubs
    (same pattern as miner #9's _EMBED_TOPK).
    """
    conn = await cm.get(DB_NAME)
    nodes = await _layer_nodes(conn, layer)
    if len(nodes) < 2:
        return {"edges": 0}
    from rag.synonyms import load_synonyms

    syn = load_synonyms()
    nlp = _get_ner()
    ents = [_entities(str(c), syn, nlp) for _, c in nodes]
    edges = 0
    degree: dict[int, int] = {}
    for i in range(len(nodes)):
        if not ents[i]:
            continue
        for j in range(i + 1, len(nodes)):
            if ents[i] & ents[j]:
                a, b = nodes[i][0], nodes[j][0]
                if degree.get(a, 0) >= _CO_MENTIONS_TOPK or degree.get(b, 0) >= _CO_MENTIONS_TOPK:
                    continue  # B6: active-entity cap — anti-hub
                edges += await _insert_edge(conn, a, b, "co_mentions", 0.4, "entities")
                degree[a] = degree.get(a, 0) + 1
                degree[b] = degree.get(b, 0) + 1
    await conn.commit()
    return {"edges": edges}


def _entities(text: str, syn: dict[str, list[str]], nlp: Any = None) -> set[str]:
    """Entities of a text: synonym-dictionary canon classes + spaCy ORG/GPE (Latin).

    Canonicalization via _canon — full class in both directions: 'Lili'/'Lily'/
    'lisenyonshchik' collapse into one entity.
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
    """Lazy spaCy NER; None if the model is not installed — the dictionary layer suffices."""
    global _ner
    if _ner is None:
        try:
            from mcp_server.utils.privacy import _get_nlp

            _ner = _get_nlp()
        except Exception:
            _ner = False
    return _ner or None


# Task G3: co-retrieval journal. FTS5 hits carry rag_pages.id; graph hits carry
# epi_nodes.node_id — two different id spaces. Compromise: log pairs of ANY hit
# ids with a type prefix ('f:5', 'g:12'); miner #7 mines g:-pairs directly, and
# f:-pairs via the rag_pages.path → wiki-node mapping (node_type='wiki_page',
# lifecycle/wiki_graph_builder.py). Mixed g/f pairs are not mined.
_G_PREFIX = "g:"
_F_PREFIX = "f:"


async def ensure_co_pairs(cm: AsyncConnectionManager) -> None:
    """Idempotent schema for the co-retrieval journal (like ConflictResolver.ensure)."""
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
    """hit-id → typed reference ('g:<node_id>' / 'f:<page_id>'); None → not journaled."""
    hid = hit.get("id")
    if not isinstance(hid, int) or hid == 0:
        return None
    if hid < -3_000_000:  # rag.multi_source._ID_OFFSET_GRAPH: graph id space (negatives)
        return f"{_G_PREFIX}{-hid - 3_000_000}"
    return f"{_F_PREFIX}{hid}"


async def log_co_pairs(cm: AsyncConnectionManager, query: str, hits: list[dict[str, Any]]) -> int:
    """Record (node_a, node_b) pairs of all hits from a successful recall. Returns the pair count."""
    refs = [r for r in (_hit_ref(h) for h in hits) if r]
    await ensure_co_pairs(cm)  # even with <2 refs: recall_events-style ensure always
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
    """#5: metadata.parents 'episode:N' → episode node → `sourced_from` edge to the fact node.

    The fact node is matched by exact content == core_memory.value (created by
    mcp fact-add); the episode node is find_or_add by content 'episode:N'.
    Wiki [[fact:]] links are not wired yet — direct parents only.
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
    """Nightly provenance bridges wiki↔L4 (S5, audit 05.09).

    [[fact:key]] in wiki_index.content → an edge between the wiki_page node
    and the record's fact node.

    The fact node is matched as in miner_provenance: node_type='fact',
    content == value. Records without a node and pages without a wiki node
    are skipped.
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
            # [[fact:backup_enc]] → the key may carry a kind prefix
            # (fact:backup_enc) or not — try both, as wiki authors write it.
            fact_row = None
            for cand in (key, f"fact:{key}"):
                fact_row = await (
                    await conn.execute(
                        "SELECT user_id, key, value, importance, memory_kind, source, metadata FROM core_memory WHERE layer=? AND key=? LIMIT 1",
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
            # S19: backlink L4→wiki — the page node_id is merged into metadata.wiki_ids
            # (idempotent set; no-op save if already there — do not bloat the LEDGER).
            from core.memory import CoreMemory, _load_meta

            meta = _load_meta(fact_row["metadata"])
            page_id = int(page["node_id"])
            ids = {int(x) for x in meta.get("wiki_ids", []) if str(x).lstrip("-").isdigit()}
            if page_id not in ids:
                ids.add(page_id)
                meta["wiki_ids"] = sorted(ids)
                cmem = CoreMemory(cm=cm, layer=layer)
                await cmem.save(
                    str(fact_row["user_id"]),
                    str(fact_row["key"]),
                    str(fact_row["value"]),
                    importance=float(fact_row["importance"]),
                    memory_kind=fact_row["memory_kind"],
                    source=str(fact_row["source"] or "consolidation:wiki_link"),
                    metadata=meta,
                )
    await conn.commit()
    return {"edges": edges}


async def miner_co_retrieval(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """#7: co-retrieval journal, count>=2 → `co_recalled` edges (g:-pairs + f:-pairs).

    g:<node_id> — a direct edge between layer nodes. f:<page_id> — a wiki/rag
    page: mapped via rag_pages.path → epi_nodes.content (node_type='wiki_page',
    created by lifecycle/wiki_graph_builder.py). Pages without a wiki node are
    skipped.
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
        if len(existing) < 2:  # nodes are not from this layer / deleted — no edge
            continue
        edges += await _insert_edge(conn, min(na, nb), max(na, nb), "co_recalled", min(0.3 + 0.1 * int(c), 0.6), "co_retrieval")
    edges += await _f_pair_edges(conn, layer, frows)
    await conn.commit()
    return {"edges": edges}


async def _f_pair_edges(conn: Any, layer: str, rows: list[Any]) -> int:
    """f:-pairs → co_recalled edges between wiki nodes (rag_pages.path → epi_nodes).

    Mapping: rag_pages.path == epi_nodes.content with node_type='wiki_page'
    (the wiki_graph_builder._ensure_node invariant). A pair is mined only when
    BOTH pages have a wiki node of this layer — otherwise there is nothing to
    hang the edge on.
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
_EMBED_TOPK = 15  # at most 15 semantic_overlap edges per node from this miner
_SEMANTIC_WEIGHT = 0.5
# B6 post-eval: the per-node co_mentions cap — a multi-topic dump (a summary
# with 11 synonym classes) gathered 109 of 137 edges; hubs drown entity-RRF.
_CO_MENTIONS_TOPK = 12
# S17 addendum 9: confirming layer — a keyword signal (shared tags/canon-tokens)
# agreeing with embedding similarity → weight 0.6; disagreeing → edge dropped.
_SEMANTIC_CONFIRMED_WEIGHT = 0.6
# S17 addendum 10: anomalous vector — |vector| == 0 or all components equal
# (bit-degeneracy) = junk node, flagged `anomaly:junk_vector` in epi_tags.

# #6: marker lexicon for causal transitions (plan G4b, Step 4).
_MARKERS = re.compile(r"починила|исправила|теперь работает|сломалось|переделали|решено|закрыто")
_MARKER_MIN, _MARKER_MAX = 300.0, 30 * 86400.0  # ts delta within [5 min, 30 days]


async def miner_markers(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """#6: node pairs sharing a canon-token with ts delta in-window and a marker on the later one → `led_to` 0.3.

    Direction A→B (A earlier, B carrying a 'fixed/broke/…' marker): the early
    node is about X, the later one is the outcome for X. Without a shared
    token or outside the window — no edge.
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
            lo, hi = (a, b) if ta_ts <= tb_ts else (b, a)  # edge from the earlier to the later (marker) node
            delta = abs(tb_ts - ta_ts)
            if _MARKER_MIN <= delta <= _MARKER_MAX:
                edges += await _insert_edge(conn, lo, hi, "led_to", 0.3, "marker")
    await conn.commit()
    return {"edges": edges}


async def miner_structural(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """#8: structural invariants — co-citation, belief propagation, louvain bridges.

    - co-citation: two layer nodes are cited by a third (non-heuristic edges) →
      `co_cited` 0.3 (heuristic citation edges are excluded: the graph must not
      close on itself).
    - belief propagation: confidence(B) += 0.1·conf(A)·w for incoming edges with
      conf(A) >= 0.8 — a one-shot boost (only default-0.5 nodes, non-recursive).
    - community bridge: pairs inside a louvain community WITHOUT a direct edge
      but sharing an epi_tag → `community_bridge` 0.2.
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

    # --- belief propagation: one-shot boost of edge targets from conf(A) >= 0.8 ---
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

    # --- louvain bridges: pairs in one community, no direct edge, sharing a tag ---
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
    """Louvain communities of layer nodes over their edges (A1.6, networkx); [] for an empty graph.

    G5 hub exclusion: MOC hubs / auto-indexes are excluded from the community
    graph — otherwise a single MOC glues everything into one community.
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
    """#9: rich embedding (content+tags) → MIB bits → pairwise Jaccard >=0.7 → `semantic_overlap`.

    A-MEM rich embedding: encodes "content + tags from epi_tags" with synonym
    token canonicalization (_canon from T2). Junk filter — same as graph_enrich
    ([{…-JSON / tool_use_id). O(n²) is fine at the current scale
    (~200 nodes = 20k pairs); top-k=15 per node.
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
    token_sets = [_canon_tokens(content, syn) for _, content in nodes]
    # S17 addendum 9: confirming layer (graph.embedding_crosscheck, default off).
    from config import config

    crosscheck = bool(config.get("graph", "embedding_crosscheck", default=False))

    try:
        # A-MEM rich embedding: f"{content} {tags}"; canonicalization (_canon from T2)
        # applies to tags so that name/technology variants land in one meaning
        # cache key. Cache key = raw content — reuses vectors seeded by the ingestor.
        # anomaly:* tags (addendum 10) never enter the text — a flag does not
        # change the node's vector.
        vecs = await embed_texts([f"{c} {' '.join(sorted(t for t in tags.get(nid, []) if not t.startswith('anomaly:')))}" for nid, c in nodes])
        bits = [_bits_int(embed_to_binary(v, dim=len(v))) for v in vecs]
    except Exception:
        return {"edges": 0}  # embedding backend unavailable (no numpy/model) — miner skipped

    # S17 addendum 10: bit-degenerate vectors (0 bits — text without significant
    # tokens, junk from L3 dumps) → flagged `anomaly:junk_vector`, cleanup candidate.
    # anomaly:* tags never enter the rich-embed text (see above) — no self-pollution.
    flagged = 0
    for (nid, _), b in zip(nodes, bits, strict=True):
        if b == 0:
            await conn.execute("INSERT OR IGNORE INTO epi_tags (node_id, tag) VALUES (?, 'anomaly:junk_vector')", (nid,))
            flagged += 1
    if flagged:
        await conn.commit()

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
            continue  # top-k=15 per node
        if crosscheck:
            # addendum 9: keyword vote — shared canon-tokens OR shared tags;
            # "vector-similar but with no lexical or tag trace" on hash
            # vectors = noise → the edge is not written (addendum 9 "dropped").
            lex_agree = bool(token_sets[i] & token_sets[j]) or bool(set(tags.get(a, [])) & set(tags.get(b, [])))
            if not lex_agree:
                continue
            edges += await _insert_edge(conn, a, b, "semantic_overlap", _SEMANTIC_CONFIRMED_WEIGHT, "embedding")
        else:
            edges += await _insert_edge(conn, a, b, "semantic_overlap", _SEMANTIC_WEIGHT, "embedding")
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1
    await conn.commit()
    return {"edges": edges, "anomalies": flagged}


async def _find_or_add_node(conn: Any, layer: str, user_id: str, node_type: str, content: str) -> int:
    """find_or_add by (layer, user_id, node_type, content) — like record_causal._node."""
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
    """#10: l0_journal tool_use+tool_result pairs (by tool_use_id) → query→tool→outcome triplets.

    Nodes: query (text from tool_use.input), action 'tool:<name>', outcome — a
    summary of the result; tool_result is_error → outcome node with
    node_type='error_outcome'. Edges query_tool / tool_outcome, weight=0.5,
    tags heuristic:triplets (idempotent: INSERT OR IGNORE + find_or_add).
    Dangling/broken blocks are skipped.
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
    """Incremental mode (G4): edges of a NEW node vs existing ones — wired at write time.

    Light signals: shared tags (tagged), >=2 shared canon-tokens + Jaccard >=0.3
    (topic_overlap), shared dictionary/NER entity (co_mentions). Heavy signals
    (embedding/sessions) stay with the nightly graph_enrich. Returns the edge count.
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
    """S17 #6: idempotent schema of the zero-result journal (open-index miner)."""
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
    """Log a failed query (0 hits) as a miner signal "what to model next"."""
    await ensure_zero_result(cm)
    conn = await cm.get(DB_NAME)
    qhash = hashlib.sha1(query.encode("utf-8", "ignore")).hexdigest()[:16]
    await conn.execute(
        "INSERT INTO recall_zero_results (ts, layer, user_id, query, query_hash) VALUES (?, ?, ?, ?, ?)",
        (time.time(), layer, user_id, query[:500], qhash),
    )
    await conn.commit()


async def miner_zero_results(cm: AsyncConnectionManager, layer: str) -> dict[str, int]:
    """#12 zero-result miner (S17 #6, open-index): repeated failed queries → question nodes.

    The same query (query_hash, per-user) failed >=2 times → find_or_add a
    question node with the query text: the graph gets an explicit gap marker
    "what to model next" (open-index: +9.4% recall on a similar signal).
    find_or_add is idempotent — the gap node is not duplicated night after
    night. The returned counter is question nodes on top of the journal (the
    MINERS contract is the single `edges` key; graph_enrich reports it as is).
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
