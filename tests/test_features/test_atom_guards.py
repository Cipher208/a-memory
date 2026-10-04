"""The atom guards: splitter, length floor, live cap, and the counter each needed.

Every test here is written to fail if its guard is reverted. The splits and
counts are asserted on real text from the live base, not on invented sentences —
the fragment problem is only visible in the shape of the actual messages.
"""

from unittest.mock import MagicMock

import pytest


# --- the knife: conjunctions are not boundaries ---------------------------------


def test_conjunction_no_longer_splits() -> None:
    """The regression. `→ оси → и` used to become its own episode.

    Reverting `_CLAUSE_SPLIT` to the conjunction pattern splits this into two and
    fails the assertion — that is the whole point of the test.
    """
    from lifecycle.distiller import atomize

    atoms = atomize("Всё измеряло вход: её сообщение → события → оси → и всё это вместе связано в одну цепочку. Второе предложение стоит отдельно.")
    assert len(atoms) == 2, atoms
    assert "→ и всё это вместе связано" in atoms[0], atoms
    assert not any(a.strip() in {"и", "и всё это вместе"} for a in atoms), atoms


def test_sentence_boundaries_still_split() -> None:
    """Three real sentences, each long enough for the floor to keep it.

    The third one is deliberately not "Третье?" — a 7-char sentence is dropped by
    the length floor, which is correct and is covered by its own test above.
    """
    from lifecycle.distiller import atomize

    atoms = atomize("Первое предложение достаточно длинное. Второе предложение тоже длинное вполне! Третье предложение закрывает мысль.")
    assert len(atoms) == 3, atoms
    assert atoms[2].startswith("Третье предложение"), atoms


def test_newlines_split_markdown_blocks() -> None:
    """These messages are markdown: a heading or bullet is a separate thought."""
    from lifecycle.distiller import atomize

    atoms = atomize("**Итог:** всё зелено\nДиск в норме, память в норме")
    assert len(atoms) == 2, atoms


def test_conjunctions_inside_a_sentence_survive() -> None:
    """The reason the old splitter was wrong: и/но attach clauses, they do not end them."""
    from lifecycle.distiller import atomize

    text = "Она проверила диск и память, но не стала трогать swap"
    atoms = atomize(text)
    assert atoms == [text], atoms


# --- the length floor -----------------------------------------------------------


def test_short_atoms_are_dropped_and_counted() -> None:
    from lifecycle.distiller import atomize_counted

    atoms, counts = atomize_counted("Да")
    assert atoms == []
    assert counts["candidates"] == 1
    assert counts["too_short"] == 1

    long_one = "это достаточно длинное предложение для памяти"
    atoms, counts = atomize_counted(f"Да. {long_one}.")
    assert long_one in atoms[0], atoms
    assert counts["too_short"] == 1, counts
    assert counts["candidates"] == 2, counts


def test_real_fragments_from_the_live_base_are_dropped() -> None:
    """These are actual episode summaries: 'Данные полные', 'Прочитала', 'Свап полон'."""
    from lifecycle.distiller import atomize

    from lifecycle.distiller import _MIN_ATOM_CHARS

    for fragment in ["Данные полные", "Разобралась", "Прочитала", "Свап полон", "прочитать"]:
        assert len(fragment) < _MIN_ATOM_CHARS, fragment
        assert atomize(fragment) == [], fragment


def test_threshold_is_on_the_atom_not_the_message() -> None:
    """A short message is still distilled in full when it is one real thought."""
    from lifecycle.distiller import atomize

    atoms = atomize("Сервер биллинга переехал в новый датацентр")
    assert len(atoms) == 1


# --- the cap, and the counter it never had --------------------------------------


def test_cap_counts_what_it_refused() -> None:
    """The counter the 52% loss was invisible without.

    Reverting `atomize` to `[...][:10]` removes `over_cap` entirely; this asserts
    the number exists and is right.
    """
    from lifecycle.distiller import atomize_counted

    text = ". ".join(f"Это предложение номер {i} и оно достаточно длинное" for i in range(15))
    atoms, counts = atomize_counted(text)
    assert len(atoms) == 10, len(atoms)
    assert counts["over_cap"] == 5, counts
    assert counts["candidates"] == 15, counts


def test_cap_honours_an_explicit_limit() -> None:
    from lifecycle.distiller import atomize_counted

    text = ". ".join(f"Это предложение номер {i} и оно достаточно длинное" for i in range(15))
    atoms, counts = atomize_counted(text, limit=3)
    assert len(atoms) == 3, atoms
    assert counts["over_cap"] == 12, counts


