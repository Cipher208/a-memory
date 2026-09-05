"""A8 MEMORY.md-бридж — человекочитаемый файл топ-фактов + drain-приём.

regenerate_bridge: топ-20 инвариантных фактов (importance ≥ 0.6) →
bridge_<layer>.md (atomic write, паттерн core/reflex.py). Всё ниже маркера
AUTO-DRAIN переживает регенерацию — заметки пользователя не затираются.
ingest_drain: текст ниже маркера → L0 capture (event='bridge_drain') →
G1 distill_and_route → ниже маркера файл очищается до инструкции-комментария.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any

from shared.connection import connection_manager
from shared.constants import DB_NAME

BRIDGE_MARKER = "# === AUTO-DRAIN BELOW ==="
DRAIN_COMMENT = "<!-- Текст ниже маркера будет проанализирован и удалён при следующем проходе -->"
MIN_IMPORTANCE = 0.6
TOP_LIMIT = 20
INVARIANT_KINDS = ("fact", "decision", "rule", "instruction", "commitment", "goal", "relationship")

_ZERO_ROUTES: dict[str, int] = {"l4_saved": 0, "l3_saved": 0, "conflicts": 0, "wired_edges": 0}


def _resolve(base_path: str | None, layer: str) -> Path:
    if base_path:
        return Path(base_path)
    return connection_manager.base_dir / f"bridge_{layer}.md"


def _tail(drain: str) -> str:
    body = drain if drain.strip() else f"{DRAIN_COMMENT}\n"
    return f"\n{BRIDGE_MARKER}\n{body}"


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)  # atomic on POSIX and Windows
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


async def regenerate_bridge(user_id: str, layer: str = "agent", base_path: str | None = None) -> Path:
    """Топ-инварианты → bridge-файл. Drain-секция ниже маркера сохраняется."""
    path = _resolve(base_path, layer)
    conn = await connection_manager.get(DB_NAME)
    placeholders = ",".join("?" * len(INVARIANT_KINDS))
    rows = await (
        await conn.execute(
            f"""SELECT key, value, importance FROM core_memory
                WHERE layer=? AND user_id=? AND memory_kind IN ({placeholders})
                  AND importance >= ? ORDER BY importance DESC LIMIT ?""",
            (layer, user_id, *INVARIANT_KINDS, MIN_IMPORTANCE, TOP_LIMIT),
        )
    ).fetchall()
    top = f"# MEMORY.md — ariel bridge (layer={layer})\n\n" + "".join(
        f"- [{r['key']}] {r['value']} (importance {float(r['importance']):.2f})\n" for r in rows
    )
    drain = ""
    if path.exists():
        with contextlib.suppress(OSError, UnicodeDecodeError):
            _, marker, below = path.read_text(encoding="utf-8").partition(BRIDGE_MARKER)
            if marker:
                drain = below
    _atomic_write(path, top + _tail(drain))
    return path


async def ingest_drain(user_id: str, layer: str = "agent", base_path: str | None = None) -> dict[str, Any]:
    """Текст ниже drain-маркера → L0 → distill; ниже маркера — только инструкция."""
    path = _resolve(base_path, layer)
    routes: dict[str, int] = dict(_ZERO_ROUTES)
    if not path.exists():
        return {"ingested": 0, "routes": routes}
    content = path.read_text(encoding="utf-8")
    if BRIDGE_MARKER not in content:
        return {"ingested": 0, "routes": routes}
    top, _, below = content.partition(BRIDGE_MARKER)
    lines = [ln for ln in below.splitlines() if ln.strip() and not ln.strip().startswith("<!--")]
    drain = "\n".join(lines)
    if drain:
        from core import MemoryManager
        from graph.epistemic import EpistemicGraph
        from lifecycle.distiller import distill_and_route
        from shared.l0 import capture

        await capture("bridge_drain", layer, user_id, drain, raw_type="user-message")
        mem = MemoryManager(cm=connection_manager).get_layer(layer, user_id)
        graph = EpistemicGraph(cm=connection_manager, layer=layer)
        routes = await distill_and_route(mem, graph, user_id, drain, 0.6, event="bridge_drain")
    _atomic_write(path, top + _tail(""))
    return {"ingested": len(lines), "routes": routes}
