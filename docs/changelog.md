# Changelog

Full history lives in [CHANGELOG.md](https://github.com/Cipher208/a-memory/blob/master/CHANGELOG.md).

## v1.7.0 (2026-08-24)

### Highlights
- **mcp 2.x native** (`mcp.server.mcpserver.MCPServer`), `mcp[cli]>=2,<3`
- **5 universal primitives by default** (`think`/`dream`/`forget`/`evolve`/`project`); full surface via `ARIEL_EXPOSE=all`
- **Layer registry** — one-call addition of new memory layers
- **Project memory layer** — decisions, artifact map, graphify code index in a separate `projects.db`
- **Dream staging pipeline** restored and wired into the hourly consolidation sweep
- **DB maintenance loop** — size thresholds, metrics, auto-VACUUM
- **Layer isolation** for core_memory/episodes (alembic migration)
- Config drift warnings for per-agent configs; ~15 previously dead yaml keys now live

See the repo CHANGELOG for the complete list.
