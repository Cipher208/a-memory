# tests/test_autohooks/test_cli.py
"""CLI: env-before-import guarantee + subcommand wiring (spec S2/S7)."""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

import pytest

from autohooks.__main__ import _parse_args, apply_env, main
from autohooks.config import load_config

if TYPE_CHECKING:
    from pathlib import Path


def _cfg_file(tmp_path: Path) -> Path:
    p = tmp_path / "a.yaml"
    p.write_text(
        f"""
data_dir: {tmp_path}/data
user_id: u1
source:
  driver: sqlite
  path: {tmp_path}/conv.db
  table: messages
  cursor_column: id
  order_by: id
  role: {{column: role}}
  text: {{column: content}}
""",
        encoding="utf-8",
    )
    return p


def test_apply_env_sets_data_dir_and_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("MCP_MEMORY_DATA_DIR", "MCP_CONFIG_PATH", "MCP_MASTER_KEY"):
        monkeypatch.delenv(var, raising=False)
    cfg = load_config(_cfg_file(tmp_path))
    apply_env(cfg)
    assert os.environ["MCP_MEMORY_DATA_DIR"] == str(cfg.data_dir)
    assert os.environ["MCP_CONFIG_PATH"] == str(cfg.data_dir / "config.yaml")
    assert "MCP_MASTER_KEY" not in os.environ


def test_apply_env_master_key_passthrough(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_MASTER_KEY", raising=False)
    p = tmp_path / "b.yaml"
    p.write_text(_cfg_file(tmp_path).read_text(encoding="utf-8") + "\nmaster_key: key123\n", encoding="utf-8")
    apply_env(load_config(p))
    assert os.environ["MCP_MASTER_KEY"] == "key123"


def test_missing_config_file_fails(tmp_path: Path) -> None:
    assert main(["daemon", "--config", str(tmp_path / "nope.yaml")]) == 2


def test_parse_inject_args(tmp_path: Path) -> None:
    ns = _parse_args(["inject", "--config", str(_cfg_file(tmp_path)), "--text", "привет", "--format", "json"])
    assert ns.command == "inject"
    assert ns.text == "привет"
    assert ns.format == "json"


def test_parse_dispatch_args(tmp_path: Path) -> None:
    ns = _parse_args(
        [
            "dispatch",
            "--config",
            str(_cfg_file(tmp_path)),
            "--event",
            "post_session_diff",
            "--since",
            "0",
            "--until",
            "100",
        ]
    )
    assert ns.command == "dispatch"
    assert ns.event == "post_session_diff"
    assert ns.since == "0"
    assert ns.until == "100"


# --- The diff window: `--since` and `--until` do NOT share a default. -------
#
# `compute_session_gaps` reads `created_at >= since AND created_at < until`.
# A missing `--until` used to be forwarded as an explicit 0.0, which is the
# epoch rather than "unset" — so the window became `< 0` and matched nothing.
# `post_session_diff` answered `gaps: 0` for every caller regardless of what had
# happened, and no `diff_gap` episode was ever written.
#
# The whole existing suite missed it because every test called the handler with
# an explicit `until=time.time() + 1`. The window was only ever exercised from
# the one place that could not supply it: a real CLI invocation.


def test_dispatch_window_until_defaults_to_now(tmp_path: Path) -> None:
    """No `--until` means "up to now", never the epoch."""
    from autohooks.__main__ import _dispatch_window

    ns = _parse_args(["dispatch", "--config", str(_cfg_file(tmp_path)), "--event", "post_session_diff"])
    before = time.time()
    since, until = _dispatch_window(ns)
    assert since == 0.0, "an absent --since still means 'from the beginning'"
    assert before <= until <= time.time() + 1, "an absent --until must open the window, not close it"
    assert until > since, "a window that cannot match any row is the bug being pinned"


def test_dispatch_window_respects_an_explicit_until(tmp_path: Path) -> None:
    from autohooks.__main__ import _dispatch_window

    ns = _parse_args(["dispatch", "--config", str(_cfg_file(tmp_path)), "--event", "post_session_diff", "--since", "10", "--until", "100"])
    assert _dispatch_window(ns) == (10.0, 100.0)


def test_dispatch_window_treats_zero_until_as_unset(tmp_path: Path) -> None:
    """An explicit `--until 0` is the same mistake by hand, and gets the same answer."""
    from autohooks.__main__ import _dispatch_window

    ns = _parse_args(["dispatch", "--config", str(_cfg_file(tmp_path)), "--event", "post_session_diff", "--until", "0"])
    _since, until = _dispatch_window(ns)
    assert until > 0, "zero is the epoch, and the epoch is always an empty window"


# --- The `source:` gate, and which side of it each command sits on. -------
#
# `source:` is a chat database the daemon TAILS. A push-driven platform has no
# such database, so the check must not stand in the way of the commands it does
# use. These tests pin the boundary itself rather than a message: `daemon` stops
# before ariel loads, everything else walks straight past.


def _cfg_file_no_source(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir(exist_ok=True)
    p = tmp_path / "nosource.yaml"
    p.write_text(
        f"""
data_dir: {tmp_path}/data
user_id: u1
""",
        encoding="utf-8",
    )
    return p


class _ReachedArielError(Exception):
    """Raised by the stubbed app context: proof that main() got past the gate."""


def test_daemon_without_source_fails_before_ariel_loads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    import autohooks.appctx as appctx

    def _boom() -> object:
        raise _ReachedArielError

    monkeypatch.setattr(appctx, "build_app_context", _boom)
    monkeypatch.setenv("ARIEL_CLI_GUARD", "0")

    with caplog.at_level("ERROR"):
        code = main(["daemon", "--config", str(_cfg_file_no_source(tmp_path))])

    assert code == 2
    assert "source:" in caplog.text
    assert "daemon needs" in caplog.text


def test_push_command_without_source_is_not_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression: this used to return 2 for a driver the command never reads."""
    import autohooks.appctx as appctx

    def _boom() -> object:
        raise _ReachedArielError

    monkeypatch.setattr(appctx, "build_app_context", _boom)
    monkeypatch.setenv("ARIEL_CLI_GUARD", "0")

    # Reaching ariel is the point: it proves the source check did not stand in
    # the way. What happens after (loading ariel for real) is another test's job.
    with pytest.raises(_ReachedArielError):
        main(["inject", "--config", str(_cfg_file_no_source(tmp_path)), "--text", "привет"])


def test_daemon_with_declared_but_missing_source_db_fails(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir(exist_ok=True)
    p = tmp_path / "gone.yaml"
    p.write_text(
        f"""
data_dir: {tmp_path}/data
user_id: u1
source:
  driver: sqlite
  path: {tmp_path}/absent.db
  table: messages
  cursor_column: id
  order_by: id
  role: {{column: role}}
  text: {{column: content}}
""",
        encoding="utf-8",
    )
    assert main(["daemon", "--config", str(p)]) == 2
