"""D1.7 memory query DSL — structured analytics without raw SQL.

Whitelisted filters → parameterized SQL over core_memory / episodes (no
user-supplied SQL, no injection surface). Read-only power-user surface;
all writes stay on the dedicated tools.
"""

from __future__ import annotations

import json
import time
from typing import Any

from shared.connection import connection_manager
from shared.constants import DB_NAME


async def query_memory(
    user_id: str,
    layer: str = "user",
    source: str = "core",
    importance_min: float | None = None,
    importance_max: float | None = None,
    key_like: str = "",
    content_like: str = "",
    created_since: float = 0.0,
    created_until: float = 0.0,
    tag: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    """Run whitelisted filters over one memory table. Returns {rows, count, filters}."""
    if source not in ("core", "episodes"):
        raise ValueError(f"unknown source: {source!r} (core|episodes)")
    if tag and source == "core":
        raise ValueError("tag filter is episodes-only (core_memory has no tags)")
    if key_like and source == "episodes":
        raise ValueError("key_like filter is core-only (episodes has no key column)")
    limit = max(1, min(int(limit), 200))

    where = ["layer=?", "user_id=?"]
    params: list[Any] = [layer, user_id]
    if source == "core":
        sql = "SELECT entry_id, layer, user_id, key, value, importance, memory_kind, source, metadata, created_at, updated_at FROM core_memory"
        imp_col = "importance"
        content_col = "value"
    else:
        sql = "SELECT episode_id, layer, user_id, summary, emotional_weight, tags, created_at FROM episodes"
        imp_col = "emotional_weight"
        content_col = "summary"
    if importance_min is not None:
        where.append(f"{imp_col} >= ?")
        params.append(float(importance_min))
    if importance_max is not None:
        where.append(f"{imp_col} <= ?")
        params.append(float(importance_max))
    if key_like and source == "core":
        where.append("key LIKE ?")
        params.append(f"%{key_like}%")
    if content_like:
        where.append(f"{content_col} LIKE ?")
        params.append(f"%{content_like}%")
    if created_since:
        where.append("created_at >= ?")
        params.append(float(created_since))
    if created_until:
        where.append("created_at < ?")
        params.append(float(created_until))
    if tag:
        where.append("tags LIKE ?")
        params.append(f'%"{tag}"%')

    conn = await connection_manager.get(DB_NAME)
    rows = await (await conn.execute(f"{sql} WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT ?", (*params, limit))).fetchall()
    out = []
    for r in rows:
        row = dict(r)
        if "tags" in row:
            with_context = row["tags"]
            try:
                row["tags"] = json.loads(with_context) if isinstance(with_context, str) else with_context
            except json.JSONDecodeError:
                row["tags"] = []
        out.append(row)
    return {
        "rows": out,
        "count": len(out),
        "filters": {
            "source": source,
            "layer": layer,
            "importance_min": importance_min,
            "importance_max": importance_max,
            "key_like": key_like,
            "content_like": content_like,
            "created_since": created_since,
            "created_until": created_until,
            "tag": tag,
            "limit": limit,
        },
        "queried_at": time.time(),
    }
