#!/usr/bin/env python3
"""Inventory possible user-to-agent graph-layer migrations.

The inventory command is deliberately read-only. It records evidence for human
review; it never changes the SQLite database and does not implement apply mode.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict
from urllib.parse import quote


if TYPE_CHECKING:
    from collections.abc import Sequence


MARKER_VERSION = 1
EXCLUDE_VERSION = 1
MARKER_SOURCES = {"none", "cli", "file", "cli+file"}
EXCLUDE_SOURCES = {"none", "cli", "file", "cli+file"}
REQUIRED_NODE_COLUMNS = {"node_id", "layer", "user_id", "content", "node_type", "tags"}
REQUIRED_EDGE_COLUMNS = {"source_id", "target_id", "relation", "tags"}
# wiki_page is deliberately absent: its `content` is a filesystem path, not prose,
# so the node type alone carries no agent-layer text evidence.
AGENT_LIKE_NODE_TYPES = {"personality_trait", "action", "outcome", "error_outcome"}
AGENT_LIKE_TAGS = {
    "learned_from",
    "decided_because",
    "evolved_to",
    "felt_in_context",
    "wiki_contains",
    "error_pattern",
    "correction_pattern",
    "personality_trait",
}
AGENT_LIKE_RELATIONS = {"decided_because", "evolved_to", "learned_from"}


class SchemaError(RuntimeError):
    """Raised when the database does not expose the expected graph schema."""


class Candidate(TypedDict):
    node_id: int
    user_id: str
    current_layer: str
    target_layer: str
    confidence: str
    reasons: list[str]
    decision: str
    edge_ids: list[dict[str, Any]]
    edge_impact: list[dict[str, Any]]
    duplicate_node_ids: list[int]
    duplicate_count: int
    target_collision_node_ids: list[int]


def _database_path(data_dir: Path) -> Path:
    return data_dir / "memory.db"


# The database is more than memory.db: SQLite keeps committed-but-not-yet-
# checkpointed rows in `-wal`, its shared-memory index in `-shm`, and a rollback
# journal in `-journal`. A manifest written over any of those siblings destroys
# live data just as surely as one written over the database itself, and harder to
# notice — the main file still looks intact while the reader gets
# "disk I/O error".
_DATABASE_SIDECARS = ("-wal", "-shm", "-journal")


def _protected_database_paths(data_dir: Path) -> list[Path]:
    db_path = _database_path(data_dir)
    return [db_path, *(db_path.with_name(db_path.name + suffix) for suffix in _DATABASE_SIDECARS)]


def _resolve_output_paths(data_dir: Path, output: Path) -> tuple[Path, Path]:
    db_path = _database_path(data_dir)
    resolved_db = db_path.resolve()
    resolved_output = output.resolve()
    protected = [path.resolve() for path in _protected_database_paths(data_dir)]
    if resolved_output in protected:
        raise SchemaError("refusing output path resolving to the database or one of its sidecars")
    if output.exists():
        db_stat = db_path.stat()
        output_stat = output.stat()
        if (output_stat.st_dev, output_stat.st_ino) == (db_stat.st_dev, db_stat.st_ino):
            raise SchemaError("refusing output path aliasing database")
        for sidecar in _protected_database_paths(data_dir)[1:]:
            if not sidecar.exists():
                continue
            sidecar_stat = sidecar.stat()
            if (output_stat.st_dev, output_stat.st_ino) == (sidecar_stat.st_dev, sidecar_stat.st_ino):
                raise SchemaError("refusing output path aliasing a database sidecar")
    return resolved_db, output.absolute()


def _fingerprint(path: Path, source_metadata: dict[str, Any]) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = path.stat()
    return {"sha256": digest.hexdigest(), "size": stat.st_size, "source": source_metadata}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _primary_key_fields(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [str(row[1]) for row in sorted(rows, key=lambda row: int(row[5])) if int(row[5]) > 0]


def _validate_schema(conn: sqlite3.Connection) -> list[str]:
    node_columns = _table_columns(conn, "epi_nodes")
    edge_columns = _table_columns(conn, "epi_edges")
    missing_nodes = sorted(REQUIRED_NODE_COLUMNS - node_columns)
    missing_edges = sorted(REQUIRED_EDGE_COLUMNS - edge_columns)
    if missing_nodes or missing_edges:
        details: list[str] = []
        if missing_nodes:
            details.append(f"epi_nodes missing columns: {', '.join(missing_nodes)}")
        if missing_edges:
            details.append(f"epi_edges missing columns: {', '.join(missing_edges)}")
        raise SchemaError("invalid graph schema: " + "; ".join(details))

    node_pk = _primary_key_fields(conn, "epi_nodes")
    if node_pk != ["node_id"]:
        raise SchemaError("invalid graph schema: epi_nodes primary key must be ['node_id']")

    edge_pk = _primary_key_fields(conn, "epi_edges")
    if edge_pk != ["source_id", "target_id", "relation"]:
        raise SchemaError("invalid graph schema: epi_edges primary key must be ['source_id', 'target_id', 'relation']")
    return edge_pk


def _json_list(value: Any) -> list[str]:
    if not value:
        return []
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError):
        return []
    if not isinstance(decoded, list):
        return []
    return [item for item in decoded if isinstance(item, str)]


def _normalize_patterns(patterns: Sequence[str]) -> list[str]:
    return sorted({pattern for pattern in patterns if pattern})


def _normalize_markers(markers: Sequence[str]) -> list[str]:
    return _normalize_patterns(markers)


def _normalize_excludes(excludes: Sequence[str]) -> list[str]:
    return _normalize_patterns(excludes)


def _pattern_policy(
    patterns: Sequence[str],
    source: str | None,
    *,
    version: int,
    sources: set[str],
    label: str,
) -> dict[str, Any]:
    normalized = _normalize_patterns(patterns)
    resolved = source if source is not None else ("none" if not normalized else "cli")
    if resolved not in sources:
        raise SchemaError(f"unknown {label} source: {resolved}")
    digest = hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()
    return {"version": version, "source": resolved, "count": len(normalized), "sha256": digest}


def _marker_policy(markers: Sequence[str], source: str | None) -> dict[str, Any]:
    return _pattern_policy(markers, source, version=MARKER_VERSION, sources=MARKER_SOURCES, label="marker")


def _exclude_policy(excludes: Sequence[str], source: str | None) -> dict[str, Any]:
    return _pattern_policy(excludes, source, version=EXCLUDE_VERSION, sources=EXCLUDE_SOURCES, label="exclude")


def _read_patterns_file(path: Path) -> list[str]:
    patterns: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        patterns.append(stripped)
    return patterns


def _resolve_patterns_file(
    patterns_file: Path | None,
    patterns: Sequence[str] | None,
) -> tuple[list[str], str]:
    from_file = _read_patterns_file(patterns_file) if patterns_file is not None else []
    from_cli = list(patterns) if patterns else []
    if patterns_file is not None and from_cli:
        source = "cli+file"
    elif patterns_file is not None:
        source = "file"
    elif from_cli:
        source = "cli"
    else:
        source = "none"
    return _normalize_patterns([*from_file, *from_cli]), source


def _resolve_markers(markers_file: Path | None, markers: Sequence[str] | None) -> tuple[list[str], str]:
    return _resolve_patterns_file(markers_file, markers)


def _resolve_excludes(excludes_file: Path | None, excludes: Sequence[str] | None) -> tuple[list[str], str]:
    return _resolve_patterns_file(excludes_file, excludes)


def _marker_reasons(content: str, markers: Sequence[str]) -> list[str]:
    return [f"marker:{marker}" for marker in markers if marker in content]


def _exclude_reasons(content: str, excludes: Sequence[str]) -> list[str]:
    return [f"excluded:{pattern}" for pattern in excludes if pattern in content]


def _node_evidence(node: sqlite3.Row) -> tuple[list[str], bool]:
    reasons: list[str] = []
    node_type = str(node["node_type"])
    tags = sorted(set(_json_list(node["tags"])))
    if node_type in AGENT_LIKE_NODE_TYPES:
        reasons.append(f"node_type:{node_type}")
    reasons.extend(f"tag:{tag}" for tag in tags if tag in AGENT_LIKE_TAGS)
    return reasons, bool(reasons)


def _edge_inventory(
    conn: sqlite3.Connection,
    node_id: int,
    user_id: str,
    edge_pk: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    rows = conn.execute(
        "SELECT e.*, "
        "source.layer AS source_layer, source.user_id AS source_user_id, "
        "target.layer AS target_layer, target.user_id AS target_user_id "
        "FROM epi_edges AS e "
        "LEFT JOIN epi_nodes AS source ON source.node_id = e.source_id "
        "LEFT JOIN epi_nodes AS target ON target.node_id = e.target_id "
        "WHERE e.source_id = ? OR e.target_id = ? "
        "ORDER BY e.source_id, e.target_id, e.relation",
        (node_id, node_id),
    ).fetchall()
    edge_ids: list[dict[str, Any]] = []
    impacts: list[dict[str, Any]] = []
    reasons: set[str] = set()
    for row in rows:
        edge_id = {field: row[field] for field in edge_pk}
        edge_ids.append(edge_id)
        opposite_id = int(row["target_id"] if int(row["source_id"]) == node_id else row["source_id"])
        opposite_layer = row["target_layer"] if int(row["source_id"]) == node_id else row["source_layer"]
        opposite_user_id = row["target_user_id"] if int(row["source_id"]) == node_id else row["source_user_id"]
        edge_tags = sorted(set(_json_list(row["tags"])))
        impact = {
            "edge_id": edge_id,
            "opposite_node_id": opposite_id,
            "opposite_layer": opposite_layer if opposite_layer is not None else "unknown",
            "opposite_user_id": opposite_user_id,
            "relation": str(row["relation"]),
            "tags": edge_tags,
        }
        impacts.append(impact)
        if str(opposite_user_id) == user_id:
            if opposite_layer == "agent":
                reasons.add("edge:opposite_layer=agent")
            if str(row["relation"]) in AGENT_LIKE_RELATIONS:
                reasons.add(f"edge_relation:{row['relation']}")
            reasons.update(f"edge_tag:{tag}" for tag in edge_tags if tag.startswith("agent:") or tag in AGENT_LIKE_TAGS)
    return edge_ids, impacts, sorted(reasons)


def _duplicate_node_ids(conn: sqlite3.Connection, node_id: int, layer: str, user_id: str, node_type: str, content: str) -> list[int]:
    """Return ids matching the graph deduplication key except for ``node_id``."""
    rows = conn.execute(
        "SELECT node_id FROM epi_nodes WHERE layer = ? AND user_id = ? AND node_type = ? AND content = ? AND node_id <> ? ORDER BY node_id",
        (layer, user_id, node_type, content, node_id),
    ).fetchall()
    return [int(row[0]) for row in rows]


def _target_collision_node_ids(conn: sqlite3.Connection, user_id: str, node_type: str, content: str) -> list[int]:
    """Return existing agent ids matching the user-to-agent collision key."""
    rows = conn.execute(
        "SELECT node_id FROM epi_nodes WHERE layer = 'agent' AND user_id = ? AND node_type = ? AND content = ? ORDER BY node_id",
        (user_id, node_type, content),
    ).fetchall()
    return [int(row[0]) for row in rows]


def _candidate(
    node: sqlite3.Row,
    conn: sqlite3.Connection,
    edge_pk: list[str],
    markers: Sequence[str],
    excludes: Sequence[str],
) -> Candidate | None:
    content = str(node["content"])
    marker_reasons = _marker_reasons(content, markers)
    exclude_reasons = _exclude_reasons(content, excludes)
    node_reasons, has_node_evidence = _node_evidence(node)
    edge_ids, edge_impact, edge_reasons = _edge_inventory(conn, int(node["node_id"]), str(node["user_id"]), edge_pk)
    reasons = marker_reasons + node_reasons + edge_reasons
    categories = sum(
        (
            bool(marker_reasons),
            has_node_evidence,
            bool(edge_reasons),
        )
    )
    if not reasons:
        return None
    confidence = "high" if categories >= 2 else "medium" if categories == 1 else "low"
    decision = "exclude" if exclude_reasons else "review"
    duplicate_node_ids = _duplicate_node_ids(
        conn,
        int(node["node_id"]),
        str(node["layer"]),
        str(node["user_id"]),
        str(node["node_type"]),
        str(node["content"]),
    )
    target_collision_node_ids = _target_collision_node_ids(conn, str(node["user_id"]), str(node["node_type"]), str(node["content"]))
    return {
        "node_id": int(node["node_id"]),
        "user_id": str(node["user_id"]),
        "current_layer": "user",
        "target_layer": "agent",
        "confidence": confidence,
        "reasons": reasons + exclude_reasons,
        "decision": decision,
        "edge_ids": edge_ids,
        "edge_impact": edge_impact,
        "duplicate_node_ids": duplicate_node_ids,
        "duplicate_count": len(duplicate_node_ids),
        "target_collision_node_ids": target_collision_node_ids,
    }


def _database_label(data_dir: Path, database: str | None) -> str:
    if database is not None:
        if Path(database).is_absolute():
            raise SchemaError("database label must not be an absolute path")
        return database
    prefix = ".mcp-ariel-memory-"
    name = data_dir.name
    return name[len(prefix) :] if name.startswith(prefix) and len(name) > len(prefix) else name


def _source_metadata(db_path: Path, conn: sqlite3.Connection) -> dict[str, Any]:
    journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
    return {
        "filename": db_path.name,
        "journal_mode": journal_mode,
        "wal": {"filename": f"{db_path.name}-wal", "present": db_path.with_name(f"{db_path.name}-wal").is_file()},
        "shm": {"filename": f"{db_path.name}-shm", "present": db_path.with_name(f"{db_path.name}-shm").is_file()},
        "snapshot": "sqlite_online_backup",
    }


def inventory(
    data_dir: Path,
    database: str | None = None,
    *,
    markers: Sequence[str] | None = None,
    marker_source: str | None = None,
    excludes: Sequence[str] | None = None,
    exclude_source: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic, read-only inventory for one memory base.

    ``summary.layer_counts`` counts every row in ``epi_nodes`` grouped by its
    exact ``layer`` value. ``summary.user_counts`` counts every row grouped by
    its exact ``user_id`` value. Candidate ``duplicate_node_ids`` contains
    every other node id whose content exactly matches the candidate while its
    layer, user, node type, and content; ids are sorted numerically and the
    candidate itself is excluded. ``target_collision_node_ids`` identifies
    existing agent rows with the same user, node type, and content. Edge
    evidence includes the opposite user and only same-user evidence affects
    confidence. A candidate matching an exclude pattern is still emitted but
    carries ``decision="exclude"`` and an ``excluded:<pattern>`` reason; the
    exclude words never appear in ``exclude_policy``. All reads and the
    fingerprint use one temporary SQLite online backup snapshot, which is
    removed before return.
    """
    db_path = _database_path(data_dir).resolve()
    if not db_path.is_file():
        raise FileNotFoundError(f"no database at {db_path}")

    normalized_markers = _normalize_markers(list(markers) if markers else [])
    policy = _marker_policy(normalized_markers, marker_source)
    normalized_excludes = _normalize_excludes(list(excludes) if excludes else [])
    exclude_policy = _exclude_policy(normalized_excludes, exclude_source)
    label = _database_label(data_dir, database)
    encoded_db_path = quote(str(db_path), safe="/")
    with closing(sqlite3.connect(f"file:{encoded_db_path}?mode=ro", uri=True)) as source_conn:
        source_metadata = _source_metadata(db_path, source_conn)
        with tempfile.TemporaryDirectory(prefix="migrate-graph-layers-") as temp_dir:
            snapshot_path = Path(temp_dir) / "memory.db"
            with sqlite3.connect(snapshot_path) as destination_conn:
                source_conn.backup(destination_conn)
            with closing(sqlite3.connect(snapshot_path)) as conn:
                conn.row_factory = sqlite3.Row
                edge_pk = _validate_schema(conn)
                rows = conn.execute(
                    "SELECT node_id, layer, user_id, content, node_type, tags FROM epi_nodes WHERE layer = 'user' ORDER BY node_id"
                ).fetchall()
                candidates = [
                    candidate for row in rows if (candidate := _candidate(row, conn, edge_pk, normalized_markers, normalized_excludes)) is not None
                ]
                layer_rows = conn.execute("SELECT layer, COUNT(*) AS row_count FROM epi_nodes GROUP BY layer ORDER BY layer").fetchall()
                user_rows = conn.execute("SELECT user_id, COUNT(*) AS row_count FROM epi_nodes GROUP BY user_id ORDER BY user_id").fetchall()
            fingerprint = _fingerprint(snapshot_path, source_metadata)

    excluded_count = sum(candidate["decision"] == "exclude" for candidate in candidates)
    # The confidence buckets describe every emitted candidate, excluded ones
    # included — they are a property of the evidence, not of the decision.
    # `reviewable` is the number a reviewer actually acts on, so a consumer never
    # has to know to subtract `excluded` itself.
    summary = {
        "candidates": len(candidates),
        "excluded": excluded_count,
        "reviewable": len(candidates) - excluded_count,
        "high": sum(candidate["confidence"] == "high" for candidate in candidates),
        "medium": sum(candidate["confidence"] == "medium" for candidate in candidates),
        "low": sum(candidate["confidence"] == "low" for candidate in candidates),
        "layer_counts": {str(row[0]): int(row[1]) for row in layer_rows},
        "user_counts": {str(row[0]): int(row[1]) for row in user_rows},
    }
    return {
        "schema": 1,
        "database": label,
        "source_layer": "user",
        "target_layer": "agent",
        "marker_policy": policy,
        "exclude_policy": exclude_policy,
        "edge_id_field": edge_pk,
        "db_fingerprint": fingerprint,
        "candidates": candidates,
        "summary": summary,
    }


