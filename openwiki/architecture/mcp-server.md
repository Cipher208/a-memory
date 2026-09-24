---
type: "Reference"
title: "MCP server"
openwiki_generated: true
verified:
  - by: openwiki/0.6.0
    at: 2026-09-24T20:32:03.830Z
sources:
  - id: openwiki-source-92ddeafbf5685f53a3443811
    resource: repo://embeddings_service.py
  - id: openwiki-source-cf7a691b6dbb3f5cf84511a3
    resource: repo://mcp_server/server.py
  - id: openwiki-source-bdc2baa12b74cc7662119bae
    resource: repo://mcp_server/tools/primitives/routing.py
  - id: openwiki-source-b80db679a570090482dc0e53
    resource: repo://mcp_server/tools/primitives/think.py
generated: { by: "opencode", at: "2026-09-24T20:32:03.830Z" }
---

# MCP server

The repo ships a stdio MCP server built on FastMCP. It is the only supported
way for agents to read and write memory — there is no remote API.

## Layout (`mcp_server/`)

- `server.py`: entry point. `_register_all_tools()` registers every tool with
  `mcp.tool(...)`, wrapped by `_scope_tool`; `main()` selects stdio vs
  dashboard mode (`_run_with_dashboard`).
- `endpoints/`: one module per tool family — `memory.py`, `session.py`,
  `graph.py`, `wiki*.py`, `skills.py`, `ops.py`, `hooks.py`, `brief.py`,
  `episodic.py`, `system.py`.
- `tools/primitives/` holds the five core
  primitives: `think.py`, `dream.py`, `forget.py`, `evolve.py`, `project.py`.
- `tools/`, `utils/`: registration helpers and shared plumbing (`registry.py`,
  `tools_layer.py`, `slots.py`, `meta_tools.py`, `annotations.py`, `schema.py`,
  `models.py`, `context.py`, `lifespan.py`, `middlewares.py`).

## Exposure scoping

- Tool visibility is governed by the `ARIEL_EXPOSE` environment variable
  (default `primitives`), resolved by `resolve_exposure()` in `server.py`
  against `_EXPOSE_PRESETS`: `full` → everything, `agent` → primitives plus
  context/insight/write/wiki/review tiers, `operator` → agent plus admin.
- Operational rule from repo discipline: every MCP server gets its own
  dedicated venv — agent runtimes are never shared.

## Embeddings boundary

- Vector search does not live in the server process. One shared e5 process
  (`embeddings_service.py`, own `.venv-embeddings`) owns the
  sentence-transformers model (`intfloat/multilingual-e5-small`) and serves an
  OpenAI-compatible `POST /v1/embeddings` on loopback port 8710.
- All memory instances call it via `shared/embeddings.py` (config key
  `embeddings.url`). The service is long-lived — do not restart it casually.
