"""
Wiki Module — .md files as source of truth + SQLite FTS5 index
"""

from .index import WikiIndex
from .manager import ALL_AGENT_TYPES, ALL_USER_TYPES, WikiManager
from .models import WikiEntry
from .parser import WikiParser

# Backward-compatible aliases
FileWiki = WikiManager
UserWiki = WikiManager
AgentWiki = WikiManager

__all__ = [
    "ALL_AGENT_TYPES",
    "ALL_USER_TYPES",
    "AgentWiki",
    "FileWiki",
    "UserWiki",
    "WikiManager",
    "WikiIndex",
    "WikiEntry",
    "WikiParser",
]
