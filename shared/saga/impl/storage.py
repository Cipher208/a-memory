from __future__ import annotations

"""Saga state storage — directory resolution and read/write helpers.

SAGA_DIR resolves from MCP_MEMORY_DATA_DIR so every instance keeps its
saga states inside its own data dir. No import-time file migration: a
process with a different data dir must never move another instance's
state files (learned the hard way — see session notes 2026-08-25).
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

from shared.constants import SAGA_DIR_NAME, UTF8

logger = logging.getLogger(__name__)

try:
    from shared.saga.impl.crypto import (
        read_state_legacy_or_encrypted,
        write_state_atomic,
    )

    _HAS_ENCRYPTION = True
except ImportError:
    _HAS_ENCRYPTION = False


def _resolve_saga_dir() -> Path:
    data_dir = os.environ.get("MCP_MEMORY_DATA_DIR") or str(Path.home() / ".mcp-ariel-memory")
    return Path(data_dir) / SAGA_DIR_NAME


SAGA_DIR = _resolve_saga_dir()


def write_state_file(path: Path, state: dict[str, Any]) -> None:
    """Persist a saga state file (encrypted when available, else plain JSON)."""
    if _HAS_ENCRYPTION:
        write_state_atomic(path, state)
    else:
        path.write_text(json.dumps(state, indent=2, default=str), encoding=UTF8)


def read_state_file(path: Path) -> dict[str, Any]:
    """Read a saga state file (encrypted preferred, legacy plain JSON fallback).

    Raises FileNotFoundError for missing paths; callers keep their own
    exists/symlink guards.
    """
    if _HAS_ENCRYPTION:
        return read_state_legacy_or_encrypted(path)
    return dict(json.loads(path.read_text(encoding=UTF8)))
