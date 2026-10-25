"""F101 — an episode id is not an authorization.

Every case uses TWO distinct user ids and BOTH layers: with a single user the
defect is invisible, because every live base is `user_id='default'` and a missing
predicate looks correct.
"""

from __future__ import annotations

import sqlite3
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from core import MemoryManager
from features.diff import compute_session_gaps
from features.rate_limiting import RateLimiter
from features.skill_pipeline import promote_episodes
from mcp_server.tools.episodic import memory_episode_get
from mcp_server.tools.primitives.forget import forget
from shared.archived_memories import ArchivedMemories
from shared.connection import AsyncConnectionManager, connection_manager
from shared.constants import DB_NAME
from shared.write_dispatch import get_write_dispatch

if TYPE_CHECKING:
    from pathlib import Path


async def _l3(mm: MemoryManager, layer: str, user_id: str) -> Any:
    mem = mm.user_memory(user_id) if layer == "user" else mm.agent_memory(user_id)
    await mem.l3._init_db()
    return mem.l3


def _app(mm: MemoryManager, cm: AsyncConnectionManager) -> SimpleNamespace:
    return SimpleNamespace(
        mm=mm,
        user_graph=MagicMock(),
        agent_graph=MagicMock(),
        rate_limiter=RateLimiter(cm=cm),
    )


def _ctx(app: Any) -> Any:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = app
    return ctx


@pytest.fixture()
async def cm(tmp_path: Path) -> Any:
    manager = AsyncConnectionManager(base_dir=str(tmp_path))
    yield manager
    await manager.close_all()


@pytest.fixture()
def fresh_dir(tmp_path: Path) -> Path:
    original = connection_manager.base_dir
    connection_manager.base_dir = tmp_path
    connection_manager._conns.clear()
    yield tmp_path
    connection_manager._conns.clear()
    connection_manager.base_dir = original


# ── 1. get_by_id is scoped to the caller's user ────────────────────────────────


async def test_get_by_id_does_not_return_a_foreign_users_episode(cm: AsyncConnectionManager) -> None:
    mm = MemoryManager(cm=cm)
    alice = await _l3(mm, "user", "alice")
    bob = await _l3(mm, "user", "bob")
    alice_ep = await alice.save("alice", "alice's episode")
    bob_ep = await bob.save("bob", "bob's episode")

    assert await alice.get_by_id(bob_ep, user_id="alice") is None
    owner = await alice.get_by_id(alice_ep, user_id="alice")
    assert owner is not None and owner.summary == "alice's episode"


# ── 2. get_by_id does not cross layers for the same user ───────────────────────


async def test_get_by_id_does_not_cross_layers_for_the_same_user(cm: AsyncConnectionManager) -> None:
    mm = MemoryManager(cm=cm)
    user_l3 = await _l3(mm, "user", "alice")
    agent_l3 = await _l3(mm, "agent", "alice")
    agent_ep = await agent_l3.save("alice", "agent-layer decision")

    assert await user_l3.get_by_id(agent_ep, user_id="alice") is None
    assert await agent_l3.get_by_id(agent_ep, user_id="alice") is not None


# ── 3. delete_by_ids is scoped to the caller's (user, layer) ───────────────────


async def test_delete_by_ids_does_not_delete_a_foreign_row(cm: AsyncConnectionManager) -> None:
    mm = MemoryManager(cm=cm)
    alice = await _l3(mm, "user", "alice")
    bob = await _l3(mm, "user", "bob")
    agent_alice = await _l3(mm, "agent", "alice")
    bob_ep = await bob.save("bob", "bob keeps this")
    agent_ep = await agent_alice.save("alice", "agent row survives")
    alice_ep = await alice.save("alice", "alice removes this")

    assert await alice.delete_by_ids([bob_ep], user_id="alice") == 0
    assert await bob.get_by_id(bob_ep, user_id="bob") is not None

    assert await alice.delete_by_ids([agent_ep], user_id="alice") == 0
    assert await agent_alice.get_by_id(agent_ep, user_id="alice") is not None

    assert await alice.delete_by_ids([alice_ep], user_id="alice") == 1


