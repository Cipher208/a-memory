"""Phase D MCP-level E2E — real AppContext flows + cross-tool scenarios.

Complements the unit suites (features/*) by exercising the MCP tool layer:
ctx resolution, tool->tool data flow (remember → query → feedback → blame,
typed → query, rules → hook gate → episode tag, scratchpad → recap → promote,
session lifecycle → recap).
"""

from unittest.mock import MagicMock

import pytest

from shared.connection import AsyncConnectionManager, connection_manager
from shared.migrations import MigrationManager


@pytest.fixture
async def phase_d_app(tmp_path, monkeypatch):
    """Real AppContext (test_tools_e2e pattern) + global base_dir redirect.

    The redirect makes features riding the GLOBAL connection_manager
    (blame / rules / scratchpad / continuity-pending) share the tmp DB the
    app-scoped cm uses.
    """
    original = connection_manager.base_dir
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    await connection_manager.close_all()

    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    mm = MigrationManager(cm=cm)
    await mm.migrate()

    from core import MemoryManager as MM
    from features.rate_limiting import RateLimiter
    from graph.epistemic import EpistemicGraph
    from hooks.agent_hooks import AgentHooks
    from hooks.user_hooks import UserHooks
    from wiki import WikiManager

    class App:
        pass

    app = App()
    app.mm = MM(cm=cm)
    app.user_wiki = WikiManager(layer="user", base_dir=str(tmp_path / "wiki_u"), cm=cm)
    app.agent_wiki = WikiManager(layer="agent", base_dir=str(tmp_path / "wiki_a"), cm=cm)
    app.user_graph = EpistemicGraph(layer="user", cm=cm)
    app.agent_graph = EpistemicGraph(layer="agent", cm=cm)
    app.rate_limiter = RateLimiter()
    app.user_hooks = UserHooks()
    app.agent_hooks = AgentHooks()

    # Production registers hook instances in the lifespan (mcp_server/context.py);
    # tests must do it explicitly — with backup/restore so handler lists don't
    # accumulate across tests.
    from hooks.registry import hook_registry

    saved_handlers = {k: list(v) for k, v in hook_registry._hooks.items()}
    hook_registry.register_instance(app.user_hooks)
    hook_registry.register_instance(app.agent_hooks)

    ctx = MagicMock()
    ctx.request_context = MagicMock()
    ctx.request_context.lifespan_context = app
    yield ctx
    hook_registry._hooks.clear()
    hook_registry._hooks.update(saved_handlers)
    connection_manager.base_dir = original
    await connection_manager.close_all()


async def test_session_lifecycle_drives_recap(phase_d_app):
    from mcp_server.tools_layer import memory_recap, memory_session_end, memory_session_start

    start = await memory_session_start(user_id="eu", ctx=phase_d_app)
    session_id = str(start.get("session_id") or start.get("id") or "")
    await memory_session_end(user_id="eu", session_id=session_id, summary="deployed ariel tier groups", topics=["tiers"], ctx=phase_d_app)
    res = await memory_recap(user_id="eu", ctx=phase_d_app)
    assert res["count"] >= 1
    session_block = next(b for b in res["blocks"] if b["axis"] == "recap_session")
    assert "deployed ariel tier groups" in session_block["content"]
    assert "tiers" in session_block["content"]


async def test_remember_query_feedback_blame_cross(phase_d_app):
    from mcp_server.tools_layer import memory_fact_blame, memory_query, memory_quality, memory_remember

    await memory_remember(user_id="eu", key="deploy", value="ssh then uv sync", importance=0.9, ctx=phase_d_app)
    await memory_remember(user_id="eu", key="pizza", value="likes pineapple", importance=0.3, ctx=phase_d_app)

    res = await memory_query(importance_min=0.5, user_id="eu", ctx=phase_d_app)
    assert res["count"] == 1 and res["rows"][0]["key"] == "deploy"
    eid = res["rows"][0]["entry_id"]

    fb = await memory_quality(action="feedback", entry_id=eid, useful=True, user_id="eu", ctx=phase_d_app)
    assert fb["new"] > fb["old"]

    blame = await memory_fact_blame(key="deploy", user_id="eu", ctx=phase_d_app)
    assert blame["provenance"] == "user_explicit"
    assert blame["counts"]["importance_changes"] == 1
    assert blame["importance_history"][0]["reason"] == "agent_feedback"
    assert blame["counts"]["audit_events"] == 1


