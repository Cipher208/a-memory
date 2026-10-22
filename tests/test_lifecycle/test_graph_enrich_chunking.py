"""The chunking of the writing phases must have ONE owner, and it must be group-aware.

`lifecycle/graph_enrich.py` had two chunkers: a flat one that sliced a list of
operations every N operations (the NREM and REM phases) and a group-aware one
that kept a node's DELETE+INSERT in one transaction (the valence phase). Two
chunkers mean two places to fix, and a flat chunker silently splits a group:
the DELETE commits and the INSERT does not, and the node is left without its
`valence:*` tag — a state the old single-transaction code could not produce.

One chunker, working on groups, is the answer: a phase whose unit of work is
a single statement passes groups of one, and a phase whose unit of work is
several statements passes one group per unit. A group larger than the limit
stays whole — it is never cut.

These tests pin:

* the chunker never splits a group, and a group bigger than the limit stays
  whole;
* every phase that chunks goes through that one chunker;
* no transaction contains half of a logical group.
"""

from __future__ import annotations

import itertools
import time
from typing import Any

import pytest

from shared.connection import connection_manager
from shared.constants import DB_NAME
from shared.migrations import MigrationManager
from shared.write_dispatch import get_write_dispatch


@pytest.fixture
async def db(tmp_path, monkeypatch):
    """A migrated base in a tmp dir — the real `~/.mcp-ariel-memory` is never touched."""
    monkeypatch.setattr("shared.connection.BUSY_TIMEOUT_MS", 250)
    connection_manager.base_dir = tmp_path
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


@pytest.fixture
def no_miners(monkeypatch):
    """The miners write too; they are not what these tests measure."""
    monkeypatch.setattr("lifecycle.graph_miners.MINERS", {})


def _flat(batch: list[list[Any]]) -> list[Any]:
    """The operations of a batch of groups, in order."""
    return [op for group in batch for op in group]


async def _seed_chunker_material() -> None:
    """Seed the raw material of all three chunking phases.

    * NREM: two fresh heuristic edges → two weight updates;
    * REM: two isolated nodes with the same tokens → one dream bridge;
    * valence: three fact nodes joined by `supports` edges → three
      DELETE+INSERT groups (two statements per group).
    """
    dispatch = get_write_dispatch(connection_manager)
    now = time.time()

    async def _run(conn: Any) -> None:
        # The heuristic edges point at node ids far above the ones the nodes
        # below will get, so the REM twins stay isolated.
        for i in range(2):
            await conn.execute(
                "INSERT INTO epi_edges (source_id, target_id, relation, weight, created_at, tags)"
                " VALUES (?, ?, 'mentions', 0.6, ?, '[\"heuristic:cofire\"]')",
                (1001 + i, 1002 + i, now),
            )
        twin_ids = []
        for i in range(2):
            cur = await conn.execute(
                "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at)"
                " VALUES ('user', 'gu', ?, 'fact', '[]', 0.5, ?)",
                (f"rem twin node {i}", now),
            )
            twin_ids.append(int(cur.lastrowid or 0))
        valence_ids = []
        for i in range(3):
            cur = await conn.execute(
                "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at)"
                " VALUES ('user', 'gu', ?, 'fact', '[]', 0.5, ?)",
                (f"valence fact {i}", now),
            )
            valence_ids.append(int(cur.lastrowid or 0))
        for a, b in itertools.pairwise(valence_ids):
            await conn.execute(
                "INSERT INTO epi_edges (source_id, target_id, relation, weight, created_at, tags) VALUES (?, ?, 'supports', 0.8, ?, '[]')",
                (a, b, now),
            )

    await dispatch.atomic(DB_NAME, _run)


def test_the_chunker_never_splits_a_group():
    """A group is a set of operations that must land in ONE transaction."""
    import lifecycle.graph_enrich as ge

    groups: list[list[Any]] = [
        [("a", ())],
        [("b", ()), ("c", ())],
        [("d", ())],
        [("e", ()), ("f", ()), ("g", ())],
    ]

    batches = list(ge._chunk_groups(groups, 2))

    for group in groups:
        holders = [batch for batch in batches if all(op in _flat(batch) for op in group)]
        assert len(holders) == 1, f"the group {group} is not whole in exactly one batch: {batches}"
    # Nothing is lost and nothing is reordered.
    assert _flat([group for batch in batches for group in batch]) == [op for group in groups for op in group]


