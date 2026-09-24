---
version: 1.0.0
updated: 2026-09-24
triggers: [ariel, pytest, gate, push, mcp-ariel-memory]
---

# ariel-memory: gates & traps

- Live clone ONLY: `~/mcp-ariel-memory`. `~/Projects/repos/mcp-ariel-memory` is STALE.
- Venv: `~/mcp-ariel-memory/.venv/bin/python -m pytest`.
- Pre-push gate FULL-REPO, no pipes: ruff check → ruff format --check → mypy
  (`features/ shared/ mcp_server/ rag/ hooks/ wiki/ lifecycle/ graph/ core/
  autohooks/`) → pytest `-q --junitxml`.
- `df /tmp` before push (full tmpfs kills gate silently).
- Comments/docstrings ENGLISH only. pyproject change → uv.lock same commit.
- Shared e5 service `ariel-embeddings.service` (:8710): do not restart casually.
- Each MCP server gets its own venv; never share agent runtimes.
