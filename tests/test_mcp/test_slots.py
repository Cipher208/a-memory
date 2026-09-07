"""Stage 2-C: slot map cross-checks against the live tool registry."""

from __future__ import annotations

import pytest

import mcp_server.tools_layer  # noqa: F401 — populates the registry
from mcp_server.registry import get_all_tools
from mcp_server.slots import SLOTS, SLOT_NAMES, slot_of

# Tools that do not exist in the registry yet but have a slot assigned.
EXPECTED_FORWARD = {"wake_up"}


def test_every_registry_tool_has_exactly_one_slot() -> None:
    names = set(get_all_tools())
    mapped = set(SLOTS)
    orphans = names - mapped - EXPECTED_FORWARD
    assert orphans == set(), f"registry tools without a slot: {sorted(orphans)}"
    # forward entries must be minimal: only documented upcoming tools
    unknown = (mapped - names) - EXPECTED_FORWARD
    assert unknown == set(), f"slots reference unknown tools: {sorted(unknown)}"


def test_slot_names_are_closed_set() -> None:
    assert set(SLOT_NAMES) == {
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
    }
    bad = {slot for slot in SLOTS.values() if slot not in SLOT_NAMES}
    assert bad == set()


def test_no_tool_in_two_groups() -> None:
    """A tool listed in two _SLOTS_BY_GROUP groups would be silently
    overwritten by the dict comprehension — this test makes that loud."""
    from mcp_server.slots import _SLOTS_BY_GROUP

    seen: dict[str, str] = {}
    dupes: list[str] = []
    for slot, tools in _SLOTS_BY_GROUP.items():
        for tool in tools:
            if tool in seen:
                dupes.append(f"{tool}: {seen[tool]} vs {slot}")
            seen[tool] = slot
    assert dupes == [], f"tools in multiple slot groups: {dupes}"


def test_slot_of_known_and_unknown() -> None:
    assert slot_of("memory_remember") == "write"
    assert slot_of("wiki_search") == "wiki"
    with pytest.raises(KeyError):
        slot_of("no_such_tool")


def test_admin_slot_membership() -> None:
    expected_admin = {
        "memory_api_key",
        "memory_backup",
        "memory_cleanup",
        "memory_data",
        "memory_lucidity_purge",
        "memory_saga",
        "memory_sync_replica",
    }
    got = {t for t, s in SLOTS.items() if s == "admin"}
    assert got == expected_admin
    assert "memory_skill_promote" not in got  # moved to write
