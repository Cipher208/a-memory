"""A refusal must be written down — the counter the sixth bug needed.

`memory_dispatch_log` records what was SAVED; nothing recorded what was REFUSED.
Every guard in `auto_save_text` and every block in the middleware pipeline returns
before the dispatch-log insert, so a refused message existed nowhere: not in
`l0_journal` (the door runs before capture), not in the log, not in a warning.
Measured on a live base before this table: 1419 dispatch rows, not one with
`score = 0.0`.

That silence hid the sixth bug. `_TRANSCRIPT_HEAD` carried the whole markdown
set, so the door discarded 591 of one persona's 734 substantive messages — 80% of
her voice — and the only way to find it was to re-derive the number from the
source database by hand. With the counter it is a rate change an operator sees:
one or two refusals a day, then 591 in a single window.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from shared.connection import connection_manager
from shared.door_log import normalize_reason

if TYPE_CHECKING:
    from pathlib import Path

_REJECTIONS_DDL = """
    CREATE TABLE IF NOT EXISTS memory_dispatch_rejections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event TEXT NOT NULL DEFAULT '',
        source_msg_id INTEGER,
        layer TEXT NOT NULL DEFAULT 'user',
        user_id TEXT NOT NULL DEFAULT 'default',
        reason TEXT NOT NULL,
        text_preview TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS memory_dispatch_log (
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
    CREATE TABLE IF NOT EXISTS l0_journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content_hash TEXT
    );
