"""D2.2 store pipeline — promote distilled episodes into skill pages.

No LLM in ariel (B1.6 cost verdict): `dream_skill` episodes are already
agent-distilled at write time (C1.12 DREAM: skill: markers), so promotion is
a structured copy with provenance. Raw auto_save chatter needs real
distillation — documented v2 ceiling (harness-side LLM).
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_SKILL_PROMOTED_TAG = "skill_promoted"


def _skill_title(summary: str) -> str:
    """First line, clamped — good enough for a draft title (lint caps the rest)."""
    text = str(summary).strip()
    line = text.splitlines()[0] if text else ""
    return (line[:60].rstrip(" .,:;-")) or "Skill draft"


async def _existing_skill_path(wiki: Any, title: str) -> str | None:
    """Path of an existing skill page matching this title, if any.

    Checks the full title path, then the topic prefix (text before ":") —
    "Deploy Flow: also check WAL" evolves the existing "Deploy Flow" page.
    """

    def _path_for(t: str) -> str:
        safe = "".join(c if c.isalnum() or c in " _-" else "_" for c in t).strip().replace(" ", "_")
        return f"skill/{safe}.md"

    candidates = [title]
    if ":" in title:
        candidates.insert(0, title.split(":", 1)[0].strip())
    for cand in candidates:
        try:
            if await wiki.get(_path_for(cand)) is not None:
                return _path_for(cand)
        except Exception as exc:
            logger.debug("skill path check failed for %s: %s", cand, exc)
            continue
    return None


async def promote_episodes(mem: Any, wiki: Any, user_id: str, episode_ids: list[int]) -> dict[str, Any]:
    """Promote episodes into skill pages. Idempotent via the skill_promoted tag.

    D2.4 evolution: when a skill page with the same title already exists, the
    new insight MERGES into it (appended line) instead of forking a duplicate.
    """
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
        insight = f"- {str(ep.summary).strip()} (episode #{int(eid)})"
        existing = await _existing_skill_path(wiki, title)
        try:
            if existing:
                page = await wiki.get(existing)
                merged = f"{page.content.rstrip()}\n{insight}"
                await wiki.update(existing, content=merged)
                path = existing
                mode = "merged"
            else:
                content = f"{str(ep.summary).strip()}\n\n{insight}"
                path = await wiki.add("skill", title, content, tags=["dream_skill"])
                mode = "created"
            with contextlib.suppress(Exception):
                await mem.l3.add_tag(int(eid), _SKILL_PROMOTED_TAG)
            promoted.append({"episode_id": int(eid), "title": title, "path": path, "mode": mode})
        except Exception as exc:
            skipped.append({"episode_id": int(eid), "reason": f"save_failed: {exc!r}"})
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


async def skill_reinforce(wiki: Any, window_hours: int = 24) -> dict[str, Any]:
    """Nightly phase (D2.4): skills read in the window gain importance +0.05.

    Usage-driven evolution: `wiki_read` logs `skill_read` audit rows; this
    phase turns reads into reinforcement (wiki.update importance, cap 1.0).
    A second run in the same window finds no NEW reads → no-op.
    """
    from shared.constants import DB_NAME

    from shared.connection import connection_manager

    conn = await connection_manager.get(DB_NAME)
    cutoff = time.time() - window_hours * 3600
    rows = await (
        await conn.execute(
            """SELECT target_id, MAX(timestamp) AS last_read FROM audit_log
               WHERE action='skill_read' AND layer='wiki' AND timestamp > ?
               GROUP BY target_id""",
            (cutoff,),
        )
    ).fetchall()
    reinforced = 0
    errors: list[str] = []
    for r in rows:
        path = str(r["target_id"])
        entry = await wiki.get(path)
        if entry is None:
            errors.append(f"get returned None for {path!r}")
            continue
        # reinforcement writes the file (mtime) — only reads NEWER than the
        # page's own last write count, so a second run is a no-op.
        # (entry.updated_at is parse-time now(), not the persisted value.)
        from shared.path_safety import safe_resolve

        mtime = safe_resolve(wiki.base_dir, path).stat().st_mtime
        if float(r["last_read"]) <= mtime:
            continue
        old = float(entry.importance)
        new = min(1.0, old + 0.05)
        if new <= old:
            continue
        try:
            await wiki.update(path, importance=new)
            reinforced += 1
        except Exception as exc:
            errors.append(f"{path}: {exc!r}")
    return {"reinforced": reinforced, "candidates": len(rows), "errors": errors}
