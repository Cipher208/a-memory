from __future__ import annotations

"""
User Layer Hooks - 12 hooks for user memory events
"""

import contextlib
import logging
from typing import Any


from .registry import hook_registry
from .shared import (
    auto_context,
    cls_replay_hook,
    conflict_resolver,
    consolidation,
    dream_buffer_staging,
    forgetting_ritual,
    retrieval_router,
)

logger = logging.getLogger(__name__)


class UserHooks:
    def __init__(self, user_id: str = "default"):
        self.user_id = user_id
        # Emotion engine requires config, initialized via server lifecycle usually
        # For ad-hoc registration we just need the methods
        self.emotion_engine = None
        self.emotion_trigger = None

    @hook_registry.mark("message_received", layer="user")
    def _message_received(self, ctx: dict[str, Any], mem: Any | None = None) -> dict[str, Any]:
        """Store message in L1 buffer for recent context."""
        text = ctx.get("text", "")
        importance = self._calculate_importance(text)
        if mem:
            try:
                mem.l1.add("user", text, importance)
                return {"saved_to_l1": True, "importance": importance, "text": text[:100]}
            except Exception:
                return {"saved_to_l1": False, "importance": importance, "error": "add_failed"}
        return {"saved_to_l1": False, "importance": importance}

    @hook_registry.mark("message_sent", layer="user")
    def _message_sent(self, ctx: dict[str, Any], mem: Any | None = None) -> dict[str, Any]:
        text = ctx.get("text", "")
        if mem:
            try:
                mem.l1.add("assistant", text, 0.3)
                return {"saved_to_l1": True, "role": "assistant", "text": text[:100]}
            except Exception:
                return {"saved_to_l1": False, "error": "add_failed"}
        return {"saved_to_l1": False, "role": "assistant", "text": text[:100]}

    @hook_registry.mark("state_delta", layer="user")
    def _state_delta(self, ctx: dict[str, Any]) -> dict[str, Any]:
        delta = ctx.get("delta", {})
        if delta:
            return {"action": "save_episode", "summary": f"State changed: {list(delta.keys())}", "weight": 0.4}
        return {"action": "skip"}

    @hook_registry.mark("consolidation", layer="both")
    async def _consolidation(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return await consolidation(ctx, self.user_id)

    @hook_registry.mark("emotion_trigger", layer="user")
    async def _emotion_trigger(self, ctx: dict[str, Any], mem: Any | None = None) -> dict[str, Any]:
        """Evaluate emotional content and save episode if weighty."""
        if not self.emotion_trigger:
            return {"saved_episode": False, "error": "emotion_trigger_not_init"}
        text = ctx.get("text", "")
        user_id = ctx.get("user_id", "default")
        should, reason, weight = self.emotion_trigger.should_save(text)
        if should and mem:
            try:
                summary = "{}={}".format(ctx.get("key", "text"), text[:50])
                await mem.l3.save(user_id, summary, weight, [reason])
                return {"saved_episode": True, "reason": reason, "weight": weight}
            except Exception:
                return {"saved_episode": False, "reason": reason, "error": "save_failed"}
        return {"saved_episode": False, "reason": reason}

    @hook_registry.mark("nightly", layer="user")
    async def _nightly(self, ctx: dict[str, Any], mem: Any | None = None) -> dict[str, Any]:
        result = {"action": "create_diary", "summary": ctx.get("daily_summary", "")}
        with contextlib.suppress(Exception):
            result["cls_replay"] = await cls_replay_hook(ctx, self.user_id)
        with contextlib.suppress(Exception):
            from lifecycle.graph_builder import build_from_episodes

            from shared.connection import connection_manager

            result["graph_build"] = await build_from_episodes(connection_manager, self.user_id, layer="user")
        with contextlib.suppress(Exception):
            from lifecycle.wiki_graph_builder import build_from_wiki

            result["wiki_graph_build"] = await build_from_wiki(user_id=self.user_id, layer="user")
        with contextlib.suppress(Exception):
            from lifecycle.graph_enrich import graph_enrich

            result["graph_enrich"] = await graph_enrich(layer="user")
        with contextlib.suppress(Exception):
            from features.skill_pipeline import auto_promote_fresh

            from wiki.manager import WikiManager

            result["skill_promotion"] = await auto_promote_fresh(mem, WikiManager(layer="user"), self.user_id)
        with contextlib.suppress(Exception):
            from features.reflection import nightly_reflection

            result["reflection"] = nightly_reflection(mem, self.user_id)
        with contextlib.suppress(Exception):
            from features.skill_pipeline import skill_reinforce

            result["skill_reinforce"] = await skill_reinforce(WikiManager(layer="user"))
        return result

    @hook_registry.mark("importance_gate", layer="user")
    async def _importance_gate(self, ctx: dict[str, Any]) -> dict[str, Any]:
        from shared.adaptive import adaptive_threshold

        text = ctx.get("text", "")
        kind = ctx.get("memory_kind")
        score = self._calculate_importance(text, kind)
        return await adaptive_threshold.gate(score)

    @hook_registry.mark("auto_context", layer="both")
    async def _auto_context(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return await auto_context(ctx, self.user_id)

    @hook_registry.mark("forgetting_ritual", layer="both")
    async def _forgetting_ritual(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return await forgetting_ritual(ctx)

    @hook_registry.mark("retrieval_router", layer="both")
    async def _retrieval_router(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return await retrieval_router(ctx, self.user_id, include_count=True)

    @hook_registry.mark("conflict_resolver", layer="both")
    async def _conflict_resolver(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return await conflict_resolver(ctx, self.user_id)

    @hook_registry.mark("dream_buffer", layer="user")
    async def _dream_buffer(self, ctx: dict[str, Any], mem: Any | None = None) -> dict[str, Any]:
        return await dream_buffer_staging(ctx, self.user_id, cm=mem._cm if mem else None)

    def _calculate_importance(self, text: str, memory_kind: str | None = None) -> float:
        if not text:
            return 0.0

        score = self._get_base_score(memory_kind)
        score += self._get_length_bonus(text)
        score += self._get_keyword_bonus(text)
        score += self._get_structure_bonus(text)
        score += self._get_emotional_bonus(text)

        return min(1.0, score)

    def _get_base_score(self, memory_kind: str | None) -> float:
        from shared.memory_types import default_importance

        return default_importance(memory_kind) if memory_kind else 0.3

    def _get_length_bonus(self, text: str) -> float:
        bonus = 0.0
        if len(text) > 15:
            bonus += 0.15
        if len(text) > 100:
            bonus += 0.1
        return bonus

    def _get_keyword_bonus(self, text: str) -> float:
        keywords = ["important", "critical", "urgent", "preference", "favorite", "hate", "love"]
        lower_text = text.lower()
        if any(kw in lower_text for kw in keywords):
            return 0.1
        return 0.0

    def _get_structure_bonus(self, text: str) -> float:
        bonus = 0.0
        if "?" in text:
            bonus += 0.15
        if text.count("\n") > 2:
            bonus += 0.1
        return bonus

    def _get_emotional_bonus(self, text: str) -> float:
        if any(c in text for c in ["!", "?"]):
            return 0.05
        return 0.0

    # ── Phase C external events (spec S3) ──

    @hook_registry.mark("session_started", layer="user")
    async def _session_started(self, ctx: dict[str, Any], mem: Any | None = None) -> dict[str, Any]:
        """Cold start: return the critical inject block (same computation as /api/context-inject)."""
        from features.inject import build_inject_blocks

        if mem is None:
            return {"blocks": [], "error": "no_mem"}
        budget = int(ctx.get("budget", 2000))
        blocks = await build_inject_blocks(mem, ctx.get("_rag"), ctx.get("user_id", self.user_id), text=ctx.get("text", ""), budget=budget)
        return {"blocks": blocks}

    @hook_registry.mark("session_ended", layer="user")
    async def _session_ended(self, ctx: dict[str, Any], mem: Any | None = None) -> dict[str, Any]:
        """Harness sends the session summary; ariel persists it to L3.

        F-T7: also stages A5 preference/experience/lesson patterns
        (best-effort, staging proposals only).
        """
        user_id = ctx.get("user_id", self.user_id)
        result: dict[str, Any] = {}
        if mem is not None:
            texts = list(ctx.get("session_texts") or [])
            if not texts:
                l1 = getattr(mem, "l1", None)
                if l1 is not None and hasattr(l1, "get_full"):
                    texts = [e.content for e in l1.get_full()]
            if texts:
                with contextlib.suppress(Exception):
                    from features.session_close import extract_and_stage

                    result["extracted"] = await extract_and_stage(mem, user_id, texts)
        summary = (ctx.get("summary") or "").strip()
        if not summary or mem is None:
            return {"saved": False, **result}
        await mem.l3.save(user_id, summary[:500], 0.6, ["session_summary"])
        return {"saved": True, **result}

    @hook_registry.mark("post_session_diff", layer="user")
    async def _post_session_diff(self, ctx: dict[str, Any], mem: Any | None = None) -> dict[str, Any]:
        """Server-side: compute session diff and materialize gaps as L3 diff_gap episodes.

        The harness fires this event after session_ended (e.g. the daemon or the
        Hermes session-end hook). One row per gap → next session_started surfaces
        them via build_inject_blocks (gap block kind).
        """
        if mem is None:
            return {"gaps": 0, "skipped": "no_mem"}
        import time as _time

        from features.diff import compute_session_gaps

        since = float(ctx.get("since", 0))
        until = float(ctx.get("until", _time.time()))
        user_id = ctx.get("user_id", self.user_id)
        gaps = compute_session_gaps(mem, since, until)
        for g in gaps:
            summary = f"diff_gap: msg={g['source_msg_id']} score={g['score']:.2f} missing={','.join(g['missing'])} preview={g['text_preview'][:200]}"
            await mem.l3.save(user_id, summary[:500], 0.5, ["diff_gap", "auto_review"])
        return {"gaps": len(gaps), "since": since, "until": until}

    @hook_registry.mark("new_message", layer="user")
    async def _new_message(self, ctx: dict[str, Any], mem: Any | None = None, graph: Any | None = None) -> dict[str, Any]:
        """Evaluate importance of incoming text; threshold-gated auto-save."""
        from hooks.external import auto_save_text

        text = ctx.get("text", "")
        if not text or mem is None or graph is None:
            return {"auto_save": {"score": 0.0, "saved_l3": False, "saved_l4": False, "saved_graph": False}, "skipped": "no_text_or_mem"}
        result = await auto_save_text(mem, graph, ctx.get("user_id", self.user_id), text)
        return {"auto_save": result}

    @hook_registry.mark("auto_save_candidate", layer="user")
    async def _auto_save_candidate(self, ctx: dict[str, Any], mem: Any | None = None, graph: Any | None = None) -> dict[str, Any]:
        """Explicit candidate pushed by a daemon — same pipeline as new_message."""
        return await self._new_message(ctx, mem=mem, graph=graph)

    @hook_registry.mark("on_turn_end", layer="user")
    async def _on_turn_end(self, ctx: dict[str, Any], mem: Any | None = None, graph: Any | None = None) -> dict[str, Any]:
        """E14: turn-level capture — same pipeline as new_message, sync result to the caller."""
        return await self._new_message(ctx, mem=mem, graph=graph)

    @hook_registry.mark("context_threshold", layer="user")
    async def _context_threshold(self, ctx: dict[str, Any], mem: Any | None = None) -> dict[str, Any]:
        """Thin advice: eviction candidates = oldest staging entries. Decision stays harness-side."""
        recent = mem.l1.get_recent(10) if mem else []
        advice = [{"role": r.role, "content": r.content[:80]} for r in recent[:3]]
        return {"advice": "evict_oldest", "candidates": advice}

    @hook_registry.mark("memory_pressure", layer="user")
    async def _memory_pressure(self, ctx: dict[str, Any], mem: Any | None = None) -> dict[str, Any]:
        """Thin advice: L1 ring is 50 entries; suggest compression when >40."""
        size = len(mem.l1.get_full()) if mem and hasattr(mem.l1, "get_full") else 0
        return {"advice": "compress_similar" if size > 40 else "ok", "l1_size": size}

    @hook_registry.mark("post_context_compression", layer="user")
    async def _post_context_compression(self, ctx: dict[str, Any], mem: Any | None = None) -> dict[str, Any]:
        """Log the compaction drift, then rehydrate candidates: retrieval over the query."""
        from features.rehydrate import log_compaction

        logged = log_compaction(
            ctx.get("user_id", self.user_id),
            old_session_id=ctx.get("old_session_id"),
            new_session_id=ctx.get("new_session_id"),
            reason=ctx.get("reason") or "compaction",
            summary=ctx.get("query"),
        )
        # E13: semantic audit — pre-window episodes vs the L4 set rehydrate
        # injects. Payload-independent (live harnesses send no summary).
        semantic_audit: dict[str, Any] = {}
        try:
            import time as _time

            from features.semantic_audit import run_semantic_audit

            semantic_audit = await run_semantic_audit(ctx.get("user_id", self.user_id), _time.time())
        except Exception as exc:
            logger.debug("semantic audit skipped: %s", exc)
        query = ctx.get("query", "")
        rag = ctx.get("_rag")
        if not query or rag is None:
            return {"candidates": [], "logged": logged, "semantic_audit": semantic_audit}
        hits = await rag.search(query, user_id=ctx.get("user_id", self.user_id), limit=5)
        candidates = []
        for h in hits:
            content = str(h.get("content") or h.get("value") or h.get("summary") or h.get("title") or "")
            if content:
                candidates.append({"content": content, "score": float(h.get("score", 0.0))})
        return {"candidates": candidates, "logged": logged, "semantic_audit": semantic_audit}
