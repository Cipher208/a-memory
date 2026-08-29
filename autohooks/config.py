# autohooks/config.py
"""Agent config load + validation (spec S3). No ariel imports — hard rule."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_TOP_KEYS = {"data_dir", "user_id", "layer", "source", "poll_seconds", "batch_limit", "state_file", "master_key"}
_SOURCE_KEYS = {"driver", "path", "table", "cursor_column", "order_by", "role", "text", "ts", "filter"}
_MAP_KEYS = {"column", "json_path"}


@dataclass(frozen=True)
class FieldMap:
    """One source field: either a plain column or (column, json_path)."""

    column: str | None = None
    json_path: tuple[str, str] | None = None


@dataclass(frozen=True)
class SourceConfig:
    driver: str
    path: Path
    table: str
    cursor_column: str
    order_by: str
    role: FieldMap
    text: FieldMap
    ts: FieldMap | None = None
    filter: str | None = None


@dataclass(frozen=True)
class AgentConfig:
    data_dir: Path
    user_id: str
    layer: str
    source: SourceConfig
    poll_seconds: int = 15
    batch_limit: int = 100
    state_file: Path = Path()  # always set by load_config (data_dir / "autohooks-cursor.json")
    master_key: str | None = None


def sql_expr(fm: FieldMap) -> str:
    """Return the SQL expression for a field mapping (json_extract for JSON columns)."""
    if fm.column is not None:
        return fm.column
    assert fm.json_path is not None
    col, path = fm.json_path
    return f"json_extract({col}, '{path}')"


def _field_map(raw: Any, what: str) -> FieldMap:
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{what}: must be a non-empty mapping")
    unknown = set(raw) - _MAP_KEYS
    if unknown:
        raise ValueError(f"{what}: unknown keys {sorted(unknown)}")
    if "column" in raw:
        if "json_path" in raw:
            raise ValueError(f"{what}: column and json_path are mutually exclusive")
        return FieldMap(column=str(raw["column"]))
    jp = raw["json_path"]
    if not isinstance(jp, (list, tuple)) or len(jp) != 2:
        raise ValueError(f"{what}: json_path must be [column, '$.path']")
    return FieldMap(json_path=(str(jp[0]), str(jp[1])))


def load_config(path: str | Path) -> AgentConfig:
    """Load + validate one agent YAML. Unknown keys are hard errors (fail fast)."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("config root must be a mapping")
    unknown = set(raw) - _TOP_KEYS
    if unknown:
        raise ValueError(f"unknown config keys {sorted(unknown)}")

    src_raw = raw.get("source")
    if not isinstance(src_raw, dict):
        raise TypeError("source: must be a mapping")
    unknown_src = set(src_raw) - _SOURCE_KEYS
    if unknown_src:
        raise ValueError(f"source: unknown source keys {sorted(unknown_src)}")
    for req in ("driver", "path", "table", "cursor_column", "order_by", "role", "text"):
        if req not in src_raw:
            raise ValueError(f"source: missing required key {req!r}")
    if src_raw["driver"] != "sqlite":
        raise ValueError(f"source: unsupported driver {src_raw['driver']!r} (v1: sqlite only)")

    data_dir = Path(raw["data_dir"]).expanduser()
    default_state = data_dir / "autohooks-cursor.json"
    return AgentConfig(
        data_dir=data_dir,
        user_id=str(raw.get("user_id", "default")),
        layer=str(raw.get("layer", "user")),
        source=SourceConfig(
            driver="sqlite",
            path=Path(src_raw["path"]).expanduser(),
            table=str(src_raw["table"]),
            cursor_column=str(src_raw["cursor_column"]),
            order_by=str(src_raw["order_by"]),
            role=_field_map(src_raw["role"], "source.role"),
            text=_field_map(src_raw["text"], "source.text"),
            ts=_field_map(src_raw["ts"], "source.ts") if "ts" in src_raw else None,
            filter=str(src_raw["filter"]) if "filter" in src_raw else None,
        ),
        poll_seconds=int(raw.get("poll_seconds", 15)),
        batch_limit=int(raw.get("batch_limit", 100)),
        state_file=Path(raw["state_file"]).expanduser() if "state_file" in raw else default_state,
        master_key=str(raw["master_key"]) if "master_key" in raw else None,
    )
