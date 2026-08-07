# Monitoring

## Metrics

Built-in metrics collection:

- Memory operations count
- Search latency
- Hook execution time
- Backup status

## Dashboard

Real-time dashboard available at `/dashboard` (when HTTP transport enabled).

## Logging

Structured logging with Python logging module:

```python
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-ariel-memory")
```
