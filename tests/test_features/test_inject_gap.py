"""build_inject_blocks surfaces diff_gap episodes as a 'gap' block (C1.10 S4)."""

from __future__ import annotations

import time


class _Episode:
    def __init__(self, summary: str, created_at: float) -> None:
        self.summary = summary
        self.created_at = created_at


class _GapL1:
    def get_recent(self, n: int = 10) -> list:
        return []


class _GapL3:
    """Mimics EpisodicMemory.search_by_tag (the real API)."""

    def __init__(self, episodes: list) -> None:
        self._episodes = episodes

    async def search_by_tag(self, user_id: str, tag: str, limit: int = 10) -> list:
        return self._episodes[:limit]


class _FakeL4:
    async def get_all(self, user_id: str, limit: int = 50) -> list:
        return []


class _FakeMem:
    def __init__(self, l3: _GapL3) -> None:
        self.l1 = _GapL1()
        self.l3 = l3
        self.l4 = _FakeL4()


class _FakeRag:
    async def search(self, query: str, user_id: str = "default", limit: int = 5, **kw) -> list:
        return []


async def test_inject_returns_gap_block_when_recent_diff_gap_exists() -> None:
    from features.inject import build_inject_blocks

    l3 = _GapL3(
        [
            _Episode("diff_gap: msg=42 score=0.85 missing=l4", time.time() - 60),
            _Episode("regular episode", time.time() - 30),
        ]
    )
    blocks = await build_inject_blocks(_FakeMem(l3), _FakeRag(), user_id="u1", text="", budget=2000)
    kinds = [b["kind"] for b in blocks]
    assert "gap" in kinds
    gap_block = next(b for b in blocks if b["kind"] == "gap")
    assert "msg=42" in gap_block["content"]
    assert "missing=l4" in gap_block["content"]
    assert "regular episode" not in gap_block["content"]


async def test_inject_no_gap_block_when_no_diff_gap_episodes() -> None:
    from features.inject import build_inject_blocks

    mem = _FakeMem(_GapL3([]))
    blocks = await build_inject_blocks(mem, _FakeRag(), user_id="u1", text="", budget=2000)
    kinds = [b["kind"] for b in blocks]
    assert "gap" not in kinds


async def test_inject_gap_block_ignores_old_episodes() -> None:
    from features.inject import build_inject_blocks

    old = time.time() - 48 * 3600  # older than the 24h cutoff
    l3 = _GapL3([_Episode("diff_gap: msg=1 score=0.9 missing=l4", old)])
    blocks = await build_inject_blocks(_FakeMem(l3), _FakeRag(), user_id="u1", text="", budget=2000)
    kinds = [b["kind"] for b in blocks]
    assert "gap" not in kinds
