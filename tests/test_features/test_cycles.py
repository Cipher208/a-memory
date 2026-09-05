"""C7: cycles-daemon config, chimera cost-cap, gap-reader flags, Mermaid canvas.

cycle_due/check_budget — чистая математика (features/cycles.py); gap-reader —
расширение audit_content (unknown-вопросы + create_safety-вердикты по L0);
Mermaid — lifecycle/graph_mermaid.py → graph TD строка для epi_nodes/epi_edges.
"""

import sqlite3
import time

import pytest

from features.cycles import CycleBudget, CycleConfig, RollingCounter, check_budget, cycle_due


# --- cycles config + cycle_due math ---


def test_cycle_config_defaults_match_plan_intervals():
    cfg = CycleConfig()
    assert (cfg.dream_hours, cfg.gap_hours, cfg.reminder_minutes, cfg.inactivity_hours) == (24, 1, 60, 3)


def test_cycle_due_math():
    now = time.time()
    assert cycle_due(now - 25 * 3600, 24) is True  # 25h since last dream (24h) → due
    assert cycle_due(now - 23 * 3600, 24) is False  # 23h → not yet
    assert cycle_due(0, 24) is True  # never run → due immediately
    assert cycle_due(now - 61 * 60, 1) is True  # 61m vs 1h interval
    assert cycle_due(now - 59 * 60, 1) is False


# --- cost-cap: allow / throttle / block ---


def test_check_budget_verdicts():
    b = CycleBudget()
    assert (b.max_per_cycle, b.max_rolling_60m, b.max_per_task) == (50, 200, 100)
    assert check_budget(10, [], 5) == "allow"
    assert check_budget(b.max_per_cycle, [], 0) == "block"  # per-cycle cap hit
    assert check_budget(10, [], b.max_per_task) == "block"  # per-task cap hit
    rolling = [time.time() - i * 10 for i in range(b.max_rolling_60m)]
    assert check_budget(10, rolling, 0) == "throttle"  # rolling-60m cap hit
    assert check_budget(int(b.max_per_cycle * 0.9), [], 0) == "throttle"  # approaching per-cycle cap
    assert check_budget(10, [time.time() - 7200], 0) == "allow"  # old stamps outside window


def test_rolling_counter_prunes_window():
    counter = RollingCounter()
    now = time.time()
    counter.record(now - 3700)  # outside 60m window
    counter.record(now)
    assert counter.count(now) == 1
    assert counter.as_list() == [now]


# --- gap-reader: audit_content extension (unknown + create_safety) ---


@pytest.fixture
async def audit_db(tmp_path, monkeypatch):
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    from shared.migrations import migration_manager

    await migration_manager.migrate()
    yield tmp_path
    connection_manager._conns.clear()


from shared.connection import connection_manager


def _fact(db, key: str, value: str, *, kind: str = "fact", age_days: float = 0.0, importance: float = 0.5, user_id: str = "u1") -> None:
    now = time.time() - age_days * 86400
    with sqlite3.connect(str(db / "memory.db")) as conn:
        conn.execute(
            "INSERT INTO core_memory (layer, user_id, key, value, importance, memory_kind, created_at, updated_at)"
            " VALUES ('user', ?, ?, ?, ?, ?, ?, ?)",
            (user_id, key, value, importance, kind, now, now),
        )


def _l0(db, text: str, ts: float | None = None) -> None:
    with sqlite3.connect(str(db / "memory.db")) as conn:
        conn.execute(
            "INSERT INTO l0_journal (ts, event, layer, user_id, text, raw_type, status, decisions)"
            " VALUES (?, 'test', 'user', 'u1', ?, 'plain', 'received', '[]')",
            (ts if ts is not None else time.time(), text),
        )


async def test_gap_reader_flags_unanswered_old_questions(audit_db):
    _fact(audit_db, "q.open", "Почему nightly-хук молчит?", kind="question", age_days=8)
    _fact(audit_db, "q.orm-choice", "Что выбрать: SQLAlchemy 2.0 или 1.4?", kind="question", age_days=8)
    _fact(audit_db, "q.orm-choice.answer", "SQLAlchemy 2.0", kind="fact", age_days=7)
    _fact(audit_db, "q.fresh", "Почему тест падает?", kind="question", age_days=1)

    from features.diagnostics import audit_content

    checks = {c["type"]: c for c in await audit_content("u1")}
    unknown = checks["unknown"]
    assert [i["key"] for i in unknown["items"]] == ["q.open"]
    assert unknown["suggestion"]