"""


@pytest.fixture()
def fresh_dir(tmp_path: Path) -> Path:
    original = connection_manager.base_dir
    connection_manager.base_dir = tmp_path
    connection_manager._conns.clear()
    yield tmp_path
    connection_manager._conns.clear()
    connection_manager.base_dir = original


@pytest.fixture()
def schema(fresh_dir: Path) -> Path:
    conn = sqlite3.connect(fresh_dir / "memory.db")
    conn.executescript(_REJECTIONS_DDL)
    conn.commit()
    conn.close()
    return fresh_dir


def _rejections() -> list[tuple]:
    conn = sqlite3.connect(connection_manager.base_dir / "memory.db")
    rows = conn.execute("SELECT reason, layer, user_id, source_msg_id, text_preview FROM memory_dispatch_rejections ORDER BY id").fetchall()
    conn.close()
    return rows


class _FakeL3:
    def __init__(self) -> None:
        self.saved: list[tuple] = []

    async def save(self, user_id: str, summary: str, weight: float, tags: list[str]) -> int:
        self.saved.append((user_id, summary, weight, tags))
        return len(self.saved)


class _FakeMem:
    def __init__(self) -> None:
        self.l3 = _FakeL3()

    async def remember(self, key: str, value: str, importance: float) -> int:
        return 1


class _FakeGraph:
    async def add_node(self, *args, **kwargs) -> int:
        return 1


async def test_refused_dump_is_written_down_with_its_reason(schema: Path) -> None:
    from hooks.external import auto_save_text

    dump = '[{"type": "text", "text": "*я дочитываю план"}]'
    result = await auto_save_text(_FakeMem(), _FakeGraph(), user_id="u1", text=dump, event="new_message", source_msg_id=42)

    assert result["skipped"] == "transcript"
    rows = _rejections()
    assert len(rows) == 1, "a refusal that leaves no row is the bug this table exists to prevent"
    reason, layer, user_id, source_msg_id, preview = rows[0]
    assert reason == "transcript"
    assert layer == "user"
    assert user_id == "u1"
    assert source_msg_id == 42
    assert preview.startswith('[{"type"')


async def test_refusal_is_recorded_under_the_layer_routing_chose(schema: Path) -> None:
    """The layer belongs to the message, so a refusal carries the routed one.

    `l0_layer` used to be computed below the guards, which meant a refusal could
    only ever be attributed by re-deriving the routing. A persona's refused
    declaration would have been logged under the owner's layer — the same
    confusion the third bug fixed for saves, repeated in the refusal log.
    """
    from hooks.external import auto_save_text

    result = await auto_save_text(
        _FakeMem(),
        _FakeGraph(),
        user_id="u1",
        text='[{"type": "text"}]',
        event="new_message",
        source_msg_id=43,
        role="assistant",
        persona_owner=True,
    )

    assert result["skipped"] == "transcript"
    assert _rejections()[0][1] == "agent"


async def test_a_saved_message_is_not_recorded_as_a_refusal(schema: Path) -> None:
    """The negative case, so the counter cannot pass by logging everything."""
    from hooks.external import auto_save_text

    text = (
        "Запомни важное: я переделал архитектуру памяти?\n"
        "Это поэтапный план, который надо надо сделать!\n"
        "Сначала — прототип, потом надо починить баг и выпустить релиз.\n"
    )
    result = await auto_save_text(_FakeMem(), _FakeGraph(), user_id="u1", text=text, event="new_message")

    assert result["saved_l3"] is True
    assert _rejections() == []


async def test_duplicate_refusal_is_recorded_too(schema: Path) -> None:
    """Every refusal, not only the interesting ones.

    A duplicate is expected — a replay produces hundreds — but it is the reason
    a replayed message does not become a second row, and an operator comparing
    two bases needs to see it rather than infer it from a missing line.
    """
    from shared.l0 import _content_hash

    text = "Обычная фраза достаточно длинная чтобы пройти порог"
    conn = sqlite3.connect(connection_manager.base_dir / "memory.db")
    conn.execute(
        "INSERT INTO l0_journal (content_hash) VALUES (?)",
        (_content_hash("user", "u1", text),),
    )
    conn.commit()
    conn.close()

    from hooks.external import auto_save_text

    result = await auto_save_text(_FakeMem(), _FakeGraph(), user_id="u1", text=text, event="new_message")

    assert result["skipped"] == "duplicate_l0_block"
    assert _rejections()[0][0] == "duplicate_l0_block"


async def test_middleware_block_is_recorded_with_a_stable_reason(schema: Path, monkeypatch) -> None:
    """The pipeline blocks before the handler, so it needs its own recording.

    This is where the fifth bug lived: 2608 rate-limit blocks, 2590 inside one
    replay, each a message deleted from a source that cannot be re-read from the
    middle. The daemon now reads that verdict by value; this makes it countable.
    """
    from shared.middleware import default_pipeline, RateLimitMiddleware
    from hooks.external import dispatch_event

    # Pin the SHARED limiter instead of monkeypatching config. `_limit()` resolves
    # the ceiling once and caches it, so a config patch is silently ignored once
    # any earlier test has driven this singleton — which is why this test passed
    # alone and failed in the full suite. Pinning the value the pipeline will
    # actually use is the only way to make the assertion about the ceiling real.
    limiter = next(m for m in default_pipeline._middlewares if isinstance(m, RateLimitMiddleware))
    monkeypatch.setattr(limiter, "_max", 1)
    monkeypatch.setattr(limiter, "_requests", {})

    # Distinct text: DedupMiddleware must not be the one that blocks, or the
    # test would pass for the wrong reason. Distinct user: another test's
    # timestamps must not count against this ceiling.
    await dispatch_event("new_message", "user", "reject-test", {"text": "первое событие", "source_msg_id": 1}, None, None)
    second = await dispatch_event("new_message", "user", "reject-test", {"text": "второе событие", "source_msg_id": 2}, None, None)

    assert second["skipped"] is True
    rows = _rejections()
    # Exactly one row: the first event passed the pipeline, the second did not.
    # If the limiter had blocked both, this would be two — so the count is what
    # proves which call the row belongs to.
    assert len(rows) == 1, rows
    reason, layer, user_id, source_msg_id, preview = rows[0]
    assert reason == "rate_limit", f"middleware reason not normalized: {reason!r}"
    assert layer == "user"
    assert user_id == "reject-test"
    assert source_msg_id == 2
    assert preview == "второе событие"


async def test_missing_table_does_not_break_the_save(fresh_dir: Path) -> None:
    """Best-effort by contract: observability must not become a new failure.

    A pre-migration database has no rejections table, and a save must still work
    — the alternative would be a guard that causes the very silent loss it was
    added to expose.
    """
    from hooks.external import auto_save_text

    text = (
        "Запомни важное: я переделал архитектуру памяти?\n"
        "Это поэтапный план, который надо надо сделать!\n"
        "Сначала — прототип, потом надо починить баг и выпустить релиз.\n"
    )
    mem = _FakeMem()
    result = await auto_save_text(mem, _FakeGraph(), user_id="u1", text=text, event="new_message")
    assert result["saved_l3"] is True

    # and a refusal on the same schema-less base must not raise either
    refused = await auto_save_text(_FakeMem(), _FakeGraph(), user_id="u1", text='[{"type":"text"}]')
    assert refused["skipped"] == "transcript"


async def test_summary_groups_by_reason(schema: Path) -> None:
    from hooks.external import auto_save_text
    from shared.door_log import rejection_summary

    for dump in ('[{"a":1}]', '{"b":2}', "[ariel recall]\n- x"):
        await auto_save_text(_FakeMem(), _FakeGraph(), user_id="u1", text=dump, event="new_message")

    summary = await rejection_summary()
    assert summary["available"] is True
    assert summary["total"] == 3
    assert summary["by_reason"] == {"transcript": 3}
    assert summary["by_layer"] == {"user": 3}


async def test_recent_rejections_carry_the_preview(schema: Path) -> None:
    """The preview is the part that says whether the guard was right.

    A count says something changed; 591 previews of the persona's own italic prose
    say the guard is wrong.
    """
    from hooks.external import auto_save_text
    from shared.door_log import recent_rejections

    await auto_save_text(
        _FakeMem(),
        _FakeGraph(),
        user_id="u1",
        text="[ariel recall]\n- [session] ring",
        event="new_message",
        source_msg_id=99,
    )

    recent = await recent_rejections(limit=5)
    assert len(recent) == 1
    assert recent[0]["reason"] == "transcript"
    assert recent[0]["source_msg_id"] == 99
    assert "ariel recall" in recent[0]["preview"]


def test_unknown_block_reason_is_kept_as_itself() -> None:
    """A new block site must appear as itself, not vanish into a catch-all.

    Folding unknown reasons into "unknown" would reproduce the original bug in a
    smaller way: the refusal happens, and the operator cannot tell what refused.
    """
    assert normalize_reason("Rate limit exceeded (100/min)") == "rate_limit"
    assert normalize_reason("below_importance_threshold(0.35, threshold=0.50)") == "below_importance_threshold"
    assert normalize_reason("user_id is required") == "invalid_input"
    assert normalize_reason("some_future_guard(x)") == "some_future_guard(x)"
    assert normalize_reason("") == "unknown"
