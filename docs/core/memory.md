# Core Memory

L4 long-term key-value store with typed memory.

## Usage

```python
from core.memory import CoreMemory

cm = CoreMemory()

# Store
await cm.store(user_id="u1", key="preference", value="dark mode", kind="preference")

# Retrieve
fact = await cm.retrieve(user_id="u1", key="preference")

# List all
facts = await cm.get_all(user_id="u1")
```

## Typed Memory

Each fact has a `kind` that determines retention:

- **instruction**, **rule**, **commitment**: never decay, never archive
- **fact**: exponential decay, archive when old + low importance
- **preference**: slow decay, rarely archived
- **observation**: fast decay, quickly archived