def test_a_group_bigger_than_the_limit_stays_whole():
    """A group that does not fit the limit is kept whole — it is never cut."""
    import lifecycle.graph_enrich as ge

    group: list[Any] = [("x", ()), ("y", ())]

    batches = list(ge._chunk_groups([group], 1))

    assert batches == [[group]], f"the group was cut: {batches}"


def test_a_batch_never_exceeds_the_limit_unless_one_group_does():
    """The limit bounds a batch of groups, not a group."""
    import lifecycle.graph_enrich as ge

    groups: list[list[Any]] = [[(f"op{i}", ())] for i in range(10)]

    for batch in ge._chunk_groups(groups, 3):
        assert len(_flat(batch)) <= 3, f"a batch exceeded the limit: {batch}"


async def test_every_chunked_phase_keeps_a_group_in_one_transaction(db, no_miners, monkeypatch):
    """Every phase that chunks goes through the one group chunker.

    A phase that chunks on its own — a flat chunker over a list of operations
    — is the defect this test pins: it can cut a group in half, and the two
    chunkers drift apart.
    """
    import lifecycle.graph_enrich as ge

    await _seed_chunker_material()
    monkeypatch.setattr(ge, "_DREAM_CHUNK", 1)

    batches: list[list[list[Any]]] = []
    real_chunk = ge._chunk_groups

    def _recording_chunk(groups: Any, limit: int) -> Any:
        for batch in real_chunk(groups, limit):
            batches.append([list(group) for group in batch])
            yield batch

    transactions: list[list[Any]] = []
    real_apply = ge._apply_ops

    async def _recording_apply(dispatch: Any, ops: list[Any]) -> None:
        transactions.append(list(ops))
        await real_apply(dispatch, ops)

    monkeypatch.setattr(ge, "_chunk_groups", _recording_chunk)
    monkeypatch.setattr(ge, "_apply_ops", _recording_apply)

    await ge.graph_enrich(layer="user")

    assert batches, "no phase chunked through the group chunker"
    assert len(transactions) == len(batches), f"a transaction was not produced by the chunker: {len(transactions)} vs {len(batches)}"
    for batch, ops in zip(batches, transactions, strict=True):
        assert _flat(batch) == ops, "a transaction is not the batch the chunker produced"

    # Every phase that chunks is represented in the batches.
    all_sql = [str(sql) for ops in transactions for sql, _ in ops]
    assert any("UPDATE epi_edges SET weight=?" in sql for sql in all_sql), "the NREM phase did not chunk"
    assert any("'dream_bridge'" in sql for sql in all_sql), "the REM phase did not chunk"
    assert any("DELETE FROM epi_tags" in sql for sql in all_sql), "the valence phase did not chunk"

    # And no transaction holds half of a group: a node's DELETE and its
    # INSERT are always in the same transaction, or in none.
    for ops in transactions:
        deletes = [params[0] for sql, params in ops if "DELETE FROM epi_tags" in sql]
        inserts = [params[0] for sql, params in ops if "INSERT OR IGNORE INTO epi_tags" in sql]
        assert sorted(deletes) == sorted(inserts), f"a transaction holds half of a DELETE+INSERT group: {ops}"


async def test_a_group_is_never_cut_by_the_chunk_limit(db, no_miners, monkeypatch):
    """A group bigger than the limit is applied whole, in a transaction of its own."""
    import lifecycle.graph_enrich as ge

    await _seed_chunker_material()
    monkeypatch.setattr(ge, "_DREAM_CHUNK", 1)

    transactions: list[list[Any]] = []
    real_apply = ge._apply_ops

    async def _recording_apply(dispatch: Any, ops: list[Any]) -> None:
        transactions.append(list(ops))
        await real_apply(dispatch, ops)

    monkeypatch.setattr(ge, "_apply_ops", _recording_apply)

    await ge.graph_enrich(layer="user")

    valence_transactions = [ops for ops in transactions if any("epi_tags" in str(sql) for sql, _ in ops)]
    assert valence_transactions, "the valence phase did not write"
    for ops in valence_transactions:
        deletes = [params[0] for sql, params in ops if "DELETE FROM epi_tags" in sql]
        inserts = [params[0] for sql, params in ops if "INSERT OR IGNORE INTO epi_tags" in sql]
        assert deletes, f"a transaction holds a tag INSERT without its DELETE: {ops}"
        assert sorted(deletes) == sorted(inserts), f"a transaction holds half of a DELETE+INSERT group: {ops}"
