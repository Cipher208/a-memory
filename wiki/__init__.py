"""
Wiki Module — .md files as source of truth + SQLite FTS5 index
"""

from .manager import ALL_AGENT_TYPES, ALL_USER_TYPES, WikiManager

# Backward-compatible aliases
FileWiki = WikiManager
UserWiki = WikiManager
AgentWiki = WikiManager

__all__ = ["ALL_AGENT_TYPES", "ALL_USER_TYPES", "AgentWiki", "FileWiki", "UserWiki", "WikiManager"]
