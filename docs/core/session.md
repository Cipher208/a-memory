# Session Store

L3 memory for conversation entries.

## Usage

```python
from core.session import SessionStore

ss = SessionStore()

# Add entry
await ss.add_entry(user_id="u1", role="user", content="What's the weather?", tokens=5)

# Get entries
entries = await ss.get_entries(user_id="u1", limit=20)

# Get recent
recent = await ss.get_recent(user_id="u1", n=10)
```
