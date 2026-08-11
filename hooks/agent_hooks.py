"""
Agent Layer Hooks - 12 hooks for agent identity events
"""

import logging
from typing import Any

from graph.epistemic import EpistemicGraph

from .registry import hook_registry
from .shared import (
    auto_context,
    conflict_resolver,
    consolidation,
    forgetting_ritual,
    retrieval_router,
    run_async,
)

logger = logging.getLogger(__name__)


class AgentHooks:
    def __init__(self, user_id: str = "default"):
        self.user_id = user_id
        self.graph = EpistemicGraph(layer="agent")

    @hook_registry.mark("importance_gate", layer="agent")
    async def _importance_gate(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Filter agent messages by importance. Type-aware with agent keywords."""
        from shared.adaptive import adaptive_threshold
        from shared.memory_types import default_importance

        text = ctx.get("text", "")
        if not text:
            return {"importance": 0.0, "bypass": True}

        kind = ctx.get("memory_kind")
        score = default_importance(kind) if kind else 0.2

        # Agent-specific keywords
        agent_keywords = ["error", "decision", "principle", "lesson", "pattern"]
        for kw in agent_keywords:
            if kw in text.lower():
                score += 0.15
        # General heuristics
        if len(text) > 50:
            score += 0.1
        if "?" in text:
            score += 0.1

        score = min(1.0, score)

        # Update EMA
        threshold = await adaptive_threshold.get_threshold()
        await adaptive_threshold.update(score)

        return {"importance": score, "threshold": threshold, "bypass": score < threshold}

    @hook_registry.mark("error_occurred", layer="agent")
    def _error_occurred(self, ctx: dict[str, Any]) -> dict[str, Any]:
        error = ctx.get("error", "")
        node_id: str = run_async(self.graph.add_node(self.user_id, error, "error_analysis", ["error_pattern"], 0.8))
        return {"action": "error_analyzed", "node_id": node_id}

    @hook_registry.mark("decision_made", layer="agent")
    def _decision_made(self, ctx: dict[str, Any]) -> dict[str, Any]:
        decision = ctx.get("decision", "")
        rationale = ctx.get("rationale", "")
        node_id: str = run_async(self.graph.add_node(self.user_id, f"{decision}: {rationale}", "decision_log", ["decided_because"], 0.7))
        return {"action": "decision_logged", "node_id": node_id}

    @hook_registry.mark("self_correction", layer="agent")
    def _self_correction(self, ctx: dict[str, Any]) -> dict[str, Any]:
        error = ctx.get("error", "")
        fix = ctx.get("fix", "")
        node_id: str = run_async(self.graph.add_node(self.user_id, f"Error: {error} → Fix: {fix}", "correction", ["correction_pattern"], 0.6))
        return {"action": "correction_logged", "node_id": node_id}

    @hook_registry.mark("personality_shift", layer="agent")
    def _personality_shift(self, ctx: dict[str, Any]) -> dict[str, Any]:
        shift = ctx.get("shift", "")
        node_id: str = run_async(self.graph.add_node(self.user_id, shift, "personality_evolution", ["personality_trait", "evolved_to"], 0.9))
        return {"action": "personality_evolved", "node_id": node_id}

    @hook_registry.mark("emotion_context", layer="agent")
    def _emotion_context(self, ctx: dict[str, Any]) -> dict[str, Any]:
        emotion = ctx.get("emotion", "")
        context = ctx.get("context", "")
        node_id: str = run_async(self.graph.add_node(self.user_id, f"{emotion} in: {context}", "emotional_context", ["felt_in_context"], 0.6))
        return {"action": "emotion_logged", "node_id": node_id}

    @hook_registry.mark("wiki_agent", layer="agent")
    def _wiki_agent(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return {"action": "wiki_sync", "summary": ctx.get("summary", "")}

    @hook_registry.mark("consolidation", layer="both")
    def _consolidation(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return consolidation(ctx, self.user_id, min_importance=0.6, action_key="agent_consolidated")

    @hook_registry.mark("forgetting_ritual", layer="both")
    def _forgetting_ritual(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return forgetting_ritual(ctx)

    @hook_registry.mark("auto_context", layer="both")
    def _auto_context(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return auto_context(ctx, self.user_id, layer="agent")

    @hook_registry.mark("retrieval_router", layer="both")
    def _retrieval_router(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return retrieval_router(ctx, self.user_id, layer="agent")

    @hook_registry.mark("conflict_resolver", layer="both")
    def _conflict_resolver(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return conflict_resolver(ctx, self.user_id)

    @hook_registry.mark("emotion", layer="agent")
    def _emotion(self, ctx: dict[str, Any]) -> dict[str, Any]:
        emotion = ctx.get("emotion", "")
        node_id: str = run_async(self.graph.add_node(self.user_id, emotion, "emotional_context", ["felt_in_context"], 0.5))
        return {"action": "emotion_recorded", "node_id": node_id}
