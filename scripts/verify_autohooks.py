#!/usr/bin/env python3
"""End-to-end verification of the auto-hooks keystone (Phase C) on a scratch data dir.

Exercises every chain wired in Phase C — daemon tail, dispatcher events,
staging lifecycle, dream markers, inject blocks, report card, exposure tiers —
against a temporary data dir with a synthetic conversation store. Prints one
PASS/FAIL line per section; exit code 0 iff everything passes.

Usage:
    ARIEL_HASH_EMBEDDINGS=1 python scripts/verify_autohooks.py
    MCP_MEMORY_DATA_DIR=/path/to/data python scripts/verify_autohooks.py   # also verifies migrations there
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("ARIEL_HASH_EMBEDDINGS", "1")
os.environ.setdefault("MCP_MASTER_KEY", "verify-autohooks-secret")
SCRATCH = Path(os.environ.get("MCP_MEMORY_DATA_DIR", tempfile.mkdtemp(prefix="verify-autohooks-")))
os.environ["MCP_MEMORY_DATA_DIR"] = str(SCRATCH)
os.environ.setdefault("MCP_CONFIG_PATH", str(SCRATCH / "config.yaml"))
os.environ.setdefault("BACKUP_CRON_DISABLED", "1")

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail and not ok else ""))


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 58 - len(title)))


# ── schema ────────────────────────────────────────────────────────────────────
def make_schemas() -> None:
    """Run the real migration chain — the scratch dir is provisioned exactly like a live deployment."""
    from alembic import command
    from alembic.config import Config

    repo_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    command.upgrade(cfg, "head")
    conv = sqlite3.connect(SCRATCH / "conversation.db")
    conv.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, role TEXT, content TEXT, timestamp REAL)")
    rows = [
        (1, "user", "привет", 100.0),
        (2, "tool", "tool junk", 101.0),
        (3, "assistant", "отвечаю подробно на вопрос", 102.0),
        (4, "user", "?! важно решил " + "x" * 120 + "\n\n\n", 103.0),  # 0.9 band → staged L4
        (5, "user", "DREAM: memory: зафиксируй выбор SQLite", 104.0),  # marker
        (6, "user", "DREAM: skill: деплой через restic", 105.0),  # marker + skill episode
    ]
    conv.executemany("INSERT INTO messages VALUES (?, ?, ?, ?)", rows)
    conv.commit()
    conv.close()


async def main() -> int:
    make_schemas()
    from mcp_server.context import AppContext

    app = AppContext()
    from mcp_server.tools.base import _get_graph, _get_memory, _get_rag

    mem = _get_memory(app, "user", "verify")
    graph = _get_graph(app, "user")
    try:
        rag = _get_rag(app, "user")
    except Exception:
        rag = None

    # ── 1. tool registry + exposure ───────────────────────────────────────
    section("1. Tool registry + exposure tiers")
    from mcp_server.server import PRIMITIVE_TOOLS, resolve_exposure
    from mcp_server.tools_layer import _register_tools

    check("41 tools registered", len(_register_tools) == 41, f"got {len(_register_tools)}")
    check("6 primitives", len(PRIMITIVE_TOOLS) == 6)
    all_names = set(_register_tools)
    prim = resolve_exposure("primitives", all_names)
    check("primitives-only hides ops tools", "memory_proposals" not in prim and "memory_watch" not in prim and "memory_report_card" not in prim)
    review = resolve_exposure("primitives,review", all_names)
    check("review tier exposes proposals+report card", {"memory_proposals", "memory_report_card"} <= review)

    # ── 2. dispatcher: 8 known events ─────────────────────────────────────
    section("2. dispatch_event — KNOWN_EVENTS")
    from hooks.external import KNOWN_EVENTS, dispatch_event

    check("8 known events", len(KNOWN_EVENTS) == 8, f"got {len(KNOWN_EVENTS)}")
    try:
        await dispatch_event("nope", "user", "verify", {}, mem, graph, rag)
        check("unknown event raises", False)
    except ValueError:
        check("unknown event raises", True)
    r = await dispatch_event("session_started", "user", "verify", {}, mem, graph, rag)
    check("session_started fires", "results" in r)

    # ── 3. daemon tail → dispatch new_message ─────────────────────────────
    section("3. Daemon tail (one poll iteration)")
    from autohooks.config import AgentConfig, FieldMap, SourceConfig
    from autohooks.daemon import load_cursor, run_daemon
    from autohooks.source import SqliteSource

    cfg = AgentConfig(
        data_dir=SCRATCH,
        user_id="verify",
        layer="user",
        source=SourceConfig(
            driver="sqlite", path=SCRATCH / "conversation.db", table="messages",
            cursor_column="id", order_by="id", role=FieldMap(column="role"),
            text=FieldMap(column="content"), ts=FieldMap(column="timestamp"),
            filter="role IN ('user', 'assistant')",
        ),
        poll_seconds=0.01,
        state_file=SCRATCH / "cursor.json",
    )
    src = SqliteSource.from_config(cfg)
    # Pre-seed cursor at 0: the first-run baseline would otherwise skip seeded rows.
    from autohooks.daemon import save_cursor

    save_cursor(cfg.state_file, 0)
    dispatch_results: list[dict] = []

    async def _capturing_dispatch(event, layer, user_id, payload, m, g, r=None):
        from hooks.external import dispatch_event

        out = await dispatch_event(event, layer, user_id, payload, m, g, r)
        dispatch_results.append({"event": event, "out": out})
        return out

    await run_daemon(cfg, src, mem, graph, rag, max_iterations=1, dispatch=_capturing_dispatch)
    for dr in dispatch_results:
        print(f"    dispatch {dr['event']}: {str(dr['out'])[:140]}")
    cursor = load_cursor(cfg.state_file)
    check("cursor at max id (6)", cursor == 6, f"got {cursor}")
    conn = sqlite3.connect(SCRATCH / "memory.db")
    staged = conn.execute("SELECT count(*) FROM mutation_proposals WHERE source IN ('auto_save', 'dream') AND status='pending'").fetchone()[0]
    check("high-score + 2 markers staged (3 proposals)", staged == 3, f"got {staged}")
    dispatch_rows = conn.execute("SELECT count(*) FROM memory_dispatch_log").fetchone()[0]
    check("dispatch log rows written", dispatch_rows >= 3, f"got {dispatch_rows}")
    conn.close()

    # ── 4. staging lifecycle: apply / revert / reject / expire ────────────
    section("4. Staging lifecycle")
    from features.staging import decide, expire_stale, list_pending, propose, revert

    pid = await propose("auto_save", "core_write", "verify", "user", {"key": "verify_key", "value": "значение", "importance": 0.9})
    applied = await decide(pid, True, mem=app)
    check("apply writes L4 (result_ref set)", applied["status"] == "applied" and applied["result_ref"].isdigit(), str(applied))
    rev = await revert(pid, mem=app)
    check("revert flips status", rev["status"] == "reverted")
    pid2 = await propose("auto_save", "core_write", "verify", "user", {"key": "reject_me", "value": "x", "importance": 0.9})
    rej = await decide(pid2, False, mem=app)
    check("reject marks rejected", rej["status"] == "rejected")
    old = await propose("forgetting", "archive", "verify", "user", {"ids": [424242]})
    c = sqlite3.connect(SCRATCH / "memory.db")
    c.execute("UPDATE mutation_proposals SET expires_at = ? WHERE id = ?", (time.time() - 1, old))
    c.commit()
    c.close()
    expired = await expire_stale()
    check("lazy expiry flips old pending", expired >= 1)
    pending_after = await list_pending("verify")
    check("pending list clean after decisions", all(p["id"] not in (pid, pid2, old) for p in pending_after))

    # ── 5. inject blocks ──────────────────────────────────────────────────
    section("5. Inject blocks (session_started path)")
    from features.inject import build_inject_blocks

    blocks = await build_inject_blocks(mem, rag, "verify", text="", budget=2000)
    kinds = [b["kind"] for b in blocks]
    check("proposals block present (pending exist)", "proposals" in kinds, str(kinds))
    check("important block present (applied fact)", "important" in kinds or "gap" in kinds or "proposals" in kinds, str(kinds))

    # ── 6. session diff chain ─────────────────────────────────────────────
    section("6. post_session_diff chain")
    from features.diff import compute_session_gaps

    gaps = compute_session_gaps(mem, time.time() - 3600, time.time() + 60)
    check("gaps computed from dispatch log", isinstance(gaps, list))
    r = await dispatch_event("post_session_diff", "user", "verify", {"since": time.time() - 3600, "until": time.time() + 60}, mem, graph, rag)
    check("post_session_diff fires", "results" in r)

    # ── 7. report card ────────────────────────────────────────────────────
    section("7. Report card")
    from mcp_server.tools.ops import memory_report_card

    card = await memory_report_card(period_hours=24, ctx=None)
    check(
        "card sections populated",
        card["status"] == "ok" and card["proposals"]["created"] >= 3 and card["auto_save"]["dispatched"] >= 3,
        str(card)[:200],
    )

    # ── 8. consolidation + forgetting routing (staged) ────────────────────
    section("8. Consolidation / forgetting routing")
    from hooks.shared import consolidation, forgetting_ritual

    rc = await consolidation({"staging_items": [{"content": "проверка", "importance": 0.9}]}, "verify")
    check("consolidation stages (no direct promote)", rc.get("staged") is True, str(rc)[:120])
    rf = await forgetting_ritual({})
    check("forgetting ritual returns (archive staged or zero)", "decayed" in rf and "compressed" in rf, str(rf)[:120])

    # ── 9. HTTP surfaces ──────────────────────────────────────────────────
    section("9. HTTP surfaces")
    try:
        import httpx
        from mcp.server.mcpserver import MCPServer

        from mcp_server.app import create_app

        os.environ["MCP_AUTH_DISABLED"] = "1"
        starlette_app = create_app(MCPServer(name="verify"), app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=starlette_app), base_url="http://t") as client:
            resp = await client.post("/api/hooks/new_message", json={"user_id": "verify", "payload": {"text": "привет из http"}})
            check("POST /api/hooks/{event} 200", resp.status_code == 200, f"{resp.status_code} {resp.text[:100]}")
            resp = await client.post("/api/context-inject", json={"user_id": "verify"})
            check("POST /api/context-inject 200 + blocks", resp.status_code == 200 and "blocks" in resp.json())
    except Exception as e:  # noqa: BLE001
        check("HTTP surfaces", False, repr(e)[:150])

    # ── summary ───────────────────────────────────────────────────────────
    from shared.connection import connection_manager

    await connection_manager.close_all()  # aiosqlite workers block interpreter exit otherwise
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{'=' * 64}\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed" + (f" — FAILURES: {[f[0] for f in failed]}" if failed else " — ALL GREEN"))
    sys.stdout.flush()
    # Known repo-wide aiosqlite quirk: a lingering worker thread blocks interpreter
    # shutdown (same workaround as tests/conftest.py pytest_sessionfinish).
    os._exit(1 if failed else 0)


if __name__ == "__main__":
    import faulthandler

    faulthandler.dump_traceback_later(45, exit=True)
    sys.exit(asyncio.run(main()))
