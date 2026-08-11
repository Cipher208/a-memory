from __future__ import annotations

"""Shared constants — eliminates string duplication across codebase."""

# Database
DB_NAME = "memory.db"

# Encoding
UTF8 = "utf-8"

# Defaults
DEFAULT_USER = "default"
DEFAULT_LAYER = "user"
AGENT_LAYER = "agent"

# Sagas
SAGA_DIR_NAME = "sagas"
SAGA_EXT = ".json"
STATUS_STUCK = "stuck"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_COMPENSATING = "compensating"
STATUS_COMPENSATED = "compensated"

# Metrics
METRIC_TOOL_CALLS = "tool_calls"

# Wiki
WIKI_DIR_NAME = "wiki"
WIKI_TYPE_GENERAL = "general"
