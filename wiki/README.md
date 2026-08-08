# Wiki System

Migrated to a dedicated `wiki/` package. Provides a modular structure for managing agent and user wikis with FTS5 search.

## Architecture

- **WikiManager**: Orchestrator coordinating file I/O (WikiParser) and database indexing (WikiIndex).
- **WikiParser**: Handles frontmatter parsing and Markdown generation.
- **WikiIndex**: Manages SQLite FTS5 search and metadata storage.
- **Layer Separation**: Clear separation between `user` and `agent` wikis.

## Features

- **FTS5 Search**: High-performance full-text search across all wiki pages.
- **Frontmatter Support**: Structured metadata (tags, importance, created_at) inside .md files.
- **Hash-based Reindexing**: Efficient reindexing that only touches changed files.
- **External Sync**: Ability to import and sync external Markdown folders into the wiki layers.

## Usage

```python
from wiki.manager import WikiManager

manager = WikiManager(layer="user")
await manager.init_db()

# Add entry
path = await manager.add(wiki_type="diary", title="Morning thoughts", content="Hello world", tags=["daily"])

# Search
results = await manager.search("thoughts")
```
