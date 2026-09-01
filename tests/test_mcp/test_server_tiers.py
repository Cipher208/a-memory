"""ARIEL_EXPOSE tier resolution — Phase D coherent groups (context/insight/write)."""

from mcp_server.server import EXTRA_TIERS, PRIMITIVE_TOOLS, resolve_exposure


def _all_names() -> set[str]:
    from mcp_server.tools_layer import _register_tools

    return set(_register_tools)


def test_primitives_default():
    exposed = resolve_exposure("primitives", _all_names())
    assert exposed == set(PRIMITIVE_TOOLS)
    assert len(exposed) == 6


def test_context_tier_exact_set():
    exposed = EXTRA_TIERS["context"]("context", _all_names())
    assert exposed == {
        "memory_recall_protocol",
        "memory_recap",
        "memory_get_smart_context",
        "memory_context",
        "memory_context_inject",
        "memory_steering",
        "memory_compress",
    }


def test_insight_tier_exact_set():
    exposed = EXTRA_TIERS["insight"]("insight", _all_names())
    assert exposed == {
        "memory_query",
        "memory_fact_blame",
        "memory_history",
        "memory_quality",
        "memory_reflect",
        "memory_stats",
        "memory_search",
        "memory_recall",
        "memory_episode_recall",
        "memory_episode_list",
        "memory_episode_get",
        "memory_session_list",
        "memory_graph_query",
        "memory_graph_nodes",
        "memory_graph_edges",
    }


def test_write_tier_exact_set():
    exposed = EXTRA_TIERS["write"]("write", _all_names())
    assert exposed == {
        "memory_remember",
        "memory_save_typed",
        "memory_load_rules",
        "memory_scratchpad",
        "memory_counterfactual",
        "memory_branch",
        "memory_history",
        "memory_stash",
        "memory_episode_save",
        "memory_graph_add",
        "memory_session_start",
        "memory_session_end",
    }


def test_full_agent_exposure_combo():
    """The live-agent combo: primitives + all six tiers."""
    exposed = resolve_exposure("primitives,context,insight,write,wiki,brief,review", _all_names())
    # admin surfaces stay hidden
    hidden = {
        "memory_forget",
        "memory_watch",
        "memory_api_key",
        "memory_backup",
        "memory_saga",
        "memory_data",
        "memory_sync_replica",
        "memory_cleanup",
        "memory_lucidity_purge",
    }
    assert exposed.isdisjoint(hidden)
    assert exposed >= PRIMITIVE_TOOLS
    assert "memory_recall_protocol" in exposed and "memory_remember" in exposed
    assert "wiki_read" in exposed and "daily_brief" in exposed and "memory_proposals" in exposed


def test_unknown_tier_ignored_and_tiers_cover_no_overlaps_with_primitives():
    all_names = _all_names()
    assert resolve_exposure("primitives,bogus_tier", all_names) == set(PRIMITIVE_TOOLS)
    for tier in ("context", "insight", "write"):
        assert EXTRA_TIERS[tier](tier, all_names).isdisjoint(PRIMITIVE_TOOLS)
