"""S18 tail offload: heavy tool logs → work_notes/refs-*.md, the context sees a ref.

TencentDB v2 pattern (competitive-intel-2): hundreds of thousands of log tokens
do not live in context/graph — the evidence is offloaded to a Markdown file,
while a compact link (hundreds of tokens instead of hundreds of thousands)
remains in graph/context.

refs live as work_notes pages with the `refs-` prefix (NOT a new wiki_type —
surface/types are not extended). The graph node is NOT created by hand: the
wiki page becomes an epi_node via wiki_graph_builder (F-T9 single-entry,
AST-invariant test_grep_invariant_no_bypass_add_node) — the "Mermaid canvas
with node_id" appears through the standard path, with no write bypass.
"""

from __future__ import annotations

import re
import time
from typing import Any

REFS_THRESHOLD_CHARS = 4000  # heavier than this — offload
_SLUG_RE = re.compile(r"[^a-z0-9а-яё]+")


def slugify(tool: str, ts: float) -> str:
    """Build refs-<date>-<tool>-<hhmmss> — deterministic, collision-free within a second."""
    t = time.strftime("%Y%m%d", time.gmtime(ts)) + "-" + time.strftime("%H%M%S", time.gmtime(ts))
    base = _SLUG_RE.sub("-", tool.lower()).strip("-")[:40] or "tool"
    return f"refs-{t}-{base}"


async def offload_tool_log(
    wiki: Any,
    user_id: str,
    tool: str,
    log_text: str,
    *,
    summary: str = "",
) -> dict[str, Any] | None:
    """Offload one heavy tool log. None = the text is not heavy (status quo).

    1. wiki.add(work_notes, refs-<slug>, full log) — the evidence lands in
       Markdown (FTS search over the log works via the wiki surface);
    2. the graph node will appear via wiki_graph_builder (a wiki_page node with
       content=file_path) — the "Mermaid canvas with node_id", standard path;
    3. ref_path is returned to the caller (drill-down: wm.get(ref_path)).
    """
    text = log_text.strip()
    if len(text) <= REFS_THRESHOLD_CHARS:
        return None
    ts = time.time()
    title = slugify(tool, ts)
    head = text[:300].replace("\n", " ")
    body = (
        f"# {tool} tool log\n\n"
        f"Offloaded: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(ts))} UTC · "
        f"{len(text)} chars · by {user_id}\n\n```\n{text}\n```\n"
    )
    abs_path = str(await wiki.add("work_notes", title, body))
    # wiki.add returns the absolute file path; externally we expose a portable refs path
    ref_path = f"work_notes/{title}.md"
    return {"ref_path": ref_path, "abs_path": abs_path, "chars": len(text), "head": head}
