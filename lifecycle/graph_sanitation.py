"""Graph sanitation (Phase G Task 5): гигиена графа без единого LLM-вызова.

- Lateral inhibition (SYNAPSE): слабые heuristic-рёбра гасятся кластером более
  сильных соседей — û_i = max(0, u_i − β·Σ_{k∈T_M}(u_k−u_i)·𝕀[u_k>u_i]),
  β=0.15, M=7 (T_M — до M соседей строго сильнее).
- Validity windows: valid_from/valid_to/status на epi_edges (NULL = бессрочно);
  validate_edges — O(|E|) recheck, active_edges_clause — фильтр активных.
- MAD-пороги: τ = median − κ·MAD (κ=1.5) вместо fixed per-miner cutoffs —
  устойчивы к outlier-скорам.
- Valence: словарь relation→валентность → buckets
  (primary/supporting/contrasting/qualifying/superseded) для classify.
- Hub exclusion: MOC-хабы/auto-indexes исключены из centrality-запросов.
"""

from __future__ import annotations

import statistics
from typing import Any

# --- (a) lateral inhibition (SYNAPSE) ---

INHIBITION_BETA = 0.15
INHIBITION_TOP_M = 7

_EXCLUDED_NODE_TYPES = ("moc", "auto_index")

# --- (d) valence: relation → валентность (prism-типы) ---

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
# Порядок разрешения конфликтов при нескольких валентностях на факте:
# противоречие важнее замещения, замещение важнее уточнения, то важнее поддержки.
_CONFLICT_ORDER = {"contrasting": 3, "superseded": 2, "qualifying": 1, "supporting": 0}


def classify_fact(relations: list[str]) -> str:
    """Отношения факта → bucket. Эвристические/нейтральные отношения = primary."""
    valences = [VALENCE[r] for r in relations if r in VALENCE]
    if not valences:
        return "primary"
    return max(sorted(set(valences)), key=lambda v: _CONFLICT_ORDER[v])


# --- (c) MAD-пороги ---


def mad_threshold(scores: list[float], kappa: float = 1.5) -> float:
    """τ = median − κ·MAD; медиана и MAD устойчивы к outlier-скорам.

    Пустой список / вырожденный (все скоры равны, MAD=0) → τ = median
    (порог не опускается ниже медианы, поэтому вырожденный кластер
    отсекает только скоры строго ниже неё — 0.0 для пустого).
    """
    if not scores:
        return 0.0
    med = statistics.median(scores)
    mad = statistics.median(abs(s - med) for s in scores)
    return med - kappa * mad


# --- (b) validity windows ---


async def validate_edges(conn: Any, now: float | None = None) -> int:
    """O(|E|) recheck: рёбра вне validity-окна → status='expired'. Возвращает число помеченных.

    NULL-окна = бессрочно (всегда active); отозвать можно только явным
    valid_to < now. Oдиночный UPDATE покрывает все строки за один проход.
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
    """WHERE-фрагмент для active-запросов: окно покрывает сейчас И статус active.

    NULL = бессрочно; status — материализованный вердикт recheck'а, но
    проверяется и здесь: свежезаписанное ребро с истёкшим окном отсекается
    до первого recheck'а.
    """
    p = f"{alias}." if alias else ""
    clause = f"({p}valid_from IS NULL OR {p}valid_from <= ?) AND ({p}valid_to IS NULL OR {p}valid_to >= ?) AND {p}status = 'active'"
    ts = _now()
    return clause, [ts, ts]


def _now() -> float:
    import time

    return time.time()


# --- (a') inhibition поверх рёбер узла ---


async def lateral_inhibition(conn: Any, node_id: int, beta: float = INHIBITION_BETA, top_m: int = INHIBITION_TOP_M) -> int:
    """Применить SYNAPSE-ингибицию к heuristic-рёбрам узла. Возвращает число изменённых весов.

    Для ребра i с весом u_i: T_M — до M соседних heuristic-рёбер узла со
    строго большим весом u_k; û_i = max(0, u_i − β·Σ(u_k−u_i)). Равные
    сильные друг друга не давят (𝕀[u_k > u_i]); изменённые веса пишутся
    только при Δ > 0 (идемпотентность: повторный прогон не меняет).
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
    """Фрагмент WHERE для centrality/louvain-запросов: MOC/auto_index не участвуют."""
    p = f"{alias}." if alias else ""
    return f"{p}node_type NOT IN ({', '.join('?' * len(_EXCLUDED_NODE_TYPES))})"


HUB_EXCLUSION_PARAMS: tuple[str, ...] = _EXCLUDED_NODE_TYPES


async def centrality_candidates(conn: Any, layer: str) -> list[int]:
    """Узлы слоя, допущенные к centrality (хабы исключены), упорядоченные по degree."""
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
