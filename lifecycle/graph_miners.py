"""Graph miners (Phase G): deterministic edge factories over existing data.

Miners read existing data (epi_tags, node contents, L0 user-message rows) and
write edges into epi_edges tagged `heuristic:<name>` (rollback: DELETE WHERE
tags LIKE '%heuristic:%'). All inserts are INSERT OR IGNORE against the
epi_edges PK — re-runs are no-ops. No LLM calls anywhere.
"""

from __future__ import annotations

import contextlib
import functools
from bisect import bisect_left, bisect_right
import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from shared.connection import AsyncConnectionManager
from shared.constants import DB_NAME
from shared.write_dispatch import get_write_dispatch

logger = logging.getLogger(__name__)

Miner = Callable[[AsyncConnectionManager, str], Awaitable[dict[str, int]]]

_TOKEN_RE = re.compile(r"[а-яёa-z0-9]+")
# Non-topic service words (RU+EN); len>=4 additionally filters noise.
_STOP_TOKENS = {"и", "но", "в", "на", "с", "для", "это", "что", "the", "a", "an", "is", "are", "of", "to"}

_SESSION_GAP = 1800.0  # L0 rows within 30 min belong to one session
_NODE_WINDOW = 300.0  # node is in-session if created_at is within ±5 min of an L0 row
_BIND_SHARED = 2  # or >=2 shared canon-tokens with session texts
_SESSION_TOPK = 12  # at most 12 same_session edges per node from this miner (anti-hub, like entities)


