# Session Store

L2 memory for working sessions.

## Usage

```python
from core.session import SessionStore

ss = SessionStore()

# Open a session
session_id = await ss.create_session(user_id="u1")

# Close with summary
await ss.close_session(session_id=session_id, summary="Discussed the weather")

# Get recent sessions
recent = await ss.get_recent_sessions(user_id="u1", limit=10)
```
