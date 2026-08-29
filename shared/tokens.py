"""Token estimation — pure text helpers (moved verbatim from mcp_server.tools.base).

base.py re-exports them as the original underscore aliases so existing callers
are untouched. Living in shared/ breaks the hooks → base → context import cycle
(created when features/inject.py needed token budgeting without importing base).
"""

from __future__ import annotations

import re

DEFAULT_TOKEN_BUDGET = 2000
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk_count = len(re.findall(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]", text))
    remaining_chars = len(text) - cjk_count
    non_cjk_tokens = remaining_chars // CHARS_PER_TOKEN
    return cjk_count + non_cjk_tokens


def truncate_to_budget(text: str, max_tokens: int) -> tuple[str, bool]:
    estimated = estimate_tokens(text)
    if estimated <= max_tokens:
        return text, False
    char_limit = max_tokens * CHARS_PER_TOKEN
    lines = text.split("\n")
    result_lines = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1
        if current_len + line_len > char_limit:
            break
        result_lines.append(line)
        current_len += line_len
    truncated = "\n".join(result_lines)
    truncated += "\n[...truncated to token budget]"
    return truncated, True