# ── 4. forget(scope="episode_ids") refuses a foreign id ────────────────────────


async def test_forget_episode_ids_does_not_reach_a_foreign_user(cm: AsyncConnectionManager) -> None:
    mm = MemoryManager(cm=cm)
    alice = await _l3(mm, "user", "alice")
    bob = await _l3(mm, "user", "bob")
    bob_ep = await bob.save("bob", "bob's private episode")
    await ArchivedMemories(cm=cm)._init_db()
    app = _app(mm, cm)

    result = await forget(scope="episode_ids", episode_ids=[bob_ep], user_id="alice", layer="user", ctx=_ctx(app))

    assert result["deleted_l3"] == 0
    assert result["not_found"] == [bob_ep]
    assert result["episodes"] == []
    assert await bob.get_by_id(bob_ep, user_id="bob") is not None
    archived = await get_write_dispatch(cm).query(DB_NAME, "SELECT user_id, original_id FROM archived_memories")
    assert archived == [], "a foreign episode must not be archived under the caller's user"
    assert await alice.get_by_id(bob_ep, user_id="alice") is None


# ── 5. skill_promote refuses a foreign episode id ──────────────────────────────


class _Wiki:
    def __init__(self) -> None:
        self.pages: list[str] = []

    async def get(self, path: str) -> None:
        return None

    async def add(self, wiki_type: str, title: str, content: str, tags: list[str] | None = None, importance: float = 0.5) -> str:
        path = f"{wiki_type}/{title.replace(' ', '_')}.md"
        self.pages.append(path)
        return path

    async def update(self, path: str, content: str | None = None, importance: float | None = None) -> None:
        self.pages.append(path)


async def test_promote_episodes_refuses_a_foreign_episode(cm: AsyncConnectionManager) -> None:
    mm = MemoryManager(cm=cm)
    bob = await _l3(mm, "user", "bob")
    bob_ep = await bob.save("bob", "bob's private skill insight", tags=["dream_skill"])
    wiki = _Wiki()

    result = await promote_episodes(mm.user_memory("alice"), wiki, "alice", [bob_ep])

    assert result["promoted"] == []
    assert result["skipped"] == [{"episode_id": bob_ep, "reason": "not_found"}]
    assert wiki.pages == []
    still = await bob.get_by_id(bob_ep, user_id="bob")
    assert still is not None and "skill_promoted" not in (still.tags or [])


# ── 6. gaps: the SQL and the hook stay inside the caller's user ────────────────


def _seed_dispatch_log(tmp_path: Path, rows: list[tuple]) -> None:
    conn = sqlite3.connect(tmp_path / "memory.db")
    conn.executescript("""
        CREATE TABLE memory_dispatch_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL, source_msg_id INTEGER,
            layer TEXT NOT NULL DEFAULT 'user',
            user_id TEXT NOT NULL DEFAULT 'default',
            score REAL,
            saved_l3 INTEGER NOT NULL DEFAULT 0,
            saved_l4 INTEGER NOT NULL DEFAULT 0,
            saved_graph INTEGER NOT NULL DEFAULT 0,
            text_preview TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL
        );
    """)
    for r in rows:
        if len(r) == 9:
            event, smid, owner, score, l3, l4, graph, created, preview = r
            layer = "user"
        else:
            event, smid, owner, layer, score, l3, l4, graph, created, preview = r
        conn.execute(
            "INSERT INTO memory_dispatch_log (event, source_msg_id, user_id, layer, score, saved_l3, saved_l4, saved_graph, created_at, text_preview) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event, smid, owner, layer, score, l3, l4, graph, created, preview),
        )
    conn.commit()
    conn.close()


class _FakeL3:
    def __init__(self, layer: str = "user") -> None:
        self.layer = layer
        self.saved: list[dict] = []

    async def save(self, user_id: str, summary: str, weight: float, tags: list[str], created_at: float | None = None) -> int:
        self.saved.append({"user_id": user_id, "summary": summary, "weight": weight, "tags": tags})
        return len(self.saved)


