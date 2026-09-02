"""Phase E E2E — full pipelines through real surfaces (no mocks below the tool layer).

Each test drives a complete user-visible path: tool call → storage → tool call.
Mirrors the test_tools_e2e fixture pattern (real AsyncConnectionManager + migrations).
"""

import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from shared.connection import AsyncConnectionManager, connection_manager
from shared.migrations import MigrationManager


@pytest.fixture()
async def e2e(tmp_path, monkeypatch):
    """Real migrated DB + a minimal AppContext-alike wired the way production does."""
    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    mm = MigrationManager(cm=cm)
    await mm.migrate()
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()

    from core import MemoryManager as MM
    from graph.epistemic import EpistemicGraph
    from wiki import WikiManager

    class _App:
        pass

    app = _App()
    app.mm = MM(cm=cm)
    app.cache = None
    app.user_graph = EpistemicGraph(layer="user", cm=cm)
    app.agent_graph = EpistemicGraph(layer="agent", cm=cm)
    app.user_wiki = WikiManager(layer="user", base_dir=str(tmp_path / "wiki_u"), cm=cm)
    app.agent_wiki = WikiManager(layer="agent", base_dir=str(tmp_path / "wiki_a"), cm=cm)

    def _ctx():
        ctx = MagicMock()
        ctx.request_context = MagicMock()
        ctx.request_context.lifespan_context = app
        return ctx

    yield {"app": app, "ctx": _ctx(), "tmp": tmp_path, "cm": cm}
    connection_manager._conns.clear()


# ─── E1+E4: L1 ring survives export/import and lands on disk ──────────────────


async def test_l1_export_import_persistence_pipeline(e2e):
    from features.import_export import ImportExport

    ie = ImportExport(cm=connection_manager)
    buf = e2e["app"].mm.user_memory("ringy").l1
    buf.clear()
    buf.add("user", "turn one", tokens=1)
    buf.add("assistant", "turn two", tokens=2)

    # tool-surface contract: mm= carries the caller's rings (app.mm), NOT the
    # global singleton's — the E2E-audit finding
    path = await ie.export_user("ringy", mm=e2e["app"].mm)
    payload = json.loads((e2e["tmp"] / "exports" / path.split("/")[-1]).read_text())
    assert payload["version"] == "1.2"
    assert len(payload["l1"]["user"]) == 2

    buf.clear()
    persist = e2e["app"].mm.user_memory("ringy").l1.persist_path
    if persist:
        __import__("pathlib").Path(persist).unlink(missing_ok=True)
    await ie.import_user(path.split("/")[-1], target_user_id="ringy", mm=e2e["app"].mm)

    assert [e.content for e in e2e["app"].mm.user_memory("ringy").l1.get_full()] == ["turn one", "turn two"]
    # restore() persisted immediately (E2-audit contract)
    assert persist and __import__("pathlib").Path(persist).exists()


# ─── E2+E3: breaker trips, diagnose sees it (via registry), heal resets ───────


async def test_breaker_trip_diagnose_heal_pipeline(e2e, monkeypatch):
    import shared.embeddings as emb
    from mcp_server.tools.ops import memory_diagnose, memory_heal
    from shared.circuit_breaker import CircuitState

    class _Boom:
        def encode(self, texts):
            raise RuntimeError("model down")

    monkeypatch.delenv("ARIEL_HASH_EMBEDDINGS", raising=False)
    monkeypatch.setattr(emb, "_get_model", lambda: _Boom())
    emb._embedding_breaker.reset()

    cache = emb.EmbeddingCache(cm=connection_manager)
    await cache.ensure()
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await cache.embed(["boom"])
    assert emb._embedding_breaker.state == CircuitState.OPEN

    # registry sees it (E2-audit contract: no registry bypass)
    metrics = emb.breaker_registry.get_all_metrics()
    assert metrics["embedding_model"]["state"] == "open"

    card = await memory_diagnose(user_id="default", ctx=e2e["ctx"])
    br_check = next(c for c in card["checks"] if c["name"] == "circuit_breakers")
    assert br_check["status"] == "fail" and card["status"] == "degraded"

    await memory_heal(user_id="default", actions=["reset_breakers"], ctx=e2e["ctx"])
    assert emb._embedding_breaker.state == CircuitState.CLOSED

    card = await memory_diagnose(user_id="default", ctx=e2e["ctx"])
    assert next(c for c in card["checks"] if c["name"] == "circuit_breakers")["status"] == "ok"


# ─── E5: recall verify → audit_log → report card integrity ────────────────────


