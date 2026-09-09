"""Graph sanitation (Phase G Task 5): graph hygiene without a single LLM call.

- Lateral inhibition (SYNAPSE): weak heuristic edges are dampened by the cluster of
  stronger neighbours — û_i = max(0, u_i − β·Σ_{k∈T_M}(u_k−u_i)·𝕀[u_k>u_i]),
  β=0.15, M=7 (T_M — up to M strictly stronger neighbours).
- Validity windows: valid_from/valid_to/status on epi_edges (NULL = no expiry);
  validate_edges — O(|E|) recheck, active_edges_clause — filter for active ones.
- MAD thresholds: τ = median − κ·MAD (κ=1.5) instead of fixed per-miner cutoffs —
  robust to outlier scores.
- Valence: relation→valence dictionary → buckets
  (primary/supporting/contrasting/qualifying/superseded) for classify.
- Hub exclusion: MOC hubs/auto-indexes are excluded from centrality queries.
"""

from __future__ import annotations

import statistics
from typing import Any

# --- (a) lateral inhibition (SYNAPSE) ---

INHIBITION_BETA = 0.15
INHIBITION_TOP_M = 7

_EXCLUDED_NODE_TYPES = ("moc", "auto_index")

# --- (d) valence: relation → valence (prism types) ---

VALENCE: dict[str, str] = {
    "supports": "supporting",
    "contradicts": "contrasting",
    "supersedes": "superseded",
    "superseded_by": "superseded",
    "invalidates": "superseded",
    "refines": "qualifying",
    "qualifies": "qualifying",
    "derives_from": "qualifying",
    "sourced_from": "supporting",
}

VALENCE_BUCKETS = ("primary", "supporting", "contrasting", "qualifying", "superseded")
# Conflict-resolution order when a fact carries several valences:
# contradiction beats supersession, supersession beats qualification, qualification beats support.
_CONFLICT_ORDER = {"contrasting": 3, "superseded": 2, "qualifying": 1, "supporting": 0}


def classify_fact(relations: list[str]) -> str:
    """Map a fact's relations to a bucket. Heuristic/neutral relations = primary."""
    valences = [VALENCE[r] for r in relations if r in VALENCE]
    if not valences:
        return "primary"
    return max(sorted(set(valences)), key=lambda v: _CONFLICT_ORDER[v])


# --- (c) MAD thresholds ---


def mad_threshold(scores: list[float], kappa: float = 1.5) -> float:
    """Compute τ = median − κ·MAD; the median and MAD are robust to outlier scores.

    Empty list / degenerate case (all scores equal, MAD=0) → τ = median
    (the threshold never drops below the median, so a degenerate cluster
    cuts off only scores strictly below it — 0.0 for an empty list).
    """
    if not scores:
        return 0.0
    med = statistics.median(scores)
    mad = statistics.median(abs(s - med) for s in scores)
    return med - kappa * mad


# --- (b) validity windows ---


async def validate_edges(conn: Any, now: float | None = None) -> int:
    """Recheck edges in O(|E|): edges outside their validity window → status='expired'. Returns the number marked.

    NULL windows = no expiry (always active); revocation requires an explicit
    valid_to < now. A single UPDATE covers all rows in one pass.
    """
    ts = now if now is not None else _now()
    cur = await conn.execute(
        "UPDATE epi_edges SET status='expired'"
        " WHERE status!='expired' AND ((valid_from IS NOT NULL AND valid_from > ?) OR (valid_to IS NOT NULL AND valid_to < ?))",
        (ts, ts),
    )
    await conn.commit()
    return int(cur.rowcount or 0)


def active_edges_clause(alias: str = "") -> tuple[str, list[Any]]:
    """Build a WHERE fragment for active-edge queries: the window covers now AND the status is active.

    NULL = no expiry; status is the materialized verdict of the recheck, but
    it is checked here too: a freshly written edge with an already-expired
    window is filtered out before the first recheck.
    """
    p = f"{alias}." if alias else ""
    clause = f"({p}valid_from IS NULL OR {p}valid_from <= ?) AND ({p}valid_to IS NULL OR {p}valid_to >= ?) AND {p}status = 'active'"
    ts = _now()
    return clause, [ts, ts]


def _now() -> float:
    import time

    return time.time()


# --- (a') inhibition over a node's edges ---


async def lateral_inhibition(conn: Any, node_id: int, beta: float = INHIBITION_BETA, top_m: int = INHIBITION_TOP_M) -> int:
    """Apply SYNAPSE inhibition to a node's heuristic edges. Returns the number of changed weights.

    For edge i with weight u_i: T_M — up to M neighbouring heuristic edges of the
    node with strictly greater weight u_k; û_i = max(0, u_i − β·Σ(u_k−u_i)). Equal
    strong edges do not suppress each other (𝕀[u_k > u_i]); changed weights are
    written only when Δ > 0 (idempotence: a repeated run changes nothing).
    """
    rows = await (
        await conn.execute(
            "SELECT source_id, target_id, relation, weight FROM epi_edges WHERE (source_id=? OR target_id=?) AND tags LIKE '%heuristic:%'",
            (node_id, node_id),
        )
    ).fetchall()
    weights = [float(r["weight"]) for r in rows]
    changed = 0
    for i, r in enumerate(rows):
        u_i = weights[i]
        stronger = sorted((u for j, u in enumerate(weights) if j != i and u > u_i), reverse=True)[:top_m]
        if not stronger:
            continue
        u_new = max(0.0, u_i - beta * sum(u_k - u_i for u_k in stronger))
        if u_new < u_i:
            await conn.execute(
                "UPDATE epi_edges SET weight=? WHERE source_id=? AND target_id=? AND relation=?",
                (u_new, r["source_id"], r["target_id"], r["relation"]),
            )
            changed += 1
    if changed:
        await conn.commit()
    return changed


# --- (e) hub exclusion ---


def hub_exclusion_clause(alias: str = "") -> str:
    """Build a WHERE fragment for centrality/louvain queries: MOC/auto_index are excluded."""
    p = f"{alias}." if alias else ""
    return f"{p}node_type NOT IN ({', '.join('?' * len(_EXCLUDED_NODE_TYPES))})"


HUB_EXCLUSION_PARAMS: tuple[str, ...] = _EXCLUDED_NODE_TYPES


async def centrality_candidates(conn: Any, layer: str) -> list[int]:
    """Return the layer's nodes eligible for centrality (hubs excluded), ordered by degree."""
    clause, params = active_edges_clause("e")
    rows = await (
        await conn.execute(
            "SELECT n.node_id, COUNT(e.source_id) + COUNT(e.target_id) AS deg"
            " FROM epi_nodes n LEFT JOIN epi_edges e"
            " ON (e.source_id = n.node_id OR e.target_id = n.node_id) AND " + clause + f" WHERE n.layer = ? AND {hub_exclusion_clause('n')}"
            " GROUP BY n.node_id ORDER BY deg DESC, n.node_id",
            (*params, layer, *HUB_EXCLUSION_PARAMS),
        )
    ).fetchall()
    return [int(r["node_id"]) for r in rows]
