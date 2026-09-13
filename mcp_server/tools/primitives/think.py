from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import time
from typing import Any, Literal

from mcp.server.mcpserver import Context  # noqa: TC002 — runtime: MCPServer evaluates this annotation at registration

from mcp_server.models import ThinkResult
from mcp_server.registry import _get_ctx
from shared.metrics import metrics

from mcp_server.tools.base import (
    _validate_layer,
    _check_rate_limit,
    _get_memory,
    _get_wiki,
    _fire_hook,
)
from mcp_server.tools.primitives.routing import _auto_route

from shared.importance.training import classify_training_value

from mcp_server.context import AppContext  # noqa: TC001 — runtime: MCPServer evaluates this annotation at registration

logger = logging.getLogger(__name__)


async def think(
    text: str,
    layer: Literal["user", "agent", "auto"] = "auto",
    user_id: str = "default",
    wiki_type: str | None = None,
    wiki_title: str | None = None,
    kind: str | None = None,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Universal Primitive: routing thoughts to correct memory layers based on importance and content.

    kind: optional declaration hint (one of L4_DECLARABLE_KINDS). A declared
    thought is authored canon — it bypasses the length/pattern gates straight
    to L4 (spec S4), rate-limited per user. Without it, behavior is unchanged.
    """
    app: AppContext = _get_ctx(ctx)
    metrics.inc("tool_calls")
    metrics.inc("tool_think")

    # S4: validate the declaration hint BEFORE any side effect (L0 capture,
    # rate slots) — an invalid call must not write anything.
    if kind is not None:
        from shared.memory_types import L4_DECLARABLE_KINDS, validate_kind

        if not validate_kind(kind):
            return {"status": "error", "message": f"invalid kind: {kind!r}"}
        if kind not in L4_DECLARABLE_KINDS:
            return {"status": "error", "message": f"kind {kind!r} is not declarable; use one of {sorted(L4_DECLARABLE_KINDS)}"}

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
    wiki = _get_wiki(app, resolved_layer)

    tasks = []

    # Audit 05.09 (P0): single entry — think-content is captured into L0 for
    # provenance, but with skip_distill: think routes itself (L4/L3/wiki),
    # replay must not distill it a second time.
    from shared.l0 import capture as _l0_capture

    tasks.append(_l0_capture(event="think", layer=resolved_layer, user_id=user_id, text=text, decisions=[{"gate": "think", "skip_distill": True}]))
    actions.append({"type": "L0_captured", "event": "think"})

    # S4: declared-canon channel — an explicit kind hint is authored truth,
    # so it lands in L4 at the 0.8 floor regardless of length gates (<=2000;
    # longer declarations still ride the wiki branch below). Rate-limited per
    # user by the shared window in shared/canon_rate.py.
    declared_canon = False
    if kind is not None and len(text) <= 2000:
        from shared.canon_rate import CANON_MAX_PER_HOUR, canon_rate_ok

        if canon_rate_ok(user_id):
            imp = max(importance, 0.8)
            key = f"canon:{kind}:{hashlib.sha1(text.encode('utf-8')).hexdigest()[:12]}"
            entry_id = await mem.remember(key, text, imp)
            actions.append({"type": "L4_declared_canon", "kind": kind, "importance": str(imp)})
            routing["declared_canon"] = True
            declared_canon = True
            with contextlib.suppress(Exception):
                from shared.connection import connection_manager

                conn = await connection_manager.get("memory.db")
                await conn.execute(
                    "INSERT INTO importance_audit (user_id, chunk_id, source, old_importance, new_importance, signal_breakdown, reason, rescored_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (user_id, int(entry_id), "declared_canon", float(importance), float(imp), "{}", f"think kind={kind}", time.time()),
                )
                await conn.commit()
        else:
            actions.append({"type": "L4_declared_canon_capped", "note": f">{CANON_MAX_PER_HOUR}/h for {user_id}"})

    forced_wiki = bool(wiki_type or wiki_title)
    large_text = len(text) > 2000

    if forced_wiki or large_text:
        w_type = wiki_type or ("decision_log" if resolved_layer == "agent" else "diary")
        title = wiki_title or f"Thought_{int(time.time())}"
        wiki_path = await wiki.add(wiki_type=w_type, title=title, content=text)

        summary = text[:200] + "..."
        text_to_save = f"Summary: {summary} | Path: {wiki_path}"
        action_type = "Wiki_save" if forced_wiki else "Wiki_thought_save"
        actions.append({"type": action_type, "path": wiki_path})

        # Also save summary/link to memory so dream() can find the page
        if importance > 0.7:
            tasks.append(mem.remember("thought_link", text_to_save, importance))
            actions.append({"type": "L4_remember_link", "importance": str(importance)})
        else:
            tasks.append(mem.l3.save(user_id, text_to_save, float(scorer_result.signals.emotional)))
            actions.append({"type": "L3_episodic_save_link", "weight": str(scorer_result.signals.emotional)})
    else:
        # Standard routing
        # If len(text) < 60 and importance is high -> Save to CoreMemory (L4)
        # (skip when the declared channel already wrote the canon entry)
        if len(text) < 60 and importance > 0.7 and not declared_canon:
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
        # F-T9: direct add_node removed; the text is already in L0 (capture
        # above) — the distiller/miners create the graph node, not the tool layer.
        actions.append({"type": "L0_captured", "event": "think"})

    # 5. Hooks
    hook_tasks = [_fire_hook("message_received", resolved_layer, {"text": text, "user_id": user_id}, mem=mem)]
    # User-emotion analysis only makes sense on the user layer; agent
    # self-reflection runs through its own graph hooks instead.
    if resolved_layer == "user":
        hook_tasks.append(_fire_hook("emotion_trigger", resolved_layer, {"text": text, "user_id": user_id, "importance": importance}, mem=mem))

    # Timeline: significant thoughts become temporal events (never breaks the primitive)
    if app.temporal and actions:
        with contextlib.suppress(Exception):
            await app.temporal.add_event(
                user_id,
                "thought",
                text[:200],
                importance=float(importance),
                metadata={"resolved_layer": resolved_layer, "actions": len(actions), "training_value": classify_training_value(text)},
                layer=resolved_layer,
            )

    import inspect

    awaitable_tasks = [t for t in tasks + hook_tasks if inspect.isawaitable(t)]

    if awaitable_tasks:
        await asyncio.gather(*awaitable_tasks)

    return ThinkResult(status="ok", routing=routing, actions=actions).dict()
