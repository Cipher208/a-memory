#!/usr/bin/env python3
"""Inventory and apply user-to-agent graph-layer migrations.

The ``inventory`` command is deliberately read-only: it records evidence for
human review and never changes the SQLite database. The ``apply`` command moves
exactly the approved candidates from the ``user`` layer to the ``agent`` layer
in one transaction, after a snapshot and a full preflight, and writes an apply
report next to the manifest (or to ``--report``). Edit manifest ``decision``
values, inspect the exact manifest bytes, compute the approval using
``confirm-token --manifest PATH``, then pass that token to ``apply``. The token
is not stored in the manifest. A fsynced recovery report is created before the
database commit and atomically moved into place afterward. SQLite and the
filesystem cannot commit atomically together; if that final move fails, apply
raises with the path to the complete recovery report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
from contextlib import closing, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, TypedDict
from urllib.parse import quote


if TYPE_CHECKING:
    from collections.abc import Sequence


MANIFEST_SCHEMA = 1
MARKER_VERSION = 1
EXCLUDE_VERSION = 1
CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}
APPROVE_CONFIDENCE_CHOICES = ("high", "medium", "low", "none")
CANDIDATE_DECISIONS = {"approve", "review", "exclude"}
CROSS_LAYER_EDGE_SAMPLE_LIMIT = 20
# Live bases are held open by running MCP servers; a checkpoint or a short write
# can hold the write lock for a moment. 30 s outlasts a checkpoint flush but
# still fails a truly wedged base instead of hanging the operator's terminal.
APPLY_BUSY_TIMEOUT_MS = 30_000
# SQLite allows 32766 bound variables per statement; stay well below it so one
# post-move layer lookup never raises inside the transaction.
LAYER_LOOKUP_CHUNK_SIZE = 500
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


class ApplyError(RuntimeError):
    """Raised when a migration apply cannot proceed safely."""


def _locked_apply_error(db_path: Path, exc: sqlite3.OperationalError) -> ApplyError:
    """Translate SQLite lock acquisition failures into the apply contract."""
    return ApplyError(f"database is locked: could not start the migration transaction on {db_path}")


def _fingerprint_mismatch_error(snapshot_path: Path, snapshot_sha: str, expected_sha: str) -> ApplyError:
    """Describe a lock-time snapshot that differs from the confirmed manifest."""
    return ApplyError(
        f"pre-transaction fingerprint {snapshot_sha} does not match the confirmed manifest fingerprint {expected_sha}; "
        f"the database changed before the lock-time snapshot (snapshot kept at {snapshot_path})"
    )


def _raise_sqlite_error(exc: sqlite3.OperationalError) -> NoReturn:
    """Re-raise an unrelated SQLite error outside the lock translation branch."""
    raise exc


def _raise_fingerprint_mismatch(snapshot_path: Path, snapshot_sha: str, expected_sha: str) -> NoReturn:
    """Raise the lock-time fingerprint refusal after the snapshot is kept."""
    raise _fingerprint_mismatch_error(snapshot_path, snapshot_sha, expected_sha)


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


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    """Persist directory-entry changes, propagating unsupported/error states."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _same_file_if_present(first: Path, second: Path) -> bool:
    """Compare existing paths by device/inode without following missing paths."""
    if not first.exists() or not second.exists():
        return False
    first_stat = first.stat()
    second_stat = second.stat()
    return (first_stat.st_dev, first_stat.st_ino) == (second_stat.st_dev, second_stat.st_ino)


def _fingerprint(path: Path, source_metadata: dict[str, Any]) -> dict[str, Any]:
    stat = path.stat()
    return {"sha256": _file_sha256(path), "size": stat.st_size, "source": source_metadata}


def _confirm_token(database: str, manifest_sha256: str) -> str:
    """Return the operator confirmation token for exact manifest bytes."""
    return f"MIGRATE-{database}-{manifest_sha256[:12]}"


