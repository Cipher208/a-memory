"""daily_brief tool — one-call status report (pending + recent + suggested).

Deterministic assembly from existing stores; no LLM call, no new schema.
Each section is independent and non-fatal: a failure renders '_(unavailable)_'.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from mcp.server.mcpserver import Context  # noqa: TC002

from mcp_server.registry import _get_ctx
from mcp_server.tools.base import _validate_layer, _get_memory
from shared.constants import DB_NAME

logger = logging.getLogger(__name__)


async def daily_brief(
    layer: str = "user",
    user_id: str = "default",
    days: int = 1,
    ctx: Context[Any, Any] | None = None,
) -> dict[str, Any]:
    """Return a 3-section daily brief: pending work, recent activity, suggested action."""
    app = _get_ctx(ctx)
    layer = _validate_layer(layer)
    days = max(1, days)
    _get_memory(app, layer, user_id)  # validate layer wiring / rate not needed here

    now = time.time()
    since = now - days * 86400

    # 1. Pending work: L4 todo facts
    pending: list[str] = []
    try:
        conn = await app.mm._cm.get(DB_NAME)
        rows = await (
            await conn.execute(
                "SELECT key, value FROM core_memory WHERE layer=? AND user_id=? AND memory_kind='todo' ORDER BY importance DESC LIMIT 20",
                (layer, user_id),
            )
        ).fetchall()
        pending = [f"- [{r[0]}] {str(r[1])[:120]}" for r in rows]
    except Exception as exc:
        logger.warning("daily_brief: pending section failed: %s", exc)
        pending = ["_(unavailable)_"]

    # 2. Recent activity: temporal events + recall_count
    recent: list[str] = []
    try:
        events = await app.temporal.get_recent(user_id, layer=layer, limit=5)
        for e in events:
            tv = (e.metadata or {}).get("training_value", "?")
            recent.append(f"- [{e.event_type}] {e.content[:120]} (tv={tv})")
        from features.recall_telemetry import count_recalls

        n_recall = await count_recalls(app.mm._cm, user_id, started_at=since, ended_at=now)
        recent.append(f"- recall calls (last {days}d): {n_recall}")
    except Exception as exc:
        logger.warning("daily_brief: recent section failed: %s", exc)
        recent = ["_(unavailable)_"]

    # 3. Suggested action: todo follow-ups + open sessions
    suggested: list[str] = []
    try:
        for r in pending:
            if not r.startswith("_(unavailable)_"):
                suggested.append(f"- follow up on: {r[3:]}")
        conn = await app.mm._cm.get(DB_NAME)
        rows = await (
            await conn.execute(
                "SELECT session_id, summary FROM sessions WHERE user_id=? AND ended_at IS NULL LIMIT 5",
                (user_id,),
            )
        ).fetchall()
        for r in rows:
            label = r[1] or r[0]
            suggested.append(f"- resume session: {str(label)[:120]}")
    except Exception as exc:
        logger.warning("daily_brief: suggested section failed: %s", exc)
        suggested = ["_(unavailable)_"]

    sections = {
        "## Pending work": pending or ["_(nothing pending)_"],
        "## Recent activity": recent or ["_(no recent activity)_"],
        "## Suggested next action": suggested or ["_(no suggested next step)_"],
    }
    summary = "\n".join(f"{h}\n" + "\n".join(lines) for h, lines in sections.items())

    return {
        "status": "ok",
        "summary": summary,
        "pending": pending,
        "recent": recent,
        "suggested": suggested,
    }
