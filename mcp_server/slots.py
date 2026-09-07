"""Stage 2-C: semantic slot map for every tool (tool -> slot).

Single static map, cross-checked against the live registry by
tests/test_mcp/test_slots.py. Slots are the catalog structure for the
meta-tool layer (server.py) and the documentation unit for
docs/tools/exposure.md. Tier membership lives in server.py EXTRA_TIERS —
a tool's slot and its tier(s) are related but distinct concerns
(e.g. daily_brief: slot=review, former tier=brief).
"""

from __future__ import annotations

SLOT_NAMES: tuple[str, ...] = (
    "core",
    "recall",
    "context",
    "episodes",
    "sessions",
    "graph",
    "wiki",
    "insight",
    "write",
    "review",
    "admin",
    "brief",
)

_SLOTS_BY_GROUP: dict[str, tuple[str, ...]] = {
    "core": (
        "think",
        "dream",
        "forget",
        "evolve",
        "project",
        "memory_hook",
        "wake_up",  # Stage 2-C E10 — registered with the wake_up tool task
    ),
    "recall": (
        "memory_recall",
        "memory_search",
        "memory_recall_protocol",
        "memory_get_smart_context",
        "memory_recap",
    ),
    "context": (
        "memory_context",
        "memory_context_inject",
        "memory_steering",
        "memory_compress",
    ),
    "episodes": (
        "memory_episode_save",
        "memory_episode_recall",
        "memory_episode_list",
        "memory_episode_get",
    ),
    "sessions": (
        "memory_session_start",
        "memory_session_end",
        "memory_session_list",
    ),
    "graph": (
        "memory_graph_add",
        "memory_graph_query",
        "memory_graph_nodes",
        "memory_graph_edges",
    ),
    "wiki": (
        "wiki_add",
        "wiki_search",
        "wiki_list",
        "wiki_read",
        "wiki_delete",
        "wiki_summarize",
        "wiki_link",
        "wiki_reflect",
        "wiki_query",
    ),
    "insight": (
        "memory_query",
        "memory_fact_blame",
        "memory_history",
        "memory_quality",
        "memory_reflect",
        "memory_stats",
        "memory_diagnose",
        "memory_standing",
        "memory_procedure",
        "memory_scratchpad",
    ),
    "write": (
        "memory_remember",
        "memory_save_typed",
        "memory_load_rules",
        "memory_counterfactual",
        "memory_branch",
        "memory_stash",
        "memory_heal",
        "memory_disclose",
        "memory_skill_promote",
    ),
    "review": (
        "memory_watch",
        "memory_proposals",
        "memory_report_card",
        "daily_brief",  # tier brief dissolved into review (Stage 2-C)
    ),
    "admin": (
        "memory_api_key",
        "memory_backup",
        "memory_cleanup",
        "memory_data",
        "memory_lucidity_purge",
        "memory_saga",
        "memory_sync_replica",
    ),
    # brief: legacy slot name, no members (tier dissolved into review)
    "brief": (),
}

SLOTS: dict[str, str] = {tool: slot for slot, tools in _SLOTS_BY_GROUP.items() for tool in tools}


def slot_of(name: str) -> str:
    """Return the slot for a tool name; KeyError on unknown."""
    return SLOTS[name]
