"""Graph enrich orchestrator (Phase G Task 1): node pre-cleanup + miner skeleton.

Pre-cleanup sweeps fact-nodes whose content is raw JSON / tool output / recall
dumps out of epi_nodes — each is first captured to the append-only l0_journal
(event='graph_cleanup') so nothing is lost, then deleted with its edges/tags.
Miners (Tasks 2-5) plug into MINERS from lifecycle.graph_miners; stubs
contribute no edges yet.

Every read and every write of this module goes through the write dispatch
(`shared/write_dispatch.py`), the process's only owner of transactions — with
one deliberate exception: the miners this pass delegates to. A miner's
borrowed branch runs on the shared handle (`connection_manager.get(DB_NAME)`)
by design, because that branch exists to serve a caller that already holds a
transaction there; such a miner reads AND writes on that one connection, so
its snapshot is never split.

The shared handle is ONE connection for the whole process: a phase that read
on it could pin a read snapshot there (the next write on that handle then
fails with BUSY_SNAPSHOT 517), and the two `await conn.commit()` calls this
module used to make published whatever a handler had left uncommitted on that
handle — the failure class #130 rolled back for the importance sweep.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

from lifecycle.graph_sanitation import HUB_EXCLUSION_PARAMS, hub_exclusion_clause
from shared.connection import connection_manager
from shared.constants import DB_NAME
from shared.write_dispatch import WriteDispatch, get_write_dispatch

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

#: One write operation: the SQL and its parameters.
Op = tuple[str, tuple[Any, ...]]
#: A set of operations that must land in ONE transaction (the valence phase's
#: DELETE+INSERT for one node). A chunk boundary may fall BETWEEN groups, never
#: inside one.
Group = list[Op]


@contextlib.contextmanager
def _timed_phase(layer: str, phase: str) -> Iterator[None]:
    """Log how long one phase of graph_enrich took, without touching its control flow.

    Measurement only. The `finally` re-raises whatever the phase raised, so every
    surrounding `try/except` and `contextlib.suppress` still decides the outcome
    exactly as it did before, and a logging failure cannot change a result.

    Why this exists: the whole step measures ~48 s on cowagent while hermes runs
    a *larger* graph (5539 nodes vs 1964) in a shorter one, so the cost is not
    graph size. The bare step number cannot say which of the twelve miners or
    which of the eleven post-miner phases spent it, and guessing at the O(n^2)
    loop in `miner_embedding` without attribution would be exactly the mistake
    this file is instrumented to avoid.
    """
    started = time.monotonic()
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            logger.info(
                "enrich phase: layer=%s phase=%s %.0f ms",
                layer,
                phase,
                (time.monotonic() - started) * 1000,
            )


# Content markers of raw-harness junk that must never live as graph nodes.
_JUNK_LIKE = ("[{%", "%tool_use_id%", "%[ariel recall]%")

# --- C6: three-phase dream (SYNAPSE §5) ---
NREM_DECAY = 0.01  # -0.01 for inactive heuristic edges older than 30 days
NREM_BOOST = 0.05  # +0.05 for freshly co-fired ones (younger than a day)
NREM_FLOOR = 0.05  # weight below floor — the edge did not survive the sleep (prune)
NREM_STALE_DAYS = 30.0
NREM_FRESH_DAYS = 1.0
REM_SIM_THRESHOLD = 0.7  # similarity of an isolated node to a similar one
REM_WEIGHT_SCALE = 0.3  # bridge weight = sim × 0.3
INSIGHT_MAX = 10  # abstractions per sleep


#: Rows per write transaction in the writing phases of this module.
#:
#: One transaction for a whole phase held the dispatch's process-wide write
#: lock for the whole pass — measured 15649 ms for the dream phase — and every
#: tool write queued behind it. Each phase therefore commits in batches, so the
#: lock is released between them; the same rule `lifecycle/forgetting.py` follows
#: for its decay.
_DREAM_CHUNK = 200


async def _apply_ops(dispatch: WriteDispatch, ops: list[Op]) -> None:
    """Apply one batch of writes in a single dispatch transaction.

    The dispatch owns the transaction: it commits when the body returns and
    rolls it back if the body raises. Nothing here touches the shared
    connection, so a handler's uncommitted row can neither be published by this
    pass nor discarded by it.
    """

    async def _run(conn: Any) -> None:
        for sql, params in ops:
            await conn.execute(sql, params)

    await dispatch.atomic(DB_NAME, _run)


def _group_chunks(groups: list[Group], limit: int) -> Iterator[list[Group]]:
    """Split groups into batches of at most `limit` operations.

    A group is a set of operations that must land in ONE transaction (the
    valence phase's DELETE+INSERT for one node), so a batch boundary falls
    BETWEEN groups and never inside one: a split group leaves the node without
    its `valence:*` tag when the second half fails — a state the old
    single-transaction code could not produce.

    A group larger than `limit` is still kept whole — it goes into a batch of
    its own rather than being cut.
    """
    batch: list[Group] = []
    size = 0
    for group in groups:
        if batch and size + len(group) > limit:
            yield batch
            batch, size = [], 0
        batch.append(group)
        size += len(group)
    if batch:
        yield batch


async def _apply_ops_in_chunks(dispatch: WriteDispatch, ops: list[Op]) -> None:
    """Apply the writes in batches, committing between them.

    A single transaction for the whole list would hold the dispatch's write lock
    for the whole phase and stall every other writer in the process behind it.
    """
    for start in range(0, len(ops), _DREAM_CHUNK):
        await _apply_ops(dispatch, ops[start : start + _DREAM_CHUNK])


async def _dream_nrem(dispatch: WriteDispatch, now: float) -> dict[str, int]:
    """Run the NREM spreading activation pass.

    Freshly co-fired heuristic edges +0.05, stale inactive ones −0.01;
    weight < floor — prune.

    Only the two actionable age bands are read, and the bounds are decided in
    SQL. An edge between a day and a month old is most of the graph and nothing
    happens to it, so pulling those rows into Python only to compute an age and
    fall through was the bulk of the work: on the busiest house database this
    read 155 486 heuristic edges to touch a few thousand. The comparison is the
    same one the loop made — `created_at < now − 30 d` is stale, and
    `created_at >= now − 1 d` is fresh, inclusive, matching `age_days <= 1`.

    A row with a NULL `created_at` is now skipped rather than raising
    `float(None)`; no house database has one, and "unknown age" is not a reason
    to decay, boost, or prune an edge.

    The writes are applied in batches: one transaction for the whole pass would
    hold the dispatch's write lock for its whole duration.
    """
    stale_before = now - NREM_STALE_DAYS * 86400.0
    fresh_from = now - NREM_FRESH_DAYS * 86400.0
    decayed = pruned = boosted = 0
    rows = await dispatch.query(
        DB_NAME,
        "SELECT source_id, target_id, relation, weight, created_at FROM epi_edges"
        " WHERE tags LIKE '%heuristic:%' AND (created_at < ? OR created_at >= ?)",
        (stale_before, fresh_from),
    )
    ops: list[tuple[str, tuple[Any, ...]]] = []
    for r in rows:
        w = float(r["weight"])
        age_days = (now - float(r["created_at"])) / 86400.0
        key = (r["source_id"], r["target_id"], r["relation"])
        if age_days > NREM_STALE_DAYS:
            w2 = w - NREM_DECAY
            if w2 < NREM_FLOOR:
                ops.append(("DELETE FROM epi_edges WHERE source_id=? AND target_id=? AND relation=?", key))
                pruned += 1
            else:
                ops.append(("UPDATE epi_edges SET weight=? WHERE source_id=? AND target_id=? AND relation=?", (w2, *key)))
                decayed += 1
        elif age_days <= NREM_FRESH_DAYS:
            ops.append(("UPDATE epi_edges SET weight=? WHERE source_id=? AND target_id=? AND relation=?", (w + NREM_BOOST, *key)))
            boosted += 1
    await _apply_ops_in_chunks(dispatch, ops)
    return {"decayed": decayed, "pruned": pruned, "boosted": boosted}


async def _dream_rem(dispatch: WriteDispatch, layer: str, now: float) -> int:
    """Build REM bridges: isolated nodes to similar unlinked ones, weight = sim × 0.3.

    Similarity — token Jaccard (len ≥ 3, rag.edm.tokens): deterministic and
    independent of the embedding hash fallback (random vectors never match by
    hash — model cosines on near-dups are unpredictable).
    """
    from rag.edm import tokens

    isolated = [
        (int(r["node_id"]), tokens(str(r["content"])))
        for r in await dispatch.query(
            DB_NAME,
            "SELECT node_id, content FROM epi_nodes n WHERE n.layer=?"
            " AND NOT EXISTS (SELECT 1 FROM epi_edges e WHERE e.source_id=n.node_id OR e.target_id=n.node_id)"
            " AND " + hub_exclusion_clause("n"),
            (layer, *HUB_EXCLUSION_PARAMS),
        )
    ]
    if not isolated:
        return 0
    others = [
        (int(r["node_id"]), tokens(str(r["content"])))
        for r in await dispatch.query(
            DB_NAME,
            "SELECT node_id, content FROM epi_nodes n WHERE n.layer=? AND " + hub_exclusion_clause("n"),
            (layer, *HUB_EXCLUSION_PARAMS),
        )
    ]
    bridged = 0
    seen: set[tuple[int, int]] = set()
    ops: list[tuple[str, tuple[Any, ...]]] = []
    for nid, nt in isolated:
        best_id, best_sim = 0, 0.0
        for oid, ot in others:
            if oid == nid:
                continue
            union = nt | ot
            sim = (len(nt & ot) / len(union)) if union else 0.0
            if sim > best_sim:
                best_id, best_sim = oid, sim
        a, b = sorted((nid, best_id))
        if best_id and best_sim >= REM_SIM_THRESHOLD and (a, b) not in seen:
            seen.add((a, b))
            ops.append(
                (
                    (
                        "INSERT OR IGNORE INTO epi_edges (source_id, target_id, relation, weight, created_at, tags)"
                        " VALUES (?, ?, 'dream_bridge', ?, ?, '[\"dream:rem\"]')"
                    ),
                    (a, b, best_sim * REM_WEIGHT_SCALE, now),
                )
            )
            bridged += 1
    await _apply_ops_in_chunks(dispatch, ops)
    return bridged


async def _insert_insight(dispatch: WriteDispatch, layer: str, summary: str, user_id: str, members: list[int]) -> None:
    """Materialize one insight node and the edges to its members.

    The node and its edges land in one transaction: an insight without its
    `insight_of` edges is an abstraction nobody can walk back from.
    """

    async def _run(conn: Any) -> None:
        cur = await conn.execute(
            "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES (?, ?, ?, 'insight', '[]', 0.6, ?)",
            (layer, user_id, summary, time.time()),
        )
        insight_id = int(cur.lastrowid or 0)
        for nid in members:
            await conn.execute(
                "INSERT OR IGNORE INTO epi_edges (source_id, target_id, relation, weight, created_at, tags)"
                " VALUES (?, ?, 'insight_of', 0.5, ?, '[\"dream:insight\"]')",
                (insight_id, nid, time.time()),
            )

    await dispatch.atomic(DB_NAME, _run)


async def _dream_insight(dispatch: WriteDispatch, layer: str) -> int:
    """Build insight abstractions from BFS communities.

    Connected components become node_type='insight' nodes with a top-token
    frequency summary, linked to their members via 'insight_of' edges.
    """
    from collections import Counter

    from rag.edm import tokens

    nodes = [
        (int(r["node_id"]), str(r["user_id"]), str(r["content"]))
        for r in await dispatch.query(
            DB_NAME,
            "SELECT node_id, user_id, content FROM epi_nodes n WHERE n.layer=? AND " + hub_exclusion_clause("n") + " AND n.node_type != 'insight'",
            (layer, *HUB_EXCLUSION_PARAMS),
        )
    ]
    member_ids = {nid for nid, _, _ in nodes}
    adj: dict[int, set[int]] = {}
    for r in await dispatch.query(DB_NAME, "SELECT source_id, target_id FROM epi_edges"):
        a, b = int(r["source_id"]), int(r["target_id"])
        if a in member_ids and b in member_ids:
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)

    seen: set[int] = set()
    created = 0
    for start in member_ids:
        if start in seen:
            continue
        community: list[int] = []
        queue = [start]
        seen.add(start)
        while queue:
            cur = queue.pop()
            community.append(cur)
            for nb in adj.get(cur, ()):
                if nb not in seen:
                    seen.add(nb)
                    queue.append(nb)
        if len(community) < 3 or created >= INSIGHT_MAX:
            continue
        contents = {nid: content for nid, _, content in nodes if nid in set(community)}
        members = sorted(community)
        freq: Counter[str] = Counter()
        for nid in members:
            freq.update(tokens(contents[nid]))
        top = ", ".join(t for t, _ in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:3])
        summary = f"insight ({len(members)} узлов): {top}"
        dup = await dispatch.select_one(
            DB_NAME,
            "SELECT node_id FROM epi_nodes WHERE layer=? AND node_type='insight' AND content=? LIMIT 1",
            (layer, summary),
        )
        if dup:
            continue
        user_id = next(u for nid, u, _ in nodes if nid == members[0])
        await _insert_insight(dispatch, layer, summary, user_id, members)
        created += 1
    return created


async def _dream(dispatch: WriteDispatch, layer: str) -> dict[str, int]:
    """Run the three-phase sleep: NREM → REM → Insight.

    NREM — spreading activation, REM — bridges, Insight — materialized
    abstractions.
    """
    nrem = await _dream_nrem(dispatch, time.time())
    rem = await _dream_rem(dispatch, layer, time.time())
    insights = await _dream_insight(dispatch, layer)
    # Each phase committed its own batches; no transaction is left open here.
    return {"nrem_decayed": nrem["decayed"] + nrem["boosted"], "nrem_pruned": nrem["pruned"], "rem_bridged": rem, "insights": insights}


# S18 item 4 (Memora cue-anchor prune): grace window for anchors — the miner may not have reached them yet.
_ORPHAN_MIN_AGE_DAYS = 7


async def _orphan_anchor_gc(dispatch: WriteDispatch, layer: str) -> int:
    """Prune derived anchors 'episode:N' (fact/question) that are edge-less and older than 7d (S18 item 4).

    Miners create anchor nodes (provenance 'episode:N', zero-result question);
    when the source record is cleaned up, the node dangles with no incoming
    edges — junk. Nodes with any edge survive; fresh ones (<7d) are untouched.
    """
    cutoff = time.time() - _ORPHAN_MIN_AGE_DAYS * 86400
    rows = await dispatch.query(
        DB_NAME,
        "SELECT node_id FROM epi_nodes n"
        " WHERE n.layer=? AND n.node_type IN ('fact', 'question')"
        " AND n.content LIKE 'episode:%'"
        " AND n.created_at < ?"
        " AND NOT EXISTS (SELECT 1 FROM epi_edges e WHERE e.source_id = n.node_id OR e.target_id = n.node_id)",
        (layer, cutoff),
    )
    ids = [int(r["node_id"]) for r in rows]
    if not ids:
        return 0
    from graph.epistemic import EpistemicGraph

    return await EpistemicGraph(cm=connection_manager, layer=layer).delete_nodes(ids)


def _skipped_report() -> dict[str, Any]:
    """Build the report of a pass that did not run.

    The shape is the normal one, with every counter at zero: the nightly
    consumer reads these keys, and a pass that skipped did nothing — it must
    not look like a pass that found nothing. The warning in the log is what
    says the pass was skipped, not this report.
    """
    return {
        "nodes_cleaned": 0,
        "orphan_gc": 0,
        "gap_registry": {"written": 0, "bumped": 0, "open": 0},
        "miners": {},
        "sanitation": {"expired": 0, "valence_tagged": 0, "centrality_top": []},
        "behavior": {},
        "dream": {"nrem_decayed": 0, "nrem_pruned": 0, "rem_bridged": 0, "insights": 0},
        "segments": {},
        "wiki_communities": [],
    }


async def graph_enrich(layer: str = "user") -> dict[str, Any]:
    """Pre-clean JSON junk from the graph, then run miners. Returns stats."""
    from graph.epistemic import EpistemicGraph
    from lifecycle.graph_miners import MINERS
    from shared.l0 import capture

    cm = connection_manager
    # Housekeeping must not ride, nor wait out, somebody else's transaction: a
    # foreign write txn on the shared handle means the dispatcher's own
    # connection would block for the full busy timeout and then raise. The next
    # tick retries — so the whole pass is skipped, not just the write that
    # would have blocked.
    shared = await cm.get(DB_NAME)
    if shared.in_transaction:
        logger.warning("graph enrich skipped, a foreign transaction is open")
        return _skipped_report()
    dispatch = get_write_dispatch(cm)
    with _timed_phase(layer, "junk_scan"):
        junk = await dispatch.query(
            DB_NAME,
            f"SELECT node_id, user_id, content FROM epi_nodes"
            f" WHERE layer=? AND node_type='fact'"
            f" AND (content LIKE {' OR content LIKE '.join(['?'] * len(_JUNK_LIKE))})",
            (layer, *_JUNK_LIKE),
        )

    cleaned = 0
    ids: list[int] = []
    with _timed_phase(layer, "junk_capture"):
        for row in junk:
            # capture() never raises; junk must reach L0 before its node is gone.
            await capture(event="graph_cleanup", layer=layer, user_id=str(row["user_id"]), text=str(row["content"]))
            ids.append(int(row["node_id"]))
    if ids:
        with _timed_phase(layer, "junk_delete"):
            cleaned = await EpistemicGraph(cm=cm, layer=layer).delete_nodes(ids)

    miners: dict[str, dict[str, Any]] = {}
    for name, miner in MINERS.items():
        try:
            with _timed_phase(layer, f"miner:{name}"):
                res = await miner(cm, layer)
            miners[name] = {"edges": int(res.get("edges", 0))}
        except Exception as exc:
            # Audit 05.09 (P0): a silently failed miner means the graph quietly
            # misses edges. Log it and reflect it in the report so diagnose sees it.
            logger.warning("miner %s failed: %s", name, exc)
            miners[name] = {"edges": -1, "error": str(exc)[:200]}  # -1 = failure (edge counts are non-negative)

    # Per-miner edge counts, on one line. The phase timings above already say
    # which miner ran; they cannot say that a miner ran and produced nothing.
    # A miner returning a flat 0 used to be indistinguishable from a healthy
    # miner with no work, and on 2026-10-05 that hid a six-day outage of
    # `embedding`: `semantic_overlap` stayed frozen while every other relation
    # kept growing. `-1` here means the miner failed outright and the reason is
    # on the warning line above.
    logger.info("miner edges: layer=%s %s", layer, " ".join(f"{name}={res.get('edges')}" for name, res in miners.items()))

    # S17 addendum 11: HDBSCAN clusters of MIB vectors + cross-check against louvain — a
    # report section behind a flag (graph.embed_clusters, default off — the report shape stays stable).
    from config import config

    if bool(config.get("graph", "embed_clusters", default=False)):
        with contextlib.suppress(Exception), _timed_phase(layer, "embed_clusters"):
            from lifecycle.embedding_clusters import cluster_embeddings

            cluster_report = await cluster_embeddings(cm, layer=layer)
            if cluster_report.get("clusters"):
                miners["embedding"]["clusters"] = cluster_report["clusters"]

    # G5 sanitation: validity recheck (edges outside the window → status='expired').
    from lifecycle.graph_sanitation import validate_edges

    expired = 0
    try:
        with _timed_phase(layer, "validate_edges"):
            expired = await dispatch.atomic(DB_NAME, validate_edges)
    except Exception as exc:
        # Like its neighbours below: a BUSY here must not take the rest of the
        # pass (the orphan GC, the gap registry, the dream, the segments, the
        # wiki communities) down with it — and the hook that calls this pass
        # suppresses exceptions, so a silent death here would look like a
        # normal night.
        logger.warning("validate_edges failed: %s", exc)

    # G5 sanitation valence: fact nodes are classified by the valence of their edges
    # (classify_fact) → 'valence:<bucket>' tag ('primary' is not tagged — the default).
    valence_tagged = 0
    try:
        from lifecycle.graph_sanitation import classify_fact

        with _timed_phase(layer, "valence"):
            rel_rows = await dispatch.query(
                DB_NAME,
                "SELECT e.source_id, e.target_id, e.relation FROM epi_edges e"
                " JOIN epi_nodes n ON n.node_id = e.source_id OR n.node_id = e.target_id"
                " WHERE n.layer=?",
                (layer,),
            )
            node_rels: dict[int, list[str]] = {}
            for r in rel_rows:
                node_rels.setdefault(int(r["source_id"]), []).append(str(r["relation"]))
                node_rels.setdefault(int(r["target_id"]), []).append(str(r["relation"]))
            groups: list[Group] = []
            for nid, rels in node_rels.items():
                bucket = classify_fact(rels)
                if bucket == "primary":
                    continue
                # The DELETE and the INSERT of one node are ONE group: a batch
                # boundary must never fall between them, or a failure there
                # leaves the node without its `valence:*` tag.
                groups.append(
                    [
                        ("DELETE FROM epi_tags WHERE node_id=? AND tag LIKE 'valence:%'", (nid,)),
                        ("INSERT OR IGNORE INTO epi_tags (node_id, tag) VALUES (?, ?)", (nid, f"valence:{bucket}")),
                    ]
                )
            for batch in _group_chunks(groups, _DREAM_CHUNK):
                await _apply_ops(dispatch, [op for group in batch for op in group])
                # Count what was applied, not what was planned: a failing batch
                # must not leave the report claiming tags that were never written.
                valence_tagged += len(batch)
    except Exception as exc:
        logger.warning("valence tagging failed: %s", exc)

    # G5 sanitation centrality: top degree of the layer excluding MOC/auto_index hubs (Ar9av) — for the nightly report.
    centrality_top: list[int] = []
    try:
        from lifecycle.graph_sanitation import centrality_candidates

        with _timed_phase(layer, "centrality"):
            centrality_top = (await dispatch.atomic(DB_NAME, lambda conn: centrality_candidates(conn, layer)))[:5]
    except Exception as exc:
        logger.warning("centrality top failed: %s", exc)

    # S6b behavior annotations: per-tool statistics in the report (Stage 2 hints).
    from lifecycle.tool_stats import tool_behavior_stats

    behavior: dict[str, dict[str, float]] = {}
    with contextlib.suppress(Exception), _timed_phase(layer, "behavior"):
        behavior = await tool_behavior_stats()

    # S18 item 4 orphan-anchor GC — BEFORE the dream: REM bridges isolated nodes
    # (anchors 'episode:N' tokenize identically → Jaccard 1.0 → a bridge),
    # so edge-less anchors must be removed before the sleep phase.
    with _timed_phase(layer, "orphan_gc"):
        orphaned = await _orphan_anchor_gc(dispatch, layer)

    # S18 item 9 gap-registry: question episodes + zero-result tails (best-effort).
    gap_written = 0
    gap_bumped = 0
    gap_open = 0
    try:
        from lifecycle.gap_registry import build_registry, count_open_gaps

        with _timed_phase(layer, "gap_registry"):
            stats = await build_registry(layer)
            gap_written = int(stats["written"])
            gap_bumped = int(stats.get("bumped", 0))
        # `open` is the number a reader acts on; `written` is only what this pass added.
        # Reporting the write count alone is how the registry looked alive while nothing
        # ever read it.
        gap_open = await count_open_gaps(layer)
    except Exception as exc:
        logger.debug("gap registry skipped: %s", exc)

    # C6: three-phase dream — NREM decay/prune → REM bridge → Insight abstracts.
    dream: dict[str, int] = {"nrem_decayed": 0, "nrem_pruned": 0, "rem_bridged": 0, "insights": 0}
    try:
        with _timed_phase(layer, "dream"):
            # Every phase committed its own batches; there is nothing left to commit
            # here, and a commit here would publish a handler's pending work.
            dream = await _dream(dispatch, layer)
    except Exception as exc:
        # The dream phase carries most of the dispatch writes, so it is the phase
        # most likely to hit a BUSY — and this phase used to run under a bare
        # `contextlib.suppress(Exception)`: its failure left no trace at all.
        logger.warning("dream phase failed: %s", exc)

    # C8 segment-consolidation: Lychee boundary map of the daily L0 (report).
    segment_map: dict[str, Any] = {}
    try:
        from lifecycle.segment_consolidation import segment_l0

        with _timed_phase(layer, "segments"):
            segment_map = await segment_l0(since_hours=24.0, layer=layer)
    except Exception as exc:
        logger.debug("segment map skipped: %s", exc)

    # A1.6 wiki communities (louvain over wiki_page nodes) — into the nightly
    # report for MOC hints. Degrades silently when networkx is missing.
    communities: list[dict[str, Any]] = []
    try:
        from lifecycle.wiki_communities import detect_communities

        with _timed_phase(layer, "wiki_communities"):
            res_c = await detect_communities(cm, layer=layer)
            communities = res_c["communities"][:5]
    except Exception as exc:
        logger.debug("wiki communities skipped: %s", exc)

    return {
        "nodes_cleaned": cleaned,
        "orphan_gc": orphaned,
        "gap_registry": {"written": gap_written, "bumped": gap_bumped, "open": gap_open},
        "miners": miners,
        "sanitation": {"expired": expired, "valence_tagged": valence_tagged, "centrality_top": centrality_top},
        "behavior": behavior,
        "dream": dream,
        "segments": segment_map,
        "wiki_communities": communities,
    }
