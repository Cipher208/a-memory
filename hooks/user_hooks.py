from __future__ import annotations

"""
User Layer Hooks - 12 hooks for user memory events
"""

import contextlib
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
    async def _nightly(self, ctx: dict[str, Any]) -> dict[str, Any]:
        result = {"action": "create_diary", "summary": ctx.get("daily_summary", "")}
        with contextlib.suppress(Exception):
            result["cls_replay"] = await cls_replay_hook(ctx, self.user_id)
        with contextlib.suppress(Exception):
            from lifecycle.graph_builder import build_from_episodes

            from shared.connection import connection_manager

            result["graph_build"] = await build_from_episodes(connection_manager, self.user_id, layer="user")
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
