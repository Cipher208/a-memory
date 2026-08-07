# Episodic Memory

L2 memory for session-level summaries.

## Usage

```python
from core.episodic import EpisodicMemory

ep = EpisodicMemory()

# Create session
await ep.create_session(user_id="u1", summary="Discussed architecture decisions")

# Get sessions
sessions = await ep.get_sessions(user_id="u1", limit=10)

# Get latest
latest = await ep.get_latest(user_id="u1")
```
