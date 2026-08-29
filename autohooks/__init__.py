# autohooks/__init__.py
"""Universal autohooks runtime (spec 2026-08-29-c19-autohooks-daemon-design).

Tails an agent's SQLite conversation store and pushes lifecycle events into
the ariel dispatcher in-process. config/source are ariel-free by design —
the CLI sets MCP_MEMORY_DATA_DIR before importing anything ariel-side.
"""
