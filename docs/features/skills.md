# Skills — Skill = Memory (D2.1-D2.4)

Skills are **agent-read Markdown pages**: the durable, distilled instructions
an agent accumulates ("how we deploy", "what the user prefers in code
review"). No embeddings for retrieval — the agent decides what to load via
**progressive disclosure**: `wiki_list → wiki_search → wiki_read`.

Skills are wiki pages of the first-class `skill` type — plain Markdown with
frontmatter, living on disk (git-versionable), indexed in FTS5.

## The lifecycle

```
DREAM: skill: <distilled instruction>        (agent writes it mid-conversation)
        │  C1.12 auto-save marker
        ▼
L3 episode tagged dream_skill                 (importance 0.95, review-gated)
        │  nightly phase 4: skill_promotion   or memory_skill_promote (manual)
        ▼
skill wiki page (Markdown, ≤4KB)              wiki_read → progressive disclosure
        │  nightly phase 6: skill_reinforce   (reads → importance +0.05)
        │  promotion merges into existing pages by title/topic-prefix
        ▼
shared SSOT  ~/skills-ssot/skill/*.md         scripts/sync_skills.py → all agents
```

## Tools

| Tool | Purpose |
|---|---|
| `wiki_list(wiki_type="skill")` | the disclosure index — titles + `path` |
| `wiki_search(query)` | titles + tags + 200-char snippets |
| `wiki_read(path)` | **full page content** (the read leg) |
| `wiki_add(wiki_type="skill", ...)` | author/update a skill directly |
| `memory_skill_promote(...)` | promote episodes (`episode_ids`) or write an agent-distilled skill (`title`+`content`) |

`memory_skill_promote` is idempotent: promoted episodes get the
`skill_promoted` tag and are skipped on retry. Promotion **merges** into an
existing skill page when the episode title matches (full title or the topic
prefix before `:`) instead of forking a duplicate.

## Lint

`skill_too_large` — a skill page over **4KB** is flagged (concept cap: API
table at the top, <100 lines). Split skills instead of growing them.

## Shared SSOT across agents (D2.3)

The write surface is a git repo; agents read synced copies:

```bash
python scripts/sync_skills.py --bootstrap   # create ~/skills-ssot (git repo)
# edit / commit skills in ~/skills-ssot/skill/*.md
python scripts/sync_skills.py               # sync into all live agent wikis
```

- Sync uses `WikiManager.sync_external`: copy + sha256 dedup + index — each
  agent's SQLite index stays consistent (symlinks were rejected for this).
- Each agent syncs in its own subprocess (`MCP_MEMORY_DATA_DIR` isolation);
  aiosqlite connections are closed before exit (no shutdown hang).
- Targets: `~/.mcp-ariel-memory-{hermes,mimocode,cowagent}`. Add an agent by
  extending the `AGENTS` map in `scripts/sync_skills.py`.
- Cron-ready: run the bare script on a schedule; hash-dedup makes repeats
  no-ops.

## Where things live

| Artifact | Path |
|---|---|
| Skill pages (per agent) | `~/.mcp-ariel-memory-<agent>/wiki/user/skill/*.md` |
| Shared SSOT | `~/skills-ssot/skill/*.md` (git) |
| Sync script | `scripts/sync_skills.py` |
| Promotion pipeline | `features/skill_pipeline.py` |
| Nightly phases 4/6 | `hooks/user_hooks.py::_nightly` |