def test_nothing_is_dropped_when_under_the_cap() -> None:
    from lifecycle.distiller import atomize_counted

    atoms, counts = atomize_counted("Первое длинное предложение здесь. Второе длинное предложение здесь.")
    assert len(atoms) == 2
    assert counts["over_cap"] == 0 and counts["too_short"] == 0


# --- the live key, and why it needs a floor -------------------------------------


def test_atom_limit_falls_back_to_the_default_never_to_zero(monkeypatch) -> None:
    """The trap this exists for: `get_limit` returns 0 for a key no file mentions.

    Every live agent mounts its OWN config through MCP_CONFIG_PATH, so a missing
    key must mean "10", not "keep nothing" — memory stopping silently is precisely
    the failure shape this repository keeps re-learning.
    """
    from lifecycle import distiller

    for absent in [{}, {"limits": {}}, {"limits": {"l3_atom_limit": 0}}, {"limits": {"l3_atom_limit": -5}}]:
        monkeypatch.setattr("config.config._data", absent, raising=False)
        assert distiller.atom_limit() == 10, absent


def test_atom_limit_reads_a_configured_value(monkeypatch) -> None:
    from lifecycle import distiller

    monkeypatch.setattr("config.config._data", {"limits": {"l3_atom_limit": 4}}, raising=False)
    assert distiller.atom_limit() == 4


def test_a_configured_cap_actually_bounds_the_atoms(monkeypatch) -> None:
    """The key must be wired end to end, not merely readable."""
    from lifecycle import distiller

    monkeypatch.setattr("config.config._data", {"limits": {"l3_atom_limit": 2}}, raising=False)
    text = ". ".join(f"Это предложение номер {i} и оно достаточно длинное" for i in range(6))
    atoms, counts = distiller.atomize_counted(text)
    assert len(atoms) == 2, atoms
    assert counts["over_cap"] == 4, counts


# --- the distiller refuses a tool dump ------------------------------------------


class FakeL3:
    def __init__(self) -> None:
        self.saved: list[tuple] = []

    async def save(self, user_id, summary, weight, tags, created_at=None):
        self.saved.append((user_id, summary, weight, tags))
        return len(self.saved)


class FakeMem:
    def __init__(self) -> None:
        self.l3 = FakeL3()
        self.layer_type = "user"


@pytest.mark.asyncio
async def test_machine_report_saves_nothing_and_is_logged() -> None:
    """Refused, counted, and visible — and it must not write a single episode.

    Reverting the early return in `distill_and_route` makes this fail on all three
    assertions: atoms reach L3, `machine_report` is False, and no rejection row.
    """
    from lifecycle.distiller import distill_and_route

    fake_mem = FakeMem()
    report = "📊 Self-Monitoring Report — 2026-09-07 08:01 MSK\n🟢 Общая оценка: система здорова\nДиск 47%, RAM 7.9/15.6 GiB, uptime 46 дней"
    stats = await distill_and_route(fake_mem, MagicMock(), "u1", report, 0.9)

    assert stats["machine_report"] is True, stats
    assert stats["refused"] == "machine_report", stats
    assert fake_mem.l3.saved == [], fake_mem.l3.saved
    assert stats["l3_saved"] == 0 and stats["l4_saved"] == 0, stats


@pytest.mark.asyncio
async def test_ordinary_text_still_routes_after_the_guard() -> None:
    """The guard is a door, not a wall: normal text must be unaffected."""
    from lifecycle.distiller import distill_and_route

    fake_mem = FakeMem()
    stats = await distill_and_route(fake_mem, MagicMock(), "u1", "наблюдение: трафик растёт по пятницам вечером", 0.6)
    assert stats["machine_report"] is False, stats
    assert stats["l3_saved"] >= 1, stats
    assert fake_mem.l3.saved, "ordinary event must still reach mem.l3.save"


@pytest.mark.asyncio
async def test_atom_counters_are_reported_on_a_normal_route() -> None:
    """A loud cap: the caller can see the loss without reading the log."""
    from lifecycle.distiller import distill_and_route

    fake_mem = FakeMem()
    text = ". ".join(f"наблюдение: событие номер {i} случилось вчера вечером" for i in range(13))
    stats = await distill_and_route(fake_mem, MagicMock(), "u1", text, 0.6)
    assert stats["atoms_kept"] == 10, stats
    assert stats["atoms_over_cap"] == 3, stats
    assert stats["atoms_kept"] + stats["atoms_over_cap"] + stats["atoms_too_short"] == 13, stats