class _FakeMem:
    def __init__(self, layer: str = "user") -> None:
        self.l3 = _FakeL3(layer)


async def test_session_gaps_and_diff_hook_stay_inside_the_callers_user(fresh_dir: Path) -> None:
    now = time.time()
    _seed_dispatch_log(
        fresh_dir,
        [
            ("new_message", 1, "alice", 0.9, 1, 0, 1, now, "alice's preview"),
            ("new_message", 2, "bob", 0.9, 1, 0, 1, now, "bob's preview"),
        ],
    )

    gaps = compute_session_gaps(_FakeMem(), since=0, until=now + 1, user_id="alice", layer="user")
    assert [g["source_msg_id"] for g in gaps] == [1]
    assert all("bob's preview" not in g["text_preview"] for g in gaps)

    from mcp_server.context import AppContext

    mem = _FakeMem(layer="user")
    result = await AppContext().user_hooks._post_session_diff({"user_id": "alice", "since": 0, "until": now + 1}, mem=mem)

    assert result["gaps"] == 1
    assert len(mem.l3.saved) == 1
    assert mem.l3.saved[0]["user_id"] == "alice"
    assert "alice's preview" in mem.l3.saved[0]["summary"]
    assert "bob's preview" not in mem.l3.saved[0]["summary"]


async def test_session_gaps_do_not_cross_layers_for_the_same_user(fresh_dir: Path) -> None:
    """One user, two layers: an agent-layer dispatch must not become a user-layer gap.

    The user predicate alone is not the scope: `memory_dispatch_log` carries a
    `layer` column, and the same user's agent-layer preview is a foreign read for
    a user-layer caller — it would land in the caller's L3 as a `diff_gap`.
    """
    now = time.time()
    _seed_dispatch_log(
        fresh_dir,
        [
            ("new_message", 11, "alice", "user", 0.9, 1, 0, 1, now, "alice USER-layer preview"),
            ("new_message", 12, "alice", "agent", 0.9, 1, 0, 1, now, "alice AGENT-layer secret"),
        ],
    )

    gaps = compute_session_gaps(_FakeMem(), since=0, until=now + 1, user_id="alice", layer="user")
    assert [g["source_msg_id"] for g in gaps] == [11]
    assert all("AGENT-layer" not in g["text_preview"] for g in gaps)

    from mcp_server.context import AppContext

    mem = _FakeMem(layer="user")
    result = await AppContext().user_hooks._post_session_diff({"user_id": "alice", "since": 0, "until": now + 1}, mem=mem)

    assert result["gaps"] == 1
    assert len(mem.l3.saved) == 1
    assert "USER-layer preview" in mem.l3.saved[0]["summary"]
    assert "AGENT-layer secret" not in mem.l3.saved[0]["summary"]

    # The hook must read the layer off the bound EpisodicMemory, not assume "user":
    # the agent-layer caller sees the agent row and never the user one.
    agent_mem = _FakeMem(layer="agent")
    agent_result = await AppContext().user_hooks._post_session_diff({"user_id": "alice", "since": 0, "until": now + 1}, mem=agent_mem)

    assert agent_result["gaps"] == 1
    assert "AGENT-layer secret" in agent_mem.l3.saved[0]["summary"]
    assert "USER-layer preview" not in agent_mem.l3.saved[0]["summary"]


# ── 7. episode_get filters the layer ───────────────────────────────────────────


async def test_memory_episode_get_does_not_cross_layers(cm: AsyncConnectionManager) -> None:
    mm = MemoryManager(cm=cm)
    agent_l3 = await _l3(mm, "agent", "alice")
    agent_ep = await agent_l3.save("alice", "agent-layer decision")
    ctx = _ctx(_app(mm, cm))

    wrong_layer = await memory_episode_get(layer="user", user_id="alice", episode_id=agent_ep, ctx=ctx)
    assert wrong_layer == {"error": "episode_not_found", "episode_id": agent_ep}

    right_layer = await memory_episode_get(layer="agent", user_id="alice", episode_id=agent_ep, ctx=ctx)
    assert right_layer["episode_id"] == agent_ep
