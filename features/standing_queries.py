"""A2.5: standing queries — declarative `.meta/*` yaml files, evaluated on demand.

A standing query is a named, saved `memory_query` invocation: the file
declares whitelisted query_dsl filters; `run_standing_query` executes it
and returns the rows. Complements D1.9 rules (write-side) — this is the
read-side: "surface commitments", "list decisions this week", etc.

File shape (<data_dir>/.meta/<name>.yaml):
    description: open commitments
    source: core
    key_like: "commitment:%"
    importance_min: 0.3
    limit: 20
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from shared.connection import connection_manager

logger = logging.getLogger(__name__)

# Whitelisted file keys that map 1:1 onto query_memory() params.
_ALLOWED_FILTERS = (
    "source",
    "layer",
    "importance_min",
    "importance_max",
    "key_like",
    "content_like",
    "created_since",
    "created_until",
    "tag",
    "tags",
    "limit",
)


def meta_dir() -> Path:
    base = connection_manager.base_dir
    return (Path(str(base)) / ".meta") if base else Path.home() / ".mcp-ariel-memory" / ".meta"


def list_standing() -> list[dict[str, Any]]:
    """All standing queries on disk (payload-free listing)."""
    d = meta_dir()
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("*.yaml")):
        try:
            import yaml

            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            out.append({"name": f.stem, "description": str(data.get("description", ""))})
        except Exception as exc:
            logger.debug("standing query %s unreadable: %s", f.name, exc)
    return out


def load_standing(name: str) -> dict[str, Any]:
    """Load + validate one standing query. Raises ValueError on unknown/invalid."""
    if not name.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"invalid standing query name: {name!r}")
    path = meta_dir() / f"{name}.yaml"
    if not path.is_file():
        raise ValueError(f"unknown standing query: {name!r}")
    import yaml

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise ValueError(f"standing query {name!r} is not valid yaml: {exc}") from exc
    if not isinstance(data, dict):
        raise TypeError(f"standing query {name!r} must be a yaml mapping")
    unknown = set(data) - set(_ALLOWED_FILTERS) - {"description"}
    if unknown:
        raise ValueError(f"standing query {name!r} has unknown filters: {sorted(unknown)}")
    return data


async def run_standing(name: str, user_id: str = "default") -> dict[str, Any]:
    """Execute a standing query through the D1.7 DSL (whitelisted, no SQL)."""
    spec = load_standing(name)
    from features.query_dsl import query_memory

    filters = {k: v for k, v in spec.items() if k in _ALLOWED_FILTERS}
    return await query_memory(user_id, **filters)


def save_standing(name: str, spec: dict[str, Any]) -> Path:
    """Write a standing query file (operator/agent surface)."""
    if not name.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"invalid standing query name: {name!r}")
    unknown = set(spec) - set(_ALLOWED_FILTERS) - {"description"}
    if unknown:
        raise ValueError(f"unknown filters: {sorted(unknown)}")
    d = meta_dir()
    d.mkdir(parents=True, exist_ok=True)
    import yaml

    path = d / f"{name}.yaml"
    path.write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def delete_standing(name: str) -> bool:
    path = meta_dir() / f"{name}.yaml"
    if not path.is_file():
        return False
    path.unlink()
    return True
