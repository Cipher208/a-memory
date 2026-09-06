"""A4 channel granularity: canonical `channel[:detail]` format for core_memory.source.

Existing free-form sources stay readable (channel_of parses prefix, unknown →
'other'); new writes SHOULD use canonical_source. No DB migration — the
channel is the prefix convention, not a column.
"""

from __future__ import annotations

KNOWN_CHANNELS: frozenset[str] = frozenset(
    {
        "user_explicit",
        "episode_promotion",
        "auto_save",
        "tool",
        "import",
        "shared",
        "consolidation",
        "branch_merge",
        "snapshot_restore",
    }
)


def channel_of(source: str | None) -> str:
    """`'tool:mcp_remember' → 'tool'`; None/empty/unknown → 'other'."""
    if not source:
        return "other"
    ch = str(source).split(":", 1)[0].strip().lower()
    return ch if ch in KNOWN_CHANNELS else "other"


def canonical_source(channel: str, detail: str = "") -> str:
    """Build a source string from a known channel (+optional detail)."""
    ch = str(channel).strip().lower()
    if ch not in KNOWN_CHANNELS:
        raise ValueError(f"unknown channel: {channel!r}; expected one of {sorted(KNOWN_CHANNELS)}")
    return f"{ch}:{detail}" if detail else ch
