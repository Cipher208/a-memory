"""F2a: the two quadratic miners must be faster and emit the SAME edges.

The reference implementations below are literal copies of the algorithms at
f6511fa (`lifecycle/graph_miners.py`). "Identical output" is proven by
comparing against them, not by asserting facts about the new internals.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from shared.connection import connection_manager
from shared.constants import DB_NAME
from shared.migrations import MigrationManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

T = 1_700_000_000.0


@pytest.fixture
async def db(tmp_path) -> AsyncIterator[Any]:
    connection_manager.base_dir = tmp_path
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


async def _node(content: str, ts: float) -> int:
    conn = await connection_manager.get(DB_NAME)
    cur = await conn.execute(
        "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES ('user', 'gu', ?, 'fact', '[]', 0.5, ?)",
        (content, ts),
    )
    await conn.commit()
    return int(cur.lastrowid or 0)


async def _l0(text: str, ts: float, source_msg_id: int | None = None) -> int:
    conn = await connection_manager.get(DB_NAME)
    cur = await conn.execute(
        "INSERT INTO l0_journal (ts, event, source_msg_id, layer, user_id, text, raw_type)"
        " VALUES (?, 'new_message', ?, 'user', 'gu', ?, 'user-message')",
        (ts, source_msg_id, text),
    )
    await conn.commit()
    return int(cur.lastrowid or 0)


async def _edges(relation: str) -> list[Any]:
    conn = await connection_manager.get(DB_NAME)
    return await (
        await conn.execute(
            "SELECT source_id, target_id, relation, weight FROM epi_edges WHERE relation=? ORDER BY source_id, target_id",
            (relation,),
        )
    ).fetchall()


# --- reference: miner_tokens, copied verbatim from f6511fa ------------------


def _ref_token_cands(nodes: list[tuple[int, str]]) -> list[tuple[int, int, float]]:
    from lifecycle.graph_miners import _canon_tokens
    from rag.synonyms import load_synonyms

    syn = load_synonyms()
    toks: dict[int, set[str]] = {nid: _canon_tokens(content, syn) for nid, content in nodes}
    ids = sorted(toks)
    cands: list[tuple[int, int, float]] = []
    for i, a in enumerate(ids):
        ta = toks[a]
        if not ta:
            continue
        for b in ids[i + 1 :]:
            shared = ta & toks[b]
            jaccard = len(shared) / len(ta | toks[b])
            if len(shared) >= 2:
                cands.append((a, b, jaccard))
    return cands


def _ref_tau(cands: list[tuple[int, int, float]]) -> float:
    from lifecycle.graph_sanitation import mad_threshold

    return max(0.3, mad_threshold([c[2] for c in cands])) if cands else 0.3


def _ref_token_wanted(cands: list[tuple[int, int, float]]) -> list[tuple[int, int, float]]:
    tau = _ref_tau(cands)
    return [(a, b, jaccard) for a, b, jaccard in cands if jaccard >= tau]


# --- reference: miner_sessions, copied verbatim from f6511fa ---------------


def _ref_session_wanted(
    l0_rows: list[tuple[float, int | None, str]], nodes: list[tuple[int, str, float]]
) -> tuple[list[tuple[int, int]], dict[int, set[int]]]:
    from lifecycle.graph_miners import (
        _BIND_SHARED,
        _NODE_WINDOW,
        _SESSION_GAP,
        _SESSION_TOPK,
        _canon_tokens,
    )

    clusters: list[dict[str, Any]] = []
    for ts, smid, text in l0_rows:
        if clusters and ts - clusters[-1]["max_ts"] <= _SESSION_GAP:
            c = clusters[-1]
        else:
            c = {"max_ts": ts, "rows": [], "smids": set(), "toks": set()}
            clusters.append(c)
        c["max_ts"] = max(c["max_ts"], ts)
        c["rows"].append(ts)
        if smid is not None:
            c["smids"].add(int(smid))
        c["toks"] |= _canon_tokens(str(text))
    merged: list[dict[str, Any]] = []
    for c in clusters:
        hit = next((m for m in merged if m["smids"] & c["smids"]), None)
        if hit is not None:
            hit["rows"] += c["rows"]
            hit["smids"] |= c["smids"]
            hit["toks"] |= c["toks"]
        else:
            merged.append(c)

    assigned: dict[int, set[int]] = {}
    for idx, c in enumerate(merged):
        for nid, content, created_at in nodes:
            ts = float(created_at)
            near = any(abs(ts - t) <= _NODE_WINDOW for t in c["rows"])
            if near or len(_canon_tokens(str(content)) & c["toks"]) >= _BIND_SHARED:
                assigned.setdefault(nid, set()).add(idx)

    wanted: list[tuple[int, int]] = []
    degree: dict[int, int] = {}
    for idx in range(len(merged)):
        members = sorted(nid for nid, cs in assigned.items() if idx in cs)
        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                if degree.get(a, 0) >= _SESSION_TOPK or degree.get(b, 0) >= _SESSION_TOPK:
                    continue
                wanted.append((a, b))
                degree[a] = degree.get(a, 0) + 1
                degree[b] = degree.get(b, 0) + 1
    return wanted, assigned


# --- fixtures --------------------------------------------------------------


def _token_contents() -> list[str]:
    """~40 nodes: pairs sharing 0/1/2/3 tokens, identical texts, empty sets, a common token."""
    contents = [
        # 4 nodes sharing 3 of 5 tokens (jaccard 0.6, 6 pairs)
        "alpha bravo charlie delta",
        "alpha bravo charlie echo",
        "alpha bravo charlie foxtrot",
        "alpha bravo charlie golf",
        # share 2 of 6 (jaccard 1/3, below tau)
        "hotel india juliet kilo",
        "hotel india lima mike",
        # identical texts (jaccard 1.0, 3 pairs)
        "november oscar papa quebec",
        "november oscar papa quebec",
        "november oscar papa quebec",
        # share exactly 1 token
        "romeo sierra tango",
        "romeo uniform victor",
        # share nothing
        "whiskey xray yankee",
        "zulu alpha1 bravo1",
        # empty token set (short words only)
        "и а я",
        # one very common token, no other overlap
        "common whiskey1",
        "common xray1",
        "common yankee1",
        "common zulu1",
        "common alpha2",
        # identical pair (jaccard 1.0)
        "bravo2 charlie2 delta2",
        "bravo2 charlie2 delta2",
    ]
    # filler: unique tokens, no overlap with anything
    for i in range(19):
        contents.append(f"filler{i} token{i} extra{i}")
    return contents


async def _insert_token_fixture() -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for i, content in enumerate(_token_contents()):
        out.append((await _node(content, T + i), content))
    return out


async def _insert_session_fixture() -> tuple[list[tuple[float, int | None, str]], list[tuple[int, str, float]]]:
    l0_rows: list[tuple[float, int | None, str]] = [
        (T, 101, "миграция postgres режима данные"),
        (T + 60, None, "миграция postgres режима данные"),
        (T + 120, 102, "миграция postgres режима данные"),
        (T + 100_000, 555, "деплой прошёл успешно"),
    ]
    for ts, smid, text in l0_rows:
        await _l0(text, ts, smid)
    nodes: list[tuple[int, str, float]] = []
    # 14 nodes bound to session A by the time window -> the degree cap bites
    for i in range(14):
        nodes.append((await _node(f"факт сессии номер{i} завершён", T + 10), f"факт сессии номер{i} завершён", T + 10))
    # bound to A by >=2 shared tokens, far from every row in time
    content = "миграция postgres отчёт готов"
    nodes.append((await _node(content, T + 50_000), content, T + 50_000))
    # only 1 shared token and far in time -> unbound
    content = "миграция финальный отчёт"
    nodes.append((await _node(content, T + 60_000), content, T + 60_000))
    # bound to session B by time
    content = "деплой завершён на проде"
    nodes.append((await _node(content, T + 100_010), content, T + 100_010))
    # unbound
    content = "случайная заметка про ужин"
    nodes.append((await _node(content, T + 250_000), content, T + 250_000))
    return l0_rows, nodes


# --- tests -----------------------------------------------------------------


async def test_miner_tokens_matches_the_reference_algorithm(db):
    nodes = await _insert_token_fixture()
    ref = _ref_token_wanted(_ref_token_cands(nodes))
    assert len(ref) >= 5, "fixture must yield edges, otherwise it proves nothing"

    from lifecycle.graph_miners import miner_tokens

    await miner_tokens(db, "user")

    got = {(int(r["source_id"]), int(r["target_id"]), r["relation"], round(float(r["weight"]), 6)) for r in await _edges("topic_overlap")}
    expected = {(a, b, "topic_overlap", round(jaccard, 6)) for a, b, jaccard in ref}
    assert got == expected


async def test_miner_tokens_threshold_is_unchanged(db, monkeypatch):
    nodes = await _insert_token_fixture()
    ref_cands = _ref_token_cands(nodes)
    assert ref_cands, "fixture must yield candidates"

    import lifecycle.graph_sanitation as sanitation

    real = sanitation.mad_threshold
    seen: list[list[float]] = []

    def spy(scores: list[float], *args: Any, **kwargs: Any) -> float:
        seen.append(list(scores))
        return real(scores, *args, **kwargs)

    monkeypatch.setattr(sanitation, "mad_threshold", spy)

    from lifecycle.graph_miners import miner_tokens

    await miner_tokens(db, "user")

    assert seen == [[c[2] for c in ref_cands]]
    assert max(0.3, real(seen[0])) == _ref_tau(ref_cands)


async def test_miner_sessions_matches_the_reference_algorithm(db):
    l0_rows, nodes = await _insert_session_fixture()
    ref, _assigned = _ref_session_wanted(l0_rows, nodes)
    assert ref, "fixture must yield edges"
    degree: dict[int, int] = {}
    for a, b in ref:
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1
    assert max(degree.values()) == 12, "the _SESSION_TOPK cap must actually bite"

    from lifecycle.graph_miners import miner_sessions

    await miner_sessions(db, "user")

    got = {(int(r["source_id"]), int(r["target_id"])) for r in await _edges("same_session")}
    assert got == set(ref)
    assert len(got) == len(ref)


async def test_miner_sessions_does_not_retokenise_per_cluster(db, monkeypatch):
    # 5 clusters, 20 nodes none of which is near any L0 row: today every node is
    # tokenised once per cluster (5 x 20), after the fix once in total.
    l0_rows: list[tuple[float, str]] = []
    for i in range(5):
        ts = T + i * 1_000_000
        text = f"сессия тема{i} обсуждение"
        l0_rows.append((ts, text))
        await _l0(text, ts, source_msg_id=1000 + i)
    nodes: list[tuple[int, str, float]] = []
    for i in range(20):
        content = f"узел отдельный номер{i} содержание"
        nodes.append((await _node(content, T + 500_000 + i), content, T + 500_000 + i))

    import lifecycle.graph_miners as graph_miners

    real = graph_miners._canon_tokens
    calls = {"n": 0}

    def counting(text: str, syn: dict[str, list[str]] | None = None) -> set[str]:
        calls["n"] += 1
        return real(text, syn)

    monkeypatch.setattr(graph_miners, "_canon_tokens", counting)

    await graph_miners.miner_sessions(db, "user")

    assert calls["n"] <= len(nodes) + len(l0_rows)


async def test_miner_tokens_does_not_scan_all_pairs(db, monkeypatch):
    nodes = await _insert_token_fixture()

    import lifecycle.graph_miners as graph_miners

    real = graph_miners._canon_tokens

    class CountingSet(set):
        ops = 0

        def __and__(self, other):  # type: ignore[no-untyped-def]
            CountingSet.ops += 1
            return set.__and__(self, other)

        def __or__(self, other):  # type: ignore[no-untyped-def]
            CountingSet.ops += 1
            return set.__or__(self, other)

    def counting(text: str, syn: dict[str, list[str]] | None = None) -> set[str]:
        return CountingSet(real(text, syn))

    monkeypatch.setattr(graph_miners, "_canon_tokens", counting)

    await graph_miners.miner_tokens(db, "user")

    n = len(nodes)
    assert CountingSet.ops < n * (n - 1) // 4


def test_fixture_is_sane() -> None:
    """The token fixture must really contain the shapes the test claims."""
    from lifecycle.graph_miners import _canon_tokens

    sets = [_canon_tokens(c) for c in _token_contents()]
    assert any(not s for s in sets)
    assert any(len(s) >= 3 for s in sets)
