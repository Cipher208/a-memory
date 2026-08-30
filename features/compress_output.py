"""D1.4 tool-output compression — shrink bulky tool/text output before re-injection.

Two modes:
- log: build/test logs → errors only (error/warn/fail patterns), consecutive
  duplicate lines collapsed, capped; first line always kept (it is usually
  the command/header).
- code: Python source → skeleton via ast.unparse (signatures kept, bodies
  dropped). Ceiling: nested defs are dropped with their parent body; a
  SyntaxError falls back to log mode (mode field reports what was used).

Deterministic, no LLM.
"""

from __future__ import annotations

import ast

_LOG_RE = ("error", "warn", "fail", "fatal", "critical", "panic", "exception", "traceback", "assert", "ошибка", "падает", "сбой")


def _is_interesting(line: str) -> bool:
    low = line.lower()
    return any(p in low for p in _LOG_RE)


def compress_log(text: str, max_lines: int = 50) -> str:
    """Keep error/warn lines (+ header), collapse consecutive duplicates, cap."""
    lines = text.splitlines()
    if not lines:
        return ""
    kept: list[str] = [lines[0]] if lines[0].strip() else []
    for line in lines[1:]:
        if _is_interesting(line):
            if kept and kept[-1] == line:
                continue
            kept.append(line)
    if len(kept) > max_lines:
        kept = [*kept[:max_lines], f"... truncated, {len(kept) - max_lines} more error lines"]
    return "\n".join(kept)


def skeletonize_python(source: str) -> str:
    """Drop function bodies to '...' placeholders; signatures + classes stay."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node.body = [ast.Expr(value=ast.Constant(value="..."))]
    return ast.unparse(tree)


def compress_output(text: str, mode: str = "auto", max_lines: int = 50) -> dict[str, str | int]:
    """Compress bulky text. Returns {mode, original_lines, kept_lines, text}."""
    text = str(text)
    used = mode
    if mode in ("auto", "code"):
        try:
            out = skeletonize_python(text)
            used = "code"
        except SyntaxError:
            if mode == "code":
                out = compress_log(text, max_lines)
                used = "code->log"
            else:
                out = compress_log(text, max_lines)
                used = "log"
    else:
        out = compress_log(text, max_lines)
    return {
        "mode": used,
        "original_lines": len(text.splitlines()),
        "kept_lines": len(out.splitlines()),
        "text": out,
    }
