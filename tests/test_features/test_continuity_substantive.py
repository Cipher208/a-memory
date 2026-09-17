"""Substantive pick + service labeling in the recap session axis.

Elli 16.09 D2: with only a diagnostic row in sessions, recap served it as
"last session". Rule: message_count-first ordering (counts are now persisted
by close_session); a service-only base surfaces the row LABELED, never
posing as work, never silent (pinned: "still show something").
"""

import time
from types import SimpleNamespace

import pytest

from features.continuity import _pick_substantive


def _s(summary, msgs, started):
    return SimpleNamespace(summary=summary, message_count=msgs, started_at=started, ended_at=started + 60)


def test_prefers_messages_over_recency():
    rows = [
        _s("Memory audit session — no user interaction, diagnostic only", 0, 1789590600.0),
        _s("Real work", 10, 1789588700.0),
    ]
    assert _pick_substantive(rows).summary == "Real work"


class _FakeL3:
    def __init__(self, by_tag=None):
        self._by_tag = by_tag or {}

    async def search_by_tag(self, user_id, tag, limit=10):
        return self._by_tag.get(tag, [])


class _FakeMem:
    def __init__(self, by_tag=None):
        self.l1 = type("R", (), {"get_recent": staticmethod(lambda n: [])})()
        self.l3 = _FakeL3(by_tag)
        self.l4 = type("L4", (), {"get_all": staticmethod(lambda u, lim: [])})()


def _rec(summary, message_count, started_at):
    from core.session import SessionRecord

    return SessionRecord(
        session_id=f"s_{summary[:8]}",
        user_id="u1",
        summary=summary,
        message_count=message_count,
        started_at=started_at,
        ended_at=started_at + 60,
    )


class _FakeSessionStore:
    def __init__(self, rows):
        self._rows = rows

    async def get_recent_sessions(self, user_id, limit):
        return self._rows[:limit]


@pytest.mark.asyncio
async def test_service_only_base_labels_not_silences(monkeypatch):
    from features.continuity import session_recap

    now = time.time()
    rows = [_rec("Memory audit session — no user interaction, diagnostic only", 0, now - 60)]
    monkeypatch.setattr("core.session.SessionStore", lambda: _FakeSessionStore(rows))

    blocks = await session_recap(_FakeMem(), "u1")
    session_block = next(b for b in blocks if b["axis"] == "recap_session")
    assert "Memory audit" in session_block["content"]
    assert "(service, 0 messages)" in session_block["content"]


@pytest.mark.asyncio
async def test_substantive_pick_stays_unlabeled(monkeypatch):
    from features.continuity import session_recap

    now = time.time()
    rows = [
        _rec("Memory audit session — no user interaction, diagnostic only", 0, now - 60),
        _rec("Migration day complete", 52, now - 7200),
    ]
    monkeypatch.setattr("core.session.SessionStore", lambda: _FakeSessionStore(rows))

    blocks = await session_recap(_FakeMem(), "u1")
    session_block = next(b for b in blocks if b["axis"] == "recap_session")
    assert "Migration day complete" in session_block["content"]
    assert "service" not in session_block["content"]
