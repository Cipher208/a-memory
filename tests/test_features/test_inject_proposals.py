"""build_inject_blocks surfaces pending proposals (C1.11 S5)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest


class _L1:
    def get_recent(self, n: int = 10) -> list:
        return []


class _L3:
    async def search_by_tag(self, user_id: str, tag: str, limit: int = 10) -> list:
        return []


class _L4:
    async def get_all(self, user_id: str, limit: int = 50) -> list:
        return []


class _FakeMem:
    def __init__(self) -> None:
        self.l1 = _L1()
        self.l3 = _L3()
        self.l4 = _L4()


class _FakeRag:
    async def search(self, query: str, user_id: str = "default", limit: int = 5, **kw) -> list:
        return []


class _FakeProposal(dict):
    """Matches the real list_pending() row shape (plain dict)."""

    def __init__(self, pid: int, kind: str, payload: dict, proposed_at: float) -> None:
        super().__init__(id=pid, kind=kind, payload=payload, proposed_at=proposed_at)


async def test_inject_includes_proposals_block(monkeypatch: pytest.MonkeyPatch) -> None:
    from features import inject as inject_mod

    async def _fake_list(user_id: str = "default", limit: int = 20) -> list:
        return [
            _FakeProposal(12, "core_write", {"key": "auto_save", "value": "важный факт", "importance": 0.9}, time.time() - 60),
        ]

    monkeypatch.setattr(inject_mod, "_pending_proposals", _fake_list)
    blocks = await inject_mod.build_inject_blocks(_FakeMem(), _FakeRag(), user_id="u1", text="", budget=2000)
    kinds = [b["kind"] for b in blocks]
    assert "proposals" in kinds
    block = next(b for b in blocks if b["kind"] == "proposals")
    assert "#12" in block["content"]
    assert "core_write" in block["content"]
    assert "memory_proposals" in block["content"]


async def test_inject_no_proposals_block_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    from features import inject as inject_mod

    async def _empty(user_id: str = "default", limit: int = 20) -> list:
        return []

    monkeypatch.setattr(inject_mod, "_pending_proposals", _empty)
    blocks = await inject_mod.build_inject_blocks(_FakeMem(), _FakeRag(), user_id="u1", text="", budget=2000)
    assert "proposals" not in [b["kind"] for b in blocks]
