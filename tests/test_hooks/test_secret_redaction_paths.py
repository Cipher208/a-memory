"""F1: no credential reaches persistent storage.

Three paths persist text and none of them masked credentials before the fix:
L0 capture wrote the raw message into `l0_journal`, the nightly replay distilled
raw historical rows, and the refusal log kept `text[:160]` of whatever the door
refused. These tests assert on the STORED ROW, not on what a function returns —
the return value was never the leak.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

import pytest

from shared.connection import connection_manager
from shared.migrations import MigrationManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

GH_TOKEN = "ghp_" + "Z" * 36


@pytest.fixture()
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


def _query(sql: str) -> list[tuple]:
    conn = sqlite3.connect(connection_manager.base_dir / "memory.db")
    rows = conn.execute(sql).fetchall()
    conn.close()
    return rows


class _FakeL3:
    def __init__(self) -> None:
        self.saved: list[str] = []

    async def save(self, user_id: str, summary: str, weight: float, tags: list[str], created_at: float | None = None) -> int:
        self.saved.append(summary)
        return len(self.saved)


class _FakeMem:
    def __init__(self) -> None:
        self.l3 = _FakeL3()

    async def remember(self, key: str, value: str, importance: float, **kwargs: Any) -> int:
        return 1


class _FakeGraph:
    async def add_node(self, *args: Any, **kwargs: Any) -> int:
        return 1


async def test_journal_row_stores_no_credential(db: Any) -> None:
    """The L0 journal is persistent storage: `capture()` must receive masked text."""
    from hooks.external import auto_save_text

    text = f"Мой старый ключ {GH_TOKEN} больше не работает, надо перевыпустить токен сегодня"
    await auto_save_text(_FakeMem(), _FakeGraph(), user_id="f1-journal", text=text, event="new_message", source_msg_id=7)

    rows = _query("SELECT text FROM l0_journal ORDER BY id")
    assert rows, "the message never reached the journal — this test would prove nothing"
    stored = "\n".join(str(r[0]) for r in rows)
    assert GH_TOKEN not in stored, f"credential reached l0_journal: {stored!r}"
    assert "⟨CREDENTIAL_1⟩" in stored


async def test_replay_sanitizes_pre_fix_rows(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rows written before the fix are raw; replay is the last line before memory.

    The spy pins R4 directly — the text handed to the distiller — and the storage
    scan proves the distilled episode/L4 value carry the placeholder, not the key.
    """
    import lifecycle.distiller as distiller_mod
    from features.replay import replay
    from shared.l0 import capture

    raw = f"Важно: я решил сменить ключ {GH_TOKEN} для проекта X, потому что старый утёк в лог"
    rid = await capture("new_message", "user", "f1-replay", raw)
    assert rid is not None
    conn = sqlite3.connect(connection_manager.base_dir / "memory.db")
    conn.execute("UPDATE l0_journal SET ts = ts - 1200 WHERE id = ?", (rid,))
    conn.commit()
    conn.close()

    seen: list[str] = []
    real = distiller_mod.distill_and_route

    async def _spy(mem: Any, graph: Any, user_id: str, text: str, score: float, **kwargs: Any) -> dict[str, Any]:
        seen.append(text)
        return await real(mem, graph, user_id, text, score, **kwargs)

    monkeypatch.setattr(distiller_mod, "distill_and_route", _spy)

    result = await replay(since_days=1, respect_gate=False)
    assert result["processed"] == 1
    assert seen, "the distiller was never called — the replay skipped the row"
    assert GH_TOKEN not in seen[0], f"raw credential reached the distiller: {seen[0]!r}"

    episodes = "\n".join(str(r[0]) for r in _query("SELECT summary FROM episodes"))
    core = "\n".join(str(r[0]) for r in _query("SELECT value FROM core_memory"))
    assert GH_TOKEN not in episodes, f"credential reached episodes: {episodes!r}"
    assert GH_TOKEN not in core, f"credential reached core_memory: {core!r}"


