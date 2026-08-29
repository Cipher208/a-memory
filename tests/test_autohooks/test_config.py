# tests/test_autohooks/test_config.py
"""Agent config load/validate (spec S3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from autohooks.config import load_config, sql_expr


def _write(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "agent.yaml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


MINIMAL = """
data_dir: ~/.mcp-ariel-memory-hermes
user_id: default
layer: user
source:
  driver: sqlite
  path: ~/.hermes/state.db
  table: messages
  cursor_column: id
  order_by: id
  role: {column: role}
  text: {column: content}
  ts: {column: timestamp}
"""


def test_minimal_config_loads_with_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    p = _write(tmp_path, MINIMAL)
    cfg = load_config(p)
    assert cfg.user_id == "default"
    assert cfg.layer == "user"
    assert cfg.poll_seconds == 15
    assert cfg.batch_limit == 100
    assert cfg.source.table == "messages"
    assert cfg.state_file == Path(tmp_path / ".mcp-ariel-memory-hermes" / "autohooks-cursor.json")
    assert cfg.data_dir == Path(tmp_path / ".mcp-ariel-memory-hermes")


def test_unknown_top_key_hard_error(tmp_path: Path) -> None:
    p = _write(tmp_path, MINIMAL + "\nunknown_key: 1\n")
    with pytest.raises(ValueError, match="unknown config keys"):
        load_config(p)


def test_unknown_source_key_hard_error(tmp_path: Path) -> None:
    p = _write(tmp_path, MINIMAL.replace("order_by: id", "order_by: id\n  bogus: x"))
    with pytest.raises(ValueError, match="unknown source keys"):
        load_config(p)


def test_missing_text_mapping_hard_error(tmp_path: Path) -> None:
    p = _write(tmp_path, MINIMAL.replace("  text: {column: content}\n", ""))
    with pytest.raises(ValueError, match="text"):
        load_config(p)


def test_column_and_json_path_exclusive(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        MINIMAL.replace("role: {column: role}", "role: {column: role, json_path: [data, '$.role']}"),
    )
    with pytest.raises(ValueError, match="mutually exclusive"):
        load_config(p)


def test_json_path_field_maps_to_sql_expr(tmp_path: Path) -> None:
    body = MINIMAL.replace("role: {column: role}", "role: {json_path: [data, '$.role']}").replace(
        "text: {column: content}", "text: {json_path: [data, '$.text']}"
    )
    p = _write(tmp_path, body)
    cfg = load_config(p)
    assert sql_expr(cfg.source.role) == "json_extract(data, '$.role')"
    assert sql_expr(cfg.source.text) == "json_extract(data, '$.text')"
    assert sql_expr(cfg.source.text) != sql_expr(cfg.source.ts)


def test_master_key_optional(tmp_path: Path) -> None:
    p = _write(tmp_path, MINIMAL + "\nmaster_key: abc123\n")
    assert load_config(p).master_key == "abc123"
    p2 = _write(tmp_path, MINIMAL)
    assert load_config(p2).master_key is None
