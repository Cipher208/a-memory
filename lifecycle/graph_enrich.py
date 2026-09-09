"""Graph enrich orchestrator (Phase G Task 1): node pre-cleanup + miner skeleton.

Pre-cleanup sweeps fact-nodes whose content is raw JSON / tool output / recall
dumps out of epi_nodes — each is first captured to the append-only l0_journal
(event='graph_cleanup') so nothing is lost, then deleted with its edges/tags.
Miners (Tasks 2-5) plug into MINERS from lifecycle.graph_miners; stubs
contribute no edges yet.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

from lifecycle.graph_sanitation import HUB_EXCLUSION_PARAMS, hub_exclusion_clause
from shared.connection import connection_manager
from shared.constants import DB_NAME

logger = logging.getLogger(__name__)

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


async def _rows(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
    rows: list[Any] = await (await conn.execute(sql, params)).fetchall()
    return rows


async def _dream_nrem(conn: Any, now: float) -> dict[str, int]:
    """Run the NREM spreading activation pass.

    Freshly co-fired heuristic edges +0.05, stale inactive ones −0.01;
    weight < floor — prune.
    """
    decayed = pruned = boosted = 0
    for r in await _rows(conn, "SELECT source_id, target_id, relation, weight, created_at FROM epi_edges WHERE tags LIKE '%heuristic:%'"):
        w = float(r["weight"])
        age_days = (now - float(r["created_at"])) / 86400.0
        key = (r["source_id"], r["target_id"], r["relation"])
        if age_days > NREM_STALE_DAYS:
            w2 = w - NREM_DECAY
            if w2 < NREM_FLOOR:
                await conn.execute("DELETE FROM epi_edges WHERE source_id=? AND target_id=? AND relation=?", key)
                pruned += 1
            else:
                await conn.execute("UPDATE epi_edges SET weight=? WHERE source_id=? AND target_id=? AND relation=?", (w2, *key))
                decayed += 1
        elif age_days <= NREM_FRESH_DAYS:
            await conn.execute("UPDATE epi_edges SET weight=? WHERE source_id=? AND target_id=? AND relation=?", (w + NREM_BOOST, *key))
            boosted += 1
    return {"decayed": decayed, "pruned": pruned, "boosted": boosted}


async def _dream_rem(conn: Any, layer: str, now: float) -> int:
    """Build REM bridges: isolated nodes to similar unlinked ones, weight = sim × 0.3.

    Similarity — token Jaccard (len ≥ 3, rag.edm.tokens): deterministic and
    independent of the embedding hash fallback (random vectors never match by
    hash — model cosines on near-dups are unpredictable).
    """
    from rag.edm import tokens

    isolated = [
        (int(r["node_id"]), tokens(str(r["content"])))
        for r in await _rows(
            conn,
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
        for r in await _rows(
            conn,
            "SELECT node_id, content FROM epi_nodes n WHERE n.layer=? AND " + hub_exclusion_clause("n"),
            (layer, *HUB_EXCLUSION_PARAMS),
        )
    ]
    bridged = 0
    seen: set[tuple[int, int]] = set()
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
            await conn.execute(
                "INSERT OR IGNORE INTO epi_edges (source_id, target_id, relation, weight, created_at, tags)"
                " VALUES (?, ?, 'dream_bridge', ?, ?, '[\"dream:rem\"]')",
                (a, b, best_sim * REM_WEIGHT_SCALE, now),
            )
            bridged += 1
    return bridged


async def _dream_insight(conn: Any, layer: str) -> int:
    """Build insight abstractions from BFS communities.

    Connected components become node_type='insight' nodes with a top-token
    frequency summary, linked to their members via 'insight_of' edges.
    """
    from collections import Counter

    from rag.edm import tokens

    nodes = [
        (int(r["node_id"]), str(r["user_id"]), str(r["content"]))
        for r in await _rows(
            conn,
            "SELECT node_id, user_id, content FROM epi_nodes n WHERE n.layer=? AND " + hub_exclusion_clause("n") + " AND n.node_type != 'insight'",
            (layer, *HUB_EXCLUSION_PARAMS),
        )
    ]
    member_ids = {nid for nid, _, _ in nodes}
    adj: dict[int, set[int]] = {}
    for r in await _rows(conn, "SELECT source_id, target_id FROM epi_edges"):
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
        dup = await (
            await conn.execute("SELECT node_id FROM epi_nodes WHERE layer=? AND node_type='insight' AND content=? LIMIT 1", (layer, summary))
        ).fetchone()
        if dup:
            continue
        user_id = next(u for nid, u, _ in nodes if nid == members[0])
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
        created += 1
    return created


import time


async def _dream(conn: Any, layer: str) -> dict[str, int]:
    """Run the three-phase sleep: NREM → REM → Insight.

    NREM — spreading activation, REM — bridges, Insight — materialized
    abstractions.
    """
    nrem = await _dream_nrem(conn, time.time())
    rem = await _dream_rem(conn, layer, time.time())
    insights = await _dream_insight(conn, layer)
    await conn.commit()
    return {"nrem_decayed": nrem["decayed"] + nrem["boosted"], "nrem_pruned": nrem["pruned"], "rem_bridged": rem, "insights": insights}


# S18 item 4 (Memora cue-anchor prune): grace window for anchors — the miner may not have reached them yet.
_ORPHAN_MIN_AGE_DAYS = 7


async def _orphan_anchor_gc(conn: Any, layer: str) -> int:
    """Prune derived anchors 'episode:N' (fact/question) that are edge-less and older than 7d (S18 item 4).

    Miners create anchor nodes (provenance 'episode:N', zero-result question);
    when the source record is cleaned up, the node dangles with no incoming
    edges — junk. Nodes with any edge survive; fresh ones (<7d) are untouched.
    """
    cutoff = time.time() - _ORPHAN_MIN_AGE_DAYS * 86400
    cur = await conn.execute(
        "SELECT node_id FROM epi_nodes n"
        " WHERE n.layer=? AND n.node_type IN ('fact', 'question')"
        " AND n.content LIKE 'episode:%'"
        " AND n.created_at < ?"
        " AND NOT EXISTS (SELECT 1 FROM epi_edges e WHERE e.source_id = n.node_id OR e.target_id = n.node_id)",
        (layer, cutoff),
    )
    ids = [int(r["node_id"]) for r in await cur.fetchall()]
    if not ids:
        return 0
    from graph.epistemic import EpistemicGraph

    return await EpistemicGraph(cm=connection_manager, layer=layer).delete_nodes(ids)


async def graph_enrich(layer: str = "user") -> dict[str, Any]:
    """Pre-clean JSON junk from the graph, then run miners. Returns stats."""
    from graph.epistemic import EpistemicGraph
    from lifecycle.graph_miners import MINERS
    from shared.l0 import capture

    cm = connection_manager
    conn = await cm.get(DB_NAME)
    junk = await (
        await conn.execute(
            f"SELECT node_id, user_id, content FROM epi_nodes"
            f" WHERE layer=? AND node_type='fact'"
            f" AND (content LIKE {' OR content LIKE '.join(['?'] * len(_JUNK_LIKE))})",
            (layer, *_JUNK_LIKE),
        )
    ).fetchall()

    cleaned = 0
    ids: list[int] = []
    for row in junk:
        # capture() never raises; junk must reach L0 before its node is gone.
        await capture(event="graph_cleanup", layer=layer, user_id=str(row["user_id"]), text=str(row["content"]))
        ids.append(int(row["node_id"]))
    if ids:
        cleaned = await EpistemicGraph(cm=cm, layer=layer).delete_nodes(ids)

    miners: dict[str, dict[str, Any]] = {}
    for name, miner in MINERS.items():
        try:
            res = await miner(cm, layer)
            miners[name] = {"edges": int(res.get("edges", 0))}
        except Exception as exc:
            # Audit 05.09 (P0): a silently failed miner means the graph quietly
            # misses edges. Log it and reflect it in the report so diagnose sees it.
            logger.warning("miner %s failed: %s", name, exc)
            miners[name] = {"edges": -1, "error": str(exc)[:200]}  # -1 = failure (edge counts are non-negative)

    # S17 addendum 11: HDBSCAN clusters of MIB vectors + cross-check against louvain — a
    # report section behind a flag (graph.embed_clusters, default off — the report shape stays stable).
    from config import config

    if bool(config.get("graph", "embed_clusters", default=False)):
        with contextlib.suppress(Exception):
            from lifecycle.embedding_clusters import cluster_embeddings

            cluster_report = await cluster_embeddings(cm, layer=layer)
            if cluster_report.get("clusters"):
                miners["embedding"]["clusters"] = cluster_report["clusters"]

    # G5 sanitation: validity recheck (edges outside the window → status='expired').
    from lifecycle.graph_sanitation import validate_edges

    expired = await validate_edges(conn)

    # G5 sanitation valence: fact nodes are classified by the valence of their edges
    # (classify_fact) → 'valence:<bucket>' tag ('primary' is not tagged — the default).
    valence_tagged = 0
    try:
        from lifecycle.graph_sanitation import classify_fact

        rel_rows = await _rows(
            conn,
            "SELECT e.source_id, e.target_id, e.relation FROM epi_edges e"
            " JOIN epi_nodes n ON n.node_id = e.source_id OR n.node_id = e.target_id"
            " WHERE n.layer=?",
            (layer,),
        )
        node_rels: dict[int, list[str]] = {}
        for r in rel_rows:
            node_rels.setdefault(int(r["source_id"]), []).append(str(r["relation"]))
            node_rels.setdefault(int(r["target_id"]), []).append(str(r["relation"]))
        for nid, rels in node_rels.items():
            bucket = classify_fact(rels)
            if bucket == "primary":
                continue
            await conn.execute("DELETE FROM epi_tags WHERE node_id=? AND tag LIKE 'valence:%'", (nid,))
            await conn.execute("INSERT OR IGNORE INTO epi_tags (node_id, tag) VALUES (?, ?)", (nid, f"valence:{bucket}"))
            valence_tagged += 1
        await conn.commit()
    except Exception as exc:
        logger.warning("valence tagging failed: %s", exc)

    # G5 sanitation centrality: top degree of the layer excluding MOC/auto_index hubs (Ar9av) — for the nightly report.
    centrality_top: list[int] = []
    try:
        from lifecycle.graph_sanitation import centrality_candidates

        centrality_top = (await centrality_candidates(conn, layer))[:5]
    except Exception as exc:
        logger.warning("centrality top failed: %s", exc)

    # S6b behavior annotations: per-tool statistics in the report (Stage 2 hints).
    from lifecycle.tool_stats import tool_behavior_stats

    behavior: dict[str, dict[str, float]] = {}
    with contextlib.suppress(Exception):
        behavior = await tool_behavior_stats()

    # S18 item 4 orphan-anchor GC — BEFORE the dream: REM bridges isolated nodes
    # (anchors 'episode:N' tokenize identically → Jaccard 1.0 → a bridge),
    # so edge-less anchors must be removed before the sleep phase.
    orphaned = await _orphan_anchor_gc(conn, layer)

    # S18 item 9 gap-registry: question episodes + zero-result tails (best-effort).
    gap_written = 0
    try:
        from lifecycle.gap_registry import build_registry

        gap_written = int((await build_registry(layer))["written"])
    except Exception as exc:
        logger.debug("gap registry skipped: %s", exc)

    # C6: three-phase dream — NREM decay/prune → REM bridge → Insight abstracts.
    dream: dict[str, int] = {"nrem_decayed": 0, "nrem_pruned": 0, "rem_bridged": 0, "insights": 0}
    with contextlib.suppress(Exception):
        dream = await _dream(conn, layer)

    # C8 segment-consolidation: Lychee boundary map of the daily L0 (report).
    segment_map: dict[str, Any] = {}
    try:
        from lifecycle.segment_consolidation import segment_l0

        segment_map = await segment_l0(since_hours=24.0, layer=layer)
    except Exception as exc:
        logger.debug("segment map skipped: %s", exc)

    # A1.6 wiki communities (louvain over wiki_page nodes) — into the nightly
    # report for MOC hints. Degrades silently when networkx is missing.
    communities: list[dict[str, Any]] = []
    try:
        from lifecycle.wiki_communities import detect_communities

        res_c = await detect_communities(cm, layer=layer)
        communities = res_c["communities"][:5]
    except Exception as exc:
        logger.debug("wiki communities skipped: %s", exc)

    return {
        "nodes_cleaned": cleaned,
        "orphan_gc": orphaned,
        "gap_registry": {"written": gap_written},
        "miners": miners,
        "sanitation": {"expired": expired, "valence_tagged": valence_tagged, "centrality_top": centrality_top},
        "behavior": behavior,
        "dream": dream,
        "segments": segment_map,
        "wiki_communities": communities,
    }