# --- the watermark must say what actually happened -------------------------------


@pytest.fixture
async def live_base(tmp_path, monkeypatch):
    """A real migrated base: the l0 status only matters against the real schema."""
    from shared.connection import connection_manager
    from shared.migrations import MigrationManager

    monkeypatch.setattr(connection_manager, "base_dir", tmp_path)
    connection_manager._conns.clear()
    await MigrationManager(cm=connection_manager).migrate()
    yield connection_manager
    connection_manager._conns.clear()


async def _status_of(cm, source_msg_id: int) -> str:
    conn = await cm.get("memory.db")
    row = await (await conn.execute("SELECT status FROM l0_journal WHERE source_msg_id=?", (source_msg_id,))).fetchone()
    assert row is not None, "the message must have been captured"
    return row[0]


def _force_distill(monkeypatch):
    """Make `auto_save_text` reach the distiller regardless of the text's score.

    Both dependencies are imported INSIDE the function body
    (`from features.importance import evaluate_importance`,
    `from shared.adaptive import adaptive_threshold`), so patching a name on
    `hooks.external` silently does nothing — the local import re-binds the real
    object on every call. The first version of these tests did exactly that and
    passed for the wrong reason: the report scored high enough on its own, and the
    ordinary sentence did not, so it never reached the routing and the journal
    stayed `received`. Patch the modules the import actually reads.
    """
    import features.importance as importance
    from shared.adaptive import adaptive_threshold

    monkeypatch.setattr(importance, "evaluate_importance", lambda text: 0.95)
    monkeypatch.setattr(adaptive_threshold, "_current_value", 0.1, raising=False)


@pytest.mark.asyncio
async def test_refused_machine_report_is_not_stamped_saved(live_base, monkeypatch) -> None:
    """The journal used to claim `saved_l3` for a message that wrote nothing.

    `"promoted_l4" if l4_saved else "saved_l3"` has no branch for "neither", so a
    refused tool dump — and any message whose every atom fell below the length
    floor — was recorded as saved. Reverting that branch to the two-way form makes
    this assertion read `saved_l3` and fail.
    """
    import hooks.external as ext

    _force_distill(monkeypatch)

    report = (
        "📊 Self-Monitoring Report — 2026-09-07 08:01 MSK\n🟢 Общая оценка: система здорова\nДиск 47%, RAM 7.9/15.6 GiB, uptime 46 дней, load 0.20"
    )
    await ext.auto_save_text(FakeMem(), MagicMock(), user_id="u1", text=report, event="new_message", source_msg_id=9001)

    assert await _status_of(live_base, 9001) == "gated_out"


@pytest.mark.asyncio
async def test_a_message_that_saves_is_still_stamped_saved(live_base, monkeypatch) -> None:
    """The negative case: the honest branch must not mislabel a real save."""
    import hooks.external as ext

    _force_distill(monkeypatch)

    await ext.auto_save_text(
        FakeMem(),
        MagicMock(),
        user_id="u1",
        text="наблюдение: трафик растёт по пятницам вечером после релиза",
        event="new_message",
        source_msg_id=9002,
    )

    assert await _status_of(live_base, 9002) in {"saved_l3", "promoted_l4"}


@pytest.mark.asyncio
async def test_message_whose_atoms_all_fall_below_the_floor_is_not_stamped_saved(live_base, monkeypatch) -> None:
    """The other silent case the two-way branch mislabelled.

    Every atom here is short ("Да.", "Нет.", "Прочитала."), so the length floor
    keeps none and nothing is written — yet the old branch still stamped
    `saved_l3`. Now the journal says `gated_out`, which is what happened.
    """
    import hooks.external as ext

    _force_distill(monkeypatch)

    await ext.auto_save_text(
        FakeMem(),
        MagicMock(),
        user_id="u1",
        text="Да. Нет. Прочитала. Разобралась.",
        event="new_message",
        source_msg_id=9003,
    )

    assert await _status_of(live_base, 9003) == "gated_out"


def test_a_short_declarative_fact_survives_the_floor() -> None:
    """The regression a 20-char floor caused, caught by test_conflict_not_silent_update.

    "база проекта: MySQL" is 19 characters and a durable fact. At a 20-char floor
    it was dropped before the conflict resolver could see it, so a contradiction
    stopped being reported. The floor is a proxy; this is the case that pins it.
    """
    from lifecycle.distiller import atomize

    assert len("база проекта: MySQL") == 19
    assert atomize("база проекта: MySQL") == ["база проекта: MySQL"]
