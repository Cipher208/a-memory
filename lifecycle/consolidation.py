from __future__ import annotations

"""
Consolidation Engine — L1→L2→L3→L4 memory promotion (async)
Type-aware promotion with memory_kind support.
"""

import contextlib
import json
import logging
import re
from typing import Any

from lifecycle.transitions import record_transition
from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME
from shared.memory_types import MemoryKind, get_policy, validate_kind

logger = logging.getLogger(__name__)

# B1.4: provenance of derived summaries. A promoted fact stores its source
# references in core_memory.metadata["parents"] as "<kind>:<id>" strings —
# a DAG (a fact may merge several parents), no schema change required.

# Transcript-shaped summaries (raw harness dumps that leaked into the episodes
# table) must never become L4 "facts" — they once produced keys like
# `ep_[{"type":_"text"...` in prod. A real summary is a prose phrase: no
# structural markup at the head, no newlines.
_TRANSCRIPT_HEAD = re.compile(r"^\s*(\[|\{|\]|\"|`|<|\||\\|#|!|~|=|\*)")


def _looks_like_transcript(summary: str) -> bool:
    if "\n" in summary[:80]:
        return True
    return bool(_TRANSCRIPT_HEAD.match(summary))


def _looks_like_dump(text: str) -> bool:
    """Narrower sibling for the auto_save write gate: raw harness dumps only.

    No newline rule — legit saved messages are freeform prose that may wrap;
    the structural heads (`[{"`... message arrays, `"[ariel recall]` echoes)
    and embedded tool_result markers are what never appears in a real memory.
    """
    if "tool_use_id" in text[:200]:
        return True
    return bool(_TRANSCRIPT_HEAD.match(text))


def _slug(text: str) -> str:
    """Filename-safe key suffix: alnum/_/-/CJK survive, punctuation collapses."""
    cleaned = re.sub(r"[^\w-]+", "_", text, flags=re.UNICODE).strip("_")
    return cleaned.lower() or "ep"


def _parent_refs(*refs: str | None) -> dict[str, Any] | None:
    parents = [r for r in refs if r]
    return {"parents": parents} if parents else None


def passes_promotion_gate(item: dict[str, Any], min_importance: float = 0.7) -> bool:
    """Would consolidate_staging promote this item — same predicate, no drift.

    Used as a PRE-gate at proposal time (2026-09-13): proposing an item that
    can never pass the apply gate creates a zombie proposal the operator must
    reject by hand (#85/#86: imp 0.5 against a 0.7 gate).
    """
    content = item.get("content", "")
    importance = float(item.get("importance", 0.7))
    kind_str = item.get("memory_kind", "fact")
    kind = MemoryKind(kind_str) if validate_kind(kind_str) else MemoryKind.FACT
    pol = get_policy(kind)

    if _looks_like_transcript(content):
        return False
    from shared.broadcast import is_status_broadcast
    from shared.dialogue import is_dialogic

    if is_dialogic(content) or is_status_broadcast(content):
        return False
    effective_threshold = (
        min_importance
        if not (pol.never_archive or kind in (MemoryKind.INSTRUCTION, MemoryKind.RULE, MemoryKind.COMMITMENT))
        else min(min_importance, 0.3)
    )
    return importance >= effective_threshold


async def stage_consolidation(
    user_id: str,
    layer: str,
    items: list[dict[str, Any]],
    min_importance: float = 0.7,
) -> int | None:
    """Propose a consolidation of the PROMOTABLE subset; None if nothing passes.

    Returns the proposal id, or (via the staging tombstone guard) an existing
    decided id when the same identity is already off the table.
    """
    promotable = [i for i in items if passes_promotion_gate(i, min_importance)]
    if not promotable:
        return None
    from features.staging import propose

    return await propose(
        "consolidation",
        "consolidate_staging",
        user_id,
        layer,
        {"items": promotable, "min_importance": min_importance},
    )