def _maintenance(miner: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Run a miner inside one dispatch transaction.

    The miner body receives `conn` and never opens a transaction of its own, so
    its writes cannot ride the shared connection's snapshot. A caller that
    already holds a transaction on the shared connection keeps it: the miner
    runs inline instead of opening a second transaction that would wait out the
    busy timeout.
    """

    @functools.wraps(miner)
    async def wrapper(cm: AsyncConnectionManager, *args: Any, **kwargs: Any) -> Any:
        shared = await cm.get(DB_NAME)
        if shared.in_transaction:
            return await miner(cm, *args, conn=shared, borrowed=True, **kwargs)
        dispatch = get_write_dispatch(cm)
        return await dispatch.atomic(DB_NAME, lambda conn: miner(cm, *args, conn=conn, borrowed=False, **kwargs))

    return wrapper


class _ReadCursor:
    """Awaitable fetchall over rows already read (the compute phase needs no live cursor)."""

    __slots__ = ("_rows",)

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[Any]:
        return self._rows


class _Reader:
    """Read-only handle for the compute phase: `execute(sql, params)` → cursor.

    Deliberately exposes NO commit, NO in_transaction and no DML: a write
    attempt inside compute fails loudly instead of silently opening a
    transaction the two-phase contract does not allow.
    """

    __slots__ = ("_run",)

    def __init__(self, run: Callable[..., Awaitable[Any]]) -> None:
        self._run = run

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> _ReadCursor:
        rows = await self._run(sql, params)
        return _ReadCursor(list(rows))


async def _rows_via(conn: Any, sql: str, params: Sequence[Any]) -> list[Any]:
    """Query-form rows through an existing connection (borrowed/inline path)."""
    rows = await (await conn.execute(sql, params)).fetchall()
    return list(rows)


def _noop(result: Any) -> Callable[[Any], Awaitable[Any]]:
    """Return the apply call of a compute phase that decided it has nothing to write."""

    async def apply(_conn: Any) -> Any:
        return result

    return apply


def _maintenance_split(
    compute: Callable[..., Awaitable[Callable[[Any], Awaitable[Any]]]],
) -> Callable[..., Awaitable[Any]]:
    """Run a miner in two phases: compute outside any transaction, apply inside one.

    22.10, live attribution: `@_maintenance` put the WHOLE body — reads, CPU,
    network — inside one transaction, so the write lock was held for the whole
    body: on the live house `wire_new_node` held it 2.217 s (`slow write:
    phase=lifecycle.graph_miners:wrapper:55`). A read-then-write body still
    needs ONE transaction, so the boundary does not disappear — it moves: the
    body reads and computes with NO transaction open (no lock held), returns
    the apply call, and the wrapper runs that apply in one short dispatch
    transaction.

    Contract: compute must not write and must not commit; apply must not
    commit — the wrapper owns the single transaction. A caller that already
    holds a transaction on the shared connection keeps it: compute reads on
    that same connection (so it sees the caller's uncommitted rows) and apply
    runs inline, with no second transaction that would wait out the busy
    timeout.
    """

    @functools.wraps(compute)
    async def wrapper(cm: AsyncConnectionManager, *args: Any, **kwargs: Any) -> Any:
        shared = await cm.get(DB_NAME)
        if shared.in_transaction:
            apply = await compute(cm, *args, read=_Reader(functools.partial(_rows_via, shared)), **kwargs)
            return await apply(shared)
        dispatch = get_write_dispatch(cm)
        apply = await compute(cm, *args, read=_Reader(functools.partial(dispatch.query, DB_NAME)), **kwargs)
        return await dispatch.atomic(DB_NAME, apply)

    return wrapper


async def _inhibit_dirty(conn: Any, dirty: set[int]) -> int:
    """Batched end-of-run inhibition: one lateral_inhibition per touched node.

    19.09 (issue G, part 2): miners insert thousands of edges per nightly;
    inhibiting per edge is O(E²) per call. Collect dirty nodes during the
    run and sweep once at the end — linear passes.

    06.10: the original claim here — "same end state" as per-edge inhibition —
    was wrong and is retracted. Per-edge inhibition fires against a half-built
    neighbourhood: by the time a weak edge is inserted, its stronger neighbours
    have already been suppressed by each other, so the new edge is suppressed
    by LESS than the formula gives for the finished neighbourhood. Batched runs
    the formula once, against the neighbourhood as it finally stands.

    Measured on a `VACUUM INTO` snapshot of live hermes (WAL included),
    `miner_embedding`, layer `user`, each variant run to convergence (from the
    3rd run on it writes nothing, so these are fixed points):

        per-edge steady: sem_zero=8218, sem_wsum=23697.3
        batched  steady: sem_zero=7419, sem_wsum=23982.6

    ~800 `semantic_overlap` edges (≈10 %) stay alive under batching that
    per-edge drove to 0.0. A 0.0 weight is not a no-op: it contributes
    0.5*weight*confidence = 0 to retrieval, and `_dream_nrem` prunes the edge
    once it is 30 days old, so a zeroed edge eventually leaves the graph. The
    gap is far smaller than the first-run transient suggests (sem_zero 26805 vs
    11078), but it is real and it persists.
    """
    from lifecycle.graph_sanitation import lateral_inhibition

    changed = 0
    for nid in dirty:
        with contextlib.suppress(Exception):  # inhibition must never crash the miner
            changed += await lateral_inhibition(conn, nid)
    return changed


async def _insert_edge(conn: Any, a: int, b: int, relation: str, weight: float, heuristic: str, dirty: set[int] | None = None) -> int:
    """UPSERT into epi_edges; returns rows actually written (re-run → 0).

    Audit 05.09 (P1): INSERT OR IGNORE froze the weight — a miner re-run
    never strengthened a link. The upsert now takes max(weight) (strongest
    evidence wins) and refreshes created_at. After a heuristic edge is
    inserted, lateral inhibition applies (G5, SYNAPSE): a weak edge is
    suppressed by the cluster of stronger neighbors around the node.

    19.09 (issue G): inhibition runs ONLY for newly created rows. A repeat
    upsert used to re-run the quadratic lateral_inhibition over the whole
    hub on every nightly (640k edges → 9h of 100% main-thread CPU that
    never converges: upsert re-raises what inhibition lowered). Return
    contract is unchanged (rows written, 0/1).

    19.09 (issue G, part 3): the upsert is gated — a weaker-or-equal repeat
    is a true no-op (no row touched, created_at stable, returns 0). Without
    the gate every repeat rewrote created_at, so no nightly ever converged
    and the DB churned WAL on millions of no-change writes.
    """
    prev = await (
        await conn.execute(
            "SELECT weight FROM epi_edges WHERE source_id=? AND target_id=? AND relation=?",
            (a, b, relation),
        )
    ).fetchone()
    cur = await conn.execute(
        """INSERT INTO epi_edges (source_id, target_id, relation, weight, created_at, tags) VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (source_id, target_id, relation)
           DO UPDATE SET weight = excluded.weight, created_at = excluded.created_at
           WHERE excluded.weight > weight""",
        (a, b, relation, weight, time.time(), json.dumps([f"heuristic:{heuristic}"])),
    )
    written = int(cur.rowcount or 0)
    if prev is None and written:
        if dirty is not None:
            dirty.update((a, b))
            return written
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


@_maintenance
async def miner_tags(cm: AsyncConnectionManager, layer: str, *, conn: Any = None, borrowed: bool = False) -> dict[str, int]:
    """#1: shared epi_tags → `tagged`, weight = min(0.3 + 0.1*shared, 0.6)."""
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
    dirty: set[int] = set()
    for a, b, shared in rows:
        edges += await _insert_edge(conn, int(a), int(b), "tagged", min(0.3 + 0.1 * int(shared), 0.6), "tags", dirty)
    await _inhibit_dirty(conn, dirty)
    return {"edges": edges}


@_maintenance_split
async def miner_tokens(cm: AsyncConnectionManager, layer: str, *, read: Any) -> Callable[[Any], Awaitable[dict[str, int]]]:
    """#2: >=2 shared rare tokens and Jaccard >= threshold → `topic_overlap`, weight = Jaccard.

    Threshold = max(0.3, mad_threshold(jaccards)) — the MAD threshold (G2
    sanitation) raises the cutoff only when the distribution is genuinely
    shifted upward; the 0.3 floor preserves historical behavior on sparse layers.

    Two phases: read the nodes and score the pairs outside any transaction
    (`read`), write the accepted edges in the wrapper's one short transaction.
    """
    from lifecycle.graph_sanitation import mad_threshold

    nodes = await (await read.execute("SELECT node_id, content FROM epi_nodes WHERE layer=?", (layer,))).fetchall()
    syn: dict[str, list[str]] | None = None
    toks: dict[int, set[str]] = {}
    for r in nodes:
        if syn is None:
            from rag.synonyms import load_synonyms

            syn = load_synonyms()
        toks[int(r["node_id"])] = _canon_tokens(str(r["content"]), syn)
    ids = sorted(toks)
    postings: dict[str, list[int]] = {}
    for nid in ids:
        for tok in toks[nid]:
            postings.setdefault(tok, []).append(nid)
    cands: list[tuple[int, int, float]] = []
    for a in ids:
        ta = toks[a]
        if not ta:
            continue
        counts: dict[int, int] = {}
        for tok in ta:
            for b in postings[tok]:
                if b > a:
                    counts[b] = counts.get(b, 0) + 1
        for b in sorted(counts):
            shared = counts[b]
            if shared < 2:
                continue
            tb = toks[b]
            jaccard = shared / (len(ta) + len(tb) - shared)
            cands.append((a, b, jaccard))
    tau = max(0.3, mad_threshold([c[2] for c in cands])) if cands else 0.3
    wanted = [(a, b, jaccard) for a, b, jaccard in cands if jaccard >= tau]

    async def apply(conn: Any) -> dict[str, int]:
        edges = 0
        dirty: set[int] = set()
        for a, b, jaccard in wanted:
            edges += await _insert_edge(conn, a, b, "topic_overlap", jaccard, "tokens", dirty)
        await _inhibit_dirty(conn, dirty)
        return {"edges": edges}

    return apply


@_maintenance_split
async def miner_sessions(cm: AsyncConnectionManager, layer: str, *, read: Any) -> Callable[[Any], Awaitable[dict[str, int]]]:
    """#4: facts of one session → `same_session`, weight = 0.3.

    L0 user-message rows are clustered by close ts (or shared source_msg_id);
    a node binds to a cluster via the ts window from L0 rows or via >=2 shared
    canon-tokens with cluster texts (synonym canonicalization).

    Two phases: read the journal and the nodes, cluster and pick the pairs
    outside any transaction; write the pairs in the wrapper's one short
    transaction.
    """
    l0 = await (
        await read.execute(
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

    nodes = await (await read.execute("SELECT node_id, content, created_at FROM epi_nodes WHERE layer=?", (layer,))).fetchall()
    node_toks: dict[int, set[str]] = {}
    node_ts: dict[int, float] = {}
    if merged:
        from rag.synonyms import load_synonyms

        syn = load_synonyms()
        for r in nodes:
            nid = int(r["node_id"])
            node_toks[nid] = _canon_tokens(str(r["content"]), syn)
            node_ts[nid] = float(r["created_at"])

    time_index: list[tuple[float, int]] = []
    tok_index: dict[str, list[int]] = {}
    for idx, c in enumerate(merged):
        c["rows"].sort()
        for t in c["rows"]:
            time_index.append((t, idx))
        for tok in c["toks"]:
            tok_index.setdefault(tok, []).append(idx)
    time_index.sort()
    times = [t for t, _ in time_index]

    assigned: dict[int, set[int]] = {}  # node_id → cluster indexes
    for nid, toks in node_toks.items():
        ts = node_ts[nid]
        bound: set[int] = set()
        lo = bisect_left(times, ts - _NODE_WINDOW)
        hi = bisect_right(times, ts + _NODE_WINDOW)
        bound.update(idx for _, idx in time_index[lo:hi])
        counts: dict[int, int] = {}
        for tok in toks:
            for idx in tok_index.get(tok, ()):
                counts[idx] = counts.get(idx, 0) + 1
        bound.update(idx for idx, shared in counts.items() if shared >= _BIND_SHARED)
        if bound:
            assigned[nid] = bound

    by_cluster: dict[int, list[int]] = {}
    for nid, cs in assigned.items():
        for idx in cs:
            by_cluster.setdefault(idx, []).append(nid)
    for members in by_cluster.values():
        members.sort()

    wanted: list[tuple[int, int]] = []
    degree: dict[int, int] = {}
    for idx in range(len(merged)):
        members = by_cluster.get(idx, [])
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                if degree.get(a, 0) >= _SESSION_TOPK or degree.get(b, 0) >= _SESSION_TOPK:
                    continue  # 19.09 (issue G, part 3): anti-hub cap — a mega-cluster
                    # must not materialize the complete graph (1.1M edges in 10 min)
                wanted.append((a, b))
                degree[a] = degree.get(a, 0) + 1
                degree[b] = degree.get(b, 0) + 1

    async def apply(conn: Any) -> dict[str, int]:
        edges = 0
        dirty: set[int] = set()
        for a, b in wanted:
            edges += await _insert_edge(conn, a, b, "same_session", 0.3, "sessions", dirty)
        await _inhibit_dirty(conn, dirty)
        return {"edges": edges}

    return apply


@_maintenance_split
async def miner_entities(cm: AsyncConnectionManager, layer: str, *, read: Any) -> Callable[[Any], Awaitable[dict[str, int]]]:
    """#3: synonym dictionary (canon classes, both sides) + spaCy NER (Latin ORG/GPE) → `co_mentions` 0.4.

    S17 B6 post-eval (2026-09-06, numbers in the design doc): no false-merge
    found (0 duplicates across 3 instances), but hub growth is real — a
    multi-topic dump with 11 canon classes gathered 109 of 137 co_mentions
    (hermes). The `_CO_MENTIONS_TOPK` per-node degree cap trims the hubs
    (same pattern as miner #9's _EMBED_TOPK).

    Two phases: NER inference and pair scoring run outside any transaction
    (the cache read is a read, the misses' rows travel to apply); the cache
    rows and the edges are written together in the wrapper's one short
    transaction.
    """
    nodes = await _layer_nodes(read, layer)
    if len(nodes) < 2:
        return _noop({"edges": 0})
    from rag.synonyms import load_synonyms

    syn = load_synonyms()
    nlp = _get_ner()
    ents, pending = await _entities_read(read, [str(c) for _, c in nodes], syn, nlp)
    wanted: list[tuple[int, int]] = []
    degree: dict[int, int] = {}
    for i in range(len(nodes)):
        if not ents[i]:
            continue
        for j in range(i + 1, len(nodes)):
            if ents[i] & ents[j]:
                a, b = nodes[i][0], nodes[j][0]
                if degree.get(a, 0) >= _CO_MENTIONS_TOPK or degree.get(b, 0) >= _CO_MENTIONS_TOPK:
                    continue  # B6: active-entity cap — anti-hub
                wanted.append((a, b))
                degree[a] = degree.get(a, 0) + 1
                degree[b] = degree.get(b, 0) + 1

    async def apply(conn: Any) -> dict[str, int]:
        await _entities_cache_write(conn, pending)
        edges = 0
        dirty: set[int] = set()
        for a, b in wanted:
            edges += await _insert_edge(conn, a, b, "co_mentions", 0.4, "entities", dirty)
        await _inhibit_dirty(conn, dirty)
        return {"edges": edges}

    return apply


def _entities(text: str, syn: dict[str, list[str]], nlp: Any = None) -> set[str]:
    """Entities of a text: synonym-dictionary canon classes + spaCy ORG/GPE (Latin).

    Canonicalization via _canon — full class in both directions: every spelling variant
    collapses into one entity.
    """
    vocab = set(syn) | {v for vs in syn.values() for v in vs}
    found = {_canon(w, syn) for w in _TOKEN_RE.findall(text.lower()) if w in vocab}
    if nlp is not None:
        with contextlib.suppress(Exception):
            found |= {ent.text.lower() for ent in nlp(text).ents if ent.label_ in _NER_LABELS}
    return found


_NER_LABELS = {"ORG", "GPE"}
_ner = None

# Chunk size for `... WHERE text_hash IN (?, ...)`: SQLite allows 32766 variables
# on modern builds and 999 on older ones; this stays below both.
_NER_CACHE_CHUNK = 500


def _ner_dict_fingerprint(syn: dict[str, list[str]]) -> str:
    """Identity of the synonym dictionary `_entities` consults.

    The dictionary half of `_entities` decides part of the result, so a cached
    entity set must not outlive the dictionary that produced it — otherwise
    editing synonyms would keep serving the old entities forever.
    """
    vocab = sorted(set(syn) | {v for vs in syn.values() for v in vs})
    return hashlib.sha256("\n".join(vocab).encode("utf-8")).hexdigest()[:16]


def _ner_cache_tag(syn: dict[str, list[str]], nlp: Any) -> str:
    """Cache tag tying a row to BOTH the dictionary and the NER backend.

    A row computed with `nlp=None` (dictionary only) must never be served once
    spaCy becomes available, and vice versa: the two produce different sets, and
    mixing them would silently change which co_mentions edges get written.
    """
    if nlp is None:
        backend = "dict-only"
    else:
        meta = getattr(nlp, "meta", None) or {}
        backend = f"{meta.get('name', 'ner')}-{meta.get('version', '?')}"
    return f"{_ner_dict_fingerprint(syn)}|{backend}"


async def _ensure_ner_cache(conn: Any) -> None:
    """Idempotent schema for the entity-extraction cache (same idea as ensure_co_pairs)."""
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS ner_cache ("
        " text_hash TEXT NOT NULL,"
        " tag TEXT NOT NULL,"
        " entities TEXT NOT NULL,"
        " created_at DATETIME DEFAULT CURRENT_TIMESTAMP,"
        " PRIMARY KEY (text_hash, tag))"
    )


async def _entities_read(read: Any, texts: list[str], syn: dict[str, list[str]], nlp: Any) -> tuple[list[set[str]], list[tuple[str, str, str]]]:
    """Cache read + fresh extraction, WITHOUT writing: returns (entities per text, rows to write).

    Split from `_entities_cached` for the two-phase miners: the compute phase
    reads the cache and parses only the misses (spaCy inference is the CPU this
    whole refactor exists to move out of the write transaction), and the rows
    for the misses travel to apply, where they are written inside the same
    transaction as the edges.

    The table's absence IS the cold cache: no schema statement runs here (a
    read must not DDL), and the first apply creates it when it has rows to
    write — a fully-warm cache implies the table already exists.
    """
    if not texts:
        return [], []
    tag = _ner_cache_tag(syn, nlp)
    hashes = [hashlib.sha256(t.encode("utf-8")).hexdigest() for t in texts]

    cached: dict[str, set[str]] = {}
    unique = list(dict.fromkeys(hashes))
    exists = await (await read.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ner_cache'")).fetchall()
    if exists:
        for start in range(0, len(unique), _NER_CACHE_CHUNK):
            chunk = unique[start : start + _NER_CACHE_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            rows = await (
                await read.execute(
                    f"SELECT text_hash, entities FROM ner_cache WHERE tag=? AND text_hash IN ({placeholders})",
                    (tag, *chunk),
                )
            ).fetchall()
            for row in rows:
                with contextlib.suppress(Exception):
                    cached[str(row["text_hash"])] = set(json.loads(str(row["entities"])))

    pending: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for text_hash, text in zip(hashes, texts, strict=True):
        if text_hash in cached or text_hash in seen:
            continue
        seen.add(text_hash)
        ents = _entities(text, syn, nlp)
        cached[text_hash] = ents
        # sorted: a set has no stable order, and a stable blob keeps the
        # table diffable and the tests deterministic
        pending.append((text_hash, tag, json.dumps(sorted(ents), ensure_ascii=False)))
    return [cached[h] for h in hashes], pending


async def _entities_cache_write(conn: Any, pending: list[tuple[str, str, str]]) -> None:
    """Persist the cache rows computed in `_entities_read` (apply phase, one transaction)."""
    if not pending:
        return
    await _ensure_ner_cache(conn)
    await conn.executemany(
        "INSERT OR REPLACE INTO ner_cache (text_hash, tag, entities) VALUES (?, ?, ?)",
        pending,
    )


async def _entities_cached(conn: Any, texts: list[str], syn: dict[str, list[str]], nlp: Any) -> list[set[str]]:
    """`_entities` for every text, computing only what the cache does not hold.

    Why: entity extraction dominated the nightly budget — spaCy inference over
    the whole layer, measured at 10.5 s for cowagent's 1860 nodes and 46.0 s for
    hermes's 5516 (both ~0.07-0.10 ms/KB, i.e. linear in text volume, not in node
    count). `_layer_nodes` has no LIMIT, so every night re-parsed every unchanged
    node. Node content is immutable once written, so a content-hash cache is
    exactly "do not recompute what did not change".

    Misses are parsed synchronously, then written in one transaction; identical
    texts are parsed once. The conn-path contract (used by the callers that
    already hold a connection) is unchanged: the caller's transaction is not
    ours to end — read ownership BEFORE this helper's own DML, commit only when
    we own the connection and we wrote something.
    """
    # `conn` arrives from the caller, so ownership has to be read here, before this helper's own
    # DML: the caller's transaction is not ours to end, and our writes ride along with it.
    borrowed = conn.in_transaction
    if not texts:
        return []
    ents, pending = await _entities_read(_Reader(functools.partial(_rows_via, conn)), texts, syn, nlp)
    if pending:
        await _entities_cache_write(conn, pending)
        if not borrowed:
            await conn.commit()
    return ents


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


@_maintenance
async def log_co_pairs(cm: AsyncConnectionManager, query: str, hits: list[dict[str, Any]], *, conn: Any = None, borrowed: bool = False) -> int:
    """Record (node_a, node_b) pairs of all hits from a successful recall. Returns the pair count."""
    refs = [r for r in (_hit_ref(h) for h in hits) if r]
    await ensure_co_pairs(cm)  # even with <2 refs: recall_events-style ensure always
    if len(refs) < 2:
        return 0
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
    return written


@_maintenance
async def miner_provenance(cm: AsyncConnectionManager, layer: str, *, conn: Any = None, borrowed: bool = False) -> dict[str, int]:
    """#5: metadata.parents 'episode:N' → episode node → `sourced_from` edge to the fact node.

    The fact node is matched by exact content == core_memory.value (created by
    mcp fact-add); the episode node is find_or_add by content 'episode:N'.
    Wiki [[fact:]] links are not wired yet — direct parents only.
    """
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
    return {"edges": edges}


@_maintenance
async def miner_wiki_fact_links(cm: AsyncConnectionManager, layer: str, *, conn: Any = None, borrowed: bool = False) -> dict[str, int]:
    """Nightly provenance bridges wiki↔L4 (S5, audit 05.09).

    [[fact:key]] in wiki_index.content → an edge between the wiki_page node
    and the record's fact node.

    The fact node is matched as in miner_provenance: node_type='fact',
    content == value. Records without a node and pages without a wiki node
    are skipped.
    """
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
    # Backlink saves go through CoreMemory, which writes on the same re-entrant
    # dispatcher as this miner: collecting them keeps the save order after the
    # edges without opening a second transaction.
    pending_saves: list[tuple[Any, dict[str, Any], str]] = []
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
                pending_saves.append((fact_row, meta, layer))
        # The save runs inside this miner's transaction (the dispatcher is
        # re-entrant), so the backlink is written atomically with the edges.
        for fact_row, meta, save_layer in pending_saves:
            cmem = CoreMemory(cm=cm, layer=save_layer)
            await cmem.save(
                str(fact_row["user_id"]),
                str(fact_row["key"]),
                str(fact_row["value"]),
                importance=float(fact_row["importance"]),
                memory_kind=fact_row["memory_kind"],
                source=str(fact_row["source"] or "consolidation:wiki_link"),
                metadata=meta,
            )
    return {"edges": edges}


@_maintenance
async def miner_co_retrieval(cm: AsyncConnectionManager, layer: str, *, conn: Any = None, borrowed: bool = False) -> dict[str, int]:
    """#7: co-retrieval journal, count>=2 → `co_recalled` edges (g:-pairs + f:-pairs).

    g:<node_id> — a direct edge between layer nodes. f:<page_id> — a wiki/rag
    page: mapped via rag_pages.path → epi_nodes.content (node_type='wiki_page',
    created by lifecycle/wiki_graph_builder.py). Pages without a wiki node are
    skipped.
    """
    await ensure_co_pairs(cm)
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
# Slack over `_EMBED_TOPK` in the per-node candidate list. The greedy can skip
# a high-ranked pair because its partner is already full and still accept a
# lower-ranked one, so the candidate list must be wider than the budget.
# With centring (see `miner_embedding`) the pool collapses to a few thousand
# pairs, so 16 (240 candidates for a 15-edge budget) never binds on real data:
# it is a safety net for a pathological corpus (a huge duplicate clique), not a
# sampling policy. Measured in the F2b round-2 report.
_CANDIDATE_FACTOR = 16
_SEMANTIC_BLOCK = 512  # rows per numpy block in the pairwise scan
_SEMANTIC_CHUNK = 1 << 16  # candidates per bulk-skip step in the greedy
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


@_maintenance_split
async def miner_markers(cm: AsyncConnectionManager, layer: str, *, read: Any) -> Callable[[Any], Awaitable[dict[str, int]]]:
    """#6: node pairs sharing a canon-token with ts delta in-window and a marker on the later one → `led_to` 0.3.

    Direction A→B (A earlier, B carrying a 'fixed/broke/…' marker): the early
    node is about X, the later one is the outcome for X. Without a shared
    token or outside the window — no edge.

    Two phases: read + tokenize + window scoring outside any transaction,
    edges in the wrapper's one short transaction.
    """
    from rag.synonyms import load_synonyms

    syn = load_synonyms()
    nodes = await (await read.execute("SELECT node_id, content, created_at FROM epi_nodes WHERE layer=?", (layer,))).fetchall()
    parsed = [(int(r["node_id"]), _canon_tokens(str(r["content"]), syn), _MARKERS.search(str(r["content"])), float(r["created_at"])) for r in nodes]
    wanted: list[tuple[int, int]] = []
    for i, (a, ta, _, ta_ts) in enumerate(parsed):
        if not ta:
            continue
        for b, tb, m, tb_ts in parsed[i + 1 :]:
            if not m or not ta & tb:
                continue
            lo, hi = (a, b) if ta_ts <= tb_ts else (b, a)  # edge from the earlier to the later (marker) node
            delta = abs(tb_ts - ta_ts)
            if _MARKER_MIN <= delta <= _MARKER_MAX:
                wanted.append((lo, hi))

    async def apply(conn: Any) -> dict[str, int]:
        edges = 0
        for lo, hi in wanted:
            edges += await _insert_edge(conn, lo, hi, "led_to", 0.3, "marker")
        return {"edges": edges}

    return apply


@_maintenance
async def miner_structural(cm: AsyncConnectionManager, layer: str, *, conn: Any = None, borrowed: bool = False) -> dict[str, int]:
    """#8: structural invariants — co-citation, belief propagation, louvain bridges.

    - co-citation: two layer nodes are cited by a third (non-heuristic edges) →
      `co_cited` 0.3 (heuristic citation edges are excluded: the graph must not
      close on itself).
    - belief propagation: confidence(B) += 0.1·conf(A)·w for incoming edges with
      conf(A) >= 0.8 — a one-shot boost (only default-0.5 nodes, non-recursive).
    - community bridge: pairs inside a louvain community WITHOUT a direct edge
      but sharing an epi_tag → `community_bridge` 0.2.
    """
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


def _packed_words(binary: list[bytes]) -> Any | None:
    """Bit vectors as uint64 words, so the scan is 6 planes wide, not 48 bytes.

    Returns None when padding cannot be expressed or numpy is absent, so the
    caller can fall back to the integer path.
    """
    import numpy as np

    if not binary or not hasattr(np, "bitwise_count"):
        return None
    n = len(binary)
    width = len(binary[0])
    if width == 0 or any(len(b) != width for b in binary):
        return None
    packed = np.frombuffer(b"".join(binary), dtype=np.uint8).reshape(n, width)
    if width % 8:
        packed = np.pad(packed, ((0, 0), (0, 8 - width % 8)))
    return np.ascontiguousarray(packed).view(np.uint64)


def _semantic_candidate_order(binary: list[bytes], threshold: float) -> tuple[Any, Any] | None:
    """Bounded candidate pairs in the exact order the greedy loop below expects.

    The previous shape — append every qualifying pair, then one global
    `lexsort` over all of them — materialised 101 551 346 pairs and 5.9 GB on
    hermes. This keeps at most `_EMBED_TOPK * _CANDIDATE_FACTOR` partners per
    node instead, so memory is O(n * M), not O(pairs). The O(n²) popcount
    compute is untouched: that cost is inherent to exact pairwise comparison
    and this function does not pretend to remove it.

    The ordering is load-bearing, not cosmetic. Jaccard here is a ratio of two
    small integer popcounts, so equal values are common, and the old
    `sorted(cands, reverse=True)` broke those ties by `i` descending and then by
    `j` descending. Reproducing that only approximately changes which pairs the
    greedy accepts: an earlier version ordered ties per block and produced
    39 431 edges where the original produced 38 593. So the union of the
    per-node lists is still sorted once, globally, by (value desc, i desc,
    j desc).

    The per-node selection uses the same comparator: `key = value + index * eps`
    with `eps` smaller than the gap between any two distinct Jaccard values
    (`p/q` with `q <= width`), so distinct values are never reordered and exact
    ties are broken by the larger index.

    `union` is `|a| + |b| - inter`, so one popcount per pair suffices instead of
    two, and the per-vector counts are computed once.

    Returns `(ii, jj)` aligned arrays, or None to request the integer fallback.
    """
    import numpy as np

    words = _packed_words(binary)
    if words is None:
        return None
    n = len(binary)
    if n < 2:
        return np.empty(0, dtype=np.int32), np.empty(0, dtype=np.int32)
    counts = np.bitwise_count(words).sum(axis=1).astype(np.int32)
    active = np.flatnonzero(counts > 0)  # a zero vector has no bits to share
    if active.size == 0:
        return np.empty(0, dtype=np.int32), np.empty(0, dtype=np.int32)
    cols = np.arange(n)
    width = words.shape[1] * 64
    keep = min(_EMBED_TOPK * _CANDIDATE_FACTOR, max(n - 1, 1))
    eps = 1.0 / (2.0 * width * max(width - 1, 1) * (n + 1))

    # Per-node candidate lists. `low_*` holds pairs where the node is the lower
    # endpoint (filled once, from its own row); `up_*` where it is the upper
    # endpoint (merged block by block).
    low_val = np.full((n, keep), -np.inf, dtype=np.float64)
    low_i = np.full((n, keep), -1, dtype=np.int32)
    low_j = np.full((n, keep), -1, dtype=np.int32)
    up_val = np.full((n, keep), -np.inf, dtype=np.float64)
    up_i = np.full((n, keep), -1, dtype=np.int32)

    for start in range(0, len(active), _SEMANTIC_BLOCK):
        rows = active[start : start + _SEMANTIC_BLOCK]
        block = words[rows]
        inter = np.zeros((len(rows), n), dtype=np.int32)
        for word in range(words.shape[1]):
            inter += np.bitwise_count(block[:, word][:, None] & words[None, :, word])
        union = counts[rows][:, None] + counts[None, :] - inter
        jac = np.divide(
            inter,
            np.maximum(union, 1),
            out=np.zeros(inter.shape, dtype=np.float64),
            where=union > 0,
        )
        valid = (jac >= threshold) & (cols[None, :] > rows[:, None])
        masked = np.where(valid, jac, -np.inf)

        # Lower endpoint: top `keep` per row by (value desc, j desc).
        idx = np.argpartition(-(masked + cols[None, :] * eps), keep - 1, axis=1)[:, :keep]
        low_val[rows] = np.take_along_axis(masked, idx, axis=1)
        low_i[rows] = rows[:, None]
        low_j[rows] = idx

        # Upper endpoint: top `keep` per column by (value desc, i desc), then
        # merge with what earlier blocks already contributed for that node.
        k_up = min(keep, len(rows))
        cidx = np.argpartition(-(masked + rows[:, None] * eps), k_up - 1, axis=0)[:k_up, :]
        comb_val = np.concatenate([up_val, np.take_along_axis(masked, cidx, axis=0).T], axis=1)
        comb_i = np.concatenate([up_i, rows[cidx].T.astype(np.int32)], axis=1)
        order = np.lexsort((-comb_i, -comb_val), axis=1)[:, :keep]
        up_val = np.take_along_axis(comb_val, order, axis=1)
        up_i = np.take_along_axis(comb_i, order, axis=1)

    # One candidate list per node: merge its lower- and upper-endpoint lists by
    # the global comparator (value desc, i desc, j desc).
    val = np.concatenate([low_val, up_val], axis=1)
    ii = np.concatenate([low_i, up_i], axis=1)
    jj = np.concatenate([low_j, np.broadcast_to(cols[:, None], (n, keep))], axis=1)
    order = np.lexsort((-jj, -ii, -val), axis=1)[:, :keep]
    val = np.take_along_axis(val, order, axis=1)
    ii = np.take_along_axis(ii, order, axis=1)
    jj = np.take_along_axis(jj, order, axis=1)

    flat_val = val.ravel()
    flat_i = ii.ravel()
    flat_j = jj.ravel()
    ok = flat_val > -np.inf
    flat_val = flat_val[ok]
    flat_i = flat_i[ok]
    flat_j = flat_j[ok]
    # Every pair appears in both endpoints' lists — keep one copy.
    pkey = flat_i.astype(np.int64) * n + flat_j
    _, first = np.unique(pkey, return_index=True)
    flat_val = flat_val[first]
    flat_i = flat_i[first]
    flat_j = flat_j[first]
    # lexsort's LAST key is primary: jaccard desc, then i desc, then j desc
    order = np.lexsort((-flat_j, -flat_i, -flat_val))
    return flat_i[order].astype(np.int32), flat_j[order].astype(np.int32)


@_maintenance_split
async def miner_embedding(cm: AsyncConnectionManager, layer: str, *, read: Any) -> Callable[[Any], Awaitable[dict[str, int]]]:
    """#9: rich embedding (content+tags) → centred MIB bits → pairwise Jaccard → `semantic_overlap`.

    A-MEM rich embedding: encodes "content + tags from epi_tags" with synonym
    token canonicalization (_canon from T2). Junk filter — same as graph_enrich
    ([{…-JSON / tool_use_id). top-k=15 per node.

    The vectors are CENTRED before quantisation. `embed_to_binary` binarises by
    `sign(emb)`, and e5 embeddings share a large common component: measured on
    the live hermes layer, the mean vector has norm 0.91 while each text vector
    has norm 1.0. `sign()` therefore encoded the common component — median
    Jaccard was 0.81 and 100 % of pairs cleared `_EMBED_JACCARD`, so the
    threshold sat below the noise floor and this miner built 101 M candidates.
    Subtracting the mean drops the median to 0.33 (0.01 % of pairs above 0.7)
    and restores meaning: duplicate texts score 1.0, random pairs 0.27–0.36.

    The centring lives HERE and not in `rag/quantize.py` on purpose: the binary
    RAG path (`searcher`, `engine`, `ablation`) and `embedding_clusters` must
    keep their current semantics.

    Consequence to accept: the mean is computed per run over the current node
    set, so an edge can shift slightly as the corpus changes. That is
    acceptable for a heuristic graph and is the price of making the bits
    discriminative.

    The candidate set is bounded per node (`_EMBED_TOPK * _CANDIDATE_FACTOR`),
    so memory is O(n * M) instead of O(pairs). With centring the bound never
    binds on real data; it exists so a pathological corpus cannot reproduce the
    multi-gigabyte spike.

    Two phases: the embedding SERVICE call (network) and the whole pairwise
    scan run outside any transaction — this is the miner whose compute phase
    is the most expensive of the six; the junk flags and the accepted edges
    are written together in the wrapper's one short transaction.
    """
    nodes = await _layer_nodes(read, layer)
    if len(nodes) < 2:
        return _noop({"edges": 0})
    from rag.quantize import embed_to_binary
    from rag.synonyms import load_synonyms
    from shared.embeddings import embed_texts

    syn = load_synonyms()
    tag_rows = await (
        await read.execute(
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

    # No try/except here, on purpose. This used to be
    # `except Exception: return {"edges": 0}` with a comment claiming it only
    # covered "no numpy/model" — two things that are both wrong. `embed_texts`
    # does not raise when no model is available (it falls back to hash vectors,
    # see `_compute_missing_embeddings`), and `embed_to_binary` has its own
    # numpy-less path, so the only exceptions reaching here are real failures:
    # a dead or slow embedding service above all.
    #
    # Swallowing them made a six-day outage of this one miner invisible: the
    # graph lost `semantic_overlap` while the log stayed empty and this miner
    # still reported a normal zero. `graph_enrich` already knows how to report
    # a failed miner (`{"edges": -1}` plus a warning) and that branch is only
    # reachable if the exception is allowed to escape.
    #
    # A-MEM rich embedding: f"{content} {tags}"; canonicalization (_canon from T2)
    # applies to tags so that name/technology variants land in one meaning
    # cache key. Cache key = raw content — reuses vectors seeded by the ingestor.
    # anomaly:* tags (addendum 10) never enter the text — a flag does not
    # change the node's vector.
    vecs = await embed_texts([f"{c} {' '.join(sorted(t for t in tags.get(nid, []) if not t.startswith('anomaly:')))}" for nid, c in nodes])
    # Centre before quantising: sign() of a raw e5 vector encodes the shared
    # component (norm 0.91 of 1.0), not the meaning — see the docstring.
    import numpy as np

    raw = np.asarray(vecs, dtype=np.float32)
    binary = [embed_to_binary(v, dim=len(v)) for v in raw - raw.mean(axis=0)]
    bits = [_bits_int(b) for b in binary]

    # S17 addendum 10: bit-degenerate vectors (0 bits — text without significant
    # tokens, junk from L3 dumps) → flagged `anomaly:junk_vector`, cleanup candidate.
    # anomaly:* tags never enter the rich-embed text (see above) — no self-pollution.
    # The flags are computed here and written in apply: a flag INSERT in compute
    # would open a transaction the two-phase contract does not allow.
    junk = [nid for (nid, _), b in zip(nodes, bits, strict=True) if b == 0]

    wanted: list[tuple[int, int, float]] = []  # (a, b, weight) accepted in compute

    def _accept(i: int, j: int) -> bool:
        """Apply the crosscheck vote and take the pair for apply (no I/O).

        The top-k budget is checked by the caller, in the same order the previous
        loop checked `degree` before the crosscheck. A crosscheck rejection
        returns False and therefore consumes no budget, exactly as the previous
        `continue` left `degree` untouched.

        Inhibition is deferred: `dirty` collects the touched nodes for one sweep
        at the end, as miners #1/#2/#3/#4 already do (see `_inhibit_dirty`).
        """
        a, b = nodes[i][0], nodes[j][0]
        if crosscheck:
            # addendum 9: keyword vote — shared canon-tokens OR shared tags;
            # "vector-similar but with no lexical or tag trace" on hash
            # vectors = noise → the edge is not written (addendum 9 "dropped").
            lex_agree = bool(token_sets[i] & token_sets[j]) or bool(set(tags.get(a, [])) & set(tags.get(b, [])))
            if not lex_agree:
                return False
            wanted.append((a, b, _SEMANTIC_CONFIRMED_WEIGHT))
        else:
            wanted.append((a, b, _SEMANTIC_WEIGHT))
        return True

    open_nodes = sum(1 for b in bits if b)
    pairs = _semantic_candidate_order(binary, _EMBED_JACCARD)
    if pairs is None:
        # numpy unavailable — the original integer scan, with the same sort order
        # so that a machine without numpy produces byte-identical edges.
        cands: list[tuple[float, int, int]] = []
        for i in range(len(nodes)):
            if not bits[i]:
                continue
            for j in range(i + 1, len(nodes)):
                jacc = _bit_jaccard(bits[i], bits[j])
                if jacc >= _EMBED_JACCARD:
                    cands.append((jacc, i, j))
        remaining = [_EMBED_TOPK] * len(nodes)
        for _, i, j in sorted(cands, reverse=True):
            if remaining[i] <= 0 or remaining[j] <= 0:
                continue
            if _accept(i, j):
                remaining[i] -= 1
                remaining[j] -= 1
    else:
        import numpy as np

        ii, jj = pairs
        # Top-k budget per node INDEX: each node appears once in `nodes`, so this
        # is the same accounting the previous `degree` dict kept by node_id.
        budget = np.full(len(nodes), _EMBED_TOPK, dtype=np.int32)
        for start in range(0, len(ii), _SEMANTIC_CHUNK):
            if open_nodes < 2:
                break  # an edge needs BOTH endpoints below the cap
            ci = ii[start : start + _SEMANTIC_CHUNK]
            cj = jj[start : start + _SEMANTIC_CHUNK]
            # drop, a chunk at a time, pairs whose endpoints are already capped —
            # they can never be accepted and no longer need a per-pair check
            keep = (budget[ci] > 0) & (budget[cj] > 0)
            if not keep.any():
                continue
            for i, j in zip(ci[keep].tolist(), cj[keep].tolist(), strict=True):
                if budget[i] <= 0 or budget[j] <= 0:
                    continue
                if not _accept(i, j):
                    continue
                budget[i] -= 1
                budget[j] -= 1
                if budget[i] == 0:
                    open_nodes -= 1
                if budget[j] == 0:
                    open_nodes -= 1

    async def apply(conn: Any) -> dict[str, int]:
        for nid in junk:
            await conn.execute("INSERT OR IGNORE INTO epi_tags (node_id, tag) VALUES (?, 'anomaly:junk_vector')", (nid,))
        edges = 0
        dirty: set[int] = set()
        for a, b, weight in wanted:
            edges += await _insert_edge(conn, a, b, "semantic_overlap", weight, "embedding", dirty)
        # One lateral_inhibition per touched node, after every edge is in place,
        # instead of one per edge against a half-built neighbourhood: the pattern
        # `_inhibit_dirty` exists for and that miners #1/#2/#3/#4 already use.
        # A/B on two identical `VACUUM INTO` snapshots of live hermes, fresh WAL
        # included, same layer: 66.2 s -> 25.3 s. The graph ends up with the same
        # edges either way; what changes is where inhibition lands (see the
        # `_inhibit_dirty` docstring).
        await _inhibit_dirty(conn, dirty)
        return {"edges": edges, "anomalies": len(junk)}

    return apply


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


async def _scan_tool_pairs_on(conn: Any, since_ts: float, layer: str | None = None) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Read the L0 tool_use/tool_result pairs on `conn` — one connection, one snapshot.

    `lifecycle.tool_stats.scan_tool_pairs` reads through the write dispatch,
    which is the right thing for a standalone statistics call but the WRONG
    thing inside a miner: the borrowed branch of `_maintenance` writes on the
    shared handle, so a read through the dispatch is a read on another
    connection, on another snapshot — a journal row that the caller inserted
    but has not committed is invisible to the miner.

    The rule this function exists to enforce: a miner reads and writes on the
    SAME connection, so the read sees exactly what the write produced.

    The matching is not re-implemented here: `lifecycle.tool_stats` owns the
    tool_use/tool_result pairing, and this path and the dispatch read go
    through the same matcher and the same query.
    """
    from lifecycle.tool_stats import read_tool_pairs_on

    pairs, _calls = await read_tool_pairs_on(conn, since_ts, layer)
    return pairs


@_maintenance
async def miner_tool_triplets(cm: AsyncConnectionManager, layer: str, *, conn: Any = None, borrowed: bool = False) -> dict[str, int]:
    """#10: l0_journal tool_use+tool_result pairs (by tool_use_id) → query→tool→outcome triplets.

    Nodes: query (text from tool_use.input), action 'tool:<name>', outcome — a
    summary of the result; tool_result is_error → outcome node with
    node_type='error_outcome'. Edges query_tool / tool_outcome, weight=0.5,
    tags heuristic:triplets (idempotent: INSERT OR IGNORE + find_or_add).
    Dangling/broken blocks are skipped.

    The journal is read on `conn` — the connection this miner writes on. The
    borrowed branch of `_maintenance` writes on the shared handle, so a read
    through the dispatch would be a read on ANOTHER connection, on another
    snapshot: a journal row the caller had inserted but not committed was
    invisible to the miner.
    """
    from lifecycle.tool_stats import _SNIP, tool_query_text, tool_result_text

    pairs = await _scan_tool_pairs_on(conn, 0.0, layer=layer)
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
    return {"edges": edges}


@_maintenance_split
async def wire_new_node(
    cm: AsyncConnectionManager,
    layer: str,
    node_id: int,
    content: str,
    tags: list[str] | None = None,
    *,
    read: Any,
) -> Callable[[Any], Awaitable[int]]:
    """Incremental mode (G4): edges of a NEW node vs existing ones — wired at write time.

    Light signals: shared tags (tagged), >=2 shared canon-tokens + Jaccard >=0.3
    (topic_overlap), shared dictionary/NER entity (co_mentions). Heavy signals
    (embedding/sessions) stay with the nightly graph_enrich. Returns the edge count.

    Two phases: this runs on the LIVE new_message path (the 2.217 s slow write
    this refactor exists for), so reading the layer, tokenizing and the NER pass
    over the neighbours all happen outside any transaction — the writes go into
    the wrapper's one short transaction. Wired-at-write order within the pair
    list is preserved; the edges are written in the order computed.
    """
    from rag.synonyms import load_synonyms

    syn = load_synonyms()
    my_toks = _canon_tokens(content, syn)
    my_ents = _entities(content, syn)
    my_tags = set(tags or [])
    if not my_toks and not my_ents and not my_tags:
        return _noop(0)
    others = await _layer_nodes(read, layer)
    other_tags: dict[int, set[str]] = {}
    for r in await (await read.execute("SELECT node_id, tag FROM epi_tags WHERE node_id != ?", (node_id,))).fetchall():
        other_tags.setdefault(int(r["node_id"]), set()).add(str(r["tag"]))
    nlp = _get_ner()
    wanted: list[tuple[int, int, str, float, str]] = []  # (a, b, relation, weight, heuristic)
    for oid, ocontent in others:
        if oid == node_id:
            continue
        otoks = _canon_tokens(ocontent, syn)
        union = my_toks | otoks
        shared_toks = my_toks & otoks
        if len(shared_toks) >= 2 and union and len(shared_toks) / len(union) >= 0.3:
            wanted.append((min(node_id, oid), max(node_id, oid), "topic_overlap", len(shared_toks) / len(union), "tokens"))
        if my_ents and my_ents & _entities(ocontent, syn, nlp):
            wanted.append((min(node_id, oid), max(node_id, oid), "co_mentions", 0.4, "entities"))
        shared = my_tags & other_tags.get(oid, set())
        if shared:
            wanted.append((min(node_id, oid), max(node_id, oid), "tagged", min(0.3 + 0.1 * len(shared), 0.6), "tags"))

    async def apply(conn: Any) -> int:
        edges = 0
        for a, b, relation, weight, heuristic in wanted:
            edges += await _insert_edge(conn, a, b, relation, weight, heuristic)
        return edges

    return apply


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


@_maintenance
async def log_zero_result(cm: AsyncConnectionManager, layer: str, user_id: str, query: str, *, conn: Any = None, borrowed: bool = False) -> None:
    """Log a failed query (0 hits) as a miner signal "what to model next"."""
    await ensure_zero_result(cm)
    qhash = hashlib.sha1(query.encode("utf-8", "ignore")).hexdigest()[:16]
    await conn.execute(
        "INSERT INTO recall_zero_results (ts, layer, user_id, query, query_hash) VALUES (?, ?, ?, ?, ?)",
        (time.time(), layer, user_id, query[:500], qhash),
    )


@_maintenance
async def miner_zero_results(cm: AsyncConnectionManager, layer: str, *, conn: Any = None, borrowed: bool = False) -> dict[str, int]:
    """#12 zero-result miner (S17 #6, open-index): repeated failed queries → question nodes.

    The same query (query_hash, per-user) failed >=2 times → find_or_add a
    question node with the query text: the graph gets an explicit gap marker
    "what to model next" (open-index: +9.4% recall on a similar signal).
    find_or_add is idempotent — the gap node is not duplicated night after
    night. The returned counter is question nodes on top of the journal (the
    MINERS contract is the single `edges` key; graph_enrich reports it as is).
    """
    await ensure_zero_result(cm)
    rows = await (
        await conn.execute(
            "SELECT query, user_id, COUNT(*) c FROM recall_zero_results WHERE layer=? GROUP BY query_hash, user_id HAVING c >= 2 LIMIT 20",
            (layer,),
        )
    ).fetchall()
    if not rows:
        return {"edges": 0}
    surfaced = 0
    # No try/except here, for the same reason `miner_embedding` has none: a
    # failure that returns `{"edges": 0}` is indistinguishable from "there was
    # nothing to surface", and this one logged at `debug`, which is invisible
    # at the level this service actually runs at. Letting it escape reaches the
    # handler in `graph_enrich` that records `edges: -1` with a warning.
    from graph.epistemic import EpistemicGraph

    g = EpistemicGraph(cm=cm, layer=layer)
    for r in rows:
        await g.find_or_add_entity(str(r["user_id"]), str(r["query"])[:300], "question")
        surfaced += 1
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
