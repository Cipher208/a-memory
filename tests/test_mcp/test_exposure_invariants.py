"""Stage 2-C: exposure invariants — orphans=0, admin tier, presets."""

from __future__ import annotations

import mcp_server.tools_layer  # noqa: F401 — populates the registry
from mcp_server.registry import get_all_tools
from mcp_server.server import EXTRA_TIERS, PRIMITIVE_TOOLS, resolve_exposure


def test_no_orphan_tools() -> None:
    """Union of primitives + all tiers must cover the registry exactly."""
    names = set(get_all_tools())
    covered = set(PRIMITIVE_TOOLS)
    for matcher in EXTRA_TIERS.values():
        covered |= matcher("x", names)
    assert names - covered == set(), f"orphan tools: {sorted(names - covered)}"


def test_admin_tier_membership() -> None:
    names = set(get_all_tools())
    admin = EXTRA_TIERS["admin"]("admin", names)
    assert admin == {
        "memory_api_key",
        "memory_backup",
        "memory_cleanup",
        "memory_data",
        "memory_lucidity_purge",
        "memory_saga",
        "memory_sync_replica",
    }


def test_skill_promote_in_write_and_brief_tier_gone() -> None:
    names = set(get_all_tools())
    assert "memory_skill_promote" in EXTRA_TIERS["write"]("write", names)
    assert "daily_brief" in EXTRA_TIERS["review"]("review", names)
    assert "brief" not in EXTRA_TIERS


def test_legacy_brief_string_still_resolves() -> None:
    names = set(get_all_tools())
    # old configs had 'brief' — unknown tier must be ignored, not crash
    exposed = resolve_exposure("primitives,wiki,brief", names)
    assert "wiki_add" in exposed
    assert "daily_brief" not in exposed  # brief tier no longer exists


def test_preset_agent_excludes_admin() -> None:
    names = set(get_all_tools())
    exposed = resolve_exposure("agent", names)
    assert "think" in exposed
    assert "memory_query" in exposed
    assert "memory_api_key" not in exposed
    assert "memory_saga" not in exposed


def test_preset_operator_superset_of_agent() -> None:
    names = set(get_all_tools())
    agent = resolve_exposure("agent", names)
    operator = resolve_exposure("operator", names)
    assert agent < operator
    assert "memory_backup" in operator


def test_preset_full_equals_all() -> None:
    names = set(get_all_tools())
    assert resolve_exposure("full", names) == names


def test_live_agent_legacy_combo_unchanged_count() -> None:
    """The exact string in the three live-agent configs: measured count."""
    names = set(get_all_tools())
    exposed = resolve_exposure("primitives,context,insight,write,wiki,brief,review", names)
    # Pre-C measurement: 57 (brief tier added daily_brief => +1 after C).
    # Measured wins over plan: re-probe if this fails.
    assert len(exposed) == 58
