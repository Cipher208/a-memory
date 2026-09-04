"""L0 raw intake — единственный вход конвейера (append-only, best-effort)."""

from __future__ import annotations

import json
import time

from shared.connection import connection_manager
from shared.constants import DB_NAME


async def capture(
    event: str,
    layer: str,
    user_id: str,
    text: str,
    *,
    source_msg_id: int | None = None,
    raw_type: str | None = None,
    decisions: list[dict] | None = None,
) -> int | None:
    """Append-only intake. Никогда не бросает — сбой L0 не блокирует поток."""
    try:
        conn = await connection_manager.get(DB_NAME)
        cur = await conn.execute(
            "INSERT INTO l0_journal (ts, event, source_msg_id, layer, user_id, text, raw_type, status, decisions)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'received', ?)",
            (
                time.time(),
                event,
                source_msg_id,
                layer,
                user_id,
                text,
                raw_type or classify_raw(text),
                json.dumps(decisions or [], ensure_ascii=False),
            ),
        )
        await conn.commit()
        return int(cur.lastrowid or 0)
    except Exception:
        return None


def classify_raw(text: str) -> str:
    t = text.strip()
    if t.startswith(("[{", '{"')):
        try:
            obj = json.loads(t)
            if isinstance(obj, dict) and obj.get("type") == "tool_result":
                return "tool_result"
            if isinstance(obj, dict) and obj.get("type") == "tool_use":
                return "tool_use"
        except ValueError:
            pass
        return "tool_result" if "tool_use_id" in t[:200] else "plain"
    for prefix in ("[ariel recall]", "[ariel memory]", "[ariel proposals]"):
        if t.startswith(prefix):
            return "recall"
    if t.startswith("[EVOLUTION]"):
        return "evolution"
    if "tool_use_id" in t[:200]:
        return "tool_result"
    return "user-message"
