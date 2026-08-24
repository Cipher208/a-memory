from __future__ import annotations
import re
import logging
import asyncio
import time
from typing import Any, Literal

from mcp.server.mcpserver import Context

from mcp_server.models import ThinkResult, DreamResult, ForgetResult, EvolveResult, ProjectResult
from mcp_server.registry import _get_ctx
from shared.metrics import metrics

from .base import (
    _validate_layer,
    _check_rate_limit,
    _get_memory,
    _get_graph,
    _get_wiki,
    _get_rag,
    _fire_hook,
    _truncate_to_budget,
    _invalidate_cache,
    DEFAULT_TOKEN_BUDGET,
)

# Runtime imports: FastMCP evaluates tool annotations at registration;
# hiding Context/AppContext under TYPE_CHECKING breaks tools/list (fix 419d577).
from mcp.server.mcpserver import Context  # noqa: TC002
from mcp_server.context import AppContext  # noqa: TC001

logger = logging.getLogger(__name__)

# Auto-routing signals. Agent layer stores identity/decisions/errors/personality
# (first-person agent voice); user layer stores facts ABOUT the user.
_AGENT_SIGNALS = re.compile(
    r"\b(i|we)\s+(decided|choose|chose|prefer|fixed|broke|learned|mistook|failed|solved)\b"
    r"|\b(my|our)\s+(personality|style|approach|rule|policy)\b"
    r"|\b(decision_log|error_analysis|mistake|lesson_learned)\b",
    re.IGNORECASE,
)
_USER_SIGNALS = re.compile(
    r"\b(user|he|she|they|(?:mr|mrs|ms)?\.?\s?[A-Z][a-z]+)\s+(likes?|prefers?|wants?|hates?|said|asked|is)\b"
    r"|\b(the user)'?s?\b",
    re.IGNORECASE,
)


def _auto_route(text: str) -> str:
    """Route auto-layer thoughts: agent-voice content → agent, user facts → user."""
    agent_score = len(_AGENT_SIGNALS.findall(text))
    user_score = len(_USER_SIGNALS.findall(text))
    return "agent" if agent_score > user_score else "user"


