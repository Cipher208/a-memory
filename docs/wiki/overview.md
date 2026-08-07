# Wiki System

## Overview

Wiki system with .md files as source of truth and FTS5 full-text search.

## Content Types

14 types: spec, decision, error, code, note, guide, reference, tutorial, faq, changelog, architecture, api, example, concept

## WikiManager

File-based wiki with external folder sync:

```python
from wiki.manager import WikiManager

fw = WikiManager(layer="user")
fw.add_page(title="Architecture", content="# Architecture\n...", wiki_type="spec")
results = fw.search("architecture", limit=5)
```

## UserWiki (layer=user) (deprecated - use WikiManager)

User-specific wiki pages:

```python
from wiki.manager import WikiManager

uw = WikiManager(layer="user")
await uw.add_page(user_id="u1", title="My Notes", content="...")
```

## AgentWiki (layer=agent) (deprecated - use WikiManager)

Agent identity wiki:

```python
from wiki.manager import WikiManager

aw = WikiManager(layer="agent")
await aw.add_page(title="Learning", content="...")
```