def write_inventory(
    data_dir: Path,
    output: Path,
    database: str | None = None,
    *,
    markers: Sequence[str] | None = None,
    marker_source: str | None = None,
    excludes: Sequence[str] | None = None,
    exclude_source: str | None = None,
) -> None:
    """Write a manifest only after the complete read-only inventory succeeds."""
    _, output_path = _resolve_output_paths(data_dir, output)
    manifest = inventory(
        data_dir,
        database,
        markers=markers,
        marker_source=marker_source,
        excludes=excludes,
        exclude_source=exclude_source,
    )
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, output_path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    inventory_parser = subparsers.add_parser("inventory", help="write a read-only migration inventory")
    inventory_parser.add_argument("--data-dir", required=True, type=Path)
    inventory_parser.add_argument("--output", required=True, type=Path)
    inventory_parser.add_argument("--database", type=str)
    inventory_parser.add_argument("--markers-file", type=Path)
    inventory_parser.add_argument("--marker", action="append", dest="markers", metavar="TEXT")
    inventory_parser.add_argument("--exclude-file", type=Path)
    inventory_parser.add_argument("--exclude", action="append", dest="excludes", metavar="TEXT")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "inventory":
        raise AssertionError(f"unsupported command: {args.command}")
    try:
        markers, marker_source = _resolve_markers(args.markers_file, args.markers)
        excludes, exclude_source = _resolve_excludes(args.exclude_file, args.excludes)
        write_inventory(
            args.data_dir,
            args.output,
            args.database,
            markers=markers,
            marker_source=marker_source,
            excludes=excludes,
            exclude_source=exclude_source,
        )
    except (FileNotFoundError, OSError, SchemaError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
