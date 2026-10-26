"""F40: an insight is a snapshot of the current graph, not a history.

The dream's insight phase used to build the dedup key from
``f"insight ({len(members)} узлов): {top}"``. The component grows every night,
so the key never matched and the layer accumulated one node per pass forever,
each carrying an ``insight_of`` star over the whole component. The invariant is
that the layer holds at most ``INSIGHT_MAX`` insight nodes, each belonging to the
LAST pass that computed it.

These tests drive the nightly door, ``graph_enrich``, not the phase helper: the
delete must happen inside a real pass, before the inserts, and the rest of the
pass must survive it.
"""

from __future__ import annotations

from itertools import pairwise
from typing import TYPE_CHECKING, Any

import pytest

from shared.connection import connection_manager
from shared.constants import DB_NAME
from shared.migrations import MigrationManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture
async def graph(tmp_path) -> AsyncIterator[Any]:
    connection_manager.base_dir = tmp_path
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()

    from graph.epistemic import EpistemicGraph

    yield EpistemicGraph(cm=connection_manager, layer="user")
    connection_manager._conns.clear()


@pytest.fixture
def no_miners(monkeypatch):
    """The miners write too; they are not what these tests measure."""
    monkeypatch.setattr("lifecycle.graph_miners.MINERS", {})


async def _add_component(graph: Any, texts: list[str]) -> list[int]:
    """A connected component: one fact per text, a `mentions` chain between them."""
    ids = [await graph.add_node("gu", text, "fact") for text in texts]
    for left, right in pairwise(ids):
        await graph.add_edge(left, right, "mentions")
    return ids


async def _insight_ids(conn: Any) -> list[int]:
    rows = await (await conn.execute("SELECT node_id FROM epi_nodes WHERE node_type='insight' ORDER BY node_id")).fetchall()
    return [int(r["node_id"]) for r in rows]


async def _run_pass() -> dict[str, Any]:
    from lifecycle.graph_enrich import graph_enrich

    return await graph_enrich(layer="user")


@pytest.mark.asyncio
async def test_a_second_pass_replaces_the_first(graph, no_miners):
    """Same component, grown between passes → exactly one insight, the second pass's."""
    conn = await connection_manager.get(DB_NAME)
    a, b, c = await _add_component(graph, ["кэш redis обслуживает воркеры", "воркеры читают кэш redis", "воркеры выполняют задачи очереди"])

    await _run_pass()
    first = await _insight_ids(conn)
    assert len(first) == 1, f"the first pass materialized one insight, got {first}"
    first_id = first[0]

    d = await graph.add_node("gu", "очередь задач растёт вечером", "fact")
    await graph.add_edge(c, d, "mentions")

    await _run_pass()

    second = await _insight_ids(conn)
    assert len(second) == 1, f"a snapshot leaves one insight, not a history: {second}"
    assert second[0] != first_id, "the surviving insight is the second pass's, not the first's"
    rows = await (
        await conn.execute(
            "SELECT target_id FROM epi_edges WHERE source_id=? AND relation='insight_of'",
            (second[0],),
        )
    ).fetchall()
    assert {int(r["target_id"]) for r in rows} == {a, b, c, d}, "the new insight covers the grown component"


@pytest.mark.asyncio
async def test_the_old_insight_edges_are_gone(graph, no_miners):
    """Replacing the node must take its `insight_of` star with it — no orphan edges."""
    conn = await connection_manager.get(DB_NAME)
    await _add_component(graph, ["кэш redis обслуживает воркеры", "воркеры читают кэш redis", "воркеры выполняют задачи очереди"])

    await _run_pass()
    first = await _insight_ids(conn)
    assert len(first) == 1, f"the first pass materialized one insight, got {first}"
    first_id = first[0]

    await graph.add_node("gu", "очередь задач растёт вечером", "fact")

    await _run_pass()

    row = await (
        await conn.execute(
            "SELECT COUNT(*) FROM epi_edges WHERE relation='insight_of' AND (source_id=? OR target_id=?)",
            (first_id, first_id),
        )
    ).fetchone()
    assert row[0] == 0, f"no edge may still point at the replaced insight {first_id}"


