# WikiManager

File-based wiki with .md files as source of truth.

## Features

- FTS5 full-text search
- External folder sync
- 14 content types
- Per-layer separation

## Usage

```python
from wiki.manager import WikiManager

fw = WikiManager(layer="user")

# Add page
fw.add_page(title="Architecture Overview", content="# Architecture\n\nTwo-layer memory system...", wiki_type="spec")

# Search
results = fw.search("architecture", limit=5)

# Count
count = fw.count()

# Reindex
fw.reindex_all()
```

## File Structure

```
~/.mcp-ariel-memory/wiki/user/
├── architecture-overview.md
├── api-reference.md
└── ...
```

## Schema lint

Every `add()` and `sync_external()` import runs a 6-check lint pass. Findings are logged as warnings; they never block the save.

| Check | Code | Default behavior |
|---|---|---|
| Frontmatter present | `frontmatter_malformed` | warning |
| Required fields (`title`, `wiki_type`) | `missing_title`, `missing_wiki_type` | warning |
| Broken `[[wikilinks]]` | `broken_wikilink` | warning |
| Page length > 50,000 chars | `page_too_long` | warning |
| Missing `INDEX.md` in type dir | `missing_index` | warning |
| Tags outside vocabulary | `unknown_tag` | warning |

Tag vocabulary = 7 hardcoded tags (`decision`, `learning`, `todo`, `principle`, `context`, `spec`, `draft`) plus the names of all currently-enabled wiki types for the layer.

**Opt-in auto-fix.** Only `missing_index` is fixable. Create the manager with `auto_fix=True`:

```python
from wiki import WikiManager

wm = WikiManager(layer="user", auto_fix=True)
await wm.add("diary", "First Page", "hello", [])
# → also creates user/diary/INDEX.md stub listing all pages in the dir
```

The `INDEX.md` stub is idempotent — second `add()` does nothing. Other 5 checks require human judgment and stay warning-only.

To run a full layer scan outside the manager (e.g. CI):

```python
from wiki import lint_wiki_layer

report = lint_wiki_layer("user", Path("~/.mcp-ariel-memory/wiki/user"), enabled_types)
print(report.findings)
```

## Secret detection

Every `add()` and `sync_external()` import also scans content for well-known
secret formats — GitHub PATs (`ghp_...`/`github_pat_...`), API keys
(`sk-...`/`AIza...`/`AKIA...`), and PEM private-key headers. Matches are
logged as WARNING (never the secret value itself, only its kind) and **never
block the save**. The detector is conservative to avoid false positives on
short or partial strings.

```python
from wiki.secrets import scan_secrets

findings = scan_secrets(content)  # -> [SecretFinding(kind=..., location="body")]
```

## Ref chain linking

Pages can be linked with typed relationships — `review_of`, `revises`, or
`follows` — so an agent can traverse a review/revision history. Links are
stored in a `wiki_links` table (separate from page content).

`[[wikilinks]]` in page content are auto-linked to resolvable pages on
`add()`/`update()` (resolved by title or filename stem; unresolvable links are
silently skipped). Explicit links go through the `wiki_link` tool or
`WikiManager`:

```python
from mcp_server.tools.wiki_link import wiki_link

await wiki_link(action="add", from_path="a.md", to_path="b.md", link_type="review_of")
links = await wiki_link(action="list", from_path="a.md")
# -> {status: ok, links: [{path, link_type, direction}]}
```
