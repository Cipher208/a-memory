"""D1.8 typed memory schemas — validate structured saves.

Built-in schemas: decision / error_pattern / relationship. Custom schemas
from <data_dir>/schemas/*.yaml merge over the built-ins (yaml.safe_load:
{field: {required, max_len}} or shorthand {field: str}). Validated payloads
store as L4 facts: key = "<type>:<name>", metadata.typed = <type> — memory
turns from a bag of strings into a typed knowledge base.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

BUILTIN_SCHEMAS: dict[str, dict[str, dict[str, Any]]] = {
    "decision": {
        "decision": {"type": "str", "required": True, "max_len": 500},
        "rationale": {"type": "str", "max_len": 1000},
        "alternatives": {"type": "str", "max_len": 500},
    },
    "error_pattern": {
        "error": {"type": "str", "required": True, "max_len": 300},
        "cause": {"type": "str", "max_len": 500},
        "fix": {"type": "str", "max_len": 500},
        "symptom": {"type": "str", "max_len": 300},
    },
    "relationship": {
        "name": {"type": "str", "required": True, "max_len": 200},
        "relation": {"type": "str", "max_len": 200},
        "notes": {"type": "str", "max_len": 1000},
    },
}


def _schemas_dir() -> Path:
    from shared.connection import connection_manager

    return connection_manager.base_dir / "schemas"


def load_custom_schemas() -> dict[str, dict[str, dict[str, Any]]]:
    """Read <data_dir>/schemas/*.yaml; shorthand {field: str} normalizes."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    d = _schemas_dir()
    if not d.is_dir():
        return out
    import yaml

    for f in sorted(d.glob("*.yaml")):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            logger.warning("schema %s parse failed: %s", f.name, exc)
            continue
        if not isinstance(data, dict):
            continue
        for name, schema in data.items():
            if not isinstance(schema, dict):
                continue
            fields: dict[str, dict[str, Any]] = {}
            for fname, spec in schema.items():
                if isinstance(spec, dict):
                    fields[str(fname)] = {"type": "str", "max_len": int(spec.get("max_len") or 2000), "required": bool(spec.get("required"))}
                else:
                    fields[str(fname)] = {"type": "str", "max_len": 2000, "required": False}
            out[str(name)] = fields
    return out


def available_schemas() -> dict[str, list[str]]:
    merged = {**BUILTIN_SCHEMAS, **load_custom_schemas()}
    return {name: list(fields) for name, fields in merged.items()}


def validate_fields(schema: dict[str, dict[str, Any]], fields: dict[str, str]) -> list[str]:
    """Return validation errors (empty list = valid)."""
    errors: list[str] = []
    for fname, spec in schema.items():
        val = str(fields.get(fname, "") or "")
        if spec.get("required") and not val:
            errors.append(f"missing required field: {fname}")
        if val and len(val) > int(spec.get("max_len") or 2000):
            errors.append(f"field {fname} exceeds max_len={spec['max_len']}")
    unknown = set(fields) - set(schema)
    if unknown:
        errors.append(f"unknown fields: {', '.join(sorted(unknown))}")
    return errors


async def save_typed(
    mem: Any,
    user_id: str,
    type_name: str,
    fields: dict[str, str],
) -> dict[str, Any]:
    """Validate fields against the schema and store as an L4 typed fact."""
    merged = {**BUILTIN_SCHEMAS, **load_custom_schemas()}
    schema = merged.get(type_name)
    if schema is None:
        raise ValueError(f"unknown type: {type_name!r}. Available: {', '.join(sorted(merged))}")
    fields = {str(k): str(v) for k, v in (fields or {}).items()}
    errors = validate_fields(schema, fields)
    if errors:
        raise ValueError("schema validation failed: " + "; ".join(errors))
    name = str(fields.get("decision") or fields.get("error") or fields.get("name") or "unnamed")[:80]
    key = f"{type_name}:{name}"
    value = "\n".join(f"{k}: {v}" for k, v in fields.items())
    eid = await mem.l4.save(user_id, key, value, metadata={"typed": type_name}, source="user_explicit")
    return {"key": key, "entry_id": eid, "type": type_name, "fields": fields}
