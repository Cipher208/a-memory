"""Continuity actuality: recap must surface the last REAL work session.

Regression for the Ф1 continuity gap (2026-09-12): the day-close checkpoint
lived in agent-layer L3, the interactive session never closed via
SessionStore, and a 6-minute diagnostic CLI session shadowed it — so the
next session woke up on "Memory audit session". Rules proven here:

- recap_session prefers the most SUBSTANTIVE closed session (message_count,
  then recency), not merely the most recently closed one.
- recap_checkpoint surfaces session_summary-tagged L3 episodes from BOTH
  layers (user + agent), newest first.
- recap_notes reads the newest notes.md tail ONLY when continuity.notes_glob
  is configured (agent-specific; empty default = off, never an error).
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from shared.connection import connection_manager


@pytest.fixture
async def recap_db(tmp_path, monkeypatch):
    """Isolated memory.db with the episodes/sessions tables (raw sqlite)."""
    original = connection_manager.base_dir
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    await connection_manager.close_all()
    db = tmp_path / "memory.db"
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE episodes ("
            " episode_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,"
            " layer TEXT NOT NULL DEFAULT 'user', summary TEXT NOT NULL,"
            " emotional_weight REAL DEFAULT 0.5, tags TEXT, memory_kind TEXT, created_at REAL NOT NULL)"
        )
        conn.commit()
    yield db
    connection_manager.base_dir = original
    await connection_manager.close_all()


class _FakeL3:
    def __init__(self, by_tag=None):
        self._by_tag = by_tag or {}

    async def search_by_tag(self, user_id, tag, limit=10):
        return self._by_tag.get(tag, [])


class _FakeL4:
    async def get_all(self, user_id, limit):
        return []


class _FakeMem:
    def __init__(self, by_tag=None):
        self.l1 = type("R", (), {"get_recent": staticmethod(lambda n: [])})()
        self.l3 = _FakeL3(by_tag)
        self.l4 = _FakeL4()


def _sess(summary, message_count, started_at, ended_at=1.0):
    from core.session import SessionRecord

    return SessionRecord(
        session_id=f"s_{summary[:8]}",
        user_id="u1",
        summary=summary,
        message_count=message_count,
        started_at=started_at,
        ended_at=ended_at,
    )


class _FakeSessionStore:
    def __init__(self, rows):
        self._rows = rows

    async def get_recent_sessions(self, user_id, limit):
        return self._rows[:limit]


# ── recap_session: substantive selection ──


@pytest.mark.asyncio
async def test_recap_degraded_marker_on_axis_failure(monkeypatch):
    """A dead axis must announce itself — a thin pack must not read as empty."""
    from features.continuity import session_recap

    class _Boom:
        async def get_recent_sessions(self, user_id, limit):
            raise RuntimeError("db locked")

    monkeypatch.setattr("core.session.SessionStore", lambda: _Boom())
    blocks = await session_recap(_FakeMem(), "u1")
    degraded = next(b for b in blocks if b["axis"] == "recap_degraded")
    assert "session:" in degraded["content"]
    assert "db locked" in degraded["content"]


@pytest.mark.asyncio
async def test_recap_session_prefers_substantive(monkeypatch):
    """A 0-message audit session closed LAST must not shadow the work day."""
    from features.continuity import session_recap

    now = time.time()
    rows = [
        _sess("Memory audit session — no user interaction", 0, now - 60),
        _sess("Migration day complete: opencode + vps2 VPN cutover", 52, now - 7200),
    ]
    monkeypatch.setattr("core.session.SessionStore", lambda: _FakeSessionStore(rows))

    blocks = await session_recap(_FakeMem(), "u1")
    session_block = next(b for b in blocks if b["axis"] == "recap_session")
    assert "Migration day complete" in session_block["content"]
    assert "audit session" not in session_block["content"]


@pytest.mark.asyncio
async def test_recap_session_falls_back_to_any_closed(monkeypatch):
    """With only trivial sessions closed, still show something (not silence)."""
    from features.continuity import session_recap

    now = time.time()
    rows = [
        _sess("cron probe session", 0, now - 60),
        _sess("another probe", 0, now - 3600),
    ]
    monkeypatch.setattr("core.session.SessionStore", lambda: _FakeSessionStore(rows))

    blocks = await session_recap(_FakeMem(), "u1")
    session_block = next(b for b in blocks if b["axis"] == "recap_session")
    assert "cron probe" in session_block["content"]


# ── recap_checkpoint: dual-layer session_summary episodes ──


@pytest.mark.asyncio
async def test_recap_checkpoint_surfaces_agent_layer(recap_db):
    """Day-close checkpoint saved to AGENT-layer L3 must reach the recap."""
    from core.episodic import EpisodicMemory
    from features.continuity import session_recap

    now = time.time()
    with sqlite3.connect(str(recap_db)) as conn:
        conn.execute(
            "INSERT INTO episodes (user_id, layer, summary, tags, created_at) VALUES (?, ?, ?, ?, ?)",
            ("u1", "agent", "DAY CLOSE: migration complete, VPN cutover done", '["session_summary"]', now - 120),
        )
        conn.commit()

    mem = _FakeMem()
    mem.l3 = EpisodicMemory(layer="user")  # user layer empty; agent has the checkpoint

    blocks = await session_recap(mem, "u1")
    checkpoint = next(b for b in blocks if b["axis"] == "recap_checkpoint")
    assert "DAY CLOSE" in checkpoint["content"]


@pytest.mark.asyncio
async def test_recap_checkpoint_newest_first_across_layers(recap_db):
    """When both layers hold checkpoints, the newest summary wins."""
    from core.episodic import EpisodicMemory
    from features.continuity import session_recap

    now = time.time()
    with sqlite3.connect(str(recap_db)) as conn:
        conn.execute(
            "INSERT INTO episodes (user_id, layer, summary, tags, created_at) VALUES (?, ?, ?, ?, ?)",
            ("u1", "agent", "older agent checkpoint", '["session_summary"]', now - 9000),
        )
        conn.execute(
            "INSERT INTO episodes (user_id, layer, summary, tags, created_at) VALUES (?, ?, ?, ?, ?)",
            ("u1", "user", "fresh user day-close", '["session_summary"]', now - 60),
        )
        conn.commit()

    mem = _FakeMem()
    mem.l3 = EpisodicMemory(layer="user")

    blocks = await session_recap(mem, "u1")
    checkpoint = next(b for b in blocks if b["axis"] == "recap_checkpoint")
    assert "fresh user day-close" in checkpoint["content"]


# ── recap_notes: config-gated notes.md tail ──


@pytest.mark.asyncio
async def test_recap_notes_off_by_default(recap_db, monkeypatch):
    """No continuity.notes_glob configured → no notes axis, no errors."""
    from features.continuity import session_recap

    monkeypatch.setattr("config.config._data", {}, raising=False)
    blocks = await session_recap(_FakeMem(), "u1")
    assert not any(b["axis"] == "recap_notes" for b in blocks)


@pytest.mark.asyncio
async def test_recap_notes_reads_latest_tail(recap_db, tmp_path, monkeypatch):
    """Glob set → newest notes.md tail surfaces for the next session."""
    from features.continuity import session_recap

    old_dir = tmp_path / "sessions" / "ses_old"
    new_dir = tmp_path / "sessions" / "ses_new"
    old_dir.mkdir(parents=True)
    new_dir.mkdir(parents=True)
    (old_dir / "notes.md").write_text("stale handoff\n", encoding="utf-8")
    (new_dir / "notes.md").write_text(
        "# Session notes\n\n## day close\n- POLZA key rotation pending\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "config.config._data",
        {"continuity": {"notes_glob": str(tmp_path / "sessions" / "*" / "notes.md")}},
        raising=False,
    )

    blocks = await session_recap(_FakeMem(), "u1")
    notes = next(b for b in blocks if b["axis"] == "recap_notes")
    assert "POLZA key rotation pending" in notes["content"]
    assert "stale handoff" not in notes["content"]


@pytest.mark.asyncio
async def test_recap_notes_skips_template_only_sessions(recap_db, tmp_path, monkeypatch):
    """Every session gets a template notes.md; the newest may be EMPTY.

    A fresh audit session closed seconds ago must not shadow the real
    handoff — files with no content beyond the 2-line template are skipped.
    """
    from features.continuity import session_recap

    tpl = "# Session notes\n_Append-only scratchpad. Format each entry as: ## [turn N].\n"
    fresh_dir = tmp_path / "sessions" / "ses_fresh"
    work_dir = tmp_path / "sessions" / "ses_work"
    fresh_dir.mkdir(parents=True)
    work_dir.mkdir(parents=True)
    (fresh_dir / "notes.md").write_text(tpl, encoding="utf-8")  # newest, empty
    (work_dir / "notes.md").write_text(
        tpl + "\n## [day close]\n- MIGRATION DAY COMPLETE\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "config.config._data",
        {"continuity": {"notes_glob": str(tmp_path / "sessions" / "*" / "notes.md")}},
        raising=False,
    )

    blocks = await session_recap(_FakeMem(), "u1")
    notes = next(b for b in blocks if b["axis"] == "recap_notes")
    assert "MIGRATION DAY COMPLETE" in notes["content"]
    assert "Append-only scratchpad" not in notes["content"]


@pytest.mark.asyncio
async def test_recap_notes_glob_no_match_is_silent(recap_db, tmp_path, monkeypatch):
    """Glob set but matches nothing (fresh agent, no notes yet) → silent skip."""
    from features.continuity import session_recap

    monkeypatch.setattr(
        "config.config._data",
        {"continuity": {"notes_glob": str(tmp_path / "nowhere" / "*" / "notes.md")}},
        raising=False,
    )
    blocks = await session_recap(_FakeMem(), "u1")
    assert not any(b["axis"] == "recap_notes" for b in blocks)
