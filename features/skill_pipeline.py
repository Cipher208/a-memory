"""D2.2 store pipeline — promote distilled episodes into skill pages.

No LLM in ariel (B1.6 cost verdict): `dream_skill` episodes are already
agent-distilled at write time (C1.12 DREAM: skill: markers), so promotion is
a structured copy with provenance. Raw auto_save chatter needs real
distillation — documented v2 ceiling (harness-side LLM).
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

_SKILL_PROMOTED_TAG = "skill_promoted"


def _skill_title(summary: str) -> str:
    """First line, clamped — good enough for a draft title (lint caps the rest)."""
    text = str(summary).strip()
    line = text.splitlines()[0] if text else ""
    return (line[:60].rstrip(" .,:;-")) or "Skill draft"


async def promote_episodes(mem: Any, wiki: Any, user_id: str, episode_ids: list[int]) -> dict[str, Any]:
    """Promote episodes into skill pages. Idempotent via the skill_promoted tag."""
    promoted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for eid in episode_ids:
        try:
            ep = await mem.l3.get_by_id(int(eid))
        except Exception:
            ep = None
        if ep is None:
            skipped.append({"episode_id": int(eid), "reason": "not_found"})
            continue
        if _SKILL_PROMOTED_TAG in (ep.tags or []):
            skipped.append({"episode_id": int(eid), "reason": "already_promoted"})
            continue
        title = _skill_title(ep.summary)
        content = f"{str(ep.summary).strip()}\n\n> promoted from episode #{int(eid)} ({time.strftime('%Y-%m-%d', time.gmtime(float(ep.created_at)))})"
        path = await wiki.add("skill", title, content, tags=["dream_skill"])
        with contextlib.suppress(Exception):
            await mem.l3.add_tag(int(eid), _SKILL_PROMOTED_TAG)
        promoted.append({"episode_id": int(eid), "title": title, "path": path})
    return {"promoted": promoted, "skipped": skipped, "count": len(promoted)}


async def auto_promote_fresh(mem: Any, wiki: Any, user_id: str, days: int = 7) -> dict[str, Any]:
    """Nightly phase: promote fresh dream_skill episodes (agent-distilled)."""
    if mem is None:
        return {"promoted": 0, "skipped": 0, "error": "no mem"}
    try:
        eps = await mem.l3.search_by_tag(user_id, "dream_skill", 20)
    except Exception as exc:
        return {"promoted": 0, "skipped": 0, "error": str(exc)}
    cutoff = time.time() - days * 86400
    fresh = [
        int(e.episode_id)
        for e in eps
        if float(getattr(e, "created_at", 0) or 0) >= cutoff and _SKILL_PROMOTED_TAG not in (getattr(e, "tags", None) or [])
    ]
    if not fresh:
        return {"promoted": 0, "skipped": 0}
    res = await promote_episodes(mem, wiki, user_id, fresh)
    return {"promoted": len(res["promoted"]), "skipped": len(res["skipped"])}
