"""Phase E chaos/hypothesis — property-based invariants + adversarial inputs.

Hypothesis for pure logic (facets, breaker, anchor regex); chaos for stateful
surfaces (crash-truncated files, hostile user_ids, oversized payloads,
unicode, concurrent-ish interleavings).
"""

import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from features.importance import detect_dream_marker
from features.query_dsl import _facet_clauses, query_memory
from shared.circuit_breaker import CircuitBreaker, CircuitState
from shared.connection import connection_manager


# ─── Hypothesis: E18 anchor regex ──────────────────────────────────────────────


@st.composite
def _marker_payload(draw):
    target = draw(st.sampled_from(["memory", "fact", "skill"]))
    content = draw(st.text(min_size=1, max_size=120).filter(lambda s: s.strip()))
    prefix = draw(st.sampled_from(["", " ", "  ", "\t", "\n "]))
    junk = draw(st.text(max_size=80))
    return target, content, prefix, junk


@given(_marker_payload())
@settings(max_examples=60, deadline=None)
def test_leading_marker_always_detected(case):
    target, content, prefix, _ = case
    m = detect_dream_marker(f"{prefix}DREAM: {target}: {content}")
    assert m is not None
    assert m["target"] == target
    assert m["content"] == content.strip()


@given(_marker_payload())
@settings(max_examples=60, deadline=None)
def test_mid_text_marker_never_detected(case):
    """Invariant E18 was built for: junk + marker NEVER triggers."""
    target, content, _, junk = case
    if not junk.strip():  # whitespace-only junk = the marker IS leading
        return
    assert detect_dream_marker(f"{junk}DREAM: {target}: {content}") is None


# ─── Hypothesis: E2 breaker state machine invariants ──────────────────────────


@given(
    failures=st.lists(st.booleans(), min_size=1, max_size=40),
    threshold=st.integers(min_value=1, max_value=5),
)
@settings(max_examples=80, deadline=None)
def test_breaker_opens_exactly_at_threshold(failures, threshold):
    """After N consecutive failures with N>=threshold, the breaker is open;
    any success in between resets the streak. Verbatim ACT-R-adjacent core."""
    b = CircuitBreaker(threshold=threshold, recovery_timeout=10_000.0)
    streak = 0
    for failed in failures:
        if failed:
            b.record_failure()
            streak += 1
            if streak >= threshold:
                assert b.state == CircuitState.OPEN
                assert not b.allow_request()
                break
        else:
            b.record_success()
            streak = 0
            assert b.state == CircuitState.CLOSED


# ─── Hypothesis: E10 facet clause generation ───────────────────────────────────


@st.composite
def _tag_lists(draw):
    dims = draw(st.lists(st.sampled_from(["lang", "area", "level"]), min_size=1, max_size=4))
    tags = []
    for d in dims:
        vals = draw(st.lists(st.sampled_from(["a", "b", "c"]), min_size=1, max_size=2))
        tags.extend(f"{d}:{v}" for v in vals)
    return tags


@given(_tag_lists())
@settings(max_examples=50, deadline=None)
def test_facet_clauses_shape(tags):
    clauses, params = _facet_clauses(tags)
    dims = {t.partition(":")[0] if ":" in t else f"\x00{t}" for t in tags}
    assert len(clauses) == len(dims)
    assert len(params) == len(set(tags))  # duplicate tags collapse (IN set semantics)
    for c in clauses:
        assert c.startswith("EXISTS (SELECT 1 FROM json_each")
        assert "OR" not in c  # OR lives in IN (...), not the SQL string


# ─── Chaos: E1 hostile/edge persist files ──────────────────────────────────────


def test_chaos_truncated_persist_files_all_shapes(tmp_path):
    """Every corruption shape loads clean and heals on next save."""
    from core.reflex import ReflexBuffer

    shapes = [
        "",  # empty file
        "{",  # truncated object
        "[{",  # truncated array element
        '[{"role": "user", "content": "ok", "tokens": 1, "timestamp": 1.0}, {',  # second entry cut
        "not json at all",  # garbage
        "\x00\x01\x02",  # binary junk
        json.dumps([{"role": "user", "content": "missing fields"}]),  # schema drift → KeyError path
        json.dumps("not a list"),  # wrong top-level type → iteration over str chars
    ]
    for i, blob in enumerate(shapes):
        p = tmp_path / f"l1_corrupt_{i}.json"
        p.write_text(blob)
        buf = ReflexBuffer(max_size=10, persist_path=str(p))
        # no exception survived the constructor; buffer starts empty or partial
        assert buf.size() in (0, 1)
        buf.add("user", "heal me", tokens=1)
        buf._save()
        data = json.loads(p.read_text())  # file is valid JSON again
        assert isinstance(data, list)


def test_chaos_hostile_user_ids_produce_safe_filenames(tmp_path, monkeypatch):
    import core

    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    hostile = [
        "../../etc/passwd",
        "..\\..\\windows",
        "user/with/slashes",
        "a" * 500,
        "",
        "🔥emoji",
        "null\x00byte",
        "../../",
        "-rf /",
        "l1_user_default.json",  # collision with our own naming scheme
    ]
    names = set()
    for uid in hostile:
        layer = core.MemoryLayer("user", uid)
        name = __import__("pathlib").Path(str(layer.l1.persist_path)).name
        assert "/" not in name and "\\" not in name and "\x00" not in name
        assert len(name) <= 120, "length-capped persist name (chaos finding)"
        assert name.startswith("l1_user_")
        names.add(name)
    # distinct hostile ids → distinct persist names (safe ids capped, rest hashed)
    assert len(names) == len(hostile)


