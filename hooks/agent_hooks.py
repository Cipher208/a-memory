"""Agent Layer Hooks - 12 hooks for agent identity events."""

import contextlib
import logging
from typing import Any

from graph.epistemic import EpistemicGraph

from .registry import hook_registry
from .shared import (
    auto_context,
    conflict_resolver,
    consolidation,
    dream_buffer_staging,
    forgetting_ritual,
    retrieval_router,
)

from shared.constants import DEFAULT_USER, AGENT_LAYER

logger = logging.getLogger(__name__)


class AgentHooks:
    def __init__(self, user_id: str = DEFAULT_USER):
        self.user_id = user_id
        self.graph = EpistemicGraph(layer=AGENT_LAYER)

    @hook_registry.mark("nightly", layer=AGENT_LAYER)
    async def _nightly(self, ctx: dict[str, Any]) -> dict[str, Any]:
        """Agent-layer nightly maintenance (mirror user_hooks._nightly).

        graph_enrich + wiki-graph на agent-слое; compact/sweep/bridge делает
        backup_cron для обоих слоёв — здесь agent-специфика.
        """
        result: dict[str, Any] = {"action": "agent_nightly"}
        with contextlib.suppress(Exception):
            from lifecycle.graph_enrich import graph_enrich

            result["graph_enrich"] = await graph_enrich(layer=AGENT_LAYER)
        with contextlib.suppress(Exception):
            from lifecycle.wiki_graph_builder import build_from_wiki

            result["wiki_graph_build"] = await build_from_wiki(layer=AGENT_LAYER)
        with contextlib.suppress(Exception):
            from lifecycle.compact import compact_under_budget

            result["compact"] = await compact_under_budget(self.user_id, AGENT_LAYER)
        with contextlib.suppress(Exception):
            from lifecycle.l0_sweep import sweep_expired

            result["sweep"] = await sweep_expired()
        with contextlib.suppress(Exception):
            from features.bridge import ingest_drain, regenerate_bridge

            result["bridge"] = str(await regenerate_bridge(self.user_id, AGENT_LAYER))
            result["ingest_drain"] = await ingest_drain(self.user_id, AGENT_LAYER)
        return result

    @hook_registry.mark("importance_gate", layer=AGENT_LAYER)
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
        for kw in ("error", "decision", "principle", "lesson", "pattern"):
            if kw in text.lower():
                score += 0.15
        # General heuristics
        if len(text) > 50:
            score += 0.1
        if "?" in text:
            score += 0.1

        return await adaptive_threshold.gate(min(1.0, score))

    @hook_registry.mark("error_occurred", layer=AGENT_LAYER)
    async def _error_occurred(self, ctx: dict[str, Any]) -> dict[str, Any]:
        error = ctx.get("error", "")
        node_id = await self.graph.add_node(self.user_id, error, "error_analysis", ["error_pattern"], 0.8)
        return {"action": "error_analyzed", "node_id": node_id}

    @hook_registry.mark("decision_made", layer=AGENT_LAYER)
    async def _decision_made(self, ctx: dict[str, Any]) -> dict[str, Any]:
        decision = ctx.get("decision", "")
        rationale = ctx.get("rationale", "")
        node_id = await self.graph.add_node(self.user_id, f"{decision}: {rationale}", "decision_log", ["decided_because"], 0.7)
        return {"action": "decision_logged", "node_id": node_id}

    @hook_registry.mark("self_correction", layer=AGENT_LAYER)
    async def _self_correction(self, ctx: dict[str, Any]) -> dict[str, Any]:
        error = ctx.get("error", "")
        fix = ctx.get("fix", "")
        node_id = await self.graph.add_node(self.user_id, f"Error: {error} → Fix: {fix}", "correction", ["correction_pattern"], 0.6)
        return {"action": "correction_logged", "node_id": node_id}

    @hook_registry.mark("personality_shift", layer=AGENT_LAYER)
    async def _personality_shift(self, ctx: dict[str, Any]) -> dict[str, Any]:
        shift = ctx.get("shift", "")
        node_id = await self.graph.add_node(self.user_id, shift, "personality_evolution", ["personality_trait", "evolved_to"], 0.9)
        return {"action": "personality_evolved", "node_id": node_id}

    @hook_registry.mark("emotion_context", layer=AGENT_LAYER)
    async def _emotion_context(self, ctx: dict[str, Any]) -> dict[str, Any]:
        emotion = ctx.get("emotion", "")
        context = ctx.get("context", "")
        node_id = await self.graph.add_node(self.user_id, f"{emotion} in: {context}", "emotional_context", ["felt_in_context"], 0.6)
        return {"action": "emotion_logged", "node_id": node_id}

    @hook_registry.mark("wiki_agent", layer=AGENT_LAYER)
    async def _wiki_agent(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return {"action": "wiki_sync", "summary": ctx.get("summary", "")}

    @hook_registry.mark("dream_buffer", layer=AGENT_LAYER)
    async def _dream_buffer(self, ctx: dict[str, Any], mem: Any | None = None) -> dict[str, Any]:
        return await dream_buffer_staging(ctx, self.user_id, layer=AGENT_LAYER, cm=mem._cm if mem else None)

    @hook_registry.mark("consolidation", layer="both")
    async def _consolidation(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return await consolidation(ctx, self.user_id, min_importance=0.6, action_key="agent_consolidated")

    @hook_registry.mark("forgetting_ritual", layer="both")
    async def _forgetting_ritual(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return await forgetting_ritual(ctx)

    @hook_registry.mark("auto_context", layer="both")
    async def _auto_context(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return await auto_context(ctx, self.user_id, layer=AGENT_LAYER)

    @hook_registry.mark("retrieval_router", layer="both")
    async def _retrieval_router(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return await retrieval_router(ctx, self.user_id, layer=AGENT_LAYER)

    @hook_registry.mark("conflict_resolver", layer="both")
    async def _conflict_resolver(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return await conflict_resolver(ctx, self.user_id)

    @hook_registry.mark("emotion", layer=AGENT_LAYER)
    async def _emotion(self, ctx: dict[str, Any]) -> dict[str, Any]:
        emotion = ctx.get("emotion", "")
        node_id = await self.graph.add_node(self.user_id, emotion, "emotional_context", ["felt_in_context"], 0.5)
        return {"action": "emotion_recorded", "node_id": node_id}