@pytest.mark.asyncio
async def test_insights_are_capped_per_layer(graph, no_miners):
    """More qualifying components than INSIGHT_MAX leave at most INSIGHT_MAX nodes.

    Two passes, with every component grown in between: the old per-pass allowance
    let a second batch of ten land beside the first ten, so the layer held twenty.
    With R1 replacing and R3 turning the guard into a total, the second pass still
    leaves INSIGHT_MAX.
    """
    from lifecycle.graph_enrich import INSIGHT_MAX

    conn = await connection_manager.get(DB_NAME)
    tails: list[int] = []
    for i in range(INSIGHT_MAX + 1):
        component = await _add_component(graph, [f"alpha{i} beta{i} gamma{i}"] * 3)
        tails.append(component[-1])

    await _run_pass()
    assert len(await _insight_ids(conn)) == INSIGHT_MAX, "the first pass is capped"

    for i, tail in enumerate(tails):
        grown = await graph.add_node("gu", f"delta{i} epsilon{i} zeta{i}", "fact")
        await graph.add_edge(tail, grown, "mentions")

    await _run_pass()

    ids = await _insight_ids(conn)
    assert len(ids) == INSIGHT_MAX, f"the cap is a total per layer, got {len(ids)} insights"


@pytest.mark.asyncio
async def test_non_insight_nodes_and_edges_survive_the_replacement(graph, no_miners):
    """The delete is scoped to insight nodes: facts and their edges are untouched."""
    conn = await connection_manager.get(DB_NAME)
    c = (await _add_component(graph, ["кэш redis обслуживает воркеры", "воркеры читают кэш redis", "воркеры выполняют задачи очереди"]))[-1]
    f1 = await graph.add_node("gu", "Борис работает в Google", "fact")
    f2 = await graph.add_node("gu", "Борис любит кофе без сахара", "fact")
    await graph.add_edge(f1, f2, "supports", 0.9)

    await _run_pass()
    first = await _insight_ids(conn)
    assert len(first) == 1, f"the first pass materialized one insight, got {first}"

    await graph.add_edge(c, f1, "mentions")
    await _run_pass()

    surviving = await (await conn.execute("SELECT node_id FROM epi_nodes WHERE node_id IN (?, ?) ORDER BY node_id", (f1, f2))).fetchall()
    assert [int(r["node_id"]) for r in surviving] == [f1, f2], "fact nodes survive the snapshot replacement"
    edge = await (
        await conn.execute(
            "SELECT weight FROM epi_edges WHERE source_id=? AND target_id=? AND relation='supports'",
            (f1, f2),
        )
    ).fetchone()
    assert edge is not None and float(edge["weight"]) == pytest.approx(0.9), "a fact edge survives the replacement"
    second = await _insight_ids(conn)
    assert len(second) == 1, f"the replacement still happened, got {second}"


@pytest.mark.asyncio
async def test_a_pass_without_a_qualifying_component_leaves_no_insight(graph, no_miners):
    """A layer with no component of size ≥ 3 ends the pass with ZERO insights."""
    conn = await connection_manager.get(DB_NAME)
    _, _, c = await _add_component(graph, ["кэш redis обслуживает воркеры", "воркеры читают кэш redis", "воркеры выполняют задачи очереди"])

    await _run_pass()
    assert len(await _insight_ids(conn)) == 1, "the first pass created the insight this test removes"

    await graph.delete_nodes([c])
    result = await _run_pass()

    assert await _insight_ids(conn) == [], "the honest snapshot of a too-small graph is no insight"
    assert result["dream"]["insights"] == 0, result["dream"]


@pytest.mark.asyncio
async def test_two_components_with_the_same_signature_yield_one_insight(graph, no_miners):
    """R2: dedup is per-pass on the top-token signature, not on the size-bearing content."""
    conn = await connection_manager.get(DB_NAME)
    await _add_component(graph, ["alpha beta gamma"] * 3)
    await _add_component(graph, ["alpha beta gamma"] * 4)

    await _run_pass()

    ids = await _insight_ids(conn)
    assert len(ids) == 1, f"one signature per pass, got {len(ids)} insights"