def test_chaos_l1_concurrent_adds(tmp_path):
    """Debounce under threading stress: file always valid, never partially written."""
    from core.reflex import ReflexBuffer

    p = tmp_path / "l1_stress.json"
    buf = ReflexBuffer(max_size=50, persist_path=str(p))

    def _worker(k):
        for i in range(30):
            buf.add(f"w{k}", f"msg {k}-{i}", tokens=1)

    threads = [threading.Thread(target=_worker, args=(k,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    buf._save()
    data = json.loads(p.read_text())
    assert len(data) == 50  # ring cap, never a torn write
    assert all(set(e) == {"role", "content", "tokens", "timestamp"} for e in data)


# ─── Chaos: E10 oversized/edge facet inputs ────────────────────────────────────


async def test_chaos_facet_edge_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    from shared.migrations import migration_manager

    await migration_manager.migrate()
    conn = await connection_manager.get("memory.db")
    await conn.execute(
        "INSERT INTO episodes (user_id, layer, summary, emotional_weight, tags, created_at) VALUES ('default','user','edge',0.5,?,?)",
        (json.dumps(["lang:python", "plain_tag", "🔥:emoji", "nested:deeper:tag"]), time.time()),
    )
    await conn.commit()
    connection_manager._conns.clear()

    # 500 duplicate tags: clause count = dims (2), not 500
    big = ["lang:python"] * 250 + ["plain_tag"] * 250
    res = await query_memory("default", source="episodes", tags=big, limit=200)
    assert res["count"] == 1

    # emoji and nested-colon dims work (nested colon → dim="nested", val="deeper:tag")
    res2 = await query_memory("default", source="episodes", tags=["🔥:emoji"], limit=200)
    assert res2["count"] == 1
    res3 = await query_memory("default", source="episodes", tags=["nested:deeper:tag"], limit=200)
    assert res3["count"] == 1
    # no match is not an error
    res4 = await query_memory("default", source="episodes", tags=["absent:x"], limit=200)
    assert res4["count"] == 0
    connection_manager._conns.clear()


# ─── Chaos: E13 hostile payloads through the live handler ──────────────────────


async def test_chaos_semantic_audit_hostile_states(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    import hooks.user_hooks
    from features.semantic_audit import run_semantic_audit
    from hooks.user_hooks import UserHooks
    from shared.migrations import MigrationManager

    from shared.connection import AsyncConnectionManager

    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    await MigrationManager(cm=cm).migrate()

    # migrated but empty DB
    res = await run_semantic_audit("default", time.time())
    assert res["score"] is None and res["compared"] == 0

    conn = sqlite3.connect(tmp_path / "memory.db")
    huge = "ж" * 5000  # 5000 unicode chars
    for i in range(25):
        conn.execute(
            "INSERT INTO episodes (user_id, layer, summary, emotional_weight, tags, created_at) VALUES ('default','user',?,0.5,'[]',?)",
            (f"ep {i} {huge[:50]}", time.time() - 30 - i),
        )
    conn.execute(
        "INSERT INTO core_memory (layer, user_id, key, value, importance, created_at, updated_at) VALUES ('user','default','big',?,0.9,?,?)",
        (huge, time.time(), time.time()),
    )
    conn.commit()
    conn.close()

    res = await run_semantic_audit("default", time.time())  # 25 eps > cap 20, huge unicode
    assert res["compared"] == 20 and res["facts"] == 1
    assert res["score"] is not None

    # live handler with no memory state → still returns the block, no crash
    # (log_compaction happily inserts an empty-window row — that's by design)
    hooks = UserHooks()
    out = await hooks._post_context_compression({"user_id": "ghost"}, mem=None)
    assert out["logged"] is True and out["semantic_audit"]["score"] is None
    connection_manager._conns.clear()


# ─── Chaos: E17b hostile staging payloads ──────────────────────────────────────


async def test_chaos_staging_hostile_payloads(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    from shared.migrations import MigrationManager

    from shared.connection import AsyncConnectionManager

    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    await MigrationManager(cm=cm).migrate()

    from features.staging import decide, propose, revert, revert_transition

    async def _ret(v):
        return v

    app = SimpleNamespace(
        mm=SimpleNamespace(user_memory=lambda u: SimpleNamespace(remember=lambda *a, **k: _ret(1), forget=lambda *a, **k: _ret(True)))
    )

    # title/content abuse: traversal, 300-char, unicode, spaces — filenames must stay safe
    hostile_titles = ["../escape", "a" * 300, "🔥🔥🔥", "with space and-dash"]
    for i, title in enumerate(hostile_titles):
        pid = await propose("agent", "wiki_write", "default", "user", {"title": title, "content": f"body {i}", "wiki_type": "work_notes"})
        res = await decide(pid, approve=True, mem=app)
        assert res["status"] == "applied"  # no OSError escaped (length cap + sanitize)
        rev = await revert(pid, mem=app)
        assert rev["status"] == "reverted"

    # double revert must fail cleanly
    pid = await propose("agent", "wiki_write", "default", "user", {"title": "revert once only", "content": "b", "wiki_type": "work_notes"})
    await decide(pid, approve=True, mem=app)
    await revert(pid, mem=app)
    with pytest.raises(ValueError, match="not applied"):
        await revert(pid, mem=app)

    # revert_transition on a non-core to_ref is rejected
    from lifecycle.transitions import record_transition

    tid = await record_transition(connection_manager, "default", "episode", "episode:1", "archived", "archived:9", "archive")
    with pytest.raises(ValueError, match="not an L4 promotion"):
        await revert_transition("default", tid)
    connection_manager._conns.clear()
