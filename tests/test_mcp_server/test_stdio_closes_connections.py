"""stdio transport must close aiosqlite connections after `mcp.run` finishes.

The stdio path used to call `mcp.run(...)` and nothing else. On stdin EOF
`mcp.run` returns and the process is expected to exit, but the aiosqlite worker
threads are non-daemon — without closing them the interpreter parks in
`threading._shutdown` and the MCP client's parent sees a server that never dies.
"""

from __future__ import annotations

import sys

import pytest


@pytest.fixture
def stdio_server(monkeypatch):
    import mcp_server.server as srv
    from shared.connection import connection_manager

    calls: list[str] = []

    async def _close() -> None:
        calls.append("close")

    monkeypatch.setattr(connection_manager, "close_all", _close)
    monkeypatch.setattr(srv, "_setup_logging", lambda: None)
    monkeypatch.setattr(srv, "_install_faulthandler", lambda: None)
    monkeypatch.setattr(sys, "argv", ["ariel-memory", "--transport", "stdio"])
    return srv, calls


def test_stdio_closes_after_run_returns(stdio_server, monkeypatch) -> None:
    srv, calls = stdio_server

    class _MCP:
        def run(self, **_kwargs) -> None:
            calls.append("run")

    monkeypatch.setattr(srv, "mcp", _MCP())
    srv.main()
    assert calls == ["run", "close"]


def test_stdio_closes_when_run_raises(stdio_server, monkeypatch) -> None:
    srv, calls = stdio_server

    class _MCP:
        def run(self, **_kwargs) -> None:
            calls.append("run")
            raise RuntimeError("boom")

    monkeypatch.setattr(srv, "mcp", _MCP())
    with pytest.raises(RuntimeError, match="boom"):
        srv.main()
    assert calls == ["run", "close"]
