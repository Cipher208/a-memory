# tests/test_autohooks/test_appctx.py
"""AppContext construction + layer resolution outside the server (spec S2)."""

from __future__ import annotations

import pytest

from autohooks.appctx import build_app_context, resolve_layer


def test_build_and_resolve_user_layer() -> None:
    app = build_app_context()
    mem, graph, rag = resolve_layer(app, "user", "test-user")
    assert mem is not None
    assert graph is not None
    assert rag is not None


def test_resolve_agent_layer_returns_agent_objects() -> None:
    app = build_app_context()
    _mem, graph, _rag = resolve_layer(app, "agent", "test-user")
    assert graph is app.agent_graph


def test_resolve_unknown_layer_raises() -> None:
    app = build_app_context()
    with pytest.raises(ValueError, match="Invalid layer"):
        resolve_layer(app, "bogus", "u")
