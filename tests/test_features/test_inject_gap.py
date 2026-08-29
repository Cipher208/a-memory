"""build_inject_blocks surfaces diff_gap episodes as a 'gap' block (C1.10 S4)."""

from __future__ import annotations

import time


class _GapEntry:
    def __init__(self, role: str, content: str, ts: float) -> None:
        self.role = role
        self.content = content
        self.timestamp = ts


class _GapL1:
    def get_recent(self, n: int = 10) -> list:
        return []


class _GapL3:
    def __init__(self) -> None:
        self.entries: list = [
            _GapEntry("system", "diff_gap: msg=42 score=0.85 missing=l4", time.time() - 60),
            _GapEntry("user", "normal message", time.time() - 30),
        ]

    def get_recent(self, n: int = 20) -> list:
        return self.entries


class _FakeL4:
    async def get_all(self, user_id: str, limit: int = 50) -> list:
        return []


class _FakeMem:
    def __init__(self) -> None:
        self.l1 = _GapL1()
        self.l3 = _GapL3()
        self.l4 = _FakeL4()


class _FakeRag:
    async def search(self, query: str, user_id: str = "default", limit: int = 5, **kw) -> list:
        return []


async def test_inject_returns_gap_block_when_recent_diff_gap_exists() -> None:
    from features.inject import build_inject_blocks

    blocks = await build_inject_blocks(_FakeMem(), _FakeRag(), user_id="u1", text="", budget=2000)
    kinds = [b["kind"] for b in blocks]
    assert "gap" in kinds
    gap_block = next(b for b in blocks if b["kind"] == "gap")
    assert "msg=42" in gap_block["content"]
    assert "missing=l4" in gap_block["content"]


async def test_inject_no_gap_block_when_no_diff_gap_episodes() -> None:
    class _NoGapL3:
        def get_recent(self, n: int = 20) -> list:
            return []

    mem = _FakeMem()
    mem.l3 = _NoGapL3()
    from features.inject import build_inject_blocks

    blocks = await build_inject_blocks(mem, _FakeRag(), user_id="u1", text="", budget=2000)
    kinds = [b["kind"] for b in blocks]
    assert "gap" not in kinds
