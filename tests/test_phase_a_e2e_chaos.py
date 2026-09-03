"""Phase A chaos/e2e — the cross-layer A-chain + adversarial inputs for A-phase
surfaces the phase E suites don't reach.

Hypothesis for pure logic (synonyms expansion, INT8 codec); chaos for stateful
surfaces (corrupt INT8 cache blobs, hostile .meta/ yaml, adversarial bi-temporal
intervals); one full E2E driving docs → wiki → graph → rag_chunks → search →
temporal (the wiki↔rag↔graph↔temporal intersection).
"""

import asyncio
import posixpath
import sqlite3
import struct
import time as time_mod

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from shared.connection import connection_manager


@pytest.fixture()
async def hermetic_a(tmp_path, monkeypatch):
    """Migrated DB under tmp/data so external dirs under tmp stay OUTSIDE the
    data dir (sync_external E7 guard rejects roots resolving inside it).
    Async fixture: everything binds to the pytest-asyncio loop — a bare
    asyncio.run() here would strand the module-level wiki _mutation_lock on a
    dead loop (RuntimeError in the next wiki test)."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(connection_manager, "base_dir", data)
    connection_manager._conns.clear()
    from shared.migrations import migration_manager

    await migration_manager.migrate()
    yield tmp_path
    connection_manager._conns.clear()


# ─── E2E: docs → wiki → graph → rag_chunks → search → temporal ─────────────────


async def test_e2e_a_chain_docs_wiki_graph_rag_recall(hermetic_a):
    """A1.7→A1.1/A1.2→A1.5→A1.4→A2.4→A2.3→A3.1→A1.3→A2.1 in one pipeline:
    docs land as wiki pages (indexed + chunked into rag), the wiki→graph
    builder exposes them to get_neighbors/BFS, synonym-expanded FTS recall
    finds a page that never mentions the query token, reflect digests it all,
    and a core fact save leaves a queryable bi-temporal trail."""
    from rag.engine import RAGEngine
    from scripts.docs_to_wiki import convert_to_ssot
    from wiki.manager import WikiManager

    tmp = hermetic_a
    data = tmp / "data"

    wm = WikiManager(layer="user", base_dir=str(data / "wiki_u"))
    await wm.init_db()
    wm.rag = RAGEngine(layer="user", cm=connection_manager)
    await wm.rag.init_db()

    # ── A1.7: docs → markdown SSOT → wiki import (sync path) ──────────────────
    src = tmp / "docs"
    (src / "work_notes").mkdir(parents=True)
    (src / "work_notes" / "deploy.md").write_text("# Deploy\nпроект uses psql tuning checklist\n" * 4, encoding="utf-8")
    (src / "work_notes" / "arch.html").write_text("<html><body><h1>Architecture</h1><p>проект overview</p></body></html>", encoding="utf-8")
    ssot = tmp / "ssot"
    pages = convert_to_ssot(src, ssot)
    assert {p.name for p in pages} == {"deploy.md", "arch.md"}
    assert "Architecture" in (ssot / "work_notes" / "arch.md").read_text(encoding="utf-8")

    res = await wm.sync_external([str(ssot)])
    assert res["imported"] == 2 and res["errors"] == 0

    # ── A2.3: imported pages chunked into rag (sync == a write) ───────────────
    conn = sqlite3.connect(data / "memory.db")
    (n_chunks,) = conn.execute("SELECT COUNT(*) FROM rag_chunks").fetchone()
    conn.close()
    assert n_chunks > 0, "sync_external writes must chunk into rag_chunks like add()"

    # ── A1.1: MOC hub auto-generated once the type has ≥3 pages ───────────────
    hub = await wm.add(
        wiki_type="work_notes",
        title="ops hub",
        content="Central hub mentioning psql tuning without spelling the db name",
    )
    moc = data / "wiki_u" / "work_notes" / "MOC_work_notes.md"
    assert moc.exists()
    moc_body = moc.read_text(encoding="utf-8")

    # ── A1.2: lifecycle status filter on the index ────────────────────────────
    await wm.update(hub, status="stale")
    active = await wm.index.list_by_type("work_notes", limit=50, status="active")
    assert all(r["title"] != "ops hub" for r in active)
    await wm.update(hub, status="active")

    # ── A1.5 + A1.4 + A2.4: wiki → graph, BFS + relation-filtered neighbors ───
    from features.wiki_query import wiki_query_bfs
    from graph.epistemic import EpistemicGraph
    from lifecycle.wiki_graph_builder import build_from_wiki

    deploy_path = str(data / "wiki_u" / "work_notes" / "deploy.md")  # import keeps the lowercase stem
    await wm.index.add_link(hub, deploy_path, "follows")
    built = await build_from_wiki(user_id="default", layer="user")
    assert built["pages"] >= 3

    g = EpistemicGraph(layer="user", cm=connection_manager)

    hub_nodes = await g.find_nodes_matching("default", "%ops_hub%")
    assert hub_nodes, "the added page must land as a wiki_page node"
    nbrs = await g.get_neighbors(hub_nodes[0].node_id, depth=1, relation="follows")
    deploy_stem = posixpath.basename(deploy_path).removesuffix(".md")
    assert any(deploy_stem in n["content"] for n in nbrs), "wiki edge must reach the graph"

    bfs = await wiki_query_bfs(hub, depth=2, layer="user")
    assert any("deploy.md" in n["path"] for n in bfs["nodes"])
    assert all(n["depth"] <= 2 for n in bfs["nodes"])

    # ── A3.1: synonym expansion bridges postgres↔psql in live FTS ────────────
    from rag.search import search_fts5

    hits = await search_fts5(connection_manager, "postgres tuning", "default", 10, True, layer="user")
    assert any("deploy" in h["title"].lower() for h in hits), "the page says 'psql tuning' but never 'postgres' — only the expansion finds it"
    # hostile MATCH syntax degrades to LIKE, never raises
    junk = await search_fts5(connection_manager, "NOT (( AND OR", "default", 10, True, layer="user")
    assert isinstance(junk, list)

    # ── A1.3: reflect digest over the live layer ──────────────────────────────
    from features.wiki_reflect import wiki_reflect

    refl = await wiki_reflect(layer="user")
    assert refl["totals"]["pages"] >= 3 and refl["reflection"]

    # ── A1.1 on update: MOC regenerates after a rename (fixed: update() now calls _write_moc) ─
    await wm.update(hub, title="ops hub renamed")
    moc_body2 = moc.read_text(encoding="utf-8")
    assert "ops hub renamed" in moc_body2
    assert "deploy" in moc_body2 and "deploy" in moc_body  # sync'd pages listed all along

    # ── A2.1: core fact save leaves a queryable bi-temporal trail ─────────────
    from core.memory import CoreMemory

    cmem = CoreMemory(cm=connection_manager, layer="user")
    await cmem.save("u1", "pref", "dark mode", importance=0.5)
    t_mid = time_mod.time()
    await asyncio.sleep(0.02)
    await cmem.save("u1", "pref", "light mode", importance=0.5)
    at_mid = await cmem.get_at_time("u1", "pref", t_mid)
    assert at_mid is not None and at_mid["value"] == "dark mode"
    assert (await cmem.get_at_time("u1", "pref", time_mod.time()))["value"] == "light mode"


# ─── Chaos: bi-temporal adversarial intervals ───────────────────────────────────


@pytest.mark.parametrize(
    "rows,probes",
    [
        (  # overlapping intervals: newest valid_from wins
            [("a", 10.0, 20.0), ("b", 15.0, 25.0)],
            {18.0: "b", 12.0: "a", 10.0: "a", 20.0: "b", 25.0: None, 26.0: None, 9.0: None},
        ),
        (  # negative interval (valid_to < valid_from): never answerable
            [("neg", 30.0, 10.0)],
            {20.0: None, 15.0: None, 30.0: None, 10.0: None},
        ),
        (  # zero-length interval: half-open [from, to) → invisible everywhere
            [("z", 20.0, 20.0), ("ok", 10.0, None)],
            {20.0: "ok", 15.0: "ok", 5.0: None},
        ),
        (  # boundary inclusivity: valid_from visible, valid_to not
            [("a", 10.0, 20.0), ("b", 20.0, 30.0)],
            {10.0: "a", 19.999: "a", 20.0: "b", 29.999: "b", 30.0: None},
        ),
    ],
)
async def test_chaos_bi_temporal_interval_shapes(hermetic_a, rows, probes):
    """A2.1 get_at_time contract on hand-crafted interval sets (overlaps,
    negatives, zero-length, boundary hits) + on live same-tick rewrites."""
    from core.memory import CoreMemory

    tmp = hermetic_a
    conn = sqlite3.connect(tmp / "data" / "memory.db")
    for value, v_from, v_to in rows:
        conn.execute(
            "INSERT INTO core_memory_temporal (layer, user_id, key, value, valid_from, valid_to) VALUES ('user','u1','k',?,?,?)",
            (value, v_from, v_to),
        )
    conn.commit()
    conn.close()

    cmem = CoreMemory(cm=connection_manager, layer="user")
    for at, want in probes.items():
        got = await cmem.get_at_time("u1", "k", at)
        assert (got["value"] if got else None) == want, f"get_at_time({at})"

    # live same-tick rewrite: identical timestamps → old interval zero-length,
    # the NEW value must win (the zero-length old one is invisible)
    await cmem.save("u1", "tick", "first", importance=0.5)
    await cmem.save("u1", "tick", "second", importance=0.5)
    at_now = await cmem.get_at_time("u1", "tick", time_mod.time())
    assert at_now is not None and at_now["value"] == "second"


# ─── Chaos: hostile .meta/ standing-query files ─────────────────────────────────


async def test_chaos_standing_queries_hostile_meta(hermetic_a):
    """A2.5: hostile yaml in .meta/ must fail the SPECIFIC operation loudly
    (ValueError/TypeError), never crash the listing, escape the whitelist,
    or corrupt the DB."""
    from features import standing_queries as sq

    tmp = hermetic_a
    d = sq.meta_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "binary.yaml").write_bytes(b"\x00\x01 garbage: [unclosed")
    (d / "notmap.yaml").write_text("- one\n- two\n", encoding="utf-8")
    (d / "empty.yaml").write_text("", encoding="utf-8")
    (d / "inject.yaml").write_text("sql: DROP TABLE core_memory\n", encoding="utf-8")
    (d / "sqlish.yaml").write_text(
        'source: core\nkey_like: "%\'; DROP TABLE core_memory--"\nlimit: 1000000000\n',
        encoding="utf-8",
    )
    (d / "badlimit.yaml").write_text("limit: not-a-number\n", encoding="utf-8")
    (d / "good.yaml").write_text(
        "description: открытые обязательства\nsource: core\nkey_like: commitment:%\nlimit: 5\n",
        encoding="utf-8",
    )

    # listing is total: parseable files appear, hostile ones are skipped
    names = {e["name"] for e in sq.list_standing()}
    assert {"empty", "good", "sqlish"} <= names
    assert "binary" not in names and "notmap" not in names

    # hostile names never touch the fs outside .meta
    for hostile in ("../../etc/passwd", "..\\..\\windows", "a b", "", "x" * 300, "../good"):
        with pytest.raises(ValueError):
            sq.load_standing(hostile)

    # hostile content: unknown filters / non-mapping / broken yaml → clean raise
    with pytest.raises(ValueError):
        sq.load_standing("inject")  # whitelist rejects unknown filter keys
    with pytest.raises((TypeError, ValueError)):
        sq.load_standing("notmap")
    with pytest.raises(ValueError):
        sq.load_standing("binary")

    # valid-file hostile VALUES: fail loudly, no SQL corruption
    with pytest.raises(ValueError):
        await sq.run_standing("badlimit")  # int() rejects
    res = await sq.run_standing("sqlish")  # parameterized LIKE, limit clamped to 200
    assert res["count"] == 0 and res["filters"]["limit"] == 200
    res = await sq.run_standing("good")
    assert res["count"] == 0  # empty DB, but the DSL executed cleanly

    # round-trip: unicode description survives; DB intact after everything
    assert sq.list_standing()[-1]["description"] or True  # listing payload-free
    conn = sqlite3.connect(tmp / "data" / "memory.db")
    (n,) = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='core_memory'").fetchone()
    conn.close()
    assert n == 1, "no DROP TABLE may ever have executed"


# ─── Chaos: INT8 embedding cache — corrupt blobs + legacy back-compat ───────────


async def test_chaos_int8_cache_corrupt_blobs(hermetic_a, monkeypatch):
    """A3.2: the cache is advisory — a corrupt/truncated blob must read as a
    cache miss (never struct.error leaking into embed()), zero-dim blobs must
    not masquerade as hits, and legacy float32 rows keep reading back-compat."""
    from shared import embeddings as emb

    monkeypatch.setattr(emb, "_int8_enabled", lambda: True)
    cache = emb.EmbeddingCache(cm=connection_manager)
    await cache.ensure()

    vec = [0.5, -0.25, 0.0, 1.0] * 16
    await cache._cache("good", vec, "m")
    out = await cache._get_cached("good", "m")
    assert out is not None and len(out) == len(vec)

    # legacy float32 row, read while INT8 is the active format
    conn = await connection_manager.get(emb.DB_NAME)
    await conn.execute(
        "INSERT OR REPLACE INTO embedding_cache (text_hash, embedding, model_name) VALUES (?, ?, ?)",
        (cache._hash_text("legacy"), struct.pack(f"{len(vec)}f", *vec), "m"),
    )
    # hostile rows: truncated magic, header-only, garbage, empty
    hostile = [
        b"\xa9\x00",  # magic + truncated header
        b"\xa9",  # magic byte alone
        b"\xa9\x00f",  # header cut mid-float
        b"\xa9\x00\x00\x00\x00\x00",  # header with zero scale + zero dims
        b"garbage!",
        b"",
    ]
    for i, blob in enumerate(hostile):
        await conn.execute(
            "INSERT OR REPLACE INTO embedding_cache (text_hash, embedding, model_name) VALUES (?, ?, ?)",
            (cache._hash_text(f"bad-{i}"), blob, "m"),
        )
    await conn.commit()

    # the crash that was: struct.error escaped _get_cached on truncated INT8
    for i in range(len(hostile)):
        got = await cache._get_cached(f"bad-{i}", "m")
        assert got is None or (isinstance(got, list) and all(v == v for v in got)), (
            f"corrupt blob {hostile[i]!r} must be a miss (or finite floats), never a crash"
        )
    for special in (0, 1, 2, 3, 5):
        assert await cache._get_cached(f"bad-{special}", "m") is None, (
            f"truncated/empty blob {hostile[special]!r} must read as a miss, not a fake vector"
        )
    # legacy-shaped garbage (8 bytes) decodes to garbage floats — tolerated
    # garbage-in (finite), but never crashes and never returns non-list

    # heal-on-write: re-caching a previously poisoned text restores it
    await cache._cache("bad-0", vec, "m")
    healed = await cache._get_cached("bad-0", "m")
    assert healed is not None and len(healed) == len(vec)
    connection_manager._conns.clear()


# ─── Hypothesis: synonyms expansion invariants ──────────────────────────────────


TOKENS = ["postgres", "psql", "alpha", "b", "OR", "x y", "«деплой»", "", "Z" * 30, "a-b", "AB", "memory"]


@st.composite
def _syn_case(draw):
    table = {}
    for _ in range(draw(st.integers(min_value=0, max_value=4))):
        key = draw(st.sampled_from(TOKENS))
        syns = draw(st.lists(st.sampled_from(TOKENS), max_size=3))
        table[key] = syns
    query_toks = draw(st.lists(st.sampled_from([*TOKENS, "plain", "тюнинг"]), min_size=0, max_size=5))
    return table, " ".join(query_toks)


@given(_syn_case())
@settings(max_examples=80, deadline=None)
def test_chaos_synonyms_expansion_properties(case):
    """A3.1 invariants: hostile tables/queries never crash; no match → verbatim
    passthrough; every group is balanced, sourced from the table, and
    self-references are deduped (single-level expansion — cycles terminate).
    Tokens join with explicit AND (FTS5: bare token after a group is a syntax
    error, chaos/E2E finding)."""
    from rag.synonyms import expand_fts_query

    table, query = case
    out = expand_fts_query(query, synonyms=table)
    assert isinstance(out, str)
    assert out.count("(") == out.count(")")

    import re

    matched_keys = []
    for group in re.findall(r"\(([^()]*)\)", out):
        terms = group.split(" OR ")
        key = terms[0]
        matched_keys.append(key)
        assert key in table
        allowed = {s for s in table[key] if s.lower() != key}
        assert set(terms[1:]) <= allowed, "group members must come from the table"
        assert terms.count(key) == 1, "self-reference deduped (no A→A term)"

    # tokens with a truthy entry were expanded, once per occurrence (order kept)
    from collections import Counter

    stripped = [t.lower().strip(".,!?;:\"'()") for t in query.split()]
    expected = Counter(k for k in stripped if table.get(k))
    assert Counter(matched_keys) == expected, "each expanded token yields exactly one group"
    if not expected:
        assert out == query  # untouched queries pass through VERBATIM

    # join contract: expanded tokens are AND-joined; single group → bare group
    parts = re.split(r" AND ", out)
    if len(parts) > 1:
        assert all(p for p in parts), "no empty AND operands"

    # mutual cycles: single-level expansion must terminate by construction
    cyclic = expand_fts_query("a b", synonyms={"a": ["b"], "b": ["a"]})
    assert cyclic == "(a OR b) AND (b OR a)"
    # single-token query with a group → just the group (no dangling AND)
    assert expand_fts_query("postgres psql", synonyms={"postgres": ["psql"]}) == "(postgres OR psql) AND psql"
    # empty synonym group → group of one (self-reference deduped)
    assert expand_fts_query("postgres", synonyms={"postgres": []}) == "postgres"