async def test_typed_save_flows_into_query(phase_d_app):
    from mcp_server.tools_layer import memory_query, memory_save_typed

    res = await memory_save_typed(
        type_name="decision",
        fields={"decision": "use tier groups", "rationale": "coherent MCP surface"},
        user_id="eu",
        ctx=phase_d_app,
    )
    assert res["key"] == "decision:use tier groups"
    q = await memory_query(key_like="decision:", user_id="eu", ctx=phase_d_app)
    assert q["count"] == 1 and "metadata" in q["rows"][0]
    with pytest.raises(ValueError, match="missing required field"):
        await memory_save_typed(type_name="error_pattern", fields={"cause": "unclear"}, user_id="eu", ctx=phase_d_app)


async def test_rules_gate_via_hook_produces_tagged_episode(phase_d_app, tmp_path):
    from mcp_server.tools_layer import memory_hook, memory_load_rules, memory_query

    (tmp_path / "rules.yaml").write_text(
        'rules:\n  - name: release-facts\n    when_content_contains: ["release", "релиз"]\n    importance_boost: 0.1\n    tags: ["release"]\n',
        encoding="utf-8",
    )
    rules = await memory_load_rules(action="reload", ctx=phase_d_app)
    assert rules["count"] == 1

    fired = await memory_hook(
        event="new_message",
        payload={
            "_test_bypass_config": True,  # fire() skips config gating in tests
            "text": "Релиз a-memory выйдет завтра?\nЭто важно: надо предупредить команду, обновить changelog и проверить CI перед публикацией сегодня!",
        },
        user_id="eu",
        ctx=phase_d_app,
    )
    assert "results" in fired, f"res={fired}"
    handler_res = next(r for r in fired["results"] if isinstance(r, dict) and "auto_save" in r)
    auto_save = handler_res["auto_save"]
    assert auto_save["rules"] == ["release-facts"]
    assert auto_save["saved_l3"] is True

    eps = await memory_query(source="episodes", tag="release", user_id="eu", ctx=phase_d_app)
    assert any("Релиз a-memory" in str(r["summary"]) for r in eps["rows"]), f"res={eps['rows']}"


async def test_scratchpad_recap_promote_chain(phase_d_app):
    from mcp_server.tools_layer import memory_query, memory_recap, memory_scratchpad

    await memory_scratchpad(action="write", key="hypothesis", content="tier exposure lifts agent surface", user_id="eu", ctx=phase_d_app)
    recap = await memory_recap(user_id="eu", ctx=phase_d_app)
    pending = next(b for b in recap["blocks"] if b["axis"] == "recap_pending")
    assert "pad:hypothesis=" in pending["content"]

    promo = await memory_scratchpad(action="promote", key="hypothesis", to="l3", user_id="eu", ctx=phase_d_app)
    assert promo["count"] == 1
    eps = await memory_query(source="episodes", tag="scratchpad_promoted", user_id="eu", ctx=phase_d_app)
    assert eps["count"] == 1


async def test_steering_and_compress_surfaces():
    from mcp_server.tools_layer import memory_compress, memory_steering

    hints = await memory_steering(query="вспомни что говорили о деплое")
    assert hints["count"] >= 1 and "memory_recall_protocol" in hints["hints"][0]["use"]
    comp = await memory_compress(text="$ pytest\nPASSED a\nFAILED b - assert\nERROR teardown")
    assert comp["mode"] == "log"  # non-python → auto falls back to log
    assert "FAILED b" in comp["text"] and "PASSED a" not in comp["text"]


