# ReflexBuffer

Ring buffer for recent messages (L1 memory).

## Properties

- **FIFO eviction**: oldest entries removed when full
- **Thread-safe**: uses `threading.Lock`
- **Persistent**: optional JSON file persistence
- **Concurrent-safe**: verified with 10-thread stress test

## Usage

```python
from core.reflex import ReflexBuffer

buf = ReflexBuffer(max_size=50, persist_path="/path/to/buffer.json")

# Add entries
buf.add(role="user", content="Hello", tokens=5)
buf.add(role="assistant", content="Hi there!", tokens=3)

# Get recent
recent = buf.get_recent(10)  # last 10 entries
full = buf.get_full()  # all entries

# Info
print(buf.size())  # current size
buf.clear()  # reset
```

## Hypothesis Tests

Property-based tests verify:

- `size() <= max_size` for any sequence of adds
- `get_recent(k)` returns last `min(k, size)` entries
- FIFO order maintained under concurrent access
- 10 threads × 100 adds on buffer of size 50 → no crashes
