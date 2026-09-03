"""A1.3: wiki_reflect — outcome digest over the wiki layer (graphify-reflect analog).

Deterministic, no LLM: counts by lifecycle status and wiki_type, top pages
by importance, pages last updated long ago (staleness signal). The text
digest re-injects nothing by itself — it's an operator/agent read surface.
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any


async def wiki_reflect(layer: str = "user", limit: int = 50) -> dict[str, Any]:
    from wiki.manager import WikiManager

    wm = WikiManager(layer=layer)
    all_rows = await wm.index.list_all(limit=200, status=None)
    now = time.time()

    by_status = Counter(str(r.get("status") or "active") for r in all_rows)
    by_type = Counter(str(r["wiki_type"]) for r in all_rows)
    stale_pages = [str(r["title"]) for r in all_rows if now - float(r["updated_at"] or 0) > 30 * 86400]

    top = sorted(all_rows, key=lambda r: -float(r["importance"] or 0))[:10]
    lines = [
        f"wiki reflect [{layer}]: {len(all_rows)} pages ({by_status.get('active', 0)} active, {by_status.get('stale', 0)} stale, {by_status.get('archived', 0)} archived)",
        f"types: {', '.join(f'{t}×{c}' for t, c in by_type.most_common())}",
        f"top by importance: {', '.join(str(r['title']) for r in top[:5])}",
    ]
    if stale_pages:
        lines.append(f"stale (30d+ untouched): {', '.join(stale_pages[:5])}")

    return {
        "totals": {"pages": len(all_rows), **dict(by_status)},
        "by_type": dict(by_type),
        "top": [{"title": str(r["title"]), "importance": float(r["importance"] or 0)} for r in top],
        "stale_30d": stale_pages[:limit],
        "reflection": "\n".join(lines),
    }
