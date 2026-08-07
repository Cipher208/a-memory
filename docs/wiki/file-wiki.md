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
