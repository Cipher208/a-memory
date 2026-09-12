"""L4 pollution guard: conversational-register clauses must not become durable facts.

Regression (2026-09-12, Ф1 follow-up): the auto-extractor keyed Lily's
greeting ("наконец явилась, сталь ждёт") and owner-addressed remarks as L4
rows with first-4-word slug keys — 61 garbage fact: rows in one base alone.
Guards proven here:

- dialogic clauses (vocatives, greetings, questions) never reach L4;
- unkeyable clauses (canonical key degrades to kind:misc) never reach L4;
- real technical invariants still save (no regression).
"""

from __future__ import annotations

import pytest


def _mem(cm):
    class _L3:
        saved: list[str] = []

        async def save(self, uid, text, score, tags):
            self.saved.append(text)

    class _Mem:
        def __init__(self, cm):
            self._cm = cm
            self.l3 = _L3()

    return _Mem(cm)


# ── pure guard unit tests ──


def test_dialogic_vocatives_and_questions():
    from lifecycle.distiller import _is_dialogic

    assert _is_dialogic("Моё видение, госпожа — коротко и по-инженерному")
    assert _is_dialogic("Привет, мамочка!")
    assert _is_dialogic("Наконец явилась, сталь ждёт")
    assert _is_dialogic("Какой порт у постгреса?")
    assert _is_dialogic("Доброе утро, всё работает?")
    assert _is_dialogic("Спасибо за отчёт")
    # conjunctions and tech clauses must NOT trip the guard
    assert not _is_dialogic("Пока не проверим — не деплоим")
    assert not _is_dialogic("Мы решили перейти на PostgreSQL 16 для продакшена")
    assert not _is_dialogic("бэкенд слушает порт 8642 на localhost")


# ── integration: the guard sits in distill_and_route's L4 branch ──


@pytest.mark.asyncio
async def test_greeting_never_reaches_l4(tmp_path):
    """L4-bound dialogic clause (decision-kind + vocative) hits the guard.

    Plain greetings already die at kind-routing (FACT decay 0.010 < the
    distiller's l4 floor 0.005); this clause routes to l4, so only the
    guard can stop it.
    """
    from shared.connection import AsyncConnectionManager
    from shared.migrations import MigrationManager
    from lifecycle.distiller import distill_and_route

    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    await MigrationManager(cm=cm).migrate()
    mem = _mem(cm)

    result = await distill_and_route(mem, None, "u1", "Госпожа, мы решили откатить миграцию базы", 0.8)
    assert result["l4_saved"] == 0, f"owner-addressed decision leaked to L4: {result}"
    assert result.get("guard_skipped", 0) >= 1


@pytest.mark.asyncio
async def test_owner_addressed_remark_never_reaches_l4(tmp_path):
    from shared.connection import AsyncConnectionManager
    from shared.migrations import MigrationManager
    from lifecycle.distiller import distill_and_route

    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    await MigrationManager(cm=cm).migrate()
    mem = _mem(cm)

    result = await distill_and_route(mem, None, "u1", "Моё видение, госпожа — коротко и по-инженерному", 0.8)
    assert result["l4_saved"] == 0, f"owner-addressed chat leaked to L4: {result}"


@pytest.mark.asyncio
async def test_misc_key_never_reaches_l4(tmp_path):
    """Only ≤2-char words → canonical key degrades to fact:misc → not a fact."""
    from shared.connection import AsyncConnectionManager
    from shared.migrations import MigrationManager
    from lifecycle.distiller import distill_and_route

    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    await MigrationManager(cm=cm).migrate()
    mem = _mem(cm)

    result = await distill_and_route(mem, None, "u1", "а я не он", 0.8)
    assert result["l4_saved"] == 0, f"misc-keyed atom leaked to L4: {result}"


@pytest.mark.asyncio
async def test_tech_invariant_still_saves(tmp_path):
    from shared.connection import AsyncConnectionManager
    from shared.migrations import MigrationManager
    from lifecycle.distiller import distill_and_route

    cm = AsyncConnectionManager(base_dir=str(tmp_path))
    await MigrationManager(cm=cm).migrate()
    mem = _mem(cm)

    result = await distill_and_route(mem, None, "u1", "Мы решили перейти на PostgreSQL 16 для продакшена", 0.8)
    assert result["l4_saved"] >= 1, f"real invariant blocked by guard: {result}"
