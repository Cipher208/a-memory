"""F96 (round 2): the claim lives at the DOOR, where the text is already masked.

One source message is captured once. Whichever path wins — the plugin push or
the daemon tail — the losing path must not write a refusal, and the winning
journal row must carry the `source_msg_id` it came from. The plugin side lives
in another repository and cannot be changed; it also cannot name the source row,
which is why the row it wins is born unattributed.

Round 1 put the claim in the daemon and matched on the RAW text. Two defects:
the journal's `content_hash` is over the MASKED text (F1 sanitizes before
capture), so any message carrying a credential missed the lookup; and a content
match cannot tell one source row from another. The claim therefore lives here,
on the rid `find_block` already resolved, with the timestamp the capture used.

Every test drives `auto_save_text` — the real door — not `find_block` with a
prepared rid. A helper fed the answer would stay green if the door stopped
resolving it.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

import pytest

from shared.connection import connection_manager
from shared.migrations import MigrationManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

TEXT = "решение по архитектуре: мост остаётся синхронным и не тянет лишние зависимости"
TS = 1_700_000_000.0
GH_TOKEN = "ghp_" + "Z" * 36


@pytest.fixture()
async def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """A private memory base; never the live one (role F6)."""
    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


@pytest.fixture()
def door() -> Iterator[None]:
    """Register the production `new_message` handler on the global registry.

    The tests below drive `dispatch_event` — the real door the plugin push and
    the daemon tail both enter — so the registry has to hold the real
    `UserHooks`. The registry is a module-level singleton and its handlers
    outlive a test, so the previous table is restored afterwards.
    """
    from hooks.registry import hook_registry
    from hooks.user_hooks import UserHooks

    saved = {name: list(handlers) for name, handlers in hook_registry._hooks.items()}
    hook_registry.register_instance(UserHooks("door"))
    try:
        yield
    finally:
        hook_registry._hooks = saved


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


def _query(sql: str) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(connection_manager.base_dir / "memory.db")
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _journal() -> list[tuple[Any, ...]]:
    return _query("SELECT id, text, source_msg_id, ts FROM l0_journal ORDER BY id")


def _rejections() -> list[tuple[Any, ...]]:
    return _query("SELECT reason, source_msg_id, text_preview FROM memory_dispatch_rejections ORDER BY id")


async def test_the_same_source_message_twice_is_claimed_not_refused(db: Any) -> None:
    """The plugin push won without a source id; the daemon tail must claim it.

    Same text, same ts, one source row: the second delivery is the SAME message,
    so it is not a duplicate to be refused — it is the missing provenance of the
    row already written. Exactly one row, no refusal, and the row now names its
    source.
    """
    from hooks.external import auto_save_text

    await auto_save_text(_FakeMem(), _FakeGraph(), user_id="f96-same", text=TEXT, event="new_message", ts=TS)
    result = await auto_save_text(_FakeMem(), _FakeGraph(), user_id="f96-same", text=TEXT, event="new_message", source_msg_id=5, ts=TS)

    rows = _journal()
    assert len(rows) == 1, f"a second delivery of one source message wrote a second row: {rows}"
    assert rows[0][2] == 5, f"the winning row did not receive its source id: {rows}"
    assert _rejections() == [], "the losing path wrote a refusal for a message it did not lose"
    assert result["skipped"] == "duplicate_l0_block", "the claimed duplicate must keep the skip shape"


async def test_a_masked_message_is_claimed_by_its_masked_hash(db: Any) -> None:
    """The one that kills the raw-text lookup.

    `ghp_…` never reaches the journal — `sanitize` runs before `capture`, so the
    stored hash is over the MASKED text. A lookup on the raw text finds nothing
    and the message falls through to `duplicate_l0_block`; the claim resolves the
    rid the door already found, so masking is irrelevant to it.
    """
    from hooks.external import auto_save_text

    text = f"мой старый ключ {GH_TOKEN} больше не работает, надо перевыпустить токен сегодня"
    await auto_save_text(_FakeMem(), _FakeGraph(), user_id="f96-masked", text=text, event="new_message", ts=TS)

    stored = _journal()
    assert len(stored) == 1, stored
    assert GH_TOKEN not in str(stored[0][1]), f"credential reached l0_journal: {stored[0][1]!r}"

    result = await auto_save_text(_FakeMem(), _FakeGraph(), user_id="f96-masked", text=text, event="new_message", source_msg_id=7, ts=TS)

    rows = _journal()
    assert len(rows) == 1, f"a masked duplicate wrote a second row: {rows}"
    assert rows[0][2] == 7, f"the masked row was never claimed: {rows}"
    assert _rejections() == [], "a masked message was refused instead of claimed"
    assert result["skipped"] == "duplicate_l0_block"


async def test_the_same_text_an_hour_later_is_still_a_duplicate(db: Any) -> None:
    """Content equality is not identity: a different message keeps its refusal.

    Same text one hour apart is another source row with the same words. The
    timestamp window is what tells the two apart, so the claim must fail and the
    first row must keep the provenance it has.
    """
    from hooks.external import auto_save_text

    await auto_save_text(_FakeMem(), _FakeGraph(), user_id="f96-later", text=TEXT, event="new_message", ts=TS)
    result = await auto_save_text(_FakeMem(), _FakeGraph(), user_id="f96-later", text=TEXT, event="new_message", source_msg_id=8, ts=TS + 3600)

    assert result["skipped"] == "duplicate_l0_block", result
    assert len(_rejections()) == 1, "a different message with the same text must still be refused, and recorded"
    rows = _journal()
    assert len(rows) == 1, rows
    assert rows[0][2] is None, f"a different message stole the first row's provenance: {rows}"


async def test_an_unstamped_row_is_not_claimed_by_a_later_message_with_the_same_text(db: Any) -> None:
    """The id guard alone cannot tell one source row from another.

    The plugin push captures the row and cannot name its source, so the row is
    born unstamped. An hour later a DIFFERENT source message carries the same
    words: `source_msg_id IS NULL` opens the id guard, and only the capture
    timestamp tells the two apart. Without `ABS(ts - ?) <= ?` the later message
    walks away with another message's provenance.
    """
    from hooks.external import auto_save_text

    await auto_save_text(_FakeMem(), _FakeGraph(), user_id="f96-unstamped-later", text=TEXT, event="new_message", ts=TS)
    result = await auto_save_text(
        _FakeMem(), _FakeGraph(), user_id="f96-unstamped-later", text=TEXT, event="new_message", source_msg_id=6, ts=TS + 3600
    )

    rows = _journal()
    assert len(rows) == 1, f"a later message with the same text wrote a second row: {rows}"
    assert rows[0][2] is None, f"a later message claimed an unstamped row: {rows}"
    assert [r[0] for r in _rejections()] == ["duplicate_l0_block"], f"the refusal path was not taken exactly once: {_rejections()}"
    assert result["skipped"] == "duplicate_l0_block", result


async def test_an_attributed_row_is_never_restamped(db: Any) -> None:
    """A row that already names its source keeps that name.

    The claim's WHERE clause is the guard: `source_msg_id IS NULL`. An
    attributed row belongs to the message that won it, and a later delivery must
    not overwrite it. Same text, same ts, a different source id: F96b's accepted
    consequence is that this genuine repeat is still refused (not stored) but no
    longer recorded — the row keeps 99 and nothing is written to the log.
    """
    from hooks.external import auto_save_text

    await auto_save_text(_FakeMem(), _FakeGraph(), user_id="f96-attributed", text=TEXT, event="new_message", source_msg_id=99, ts=TS)
    result = await auto_save_text(_FakeMem(), _FakeGraph(), user_id="f96-attributed", text=TEXT, event="new_message", source_msg_id=100, ts=TS)

    assert result["skipped"] == "duplicate_l0_block", result
    assert _rejections() == [], "a same-window repeat is refused silently (F96b accepted consequence)"
    rows = _journal()
    assert len(rows) == 1, rows
    assert rows[0][2] == 99, f"the existing provenance was overwritten: {rows}"


async def test_a_third_delivery_of_the_same_source_message_is_silent(db: Any) -> None:
    """A repeat delivery is a loser too: it writes no refusal and keeps the claim.

    The plugin push captures the row without a source id; the daemon tail then
    claims it for source 5. A THIRD delivery of that same source message finds
    the row already stamped — the old `source_msg_id IS NULL` guard read that as
    "someone else owns it" and wrote a refusal for a message already accounted
    for. A row may accept its own id again.
    """
    from hooks.external import auto_save_text

    await auto_save_text(_FakeMem(), _FakeGraph(), user_id="f96-third", text=TEXT, event="new_message", ts=TS)
    await auto_save_text(_FakeMem(), _FakeGraph(), user_id="f96-third", text=TEXT, event="new_message", source_msg_id=5, ts=TS)
    result = await auto_save_text(_FakeMem(), _FakeGraph(), user_id="f96-third", text=TEXT, event="new_message", source_msg_id=5, ts=TS)

    rows = _journal()
    assert len(rows) == 1, f"a repeat delivery wrote a second row: {rows}"
    assert rows[0][2] == 5, f"the row lost its source id: {rows}"
    assert _rejections() == [], "a repeat delivery wrote a refusal for a message already accounted for"
    assert result["skipped"] == "duplicate_l0_block", result


async def test_the_plugin_lag_does_not_defeat_the_claim(db: Any) -> None:
    """The live regression: the plugin captures LATER than the message was written.

    Measured on 204 live plugin-captured rows: `row.ts - message.timestamp` is
    never negative and reaches 615.8s (p50 4.7s, p90 13.7s, p99 116.8s). The
    symmetric 1s window therefore matched none of them: the daemon tail arrived
    15s after the plugin's capture and `_refuse("duplicate_l0_block")` wrote a
    rejection for a message it had not lost. The window must be causal — a
    capture can only follow its message.
    """
    from hooks.external import auto_save_text

    await auto_save_text(_FakeMem(), _FakeGraph(), user_id="f96-lag", text=TEXT, event="new_message", ts=TS)
    result = await auto_save_text(_FakeMem(), _FakeGraph(), user_id="f96-lag", text=TEXT, event="new_message", source_msg_id=5, ts=TS - 15)

    rows = _journal()
    assert len(rows) == 1, f"a lagging delivery wrote a second row: {rows}"
    assert rows[0][2] == 5, f"the plugin-captured row was never claimed: {rows}"
    assert _rejections() == [], "a message the plugin captured 15s late was refused as a duplicate"
    assert result["skipped"] == "duplicate_l0_block", result


async def test_a_message_older_than_the_capture_lag_is_still_a_duplicate(db: Any) -> None:
    """Beyond the measured lag the causal window separates two messages again.

    Same text 3600s after the capture is a different source message. The claim
    must fail, the refusal must be recorded, and the first row must keep the
    provenance it has.
    """
    from hooks.external import auto_save_text

    await auto_save_text(_FakeMem(), _FakeGraph(), user_id="f96-lag-over", text=TEXT, event="new_message", ts=TS)
    result = await auto_save_text(_FakeMem(), _FakeGraph(), user_id="f96-lag-over", text=TEXT, event="new_message", source_msg_id=6, ts=TS + 3600)

    assert result["skipped"] == "duplicate_l0_block", result
    assert len(_rejections()) == 1, "a message beyond the capture lag must still be refused, and recorded"
    rows = _journal()
    assert len(rows) == 1, rows
    assert rows[0][2] is None, f"a later message stole the row's provenance: {rows}"


async def test_a_capture_never_precedes_its_message(db: Any) -> None:
    """The window has a direction: a capture cannot come before the message.

    A message written 5s AFTER the capture is impossible in reality (the plugin
    only sees messages that already exist), so it is not the same message and
    must not be claimed. This pins the causal half of the window — without it a
    wide symmetric window would swallow the case. F96b's symmetric proximity
    check only decides whether the refusal is LOGGED, so this case is refused
    silently while the row keeps its unattributed state.
    """
    from hooks.external import auto_save_text

    await auto_save_text(_FakeMem(), _FakeGraph(), user_id="f96-precedes", text=TEXT, event="new_message", ts=TS)
    result = await auto_save_text(_FakeMem(), _FakeGraph(), user_id="f96-precedes", text=TEXT, event="new_message", source_msg_id=7, ts=TS + 5)

    assert result["skipped"] == "duplicate_l0_block", result
    assert _rejections() == [], "a same-window refusal is silent (F96b accepted consequence)"
    rows = _journal()
    assert len(rows) == 1, rows
    assert rows[0][2] is None, f"an impossible message claimed the row: {rows}"


async def test_a_different_source_id_is_never_stamped_over_the_owner(db: Any) -> None:
    """The claim accepts its own id, not any id.

    A row claimed by source A is offered again with source B and the same ts:
    the widened predicate must still refuse and the row must keep A. The refusal
    itself is no longer recorded — F96b treats a same-window repeat as the
    message already stored, and only the log line is gone.
    """
    from hooks.external import auto_save_text

    await auto_save_text(_FakeMem(), _FakeGraph(), user_id="f96-other", text=TEXT, event="new_message", ts=TS)
    await auto_save_text(_FakeMem(), _FakeGraph(), user_id="f96-other", text=TEXT, event="new_message", source_msg_id=11, ts=TS)
    result = await auto_save_text(_FakeMem(), _FakeGraph(), user_id="f96-other", text=TEXT, event="new_message", source_msg_id=12, ts=TS)

    assert result["skipped"] == "duplicate_l0_block", result
    assert _rejections() == [], "a same-window repeat is refused silently (F96b accepted consequence)"
    rows = _journal()
    assert len(rows) == 1, rows
    assert rows[0][2] == 11, f"a different source id stole the row's provenance: {rows}"


async def _deliver(user_id: str, text: str, **payload: Any) -> dict[str, Any]:
    """Deliver one message through the REAL door (`dispatch_event` + `UserHooks`)."""
    from hooks.external import dispatch_event

    return await dispatch_event("new_message", "user", user_id, {"text": text, **payload}, _FakeMem(), _FakeGraph())


async def test_the_plugin_losing_without_an_id_writes_no_rejection(db: Any, door: None) -> None:
    """The live case: the loser cannot name the source row, only its timestamp.

    Journal row 5095 was captured at 17:59:43 by the daemon and carries
    `sid=159292`; at 18:00:07 the plugin pushed the same text WITHOUT an id.
    `find_block` resolved row 5095, the claim had nothing to claim it with, and
    the door wrote `duplicate_l0_block` with `source_msg_id IS NULL` — pure log
    noise, ~10/hour, while the message was already stored.

    The losing path must be silent whichever side it is. Timestamp proximity is
    the only evidence left when the loser cannot name the row, so the door reads
    it: the capture is recent, therefore this is the message already stored.
    """
    first = await _deliver("f96-r6-plugin-loses", TEXT, source_msg_id=159292, ts=TS)
    assert first["results"][0]["auto_save"].get("skipped") is None, first

    result = await _deliver("f96-r6-plugin-loses", TEXT, ts=TS)

    rows = _journal()
    assert len(rows) == 1, f"the losing delivery wrote a second row: {rows}"
    assert rows[0][2] == 159292, f"the winning row lost its source id: {rows}"
    assert _rejections() == [], "the plugin losing without an id wrote a rejection row"
    assert result["results"][0]["auto_save"]["skipped"] == "duplicate_l0_block", result


async def test_a_repeat_far_outside_the_window_is_still_logged(db: Any, door: None) -> None:
    """A genuine repeat keeps its refusal: proximity is what makes it the same message.

    Same text an hour after the capture is another source message with the same
    words. It is refused exactly as before (`find_block` has no time window), and
    because it is outside the capture window the refusal is still recorded.
    """
    await _deliver("f96-r6-far-out", TEXT, source_msg_id=159292, ts=TS)
    result = await _deliver("f96-r6-far-out", TEXT, ts=TS + 3600)

    assert result["results"][0]["auto_save"]["skipped"] == "duplicate_l0_block", result
    rejections = _rejections()
    assert len(rejections) == 1, f"a repeat outside the window must still be recorded: {rejections}"
    rows = _journal()
    assert len(rows) == 1, rows
    assert rows[0][2] == 159292, f"a later message stole the row's provenance: {rows}"


async def test_a_loser_without_a_timestamp_still_refuses(db: Any, door: None) -> None:
    """No id and no timestamp: the two deliveries cannot be told apart.

    Timestamp proximity is the only evidence the losing path has. Without it the
    door must refuse loudly rather than assume, so the refusal stays recorded.
    """
    await _deliver("f96-r6-no-ts", TEXT, source_msg_id=159292, ts=TS)
    result = await _deliver("f96-r6-no-ts", TEXT)

    assert result["results"][0]["auto_save"]["skipped"] == "duplicate_l0_block", result
    rejections = _rejections()
    assert len(rejections) == 1, f"a loser with no timestamp must still be recorded: {rejections}"
    rows = _journal()
    assert len(rows) == 1, rows
    assert rows[0][2] == 159292, f"an unidentifiable delivery stole the row's provenance: {rows}"
