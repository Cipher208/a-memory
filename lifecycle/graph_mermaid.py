"""C7 / S13: Mermaid canvas — epi_nodes/epi_edges → `graph TD` строка.

Узлы N<id>["content (type)"], рёбра только между отрендеренными узлами
(иначе Mermaid придумает призрачные узлы). CLI: scripts/ariel_cli.py mermaid.
"""

from __future__ import annotations

from typing import Any

CONTENT_MAX = 60


def _label(content: str, node_type: str) -> str:
    label = f"{content[:CONTENT_MAX]} ({node_type})"
    return label.replace('"', "'").replace("\n", " ").strip()


async def render_mermaid(conn: Any, layer: str, limit: int = 50) -> str:
    """Render up to `limit` epi_nodes of `layer` + their inner edges as Mermaid."""
    nodes = await (
        await conn.execute(
            "SELECT node_id, content, node_type FROM epi_nodes WHERE layer=? ORDER BY node_id LIMIT ?",
            (layer, limit),
        )
    ).fetchall()
    if not nodes:
        return "graph TD"
    ids = {int(r["node_id"]) for r in nodes}
    # только active-рёбра (G2.0 validity windows) — expired на канвасе не рисуем
    edges = await (await conn.execute("SELECT source_id, target_id, relation FROM epi_edges WHERE status='active'")).fetchall()
    # ponytail: full epi_edges scan + python filter — graph is audit-scale (<100k);
    # IN-list SQL if the graph ever outgrows that.
    lines = ["graph TD"]
    for r in nodes:
        lines.append(f'  N{int(r["node_id"])}["{_label(str(r["content"]), str(r["node_type"]))}"]')
    for e in edges:
        if int(e["source_id"]) in ids and int(e["target_id"]) in ids:
            lines.append(f"  N{int(e['source_id'])} -->|{e['relation']}| N{int(e['target_id'])}")
    return "\n".join(lines)