async def test_branch_ab_persona_chain(phase_d_app):
    """D1.11: create → diverge → diff → cherry-pick merge → provenance → delete."""
    from mcp_server.tools_layer import memory_branch, memory_history, memory_remember

    await memory_remember(key="principle_yagni", value="YAGNI ruthlessly", importance=0.9, user_id="eu", ctx=phase_d_app)

    created = await memory_branch(action="create", name="exp1", user_id="eu", ctx=phase_d_app)
    assert created["copied"] == 1

    await memory_branch(action="write", name="exp1", key="principle_yagni", value="BUT: test first", importance=0.8, user_id="eu", ctx=phase_d_app)
    await memory_branch(action="write", name="exp1", key="principle_ab", value="A/B test personas", user_id="eu", ctx=phase_d_app)

    diff = await memory_branch(action="diff", name="exp1", user_id="eu", ctx=phase_d_app)
    assert diff["added"] == ["principle_ab"] and diff["changed"] == ["principle_yagni"]

    merged = await memory_branch(action="merge", name="exp1", keys=["principle_ab"], user_id="eu", ctx=phase_d_app)
    assert merged["merged"] == ["principle_ab"]

    hist = await memory_history(action="list", key="principle_ab", layer="user", user_id="eu", ctx=phase_d_app)
    assert hist["rows"][0]["triggered_by"] == "branch_merge:exp1"

    await memory_branch(action="delete", name="exp1", user_id="eu", ctx=phase_d_app)
    assert (await memory_branch(action="list", user_id="eu", ctx=phase_d_app))["branches"] == []


async def test_versioning_snapshot_rollback_chain(phase_d_app, monkeypatch):
    """D1.14: snapshot → mutations → rollback one → full restore."""
    # the importance gate's EMA drifts within the test (first save raises the
    # threshold); this chain is about versioning, not gate dynamics — pin it open.
    from shared.adaptive import adaptive_threshold

    async def _always_pass(score):
        return {"importance": score, "threshold": 0.0, "bypass": False}

    monkeypatch.setattr(adaptive_threshold, "gate", _always_pass)

    from mcp_server.tools_layer import memory_history, memory_query, memory_remember

    await memory_remember(key="persona_a", value="original persona", importance=0.9, user_id="eu", ctx=phase_d_app)
    snap = await memory_history(action="snapshot_create", name="saga1", user_id="eu", ctx=phase_d_app)
    assert snap["facts"] == 1

    await memory_remember(key="persona_a", value="broken persona", importance=0.9, user_id="eu", ctx=phase_d_app)
    await memory_remember(key="extra", value="junk", user_id="eu", ctx=phase_d_app)

    # rollback the "broken" update: find its ledger row
    hist = await memory_history(action="list", key="persona_a", layer="user", user_id="eu", ctx=phase_d_app)
    upd = next(r for r in hist["rows"] if r["new_value"] == "broken persona")
    rb = await memory_history(action="rollback", history_id=int(upd["history_id"]), user_id="eu", ctx=phase_d_app)
    assert rb["action"] == "restored"

    q = await memory_query(source="core", key_like="persona_a", user_id="eu", ctx=phase_d_app)
    row = next(r for r in q["rows"] if r["key"] == "persona_a")
    assert row["value"] == "original persona"

    # full restore removes 'extra' and keeps persona_a at the snapshot state
    out = await memory_history(action="snapshot_restore", name="saga1", user_id="eu", ctx=phase_d_app)
    assert out["restored"] == 1 and out["deleted"] == 1
    q2 = await memory_query(source="core", key_like="persona", user_id="eu", ctx=phase_d_app)
    assert {r["key"] for r in q2["rows"]} == {"persona_a"}
    assert next(r["value"] for r in q2["rows"] if r["key"] == "persona_a") == "original persona"