async def test_gap_reader_no_questions_no_unknown_check(audit_db):
    _fact(audit_db, "plain", "nothing to see")
    from features.diagnostics import audit_content

    assert await audit_content("u1") == []


async def test_create_safety_verdicts_from_l0(audit_db):
    _fact(audit_db, "f.exists", "Deploy happens at noon sharp", importance=0.9)
    _fact(audit_db, "f.probable", "PostgreSQL migration scheduled for June", importance=0.85)
    _fact(audit_db, "f.unknown", "User prefers vim keybindings in the terminal", importance=0.8)
    _fact(audit_db, "f.low", "Low importance hearsay nobody confirms", importance=0.5)
    _l0(audit_db, "Deploy happens at noon sharp per the schedule")
    _l0(audit_db, "talked about the postgresql migration timeline today")

    from features.diagnostics import audit_content

    checks = {c["type"]: c for c in await audit_content("u1")}
    safety = {i["key"]: i["verdict"] for i in checks["create_safety"]["items"]}
    assert safety == {"f.exists": "exists", "f.probable": "probable", "f.unknown": "unknown"}
    assert checks["create_safety"]["severity"] == "warn"


# --- Mermaid canvas ---


def _epi(db, nodes: list[tuple[int, str, str]], edges: list[tuple[int, int, str]]) -> None:
    now = time.time()
    with sqlite3.connect(str(db / "memory.db")) as conn:
        conn.executemany(
            "INSERT INTO epi_nodes (node_id, layer, user_id, content, node_type, created_at) VALUES (?, 'user', 'u1', ?, ?, ?)",
            [(nid, content, ntype, now) for nid, content, ntype in nodes],
        )
        conn.executemany(
            "INSERT INTO epi_edges (source_id, target_id, relation, weight, created_at) VALUES (?, ?, ?, 0.8, ?)",
            [(s, t, rel, now) for s, t, rel in edges],
        )


async def test_render_mermaid_nodes_and_edges(audit_db):
    _epi(
        audit_db,
        [(1, "Boris", "person"), (2, 'Acme "corp"', "organization")],
        [(1, 2, "works_with")],
    )
    from lifecycle.graph_mermaid import render_mermaid
    from shared.connection import connection_manager
    from shared.constants import DB_NAME

    conn = await connection_manager.get(DB_NAME)
    out = await render_mermaid(conn, "user")
    assert out.startswith("graph TD")
    assert 'N1["Boris (person)"]' in out
    assert "N1 -->|works_with| N2" in out
    assert "\"Acme 'corp' (organization)\"" in out  # quotes escaped, type shown


async def test_render_mermaid_limit_and_edge_filter(audit_db):
    _epi(
        audit_db,
        [(1, "A", "person"), (2, "B", "person"), (3, "C", "person")],
        [(2, 3, "knows")],  # edge to node outside limit → must not render
    )
    now = time.time()
    with sqlite3.connect(str(audit_db / "memory.db")) as db:
        db.execute(
            "INSERT INTO epi_edges (source_id, target_id, relation, weight, created_at, status) VALUES (1, 2, 'expired_rel', 0.8, ?, 'expired')",
            (now,),
        )
    from lifecycle.graph_mermaid import render_mermaid
    from shared.connection import connection_manager
    from shared.constants import DB_NAME

    conn = await connection_manager.get(DB_NAME)
    out = await render_mermaid(conn, "user", limit=2)
    assert "N1" in out and "N2" in out and "N3" not in out
    assert "knows" not in out
    assert "expired_rel" not in out  # G2.0: expired-рёбра не рисуем


async def test_render_mermaid_empty_graph(audit_db):
    from lifecycle.graph_mermaid import render_mermaid
    from shared.connection import connection_manager
    from shared.constants import DB_NAME

    conn = await connection_manager.get(DB_NAME)
    assert await render_mermaid(conn, "user") == "graph TD"
