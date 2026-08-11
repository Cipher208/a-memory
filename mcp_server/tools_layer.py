from __future__ import annotations

"""Layer tools — unified user/agent memory operations.

Refactored into modular tool categories.
"""

from typing import Any
from mcp_server.registry import register_tool

from .tools.memory import memory_remember, memory_recall, memory_forget
from .tools.session import memory_session_start, memory_session_end, memory_session_list
from .tools.episodic import memory_episode_save, memory_episode_recall, memory_episode_list, memory_episode_get
from .tools.graph import memory_graph_add, memory_graph_query, memory_graph_nodes, memory_graph_edges
from .tools.wiki import wiki_add, wiki_search, wiki_list, wiki_delete
from .tools.ops import (
    memory_stats,
    memory_context,
    memory_context_inject,
    memory_api_key,
    memory_backup,
    memory_saga,
    memory_data,
    memory_sync_replica,
    memory_cleanup,
    memory_lucidity_purge,
    memory_search,
)

# Re-export _fire_hook for backward compatibility and tests


# For tests that monkeypatch tl._fire_hook
def _fire_hook_wrapper(*args, **kwargs):
    from .tools import base

    return base._fire_hook(*args, **kwargs)


# Register all layer tools
_register_tools: dict[str, Any] = {
    "memory_remember": memory_remember,
    "memory_recall": memory_recall,
    "memory_forget": memory_forget,
    "memory_session_start": memory_session_start,
    "memory_session_end": memory_session_end,
    "memory_episode_save": memory_episode_save,
    "memory_episode_recall": memory_episode_recall,
    "memory_graph_add": memory_graph_add,
    "memory_graph_query": memory_graph_query,
    "memory_session_list": memory_session_list,
    "memory_episode_list": memory_episode_list,
    "memory_episode_get": memory_episode_get,
    "memory_graph_nodes": memory_graph_nodes,
    "memory_graph_edges": memory_graph_edges,
    "memory_stats": memory_stats,
    "memory_context": memory_context,
    "memory_context_inject": memory_context_inject,
    "memory_api_key": memory_api_key,
    "memory_backup": memory_backup,
    "memory_saga": memory_saga,
    "memory_data": memory_data,
    "memory_sync_replica": memory_sync_replica,
    "memory_cleanup": memory_cleanup,
    "memory_lucidity_purge": memory_lucidity_purge,
    "memory_search": memory_search,
    "wiki_add": wiki_add,
    "wiki_search": wiki_search,
    "wiki_list": wiki_list,
    "wiki_delete": wiki_delete,
}

for _name, _func in _register_tools.items():
    register_tool(_name, _func)