async def test_integrity_pipeline(e2e):
    from mcp_server.tools.ops import memory_report_card

    class _Rag:
        async def search(self, query, user_id=None, limit=8):
            return [
                {"content": f"redis cluster {query}", "score": 0.9},
                {"content": "zzqx noise", "score": 0.8},
            ]

    class _L1:
        def get_recent(self, n):
            return []

    class _L3:
        async def search_by_tag(self, user_id, tag, limit):
            return []

    class _L4:
        async def get_all(self, user_id, limit):
            return []

    from features.recall import recall_protocol

    await recall_protocol(SimpleNamespace(l1=_L1(), l3=_L3(), l4=_L4()), _Rag(), "default", query="redis cluster", budget=2000)

    card = await memory_report_card(period_hours=24, ctx=e2e["ctx"])
    integrity = card["integrity"]
    assert integrity["verified"] >= 1 and integrity["dropped"] >= 1
    assert integrity["score"] == pytest.approx(100.0 * integrity["verified"] / (integrity["verified"] + integrity["dropped"]), abs=0.2)


# ─── E10+E11: facets find tagged episodes; disclosure triggers surface ────────


async def test_facets_and_disclosure_pipeline(e2e):
    from mcp_server.tools.ops import memory_disclose, memory_query

    seed = e2e["cm"]
    cur_conn = await seed.get("memory.db")
    for summary, tags in [("py deploy runbook", ["lang:python", "area:deploy"]), ("go deploy runbook", ["lang:go", "area:deploy"])]:
        await cur_conn.execute(
            "INSERT INTO episodes (user_id, layer, summary, emotional_weight, tags, created_at) VALUES ('default', 'user', ?, 0.5, ?, ?)",
            (summary, json.dumps(tags), time.time()),
        )
    await cur_conn.commit()

    res = await memory_query(source="episodes", tags=["lang:python", "area:deploy"], user_id="default", ctx=e2e["ctx"])
    assert [r["summary"] for r in res["rows"]] == ["py deploy runbook"]

    staged = await memory_disclose(
        "add", name="deploy rule", trigger_keywords=["deploy"], content="always announce rollbacks", user_id="default", ctx=e2e["ctx"]
    )
    assert staged["status"] == "ok"

    class _L1:
        def get_recent(self, n):
            return []

    class _L3:
        async def search_by_tag(self, user_id, tag, limit):
            return []

    class _L4:
        async def get_all(self, user_id, limit):
            return []

    from features.recall import recall_protocol

    blocks = await recall_protocol(SimpleNamespace(l1=_L1(), l3=_L3(), l4=_L4()), None, "default", query="how do we deploy safely", budget=2000)
    triggered = [b for b in blocks if b["axis"] == "triggered"]
    assert triggered and "rollbacks" in triggered[0]["content"]


# ─── E13: live-shape compaction payload (no query) produces the audit ─────────


async def test_semantic_audit_pipeline_live_payload(e2e):
    from hooks.external import dispatch_event
    import hooks.user_hooks  # noqa: F401 — registration
    from hooks.registry import hook_registry
    from hooks.user_hooks import UserHooks

    hook_registry.register_instance(UserHooks())
    conn = await e2e["cm"].get("memory.db")
    await conn.execute(
        "INSERT INTO episodes (user_id, layer, summary, emotional_weight, tags, created_at) VALUES ('default', 'user', 'compacted topic X', 0.5, '[]', ?)",
        (time.time() - 60,),
    )
    await conn.execute(
        "INSERT INTO core_memory (layer, user_id, key, value, importance, created_at, updated_at) VALUES ('user', 'default', 'k_x', 'compacted topic X fact', 0.9, ?, ?)",
        (time.time(), time.time()),
    )
    await conn.commit()

    import shared.embeddings as emb

    async def _det(texts, prefix=""):
        return [[1.0] if "topic x" in t.lower() else [0.0] for t in texts]

    emb.embed_texts = _det  # deterministic; hash-embedding quality is a documented ceiling

    # the REAL live payload shape: Hermes/cow/MiMoCode send no query
    result = await dispatch_event(
        "post_context_compression", "user", "default", {"reason": "compaction"}, e2e["app"].mm.user_memory("default"), None, None
    )
    handler = result["results"][0]
    assert handler["logged"] is True
    assert handler["semantic_audit"]["compared"] == 1 and handler["semantic_audit"]["score"] is not None

    trail = sqlite3.connect(e2e["tmp"] / "memory.db")
    rows = trail.execute("SELECT COUNT(*) FROM audit_log WHERE action='semantic_audit'").fetchone()[0]
    trail.close()
    assert rows == 1
    hook_registry._hooks.pop("post_context_compression", None)


# ─── E17: full staging lifecycle (propose → decide → page → revert) ───────────