async def test_refusal_preview_stores_no_credential(db: Any) -> None:
    """The refusal log keeps a preview of text the door refused — masked too.

    The guards themselves must keep seeing the raw text (that is how a transcript
    dump is recognised), so only the stored preview is sanitized.
    """
    from hooks.external import auto_save_text

    dump = '[{"type": "text", "text": "мой токен ' + GH_TOKEN + '"}]'
    result = await auto_save_text(_FakeMem(), _FakeGraph(), user_id="f1-refuse", text=dump, event="new_message", source_msg_id=11)

    assert result["skipped"] == "transcript"
    rows = _query("SELECT text_preview FROM memory_dispatch_rejections")
    assert len(rows) == 1, rows
    preview = str(rows[0][0])
    assert GH_TOKEN not in preview, f"credential reached the refusal log: {preview!r}"


async def test_credential_message_keeps_its_admission_score(db: Any) -> None:
    """Masking must not change whether a message is admitted.

    `structure_score` gives +0.2 above 100 characters, and a credential is
    replaced by a SHORTER placeholder — so scoring the masked text demoted a
    message that had passed the gate: measured 118 chars → 0.4 versus 69 chars
    → 0.2, with the live EMA at 0.24. The message was then `gated_out` and never
    reached memory. The replay already scores the raw text for this reason; the
    door has to do the same, while everything that is STORED stays masked.
    """
    from features.importance import evaluate_importance
    from hooks.external import auto_save_text
    from mcp_server.utils.privacy import sanitize

    token = "sk-" + "a" * 60
    raw = f"Важно: я решил сменить ключ {token} для проекта X, старый утёк"
    masked, _m = sanitize(raw, use_ner=False)

    # The two halves of the regression, stated as facts about this fixture.
    assert len(raw) > 100 >= len(masked)
    assert evaluate_importance(masked) < evaluate_importance(raw)

    result = await auto_save_text(_FakeMem(), _FakeGraph(), user_id="f1-score", text=raw, event="new_message")

    assert result["score"] == evaluate_importance(raw), "admission was scored on the masked text"
    rows = _query("SELECT text FROM l0_journal ORDER BY id")
    assert len(rows) == 1, rows
    stored = str(rows[0][0])
    assert token not in stored, f"credential reached l0_journal: {stored!r}"
    assert "⟨CREDENTIAL_1⟩" in stored


async def test_two_messages_differing_only_by_secret_collapse(db: Any) -> None:
    """A key rotation is ONE journal row — because the DOOR masks before capture.

    This is a door-level test on purpose: it drives `auto_save_text` twice with
    two different credentials, not `capture` twice with two already-masked
    strings. The latter only proves that identical input dedups, which `capture`
    did long before this package, and it stays green if the door starts passing
    raw text down — the exact mutation this test exists to catch.

    What it pins: the masked texts are identical, so `find_block` sees the first
    message as a duplicate and the second is refused without writing anything.
    The journal keeps the fact that a key was rotated, once, not two copies —
    and never the token itself. Accepted consequence 1 of the F1 spec.
    """
    from hooks.external import auto_save_text

    first = "ключ ghp_" + "A" * 36 + " больше не работает, надо перевыпустить токен"
    second = "ключ ghp_" + "B" * 36 + " больше не работает, надо перевыпустить токен"

    await auto_save_text(_FakeMem(), _FakeGraph(), user_id="f1-dedup", text=first, event="new_message", source_msg_id=1)
    result = await auto_save_text(_FakeMem(), _FakeGraph(), user_id="f1-dedup", text=second, event="new_message", source_msg_id=2)

    assert result.get("skipped") == "duplicate_l0_block", f"the second rotation was not folded: {result}"
    rows = _query("SELECT text FROM l0_journal ORDER BY id")
    assert len(rows) == 1, f"a rotation must be one row, got {len(rows)}"
    stored = str(rows[0][0])
    assert "⟨CREDENTIAL_1⟩" in stored, stored
    assert "ghp_" + "A" * 36 not in stored, f"first credential reached l0_journal: {stored!r}"
    assert "ghp_" + "B" * 36 not in stored, f"second credential reached l0_journal: {stored!r}"