def _confirm_token_from_manifest(path: Path) -> str:
    """Return the token for a reviewed manifest without modifying it."""
    try:
        manifest_bytes = path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, ValueError) as exc:
        raise ApplyError(f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("database"), str):
        raise ApplyError("manifest must be a JSON object with a database label")
    label = str(manifest["database"])
    _database_label(path.parent, label)
    return _confirm_token(label, hashlib.sha256(manifest_bytes).hexdigest())


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _snapshot_to(db_path: Path, destination: Path) -> None:
    """Write a consistent SQLite online backup, WAL included, to ``destination``."""
    encoded_db_path = quote(str(db_path.resolve()), safe="/")
    with (
        closing(sqlite3.connect(f"file:{encoded_db_path}?mode=ro", uri=True)) as source_conn,
        sqlite3.connect(destination) as destination_conn,
    ):
        source_conn.backup(destination_conn)


def _snapshot_connection(source_conn: sqlite3.Connection, destination: Path) -> None:
    """Back up committed state while ``source_conn`` owns the write lock.

    SQLite's backup API cannot copy from a connection into a file destination
    while that same connection has an open write transaction on this build: the
    destination repeatedly waits for the source transaction.  The locked
    connection remains authoritative; a short read-only handle to the same
    database supplies the backup API's source while the RESERVED lock prevents
    any writer from changing the committed state during the copy.
    """
    database_list = source_conn.execute("PRAGMA database_list").fetchall()
    main_database = next((str(row[2]) for row in database_list if str(row[1]) == "main"), "")
    if not main_database:
        raise sqlite3.OperationalError("cannot resolve the main database path for the snapshot")
    encoded_path = quote(main_database, safe="/")
    with (
        closing(sqlite3.connect(f"file:{encoded_path}?mode=ro", uri=True)) as backup_source,
        closing(sqlite3.connect(destination)) as destination_conn,
    ):
        backup_source.backup(destination_conn)


