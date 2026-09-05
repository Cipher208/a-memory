"""Graph enrich orchestrator (Phase G Task 1): node pre-cleanup + miner skeleton.

Pre-cleanup sweeps fact-nodes whose content is raw JSON / tool output / recall
dumps out of epi_nodes — each is first captured to the append-only l0_journal
(event='graph_cleanup') so nothing is lost, then deleted with its edges/tags.
Miners (Tasks 2-5) plug into MINERS from lifecycle.graph_miners; stubs
contribute no edges yet.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

from shared.connection import connection_manager
from shared.constants import DB_NAME

# Content markers of raw-harness junk that must never live as graph nodes.
_JUNK_LIKE = ("[{%", "%tool_use_id%", "%[ariel recall]%")

# --- C6: трёхфазный dream (SYNAPSE §5) ---
NREM_DECAY = 0.01  # −0.01 неактивным heuristic-рёбрам старше 30 дней
NREM_BOOST = 0.05  # +0.05 свежим со-сработавшим (моложе суток)
NREM_FLOOR = 0.05  # вес ниже floor — ребро не пережило сон (prune)
NREM_STALE_DAYS = 30.0
NREM_FRESH_DAYS = 1.0
REM_SIM_THRESHOLD = 0.7  # similarity изолированного узла к похожему
REM_WEIGHT_SCALE = 0.3  # weight моста = sim × 0.3
INSIGHT_MAX = 10  # абстракций за один сон


async def _rows(conn: Any, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
    return await (await conn.execute(sql, params)).fetchall()


async def _dream_nrem(conn: Any, now: float) -> dict[str, int]:
    """NREM: spreading activation — свежие со-сработавшие heuristic-рёбра
    +0.05, старые неактивные −0.01; weight < floor — prune."""
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
    """REM: мосты изолированных узлов к похожим несвязанным, weight = sim × 0.3.

    Similarity — token Jaccard (len ≥ 3, rag.edm.tokens): детерминирован и не
    зависит от hash-фолбэка эмбеддингов (случайные векторы ≠ поhash — модельные
    косинусы на near-dup непредсказуемы)."""
    from rag.edm import tokens

    isolated = [(int(r["node_id"]), tokens(str(r["content"]))) for r in await _rows(
        conn,
        "SELECT node_id, content FROM epi_nodes n WHERE n.layer=? AND node_type NOT IN ('moc','auto_index')"
        " AND NOT EXISTS (SELECT 1 FROM epi_edges e WHERE e.source_id=n.node_id OR e.target_id=n.node_id)",
        (layer,),
    )]
    if not isolated:
        return 0
    others = [(int(r["node_id"]), tokens(str(r["content"]))) for r in await _rows(conn, "SELECT node_id, content FROM epi_nodes WHERE layer=?", (layer,))]
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
    """Insight: BFS-комьюнити (связные компоненты) → материализованные
    абстракции — узлы node_type='insight' с частотным топ-токенами summary,
    связанные с членами ребром 'insight_of'."""
    from collections import Counter

    from rag.edm import tokens

    nodes = [(int(r["node_id"]), str(r["user_id"]), str(r["content"])) for r in await _rows(
        conn, "SELECT node_id, user_id, content FROM epi_nodes WHERE layer=? AND node_type NOT IN ('moc','auto_index','insight')", (layer,)
    )]
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
        dup = await (await conn.execute(
            "SELECT node_id FROM epi_nodes WHERE layer=? AND node_type='insight' AND content=? LIMIT 1", (layer, summary)
        )).fetchone()
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
    """Трёхфазный dream: NREM (spreading activation) → REM (мосты) → Insight
    (материализованные абстракции)."""
    nrem = await _dream_nrem(conn, time.time())
    rem = await _dream_rem(conn, layer, time.time())
    insights = await _dream_insight(conn, layer)
    await conn.commit()
    return {"nrem_decayed": nrem["decayed"] + nrem["boosted"], "nrem_pruned": nrem["pruned"], "rem_bridged": rem, "insights": insights}


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

    miners: dict[str, dict[str, int]] = {}
    for name, miner in MINERS.items():
        try:
            res = await miner(cm, layer)
            miners[name] = {"edges": int(res.get("edges", 0))}
        except Exception:
            miners[name] = {"edges": 0}

    # G5 sanitation: validity recheck (рёбра вне окна → status='expired').
    from lifecycle.graph_sanitation import validate_edges

    expired = await validate_edges(conn)

    # S6b behavior-аннотации: per-tool статистика в отчёте (Stage 2 hints).
    from lifecycle.tool_stats import tool_behavior_stats

    behavior: dict[str, dict[str, float]] = {}
    with contextlib.suppress(Exception):
        behavior = await tool_behavior_stats()

    # C6: трёхфазный dream — NREM decay/prune → REM bridge → Insight abstracts.
    dream: dict[str, int] = {"nrem_decayed": 0, "nrem_pruned": 0, "rem_bridged": 0, "insights": 0}
    with contextlib.suppress(Exception):
        dream = await _dream(conn, layer)

    return {
        "nodes_cleaned": cleaned,
        "miners": miners,
        "sanitation": {"expired": expired},
        "behavior": behavior,
        "dream": dream,
    }