async def test_staging_wiki_lifecycle(e2e):
    from mcp_server.tools.ops import memory_proposals

    staged = await memory_proposals(
        "propose",
        kind="wiki_write",
        payload={"title": "runbook deploy", "content": "# deploy steps", "wiki_type": "work_notes"},
        user_id="default",
        ctx=e2e["ctx"],
    )
    decided = await memory_proposals("decide", proposal_id=staged["proposal_id"], approve=True, ctx=e2e["ctx"])
    assert decided["result_ref"].startswith("wiki:")
    # staging wiki_write is instance-scoped: <base>/wiki/<layer> (E2E-audit fix)
    pages = list((e2e["tmp"] / "wiki" / "user").rglob("*runbook_deploy*.md"))
    assert pages, "instance-scoped WikiManager must write under the tmp data dir"

    reverted = await memory_proposals("revert", proposal_id=staged["proposal_id"], ctx=e2e["ctx"])
    assert reverted["restored"] == 1
    assert not list((e2e["tmp"] / "wiki" / "user").rglob("*runbook_deploy*.md"))


async def test_causal_and_transition_revert_pipeline(e2e):
    from mcp_server.tools.graph import memory_graph_add
    from mcp_server.tools.ops import memory_proposals

    res = await memory_graph_add(
        action="causal", content="migrated db", outcome="zero downtime", relation="led_to", user_id="default", ctx=e2e["ctx"]
    )
    assert res["action_node"] > 0 and res["outcome_node"] > 0

    # simulate a consolidation promotion + its transition row, then revert it
    from core.episodic import EpisodicMemory
    from core.memory import CoreMemory
    from lifecycle.transitions import record_transition

    epi = EpisodicMemory(cm=connection_manager, layer="user")
    eid = await epi.save("default", "big promoted episode", 0.9, ["t"])
    cm = CoreMemory(cm=connection_manager, layer="user")
    entry_id = await cm.save("default", "ep_big_promoted_episode", "big promoted episode", importance=0.9, source="episode_promotion")
    tid = await record_transition(connection_manager, "default", "episode", f"episode:{eid}", "l4", f"core:{entry_id}", "episode_promotion")

    out = await memory_proposals("revert_transition", transition_id=tid, user_id="default", ctx=e2e["ctx"])
    assert out["deleted"] is True
    hits = await cm.search("default", "big promoted episode", limit=5)
    assert all(h["key"] != "ep_big_promoted_episode" for h in hits)
    rows = await epi.search("default", "big promoted episode", limit=5)
    assert any(getattr(r, "episode_id", None) == eid for r in rows)


# ─── E9+E18: inject render order + marker via real dispatch; marker anchoring ──


async def test_inject_render_pipeline(e2e, monkeypatch):
    import features.inject as inj
    import time as _time
    from autohooks.inject import _render_md

    monkeypatch.setattr(inj, "_pending_proposals", lambda *a, **k: _noop())

    async def _noop():
        return []

    class _L1:
        def get_recent(self, n):
            return [SimpleNamespace(role="user", content="live chatter", timestamp=_time.time())]

    class _L3:
        async def search_by_tag(self, user_id, tag, limit):
            return []

    class _L4:
        async def get_all(self, user_id, limit):
            return [SimpleNamespace(key="core fact", value="stable", importance=0.95)]

    mem = SimpleNamespace(l1=_L1(), l3=_L3(), l4=_L4())
    blocks = await inj.build_inject_blocks(mem, rag=None, user_id="default", text="", budget=2000)
    md = _render_md(blocks)
    lines = md.splitlines()
    kinds = [b["kind"] for b in blocks]
    assert kinds[0] == "important" and kinds[1] == "cache_break"
    assert lines[1] == "<cache:break>"


async def test_dream_marker_anchoring_e2e(e2e):
    """Junk text (marker buried in a document) stages nothing; leading marker does."""
    from hooks.external import auto_save_text

    class _L3:
        def __init__(self):
            self.saved = []

        async def save(self, user_id, summary, weight, tags):
            self.saved.append(summary)
            return 1

    class _Mem:
        def __init__(self):
            self.l3 = _L3()
            self.remembered = []

        async def remember(self, key, value, importance):
            self.remembered.append((key, value))
            return 1

    class _Graph:
        async def add_node(self, *a, **k):
            return 1

    mem = _Mem()
    await auto_save_text(mem, _Graph(), "default", "Read this doc: it cites `DREAM: skill: something` mid-paragraph, then more text")
    assert mem.l3.saved == [] and mem.remembered == [], "mid-text marker must be inert (E18)"

    # a leading marker goes through staging (staging on by default → proposal, no direct write)
    await auto_save_text(mem, _Graph(), "default", "DREAM: skill: deploy via restic first")
    conn = sqlite3.connect(e2e["tmp"] / "memory.db")
    n = conn.execute("SELECT COUNT(*) FROM mutation_proposals WHERE source='dream'").fetchone()[0]
    conn.close()
    assert n == 1
