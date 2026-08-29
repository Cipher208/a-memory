"""HTTP surfaces for external events + inject (spec S5)."""

from __future__ import annotations

from typing import Any

import pytest

from mcp_server.endpoints.hooks import HooksEndpoints


class _App:
    pass


@pytest.mark.asyncio
async def test_hooks_event_unknown_event_400(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_AUTH_DISABLED", "1")
    ep = HooksEndpoints(_App(), None)

    class _Req:
        headers: dict[str, str] = {}

        async def json(self) -> dict[str, Any]:
            return {"user_id": "u1", "payload": {}}

        @property
        def path_params(self) -> dict[str, str]:
            return {"event": "nope"}

    resp = await ep.hooks_event(_Req())  # type: ignore[arg-type]
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_context_inject_returns_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_AUTH_DISABLED", "1")
    import features.inject as inj

    async def _empty_get_all(user_id: str, limit: int = 50) -> list[Any]:
        return []

    class _FakeMem:
        l1 = type("L1", (), {"get_recent": staticmethod(lambda n=10: [])})()
        l4 = type("L4", (), {"get_all": staticmethod(_empty_get_all)})()

    class _FakeRag:
        async def search(self, query: str, user_id: str = "default", limit: int = 5, **kw: Any) -> list[dict[str, Any]]:
            return [{"content": "c", "score": 0.5}]

    orig_mem, orig_rag = inj._resolve_mem, inj._resolve_rag
    inj._resolve_mem = lambda app, layer, user_id: _FakeMem()  # type: ignore[assignment]
    inj._resolve_rag = lambda app, layer: _FakeRag()  # type: ignore[assignment]
    try:
        ep = HooksEndpoints(_App(), None)

        class _Req:
            headers: dict[str, str] = {}

            async def json(self) -> dict[str, Any]:
                return {"user_id": "u1", "layer": "user", "text": "q", "budget": 500}

        resp = await ep.context_inject(_Req())  # type: ignore[arg-type]
        assert resp.status_code == 200
        body = resp.body if isinstance(resp.body, bytes) else b"{}"
        assert b"blocks" in body
    finally:
        inj._resolve_mem, inj._resolve_rag = orig_mem, orig_rag  # type: ignore[assignment]
