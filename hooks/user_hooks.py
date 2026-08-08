"""
User Layer Hooks - 12 hooks for user memory events
"""

import asyncio
from typing import Any

from lifecycle.emotion import EmotionTrigger, EmotionEngine

from .registry import hook_registry
from .shared import (
    auto_context,
    conflict_resolver,
    consolidation,
    forgetting_ritual,
    retrieval_router,
)


class UserHooks:
    def __init__(self, user_id: str = "default"):
        self.user_id = user_id
        self.emotion_engine = EmotionEngine()
        self.emotion_trigger = EmotionTrigger(self.emotion_engine)
        self._register_all()

    def _register_all(self):
        hook_registry.register("message_received", self._message_received, layer="user")
        hook_registry.register("message_sent", self._message_sent, layer="user")
        hook_registry.register("state_delta", self._state_delta, layer="user")
        hook_registry.register("consolidation", self._consolidation, layer="both")
        hook_registry.register("emotion_trigger", self._emotion_trigger, layer="user")
        hook_registry.register("nightly", self._nightly, layer="user")
        hook_registry.register("importance_gate", self._importance_gate, layer="user")
        hook_registry.register("auto_context", self._auto_context, layer="both")
        hook_registry.register("forgetting_ritual", self._forgetting_ritual, layer="both")
        hook_registry.register("retrieval_router", self._retrieval_router, layer="both")
        hook_registry.register("conflict_resolver", self._conflict_resolver, layer="both")
        hook_registry.register("dream_buffer", self._dream_buffer, layer="user")

    def _message_received(self, ctx: dict[str, Any], mem=None) -> dict[str, Any]:
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

    def _message_sent(self, ctx: dict[str, Any], mem=None) -> dict[str, Any]:
        text = ctx.get("text", "")
        if mem:
            try:
                mem.l1.add("assistant", text, 0.3)
                return {"saved_to_l1": True, "role": "assistant", "text": text[:100]}
            except Exception:
                return {"saved_to_l1": False, "error": "add_failed"}
        return {"saved_to_l1": False, "role": "assistant", "text": text[:100]}

    def _state_delta(self, ctx: dict[str, Any]) -> dict[str, Any]:
        delta = ctx.get("delta", {})
        if delta:
            return {"action": "save_episode", "summary": f"State changed: {list(delta.keys())}", "weight": 0.4}
        return {"action": "skip"}

    def _consolidation(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return consolidation(ctx, self.user_id)

    def _emotion_trigger(self, ctx: dict[str, Any], mem=None) -> dict[str, Any]:
        """Evaluate emotional content and save episode if weighty."""
        text = ctx.get("text", "")
        user_id = ctx.get("user_id", "default")
        should, reason, weight = self.emotion_trigger.should_save(text)
        if should and mem:
            try:
                summary = "{}={}".format(ctx.get("key", "text"), text[:50])
                asyncio.run(mem.l3.save(user_id, summary, weight, [reason]))
                return {"saved_episode": True, "reason": reason, "weight": weight}
            except Exception:
                return {"saved_episode": False, "reason": reason, "error": "save_failed"}
        return {"saved_episode": False, "reason": reason}

    def _nightly(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return {"action": "create_diary", "summary": ctx.get("daily_summary", "")}

    async def _importance_gate(self, ctx: dict[str, Any]) -> dict[str, Any]:
        from shared.adaptive import adaptive_threshold
        text = ctx.get("text", "")
        kind = ctx.get("memory_kind")
        score = self._calculate_importance(text, kind)

        # Update EMA with new score
        threshold = await adaptive_threshold.get_threshold()
        await adaptive_threshold.update(score)

        return {"importance": score, "threshold": threshold, "bypass": score < threshold}

    def _auto_context(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return auto_context(ctx, self.user_id)

    def _forgetting_ritual(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return forgetting_ritual(ctx)

    def _retrieval_router(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return retrieval_router(ctx, self.user_id, include_count=True)

    def _conflict_resolver(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return conflict_resolver(ctx, self.user_id)

    def _dream_buffer(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return {"action": "add_to_staging", "content": ctx.get("text", "")}

    def _calculate_importance(self, text: str, memory_kind: str | None = None) -> float:
        from shared.memory_types import default_importance

        if not text:
            return 0.0

        # Start with type-based importance if kind specified
        score = default_importance(memory_kind) if memory_kind else 0.3

        # Length heuristics
        if len(text) > 15:
            score += 0.15
        if len(text) > 100:
            score += 0.1
        # Semantic keywords
        keywords = ["important", "critical", "urgent", "preference", "favorite", "hate", "love"]
        for kw in keywords:
            if kw in text.lower():
                score += 0.1
                break
        # Structure signals
        if "?" in text:
            score += 0.15
        if text.count("\n") > 2:
            score += 0.1
        # Emotional markers
        if any(c in text for c in ["!", "?"]):
            score += 0.05
        return min(1.0, score)