async def think(
    text: str,
    layer: Literal["user", "agent", "auto"] = "auto",
    user_id: str = "default",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Universal Primitive: routing thoughts to correct memory layers based on importance and content."""
    app: AppContext = _get_ctx(ctx)
    metrics.inc("tool_calls")
    metrics.inc("tool_think")

    # 1. Rate limiting
    rate_limit = await _check_rate_limit(app, user_id)
    if rate_limit:
        return dict(rate_limit)

    # 2. Importance Scoring
    scorer_result = app.importance.score(text)
    importance = scorer_result.score

    # 3. Layer Resolution
    resolved_layer: str = layer
    if layer == "auto":
        resolved_layer = _auto_route(text)

    _validate_layer(resolved_layer)

    # 4. Routing Logic
    actions = []
    routing = {"importance": importance, "length": len(text), "emotional_weight": scorer_result.signals.emotional, "resolved_layer": resolved_layer}

    mem = _get_memory(app, resolved_layer, user_id)
    graph = _get_graph(app, resolved_layer)
    wiki = _get_wiki(app, resolved_layer)

    tasks = []

    # Large input (> 2000 chars) -> save to Wiki thoughts
    if len(text) > 2000:
        thought_title = f"Thought_{int(time.time())}"
        wiki_path = await wiki.add(wiki_type="decision_log" if resolved_layer == "agent" else "diary", title=thought_title, content=text)

        summary = text[:200] + "..."
        text_to_save = f"Summary: {summary} | Path: {wiki_path}"
        actions.append({"type": "Wiki_thought_save", "path": wiki_path})

        # Also save summary/link to memory
        if importance > 0.7:
            tasks.append(mem.remember("thought_link", text_to_save, importance))
            actions.append({"type": "L4_remember_link", "importance": str(importance)})
        else:
            tasks.append(mem.l3.save(user_id, text_to_save, float(scorer_result.signals.emotional)))
            actions.append({"type": "L3_episodic_save_link", "weight": str(scorer_result.signals.emotional)})
    else:
        # Standard routing
        # If len(text) < 60 and importance is high -> Save to CoreMemory (L4)
        if len(text) < 60 and importance > 0.7:
            tasks.append(mem.remember("thought", text, importance))
            actions.append({"type": "L4_remember", "importance": str(importance)})

        # If len(text) >= 60 or emotional weight is detected -> Save to Episodic (L3)
        if len(text) >= 60 or scorer_result.signals.emotional > 0.5:
            tasks.append(mem.l3.save(user_id, text, float(scorer_result.signals.emotional)))
            actions.append({"type": "L3_episodic_save", "weight": str(scorer_result.signals.emotional)})

        # Fallback: a write primitive must never silently drop content that
        # matched neither the L4 nor the L3 rule.
        if not any(a["type"].startswith(("L4_", "L3_", "Wiki_")) for a in actions):
            tasks.append(mem.l3.save(user_id, text, float(scorer_result.signals.emotional)))
            actions.append({"type": "L3_episodic_save_fallback", "weight": str(scorer_result.signals.emotional)})

    # Relation detection
    relation_patterns = [r"\b\w+\s+(is|related\s+to|connected\s+to|part\s+of)\s+\w+\b"]
    has_relation = any(re.search(p, text, re.IGNORECASE) for p in relation_patterns)

    if has_relation:
        tasks.append(graph.add_node(user_id, text[:500], "relation", ["think_primitive"], importance))
        actions.append({"type": "Graph_node_add", "node_type": "relation"})

    # 5. Hooks
    hook_tasks = [
        _fire_hook("message_received", resolved_layer, {"text": text, "user_id": user_id}, mem=mem),
        _fire_hook("emotion_trigger", resolved_layer, {"text": text, "user_id": user_id, "importance": importance}, mem=mem),
    ]

    import inspect

    awaitable_tasks = [t for t in tasks + hook_tasks if inspect.isawaitable(t)]

    if awaitable_tasks:
        await asyncio.gather(*awaitable_tasks)

    return ThinkResult(status="ok", routing=routing, actions=actions).dict()


async def dream(
    query: str,
    limit: int = 10,
    layer: Literal["user", "agent"] = "user",
    user_id: str = "default",
    intent: Literal["recent", "core", "balanced"] = "balanced",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Universal Primitive: Hybrid search across ALL layers (L3, L4, Wiki, Graph) with context construction."""
    app: AppContext = _get_ctx(ctx)
    metrics.inc("tool_calls")
    metrics.inc("tool_dream")

    _validate_layer(layer)

    # 1. Hybrid Search
    multi_rag = _get_rag(app, layer)
    results = await multi_rag.search(query, user_id=user_id, limit=limit, intent=intent)

    # 2. Context Construction
    summary_parts = []
    for r in results:
        title = r.get("title", "Untitled")
        content = r.get("content", "")
        source = r.get("source", "unknown")
        summary_parts.append(f"### {title} (Source: {source})\n{content}")

    full_summary = "\n\n".join(summary_parts)
    summary, truncated = _truncate_to_budget(full_summary, DEFAULT_TOKEN_BUDGET)

    # 3. Hooks
    mem = _get_memory(app, layer, user_id)
    hook_tasks = [
        _fire_hook("auto_context", layer, {"query": query, "results": results}, mem=mem),
        _fire_hook("dream_buffer", layer, {"query": query, "summary": summary}, mem=mem),
    ]
    awaitable_hooks = [t for t in hook_tasks if asyncio.iscoroutine(t)]
    if awaitable_hooks:
        await asyncio.gather(*awaitable_hooks)

    return DreamResult(
        summary=summary,
        truncated=truncated,
        result_count=len(results),
    ).dict()


async def forget(
    key: str,
    scope: Literal["exact", "fuzzy", "recent"] = "exact",
    layer: Literal["user", "agent"] = "user",
    user_id: str = "default",
    minutes: int = 60,
    shadow_bin: bool = True,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Universal Primitive: context-aware forgetting with Shadow Bin support."""
    app: AppContext = _get_ctx(ctx)
    metrics.inc("tool_calls")
    metrics.inc("tool_forget_primitive")

    _validate_layer(layer)
    mem = _get_memory(app, layer, user_id)
    graph = _get_graph(app, layer)

    archived_l4 = 0
    archived_l3 = 0
    archived_graph = 0

    from shared.archived_memories import ArchivedMemories

    am = ArchivedMemories(cm=app.mm._cm)
    await am._init_db()

    if scope == "exact":
        entry = await mem.l4.get(user_id, key)
        if entry:
            if shadow_bin:
                await am.archive(
                    user_id=user_id,
                    content=f"{entry.key}={entry.value}",
                    memory_type=entry.memory_kind,
                    importance=entry.importance,
                    original_id=entry.entry_id,
                    reason="forget_primitive_exact",
                )
            await mem.forget(key)
            archived_l4 = 1

    elif scope == "fuzzy":
        # 1. Search and Archive L4
        l4_hits = await mem.l4.search(user_id, key, limit=10)
        for hit in l4_hits:
            entry = await mem.l4.get(user_id, hit["key"])
            if entry:
                if shadow_bin:
                    await am.archive(
                        user_id=user_id,
                        content=f"{entry.key}={entry.value}",
                        memory_type=entry.memory_kind,
                        importance=entry.importance,
                        original_id=entry.entry_id,
                        reason="forget_primitive_fuzzy_l4",
                    )
                if await mem.l4.delete(user_id, hit["key"]):
                    archived_l4 += 1

        # 2. Search and Archive L3 (Episodic)
        episodes = await mem.l3.search(user_id, key, limit=10)
        for e in episodes:
            if shadow_bin:
                await am.archive(
                    user_id=user_id,
                    content=e.summary,
                    memory_type="episode",
                    importance=e.emotional_weight,
                    original_id=e.episode_id,
                    reason="forget_primitive_fuzzy_l3",
                )
        archived_l3 = await mem.l3.delete_by_ids([e.episode_id for e in episodes])

        # 3. Search and Archive Graph Nodes
        nodes = await graph.find_nodes_matching(user_id, f"%{key}%")
        for n in nodes:
            if shadow_bin:
                await am.archive(
                    user_id=user_id,
                    content=n.content,
                    memory_type=f"graph:{n.node_type}",
                    importance=n.confidence,
                    original_id=n.node_id,
                    reason="forget_primitive_fuzzy_graph",
                )
        archived_graph = await graph.delete_nodes([n.node_id for n in nodes])

    elif scope == "recent":
        # Purge last N minutes
        # Recent doesn't easily support shadow bin without complex query-before-delete
        # But for consistency, we skip shadow bin for mass purge or implement simple one
        from .ops import _purge_table, _purge_staging

        cutoff = time.time() - (minutes * 60)
        results = await asyncio.gather(
            _purge_table(mem.l4._cm, "core_memory", user_id, cutoff),
            _purge_table(mem.l3._cm, "episodes", user_id, cutoff),
            _purge_table(graph._cm, "epi_nodes", user_id, cutoff),
            _purge_staging(user_id),
        )
        # gather order: [l4, l3, graph, staging]
        archived_l4 = results[0]
        archived_l3 = results[1]
        archived_graph = results[2]

    _invalidate_cache(layer, user_id)
    return ForgetResult(deleted_l4=archived_l4, deleted_l3=archived_l3, deleted_graph=archived_graph).dict()


async def evolve(
    instruction: str,
    user_id: str = "default",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Universal Primitive: update agent personality and triggering evolution."""
    app: AppContext = _get_ctx(ctx)
    metrics.inc("tool_calls")
    metrics.inc("tool_evolve")

    # Save to Agent's CoreMemory (L4)
    mem = app.mm.agent_memory(user_id)
    await mem.remember("agent_evolution", instruction, importance=1.0)

    # Personality shift hook
    hook_result = await _fire_hook("personality_shift", "agent", {"instruction": instruction, "user_id": user_id}, mem=mem)

    summary = hook_result.get("summary", "Personality evolution recorded.")
    return EvolveResult(status="ok", summary=summary).dict()


async def project(
    action: Literal["init", "update", "archive", "mapping", "audit"],
    name: str,
    details: str = "",
    role: str = "",
    status: str = "",
    layer: Literal["user", "agent"] = "user",
    user_id: str = "default",
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Universal Primitive: managing project-specific context and file mapping."""
    app: AppContext = _get_ctx(ctx)
    metrics.inc("tool_calls")
    metrics.inc("tool_project")

    _validate_layer(layer)
    wiki = _get_wiki(app, layer)
    mem = _get_memory(app, layer, user_id)

    path = None
    audit_report = None

    if action == "init":
        # Create a new project page in Wiki under type project_spec
        path = await wiki.add(wiki_type="project_spec", title=name, content=details)
        status_res = "ok"
    elif action == "update":
        # Update existing project context
        results = await wiki.index.search(name, limit=1)
        if results and results[0]["title"] == name:
            await wiki.update(file_path=results[0]["file_path"], content=details)
            path = results[0]["file_path"]
            status_res = "updated"
        else:
            path = await wiki.add(wiki_type="project_spec", title=name, content=details)
            status_res = "ok"
    elif action == "mapping":
        # Store file roles and statuses in project_spec
        # Format: [name] mapping: role=X, status=Y
        mapping_content = f"File: {name} | Role: {role} | Status: {status} | Details: {details}"
        path = await wiki.add(wiki_type="project_spec", title=f"Map_{name}", content=mapping_content)
        status_res = "mapped"
    elif action == "archive":
        # Move project memories to archive
        results = await wiki.index.search(name, limit=1)
        if results and results[0]["title"] == name:
            from shared.archived_memories import ArchivedMemories

            am = ArchivedMemories(cm=wiki._cm)
            await am.archive(
                user_id=user_id,
                content=details or results[0].get("content", ""),
                memory_type="project_archive",
                importance=0.8,
                original_id=0,
                reason="project_archived",
            )
            await wiki.delete(results[0]["file_path"])
            path = "archived"
            status_res = "archived"
        else:
            path = None
            status_res = "not_found"
    elif action == "audit":
        # 1. Gather Context
        multi_rag = _get_rag(app, layer)
        context_results = await multi_rag.search(name, user_id=user_id, limit=20, intent="balanced")

        # 2. Gap Analysis
        has_arch = any(
            "architecture" in (r.get("title", "") + r.get("content", "")).lower() or "design" in (r.get("title", "") + r.get("content", "")).lower()
            for r in context_results
        )
        has_sec = any(
            "security" in (r.get("title", "") + r.get("content", "")).lower() or "hardening" in (r.get("title", "") + r.get("content", "")).lower()
            for r in context_results
        )
        has_test = any(
            "testing" in (r.get("title", "") + r.get("content", "")).lower() or "verification" in (r.get("title", "") + r.get("content", "")).lower()
            for r in context_results
        )

        verdicts = []
        if has_arch:
            verdicts.append("Architecture/Design is documented.")
        else:
            verdicts.append("Architecture/Design documentation is missing.")

        if has_sec:
            verdicts.append("Security/Hardening entries found.")
        else:
            verdicts.append("Security/Hardening information is missing.")

        if has_test:
            verdicts.append("Testing/Verification protocols exist.")
        else:
            verdicts.append("Testing/Verification entries are missing.")

        # 3. Conflict Detection in L4 (CoreMemory)
        l4_hits = await mem.l4.search(user_id, name, limit=50)
        keys_seen: dict[str, Any] = {}
        conflicts = []
        for hit in l4_hits:
            key = hit.get("key")
            val = hit.get("value")
            if key in keys_seen and keys_seen[key] != val:
                conflicts.append(f"Memory conflict: '{key}' has multiple values ('{keys_seen[key]}' vs '{val}')")
            keys_seen[key] = val

        if conflicts:
            verdicts.extend(conflicts)
        else:
            verdicts.append("No memory conflicts detected in CoreMemory.")

        audit_report = "\n".join(verdicts)
        status_res = "audit"
        path = None
    else:
        status_res = "error"
        path = None

    return ProjectResult(status=status_res, title=name, path=path, audit_report=audit_report).dict()