class ConsolidationEngine:
    def __init__(self, cm: AsyncConnectionManager | None = None, layer: str = "user"):
        self._cm = cm or connection_manager
        self.layer = layer

    async def consolidate_staging(
        self,
        user_id: str,
        staging_items: list[dict[str, Any]],
        min_importance: float = 0.7,
    ) -> dict[str, int]:
        """Type-aware promotion: instruction/rule/commitment pass even with low importance."""
        from core.memory import CoreMemory

        cm = CoreMemory(cm=self._cm)
        promoted = 0
        skipped = 0

        for item in staging_items:
            content = item.get("content", "")
            importance = float(item.get("importance", 0.7))
            kind_str = item.get("memory_kind", "fact")

            # Single source of truth for skip-vs-promote (2026-09-13 refactor):
            # the pre-gate at proposal time uses the same predicate.
            if not passes_promotion_gate(item, min_importance):
                logger.debug("skipping gated-out staging item from L4 promotion")
                skipped += 1
                continue

            # Audit 05.09: canonical key via the distiller (synonyms collapse,
            # kind prefix) instead of the truncated staging_{content[:30]} keys.
            from lifecycle.distiller import _canonical_key

            kind = MemoryKind(kind_str) if validate_kind(kind_str) else MemoryKind.FACT
            key = _canonical_key(content, kind)
            entry_id = await cm.save(
                user_id,
                key,
                content,
                importance=importance,
                memory_kind=kind_str,
                source="staging_promotion",
                metadata=_parent_refs(f"event:{item['event_id']}" if item.get("event_id") else None),
            )
            with contextlib.suppress(Exception):
                from_ref = f"event:{item['event_id']}" if item.get("event_id") else f"staging:{key}"
                await record_transition(self._cm, user_id, "staging", from_ref, "l4", f"core:{entry_id}", "staging_promotion")
            promoted += 1

        return {"promoted": promoted, "skipped": skipped}

    async def consolidate_episodes(
        self,
        user_id: str,
        episodic_db: str | None = None,
        min_weight: float = 0.7,
    ) -> int:
        """Promote high-weight episodes of THIS engine's layer into L4 facts.

        Idempotent: the L4 key is derived from the summary, re-runs update
        in place instead of duplicating.
        """
        from core.memory import CoreMemory

        cm = CoreMemory(cm=self._cm, layer=self.layer)
        epi_db = episodic_db or "memory.db"
        epi_conn = await self._cm.get(epi_db)
        cursor = await epi_conn.execute(
            "SELECT episode_id, summary, emotional_weight, tags FROM episodes WHERE layer=? AND user_id=? AND emotional_weight > ? ORDER BY created_at DESC LIMIT 10",
            (self.layer, user_id, min_weight),
        )
        rows = await cursor.fetchall()

        if not rows:
            return 0

        consolidated = 0
        for row in rows:
            summary = row["summary"]
            weight = row["emotional_weight"]
            if _looks_like_transcript(summary):
                logger.warning(
                    "skipping transcript-shaped episode %s from L4 promotion",
                    row["episode_id"],
                )
                continue
            # Audit 05.09 (P1): an event with live decay (question/hypothesis/
            # context) must not become an eternal L4 fact. Facts with near-zero
            # decay (fact/decision/preference/relationship + never_archive)
            # are promoted as before — kind routing is consistent with the distiller.
            from lifecycle.distiller import _canonical_key
            from shared.broadcast import is_status_broadcast
            from shared.dialogue import is_dialogic
            from shared.memory_types import kind_for_text

            kind = kind_for_text(summary)
            if get_policy(kind).decay_rate > 0.01:
                logger.debug("episode %s is event-kind (%s), stays in L3", row["episode_id"], kind.value)
                continue
            key = _canonical_key(summary, kind)
            # F1 2026-09-12: the transcript + event-kind gates miss the
            # conversational class — greetings/vocatives/questions promoted
            # verbatim filled L4 with `fact:…` chat slugs (61 rows, one base).
            # Status-broadcast follow-up: close-out echoes are not facts either.
            if is_dialogic(summary) or is_status_broadcast(summary) or key.endswith(":misc"):
                logger.debug("episode %s is conversational/unkeyable, stays in L3", row["episode_id"])
                continue
            entry_id = await cm.save(
                user_id,
                key,
                summary[:200],
                importance=weight,
                memory_kind=kind.value,
                source="episode_promotion",
                metadata=_parent_refs(f"episode:{row['episode_id']}"),
            )
            with contextlib.suppress(Exception):
                await record_transition(self._cm, user_id, "episode", f"episode:{row['episode_id']}", "l4", f"core:{entry_id}", "episode_promotion")
            consolidated += 1
        return consolidated

    async def get_lineage(self, entry_id: int) -> list[str]:
        """Return the parent references recorded for a promoted fact (B1.4)."""
        conn = await self._cm.get(DB_NAME)
        row = await (await conn.execute("SELECT metadata FROM core_memory WHERE entry_id=?", (entry_id,))).fetchone()
        if not row or not row["metadata"]:
            return []
        try:
            meta = json.loads(row["metadata"])
        except (TypeError, ValueError):
            return []
        parents = meta.get("parents", []) if isinstance(meta, dict) else []
        return [str(p) for p in parents if p]

    async def get_stats(self, user_id: str) -> dict[str, int]:
        conn = await self._cm.get(DB_NAME)
        total_cursor = await conn.execute("SELECT COUNT(*) FROM core_memory WHERE layer=? AND user_id=?", (self.layer, user_id))
        total_row = await total_cursor.fetchone()
        total = int(total_row[0]) if total_row and total_row[0] is not None else 0

        high_cursor = await conn.execute(
            "SELECT COUNT(*) FROM core_memory WHERE layer=? AND user_id=? AND importance > 0.7",
            (self.layer, user_id),
        )
        high_row = await high_cursor.fetchone()
        high = int(high_row[0]) if high_row and high_row[0] is not None else 0

        low_cursor = await conn.execute(
            "SELECT COUNT(*) FROM core_memory WHERE layer=? AND user_id=? AND importance < 0.3",
            (self.layer, user_id),
        )
        low_row = await low_cursor.fetchone()
        low = int(low_row[0]) if low_row and low_row[0] is not None else 0
        return {"total": total, "high_importance": high, "low_importance": low}
