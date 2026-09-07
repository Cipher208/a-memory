"""Stage 2-C: wake_up tool — registration, annotations, budget split."""

from __future__ import annotations

from typing import Any

import pytest

import mcp_server.tools_layer  # noqa: F401
from mcp_server.registry import get_all_tools
from mcp_server.server import PRIMITIVE_TOOLS
from mcp_server.annotations import hints_for


class _FakeMem:
    async def list_recent_sessions(self, user_id: str, limit: int = 1) -> list[dict[str, Any]]:
        return []


class _FakeRag:
    def search(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        return []


@pytest.mark.asyncio
async def test_wake_up_registered_and_is_primitive() -> None:
    assert "wake_up" in get_all_tools()
    assert "wake_up" in PRIMITIVE_TOOLS


def test_wake_up_annotations_read_only() -> None:
    hints = hints_for("wake_up")
    assert hints.read_only is True
    assert hints.destructive is False


@pytest.mark.asyncio
async def test_wake_up_budget_split(monkeypatch: pytest.MonkeyPatch) -> None:
    """recap side takes at most half the budget; inject gets the rest."""
    calls: list[int] = []

    async def fake_recap(mem: Any, user_id: str, budget: int) -> list[dict[str, Any]]:
        calls.append(budget)
        return [{"axis": "recap_session", "content": "x" * 100, "score": 1.0}]

    async def fake_inject(mem: Any, rag: Any, user_id: str, text: str = "", budget: int = 2000) -> list[dict[str, Any]]:
        calls.append(budget)
        return [{"kind": "important", "content": "y" * 50, "score": 1.0}]

    monkeypatch.setattr("features.continuity.session_recap", fake_recap)
    monkeypatch.setattr("features.inject.build_inject_blocks", fake_inject)

    from features.wake_up import wake_up_blocks

    out = await wake_up_blocks(_FakeMem(), _FakeRag(), "default", budget=2000)
    assert calls[0] == 1000
    assert out["recap"] and out["inject"]
    assert out["used_tokens"] > 0


def test_render_wake_up_md_empty_and_filled() -> None:
    from features.wake_up import render_wake_up_md

    assert render_wake_up_md({}) == "—"
    md = render_wake_up_md(
        {
            "recap": [{"axis": "recap_session", "content": "hello"}],
            "inject": [{"kind": "pinned", "content": "world"}],
        }
    )
    assert "[recap_session] hello" in md
    assert "<cache:break>" in md
    assert "[pinned] world" in md
