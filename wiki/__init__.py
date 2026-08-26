"""Wiki Module — .md files as source of truth + SQLite FTS5 index."""

from .index import WikiIndex
from .lint import (
    Finding,
    LintReport,
    auto_fix_type_dirs,
    lint_entry,
    lint_wiki_layer,
    wiki_lint_tag_vocabulary,
)
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
    "Finding",
    "LintReport",
    "UserWiki",
    "WikiEntry",
    "WikiIndex",
    "WikiManager",
    "WikiParser",
    "auto_fix_type_dirs",
    "lint_entry",
    "lint_wiki_layer",
    "wiki_lint_tag_vocabulary",
]
