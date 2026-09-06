"""S18-хвост offload: тяжёлые tool-логи → work_notes/refs-*.md, контекст видит ref.

TencentDB v2-паттерн (competitive-intel-2): сотни тысяч токенов логов не живут
в контексте/графе — доказательства оффлоадятся в Markdown-файл, а в графе/
контексте остаётся compact-ссылка (сотни токенов вместо сотен тысяч).

refs живут как work_notes-страницы с префиксом `refs-` (НЕ новый wiki_type —
surface/types не расширяются). Графовый узел НЕ создаётся руками: wiki-страница
становится epi_node через wiki_graph_builder (F-T9 single-entry, AST-инвариант
test_grep_invariant_no_bypass_add_node) — «Mermaid-канвас с node_id» получается
штатно, без bypass записи.
"""

from __future__ import annotations

import re
import time
from typing import Any

REFS_THRESHOLD_CHARS = 4000  # тяжелее — оффлоадим
_SLUG_RE = re.compile(r"[^a-z0-9а-яё]+")


def slugify(tool: str, ts: float) -> str:
    """refs-<date>-<tool>-<hhmmss> — детерминированно, без коллизий в секунду."""
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
    """Оффлоад одного тяжёлого tool-лога. None = текст не тяжёлый (статус-кво).

    1. wiki.add(work_notes, refs-<slug>, полный лог) — доказательства в Markdown
       (FTS-поиск по логу работает через wiki-поверхность);
    2. графовый узел появится через wiki_graph_builder (wiki_page-узел с
       content=file_path) — «Mermaid-канвас с node_id» штатно;
    3. ref_path возвращается вызывавшему (drill-down: wm.get(ref_path)).
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
    # wiki.add возвращает абсолютный путь файла; наружу — переносимый refs-путь
    ref_path = f"work_notes/{title}.md"
    return {"ref_path": ref_path, "abs_path": abs_path, "chars": len(text), "head": head}