def _reserve_snapshot(directory: Path) -> tuple[Path, int]:
    """Create a private staging directory and exclusive temporary snapshot."""
    staging_dir = Path(tempfile.mkdtemp(prefix=".snapshot-stage-", dir=directory))
    temp_path = staging_dir / "snapshot.db"
    fd: int | None = None
    try:
        _fsync_directory(directory)
        fd = os.open(temp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.fchmod(fd, 0o600)
        return temp_path, fd
    except BaseException:
        if fd is not None:
            os.close(fd)
        _discard_snapshot_staging(temp_path)
        raise


def _before_snapshot_install(path: Path) -> None:
    """Provide a narrow hook for exercising the no-overwrite installation race."""


def _install_snapshot(temp_path: Path, final_path: Path) -> None:
    """Install a completed snapshot without replacing any existing directory entry."""
    _before_snapshot_install(final_path)
    try:
        os.link(temp_path, final_path)
    except FileExistsError as exc:
        raise ApplyError(f"refusing to overwrite existing snapshot {final_path}") from exc
    temp_path.unlink()
    _fsync_directory(temp_path.parent)
    temp_path.parent.rmdir()
    _fsync_directory(final_path.parent)


def _discard_snapshot_staging(temp_path: Path) -> None:
    """Best-effort cleanup that can never mask the original migration failure."""
    with suppress(OSError):
        temp_path.unlink(missing_ok=True)
    with suppress(OSError):
        temp_path.parent.rmdir()


def _ensure_snapshot_directory(path: Path) -> None:
    """Create missing snapshot directories and persist each new parent entry."""
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            raise ApplyError(f"cannot resolve an existing parent for snapshot directory {path}")
        current = parent
    if not current.is_dir():
        raise ApplyError(f"snapshot directory parent is not a directory: {current}")
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            if not directory.is_dir():
                raise ApplyError(f"snapshot directory path is not a directory: {directory}") from None
        else:
            _fsync_directory(directory.parent)
    if not path.is_dir():
        raise ApplyError(f"snapshot path is not a directory: {path}")


def _sync_snapshot(path: Path) -> None:
    """Set private permissions and durably persist snapshot bytes and its name."""
    os.chmod(path, 0o600)
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise OSError(f"snapshot permissions are not owner-only: {path}")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    snapshot_fd = os.open(path, flags)
    try:
        os.fsync(snapshot_fd)
    finally:
        os.close(snapshot_fd)
    _fsync_directory(path.parent)


def _commit_transaction(conn: sqlite3.Connection) -> None:
    """Commit the migration after its recovery artifacts are durable."""
    conn.execute("COMMIT")


def _resolve_snapshot_path(data_dir: Path, snapshot_dir: Path, name: str) -> Path:
    """Refuse a snapshot path that would alias the database or one of its sidecars."""
    db_path = _database_path(data_dir)
    target = (snapshot_dir / name).absolute()
    resolved_target = target.resolve()
    protected = [path.resolve() for path in _protected_database_paths(data_dir)]
    if resolved_target in protected:
        raise SchemaError("refusing snapshot path resolving to the database or one of its sidecars")
    if target.exists():
        db_stat = db_path.stat()
        target_stat = target.stat()
        if (target_stat.st_dev, target_stat.st_ino) == (db_stat.st_dev, db_stat.st_ino):
            raise SchemaError("refusing snapshot path aliasing database")
        for sidecar in _protected_database_paths(data_dir)[1:]:
            if not sidecar.exists():
                continue
            sidecar_stat = sidecar.stat()
            if (target_stat.st_dev, target_stat.st_ino) == (sidecar_stat.st_dev, sidecar_stat.st_ino):
                raise SchemaError("refusing snapshot path aliasing a database sidecar")
    return target


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _write_report_temp(path: Path, payload: dict[str, Any], manifest_sha256: str) -> Path:
    """Durably write a unique, deterministic recovery report beside its target."""
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    stem = f".{path.name}.recovery-{manifest_sha256[:12]}"
    counter = 0
    while True:
        suffix = ".tmp" if counter == 0 else f"-{counter}.tmp"
        temp_path = path.with_name(stem + suffix)
        try:
            fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            counter += 1
            continue
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
                temp_file.write(content)
                temp_file.flush()
                os.fsync(temp_file.fileno())
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        try:
            _fsync_directory(path.parent)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        return temp_path


def _default_report_path(manifest_path: Path) -> Path:
    return manifest_path.with_name(f"{manifest_path.name}.apply-report.json")


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
    label = database
    if label is None:
        prefix = ".mcp-ariel-memory-"
        name = data_dir.name
        label = name[len(prefix) :] if name.startswith(prefix) and len(name) > len(prefix) else name
    if not label or label in {".", ".."} or ".." in label or re.fullmatch(r"[A-Za-z0-9._-]+", label) is None:
        raise SchemaError("database label must be one safe filename component (ASCII letters, digits, dot, underscore, hyphen)")
    return label


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
        "schema": MANIFEST_SCHEMA,
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


def _should_move(candidate: dict[str, Any], approve_confidence: str) -> bool:
    """Return whether a manifest candidate belongs to the move set."""
    decision = str(candidate["decision"])
    if decision == "exclude":
        return False
    if decision == "approve":
        return True
    if decision == "review":
        if approve_confidence == "none":
            return False
        return CONFIDENCE_RANK[str(candidate["confidence"])] >= CONFIDENCE_RANK[approve_confidence]
    raise ApplyError(f"candidate {candidate['node_id']} has unknown decision {decision!r}")


def _validated_candidates(conn: sqlite3.Connection, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate every manifest candidate against the snapshot before any write."""
    raw = manifest.get("candidates")
    if not isinstance(raw, list):
        raise ApplyError("manifest candidates must be a list")
    candidates: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ApplyError("manifest candidate entries must be objects")
        try:
            node_id = int(item["node_id"])
            user_id = str(item["user_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ApplyError("manifest candidate is missing a usable node_id/user_id") from exc
        decision = str(item.get("decision"))
        if decision not in CANDIDATE_DECISIONS:
            raise ApplyError(f"candidate {node_id} has unknown decision {decision!r}")
        confidence = str(item.get("confidence"))
        if confidence not in CONFIDENCE_RANK:
            raise ApplyError(f"candidate {node_id} has unknown confidence {confidence!r}")
        if node_id in seen:
            raise ApplyError(f"duplicate candidate node_id {node_id} in manifest")
        seen.add(node_id)
        row = conn.execute("SELECT layer, user_id FROM epi_nodes WHERE node_id = ?", (node_id,)).fetchone()
        if row is None:
            raise ApplyError(f"candidate node_id {node_id} does not exist in epi_nodes")
        if str(row["layer"]) != "user":
            raise ApplyError(f"candidate node_id {node_id} is no longer in layer 'user' (found {row['layer']!r})")
        if str(row["user_id"]) != user_id:
            raise ApplyError(f"candidate node_id {node_id} belongs to user {row['user_id']!r}, not {user_id!r}")
        candidates.append(item)
    return candidates


def _layer_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT layer, COUNT(*) FROM epi_nodes GROUP BY layer ORDER BY layer").fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def _layers_for(conn: sqlite3.Connection, node_ids: Sequence[int]) -> dict[int, str]:
    if not node_ids:
        return {}
    ids = list(node_ids)
    layers: dict[int, str] = {}
    for start in range(0, len(ids), LAYER_LOOKUP_CHUNK_SIZE):
        chunk = ids[start : start + LAYER_LOOKUP_CHUNK_SIZE]
        placeholders = ", ".join("?" for _ in chunk)
        rows = conn.execute(f"SELECT node_id, layer FROM epi_nodes WHERE node_id IN ({placeholders})", tuple(chunk)).fetchall()
        layers.update({int(row["node_id"]): str(row["layer"]) for row in rows})
    return layers


def _move_node(conn: sqlite3.Connection, node_id: int, user_id: str) -> int:
    cursor = conn.execute(
        "UPDATE epi_nodes SET layer = 'agent' WHERE node_id = ? AND layer = 'user' AND user_id = ?",
        (node_id, user_id),
    )
    return int(cursor.rowcount)


def _cross_layer_edges(conn: sqlite3.Connection, edge_pk: list[str]) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT e.* FROM epi_edges AS e "
        "JOIN epi_nodes AS source ON source.node_id = e.source_id "
        "JOIN epi_nodes AS target ON target.node_id = e.target_id "
        "WHERE (source.layer = 'user' AND target.layer = 'agent') "
        "OR (source.layer = 'agent' AND target.layer = 'user') "
        "ORDER BY e.source_id, e.target_id, e.relation"
    ).fetchall()
    keys = [{field: row[field] for field in edge_pk} for row in rows]
    return {"count": len(keys), "sample": keys[:CROSS_LAYER_EDGE_SAMPLE_LIMIT]}


def _run_moves(
    conn: sqlite3.Connection,
    edge_pk: list[str],
    moved: list[dict[str, Any]],
    unmoved: list[dict[str, Any]],
    moved_ids: list[int],
    before_layers: dict[int, str],
) -> tuple[dict[str, int], dict[str, int], dict[str, bool], dict[str, Any]]:
    """Move approved rows and verify moved and unapproved node-layer identity.

    Migration changes only ``layer``; the all-unapproved invariant therefore
    compares the complete node-id-to-layer map outside the moved-id set.
    """
    before_counts = _layer_counts(conn)
    total_before = sum(before_counts.values())
    for candidate in moved:
        node_id = int(candidate["node_id"])
        rowcount = _move_node(conn, node_id, str(candidate["user_id"]))
        if rowcount != 1:
            raise ApplyError(f"expected exactly one user row for node_id {node_id}, updated {rowcount}")
    after_counts = _layer_counts(conn)
    total_after = sum(after_counts.values())
    moved_layers = _layers_for(conn, moved_ids)
    unmoved_layers = _layers_for(conn, [int(candidate["node_id"]) for candidate in unmoved])
    after_layers = {int(row["node_id"]): str(row["layer"]) for row in conn.execute("SELECT node_id, layer FROM epi_nodes")}
    before_unapproved = {node_id: layer for node_id, layer in before_layers.items() if node_id not in moved_ids}
    after_unapproved = {node_id: layer for node_id, layer in after_layers.items() if node_id not in moved_ids}
    postconditions = {
        "moved_nodes_in_agent": all(moved_layers.get(node_id) == "agent" for node_id in moved_ids),
        "moved_nodes_not_in_user": all(moved_layers.get(node_id) != "user" for node_id in moved_ids),
        "total_count_unchanged": total_after == total_before,
        "user_delta_exact": before_counts.get("user", 0) - after_counts.get("user", 0) == len(moved_ids),
        "agent_delta_exact": after_counts.get("agent", 0) - before_counts.get("agent", 0) == len(moved_ids),
        "unmoved_nodes_unchanged": all(unmoved_layers.get(int(candidate["node_id"])) == str(candidate["current_layer"]) for candidate in unmoved),
        "all_unapproved_nodes_unchanged": before_unapproved == after_unapproved,
    }
    if not all(postconditions.values()):
        failed = ", ".join(sorted(name for name, passed in postconditions.items() if not passed))
        raise ApplyError(f"postcondition failed: {failed}")
    return before_counts, after_counts, postconditions, _cross_layer_edges(conn, edge_pk)


def _apply_transaction(
    db_path: Path,
    moved: list[dict[str, Any]],
    unmoved: list[dict[str, Any]],
    moved_ids: list[int],
    snapshot_path: Path,
    expected_sha: str,
    report_path: Path,
    report_fields: dict[str, Any],
    manifest_sha256: str,
) -> tuple[dict[str, int], dict[str, int], dict[str, bool], dict[str, Any], str, dict[str, Any], Path]:
    """Move every candidate and verify postconditions in one transaction.

    The write lock is acquired before the authoritative snapshot is created.
    A busy base therefore fails without a snapshot to keep.
    """
    snapshot_sha = ""
    snapshot_temp: Path | None = None
    snapshot_durable = False
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(APPLY_BUSY_TIMEOUT_MS)}")
        edge_pk = _validate_schema(conn)
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise _locked_apply_error(db_path, exc) from exc
            _raise_sqlite_error(exc)
        try:
            snapshot_temp, reservation_fd = _reserve_snapshot(snapshot_path.parent)
            os.close(reservation_fd)
            before_layers = {int(row["node_id"]): str(row["layer"]) for row in conn.execute("SELECT node_id, layer FROM epi_nodes")}
            try:
                _snapshot_connection(conn, snapshot_temp)
                _sync_snapshot(snapshot_temp)
                _install_snapshot(snapshot_temp, snapshot_path)
                snapshot_temp = None
                snapshot_durable = True
            except BaseException as exc:
                if snapshot_temp is not None:
                    _discard_snapshot_staging(snapshot_temp)
                    snapshot_temp = None
                if snapshot_path.is_file():
                    raise ApplyError(
                        f"snapshot was installed but final directory durability failed; "
                        f"the database transaction was rolled back and snapshot remains at {snapshot_path}: {exc}"
                    ) from exc
                raise ApplyError(f"snapshot creation or durability failed; partial snapshot discarded: {exc}") from exc
            snapshot_sha = _file_sha256(snapshot_path)
            if snapshot_sha != expected_sha:
                _raise_fingerprint_mismatch(snapshot_path, snapshot_sha, expected_sha)
            result = _run_moves(conn, edge_pk, moved, unmoved, moved_ids, before_layers)
            before_counts, after_counts, postconditions, cross_layer_edges = result
            report_payload = {
                **report_fields,
                "snapshot": {"path": str(snapshot_path), "sha256": snapshot_sha},
                "before": {"layer_counts": before_counts},
                "after": {"layer_counts": after_counts},
                "cross_layer_edges": cross_layer_edges,
                "postconditions": postconditions,
                "success": all(postconditions.values()),
            }
            report_payload["fingerprint"] = {
                **dict(report_fields["fingerprint"]),
                "pre_transaction_sha256": snapshot_sha,
            }
            report_temp = _write_report_temp(report_path, report_payload, manifest_sha256)
            _commit_transaction(conn)
        except BaseException as original_error:
            rollback_error: BaseException | None = None
            try:
                conn.execute("ROLLBACK")
            except BaseException as exc:
                rollback_error = exc
            finally:
                if snapshot_temp is not None:
                    _discard_snapshot_staging(snapshot_temp)
                if "report_temp" in locals():
                    with suppress(OSError):
                        report_temp.unlink(missing_ok=True)
            if rollback_error is not None:
                raise ApplyError(
                    f"migration failed ({original_error}); rollback also failed ({rollback_error}); "
                    f"database state requires recovery from durable snapshot {snapshot_path}"
                ) from original_error
            raise
    finally:
        conn.close()
    return (*result, snapshot_sha, report_payload, report_temp)


def apply(
    data_dir: Path,
    manifest_path: Path,
    snapshot_dir: Path,
    *,
    confirm: str,
    approve_confidence: str = "none",
    report: Path | None = None,
    database: str | None = None,
) -> dict[str, Any]:
    """Apply one reviewed inventory manifest to one memory base.

    Every preflight check runs before any write: manifest schema and layer
    direction, the database label, the exact-manifest confirmation token, the
    probe-snapshot fingerprint, candidate ids against ``epi_nodes``, and a
    non-empty move set. Only ``decision="approve"`` candidates move
    by default; ``--approve-confidence`` additionally promotes
    ``decision="review"`` candidates at or above that confidence. A candidate
    with ``decision="exclude"`` never moves.

    An ephemeral read-only probe snapshot is used before the transaction only
    for fingerprint and candidate preflight. The authoritative SQLite snapshot
    is written to ``snapshot_dir`` only after the apply connection acquires
    ``BEGIN IMMEDIATE``; ``snapshot_dir`` must live outside the data directory.
    Its hash is verified against the confirmed
    manifest while that same write transaction is held, so a writer between the
    probe and the transaction cannot slip through unnoticed. All moves happen in
    that transaction and are rolled back on any rowcount or postcondition
    failure. A base that cannot be locked within ``APPLY_BUSY_TIMEOUT_MS``
    raises a clean ``ApplyError`` before a snapshot exists. The report is fsynced
    to a unique recovery file before commit, then atomically installed at
    ``<manifest>.apply-report.json`` unless ``report`` overrides it. SQLite and
    the filesystem cannot commit atomically together: if final replacement fails
    after commit, the complete recovery report remains at the path named in the
    raised ``ApplyError``.

    The confirmation token is ``MIGRATE-<database>-<manifest_sha256[:12]>`` over
    the exact reviewed manifest bytes. Edit decisions, inspect that exact file,
    then compute its token and apply; no token is embedded in the manifest.
    """
    if approve_confidence not in APPROVE_CONFIDENCE_CHOICES:
        raise ApplyError(f"unknown approve-confidence: {approve_confidence}")

    db_path = _database_path(data_dir).resolve()
    if not db_path.is_file():
        raise FileNotFoundError(f"no database at {db_path}")

    report_target = report if report is not None else _default_report_path(manifest_path)
    _, report_path = _resolve_output_paths(data_dir, report_target)
    resolved_manifest = manifest_path.resolve()
    if report_path.resolve() == resolved_manifest or _same_file_if_present(report_path, manifest_path):
        raise ApplyError(f"report path {report_path} aliases the reviewed manifest {manifest_path}")

    if snapshot_dir.resolve().is_relative_to(data_dir.resolve()):
        raise ApplyError(f"refusing snapshot directory {snapshot_dir} inside the data directory {data_dir}: snapshots belong outside the live base")

    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except ValueError as exc:
        raise ApplyError(f"manifest {manifest_path} is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ApplyError("manifest must be a JSON object")
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ApplyError(f"manifest schema {manifest.get('schema')!r} is not {MANIFEST_SCHEMA}")
    if manifest.get("source_layer") != "user" or manifest.get("target_layer") != "agent":
        raise ApplyError("manifest must describe a user -> agent migration")

    label = _database_label(data_dir, database)
    if manifest.get("database") != label:
        raise ApplyError(f"manifest database {manifest.get('database')!r} does not match resolved database {label!r}")

    fingerprint = manifest.get("db_fingerprint")
    if not isinstance(fingerprint, dict) or not isinstance(fingerprint.get("sha256"), str):
        raise ApplyError("manifest is missing db_fingerprint.sha256")
    expected_sha = str(fingerprint["sha256"])

    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    expected_token = _confirm_token(label, manifest_sha256)
    if confirm != expected_token:
        raise ApplyError(f"confirmation token does not match the reviewed manifest; expected {expected_token}")

    with tempfile.TemporaryDirectory(prefix="migrate-graph-apply-") as temp_dir:
        probe_path = Path(temp_dir) / "probe.db"
        _snapshot_to(db_path, probe_path)
        with closing(sqlite3.connect(probe_path)) as probe_conn:
            probe_conn.row_factory = sqlite3.Row
            _validate_schema(probe_conn)
            # Candidates are checked before the fingerprint so a re-run reports
            # the moved nodes rather than only that the database changed.
            candidates = _validated_candidates(probe_conn, manifest)
        if _file_sha256(probe_path) != expected_sha:
            raise ApplyError("current database fingerprint does not match the manifest")

    moved = [candidate for candidate in candidates if _should_move(candidate, approve_confidence)]
    if not moved:
        raise ApplyError("no candidates to move: no approve decisions and no review candidates at the requested confidence")
    moved_by_id = {int(candidate["node_id"]): candidate for candidate in moved}
    unmoved = [candidate for candidate in candidates if int(candidate["node_id"]) not in moved_by_id]
    moved_ids = [int(candidate["node_id"]) for candidate in moved]

    report_fields: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "command": "apply",
        "database": label,
        "manifest": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "snapshot_taken_under_write_lock": True,
        "snapshot_verified_under_write_lock": True,
        "fingerprint_verified_pre_transaction": True,
        "fingerprint": {
            "confirmed_sha256": expected_sha,
            "pre_transaction_sha256": expected_sha,
        },
        "approval_policy": {
            "approve_confidence": approve_confidence,
            "moved_decisions": sorted({str(candidate["decision"]) for candidate in moved}),
        },
        "moved_node_ids": moved_ids,
        "moved_count": len(moved_ids),
        "excluded_count": sum(str(candidate["decision"]) == "exclude" for candidate in candidates),
        "skipped_count": sum(str(candidate["decision"]) == "review" and int(candidate["node_id"]) not in moved_by_id for candidate in candidates),
    }

    snapshot_name = f"{label}-{_utc_timestamp()}-{expected_sha[:12]}.db"
    snapshot_path = _resolve_snapshot_path(data_dir, snapshot_dir, snapshot_name)
    if report_path.resolve() == snapshot_path.resolve() or _same_file_if_present(report_path, snapshot_path):
        raise ApplyError(f"report path {report_path} aliases the predicted snapshot {snapshot_path}")
    _ensure_snapshot_directory(snapshot_dir)
    _, _, _, _, _, report_payload, report_temp = _apply_transaction(
        db_path, moved, unmoved, moved_ids, snapshot_path, expected_sha, report_path, report_fields, manifest_sha256
    )
    try:
        os.replace(report_temp, report_path)
        _fsync_directory(report_path.parent)
    except OSError as exc:
        recovery_path = report_path if report_path.exists() and not report_temp.exists() else report_temp
        raise ApplyError(
            f"database migration committed but final report installation failed; complete recovery report retained at {recovery_path}: {exc}"
        ) from exc
    return report_payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    inventory_parser = subparsers.add_parser("inventory", help="write a read-only migration inventory for review")
    inventory_parser.add_argument("--data-dir", required=True, type=Path)
    inventory_parser.add_argument("--output", required=True, type=Path)
    inventory_parser.add_argument("--database", type=str)
    inventory_parser.add_argument("--markers-file", type=Path)
    inventory_parser.add_argument("--marker", action="append", dest="markers", metavar="TEXT")
    inventory_parser.add_argument("--exclude-file", type=Path)
    inventory_parser.add_argument("--exclude", action="append", dest="excludes", metavar="TEXT")

    apply_parser = subparsers.add_parser("apply", help="apply a reviewed migration manifest")
    apply_parser.add_argument("--data-dir", required=True, type=Path)
    apply_parser.add_argument("--manifest", required=True, type=Path)
    apply_parser.add_argument("--snapshot-dir", required=True, type=Path)
    apply_parser.add_argument("--confirm", required=True)
    apply_parser.add_argument("--approve-confidence", choices=list(APPROVE_CONFIDENCE_CHOICES), default="none")
    apply_parser.add_argument("--database", type=str)
    apply_parser.add_argument("--report", type=Path)

    token_parser = subparsers.add_parser("confirm-token", help="print the confirmation token for an exact reviewed manifest")
    token_parser.add_argument("--manifest", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "inventory":
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
        elif args.command == "confirm-token":
            print(_confirm_token_from_manifest(args.manifest))
        else:
            apply(
                args.data_dir,
                args.manifest,
                args.snapshot_dir,
                confirm=args.confirm,
                approve_confidence=args.approve_confidence,
                report=args.report,
                database=args.database,
            )
    except (FileNotFoundError, OSError, SchemaError, ApplyError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
