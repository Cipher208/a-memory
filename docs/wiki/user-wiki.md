# UserWiki (deprecated - use WikiManager)

User-specific wiki pages stored in database.

## Usage

```python
from wiki.manager import WikiManager

uw = WikiManager(layer="user")

# Add page
await uw.add_page(user_id="u1", title="My Notes", content="# Notes\n\nImportant things to remember...")

# Search
results = await uw.search(user_id="u1", query="notes")

# Get page
page = await uw.get_page(user_id="u1", page_id=1)
```
