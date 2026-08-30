"""D1.10 smart context budget — weighted token distribution across memory sources.

The inject builder is first-come-first-served: one fat source starves the
rest. Here every source gets a weight-proportional floor first; leftover
budget from unfilled sources redistributes to the others in a second pass.
Weights mirror the concept's slots (§12.14: system 10 / memory 20 / file 40 /
history 30) mapped onto ariel's own sources.

Takes PRE-RESOLVED mem/rag — no mcp_server imports (module-cycle rule).
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# source → (weight, kind for the block)
WEIGHTS: dict[str, float] = {
    "important": 0.30,  # L4 facts ≥ inject.important_min
    "relevant": 0.30,  # RAG hits (needs query)
    "recent": 0.15,  # L1 chatter, 24h
    "day": 0.15,  # L3 auto_save digest, 24h
    "ops": 0.10,  # open diff_gaps (proposals ride the inject builder)
}


def _norm(content: str) -> str:
    return " ".join(str(content).split())[:80]


async def build_smart_context(
    mem: Any,
    rag: Any,
    user_id: str,
    query: str = "",
    budget: int = 2000,
) -> dict[str, Any]:
    """Weighted token allocation across memory sources. Never exceeds budget."""
    from shared.tokens import estimate_tokens

    cutoff = time.time() - 24 * 3600
    full = bool(query and query.strip())

    # ── collect candidates per source (content, base_score) ─────────────
    candidates: dict[str, list[tuple[str, float]]] = {s: [] for s in WEIGHTS}

    try:
        facts = await mem.l4.get_all(user_id, 50)
        important_min = 0.8
        from config import config as _cfg

        important_min = float(_cfg.get("inject", "important_min", default=0.8))
        candidates["important"] = [(f"{f.key}={f.value[:80]}", float(f.importance)) for f in facts if f.importance >= important_min]
    except Exception as exc:
        logger.debug("smart context read failed: %s", exc)

    if full and rag is not None:
        try:
            hits = await rag.search(query, user_id=user_id, limit=5)
            candidates["relevant"] = [
                (
                    str(h.get("content") or h.get("value") or h.get("summary") or h.get("title") or ""),
                    float(h.get("score", 0.0)),
                )
                for h in hits
            ]
            candidates["relevant"] = [(c, s) for c, s in candidates["relevant"] if c]
        except Exception as exc:
            logger.debug("smart context relevant read failed: %s", exc)

    try:
        recent = [r for r in mem.l1.get_recent(10) if float(getattr(r, "timestamp", 0)) >= cutoff]
        candidates["recent"] = [(f"{r.role}: {r.content[:80]}", 0.5) for r in recent]
    except Exception as exc:
        logger.debug("smart context read failed: %s", exc)

    try:
        day_eps = await mem.l3.search_by_tag(user_id, "auto_save", 5)
        candidates["day"] = [
            (str(getattr(e, "summary", "") or "").strip(), 0.4)
            for e in day_eps
            if float(getattr(e, "created_at", 0) or 0) >= cutoff and str(getattr(e, "summary", "")).strip()
        ]
    except Exception as exc:
        logger.debug("smart context read failed: %s", exc)

    try:
        gap_eps = await mem.l3.search_by_tag(user_id, "diff_gap", 5)
        candidates["ops"] = [
            (str(getattr(e, "summary", "") or "").strip(), 0.6)
            for e in gap_eps
            if float(getattr(e, "created_at", 0) or 0) >= cutoff and str(getattr(e, "summary", "")).strip()
        ]
    except Exception as exc:
        logger.debug("smart context read failed: %s", exc)

    # ── pass 1: fill each source up to its weight-proportional floor ────
    blocks: list[dict[str, Any]] = []
    seen: set[str] = set()  # normalized content, first source wins
    used: dict[str, int] = dict.fromkeys(WEIGHTS, 0)
    floors = {s: int(budget * w) for s, w in WEIGHTS.items()}

    def _try_add(source: str, content: str, score: float, cap: int) -> bool:
        content = str(content).strip()
        if not content:
            return False
        key = _norm(content)
        if key in seen:
            return False
        cost = estimate_tokens(content)
        if cost > cap or used[source] + cost > cap:
            return False
        seen.add(key)
        used[source] += cost
        blocks.append({"source": source, "content": content, "score": score, "tokens": cost})
        return True

    for source, cap in floors.items():
        for content, score in candidates[source]:
            if used[source] >= cap:
                break
            _try_add(source, content, score, cap)

    # ── pass 2: redistribute leftover (unfilled floors) in weight order ──
    # Ceiling = 2x floor: redistribution helps starved sources but no source
    # may exceed twice its weight share (proportionality survives).
    leftover = budget - sum(used.values())
    if leftover > 0:
        for source in sorted(WEIGHTS, key=lambda s: WEIGHTS[s], reverse=True):
            if leftover <= 0:
                break
            ceiling = max(2 * floors[source], 40)
            if used[source] >= ceiling:
                continue
            cap = min(used[source] + leftover, ceiling)
            for content, score in candidates[source]:
                if used[source] >= cap or leftover <= 0:
                    break
                before = used[source]
                if _try_add(source, content, score, cap):
                    leftover -= used[source] - before

    allocations = {s: {"floor": floors[s], "used": used[s], "weight": WEIGHTS[s]} for s in WEIGHTS}
    return {
        "blocks": blocks,
        "allocations": allocations,
        "budget": budget,
        "total_tokens": sum(used.values()),
    }
