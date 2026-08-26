# Episodic Memory

L3 memory for important moments with emotional weight and tags.

## Usage

```python
from core.episodic import EpisodicMemory

ep = EpisodicMemory()

# Save an episode
await ep.save(user_id="u1", summary="Chose PostgreSQL over MySQL", emotional_weight=0.6, tags=["decision"])

# List episodes
episodes = await ep.get_episodes(user_id="u1", limit=10)

# Search by tag
tagged = await ep.search_by_tag(user_id="u1", tag="decision")
```
