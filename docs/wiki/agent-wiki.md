# AgentWiki (deprecated - use WikiManager)

Agent identity wiki for storing agent learning and evolution.

## Usage

```python
from wiki.manager import WikiManager

aw = WikiManager(layer="agent")

# Add page
await aw.add_page(title="Learning Patterns", content="# Learning\n\nI've learned that users prefer...")

# Search
results = await aw.search("learning patterns")

# Get page
page = await aw.get_page(page_id=1)
```
