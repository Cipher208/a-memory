# tests/test_autohooks/test_cli.py
"""CLI: env-before-import guarantee + subcommand wiring (spec S2/S7)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from autohooks.__main__ import _parse_args, apply_env, main
from autohooks.config import load_config

if TYPE_CHECKING:
    import pytest
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
