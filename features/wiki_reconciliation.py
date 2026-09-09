"""S13 file↔DB reconciliation: wiki .md files vs wiki_index.

WikiManager writes the pair (file, wiki_index row); drift appears after manual
edits/deletions. reconcile() is a read-only audit:
  (a) orphan md — the file exists, no index row (manual creation, failed save);
  (b) stale index — the index row exists, no file (manual deletion).

# ponytail: hash mismatch (file edited by hand after indexing) is not checked —
# the hash bases are inconsistent: add() hashes the raw content, update() the
# whole rendered .md (wiki/manager.py), so there is no cheap unambiguous comparison.
# Upgrade path: a single hash basis in the manager → compare content_hash here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

# Auto-generated files written to disk without indexing (wiki/manager.py
# _write_moc, wiki/lint INDEX-stub) — not orphans.
_AUTO_GENERATED = {"INDEX.md"}


async def reconcile(user_id: str = "default", layer: str = "user") -> dict[str, Any]:
    """Reconcile wiki files against wiki_index: {'orphans': [paths], 'stale': [paths], 'checked': N}.

    checked = number of reconciled pairs (md files + index rows of the layer).
    user_id — signature compatibility with memory_audit: wiki_index is not
    user-scoped, the audit runs over (layer, file_path). The wiki directory
    comes from connection_manager (same data dir as WikiManager —
    wiki/manager.py docstring).
    """
    from shared.connection import connection_manager
    from shared.constants import DB_NAME

    base_dir = Path(str(connection_manager.base_dir)) / "wiki" / layer
    conn = await connection_manager.get(DB_NAME)
    db_paths = {str(r["file_path"]) for r in (await (await conn.execute("SELECT file_path FROM wiki_index WHERE layer=?", (layer,))).fetchall())}

    def _scan() -> tuple[list[Path], list[str]]:
        files: list[Path] = []
        if base_dir.exists():
            for type_dir in sorted(base_dir.iterdir()):
                if not type_dir.is_dir() or type_dir.name == "_retired":
                    continue
                for f in sorted(type_dir.glob("*.md")):
                    if f.name in _AUTO_GENERATED or f.name.startswith("MOC_"):
                        continue
                    files.append(f)
        stale = sorted(p for p in db_paths if not Path(p).exists())  # ASYNC240: pathlib in a thread
        return files, stale

    md_files, stale = await asyncio.to_thread(_scan)
    orphans = [str(f) for f in md_files if str(f) not in db_paths]
    return {"orphans": orphans, "stale": stale, "checked": len(md_files) + len(db_paths)}
