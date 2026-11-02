"""Tests for the read-only graph-layer inventory."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

from scripts import migrate_graph_layers as migration

if TYPE_CHECKING:
    from collections.abc import Generator
    from os import PathLike


TEST_MARKERS = ("alpha_marker", "beta_marker")


NODE_DDL = """
CREATE TABLE epi_nodes (
    node_id INTEGER PRIMARY KEY AUTOINCREMENT,
    layer TEXT NOT NULL DEFAULT 'user',
    user_id TEXT NOT NULL,
    content TEXT NOT NULL,
    node_type TEXT NOT NULL,
    tags TEXT,
    confidence REAL DEFAULT 0.5,
    created_at REAL NOT NULL
);
"""

EDGE_DDL = """
CREATE TABLE epi_edges (
    source_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    relation TEXT NOT NULL,
    weight REAL DEFAULT 0.8,
    created_at REAL NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (source_id, target_id, relation)
);
"""


def _inventory(base: Path) -> dict:
    return migration.inventory(base, markers=TEST_MARKERS)


@pytest.fixture
def fixture_base(tmp_path: Path) -> Generator[Path, None, None]:
    base = tmp_path / "inventory"
    base.mkdir()
    db = base / "memory.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(NODE_DDL + EDGE_DDL)
        conn.executemany(
            "INSERT INTO epi_nodes (node_id, layer, user_id, content, node_type, tags, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "user", "default", "alpha_marker note", "fact", '["user_preference"]', 0.8, 1.0),
                (2, "user", "default", "beta_marker rule", "personality_trait", '["personality_trait"]', 0.9, 2.0),
                (3, "agent", "default", "agent persona note", "personality_trait", json.dumps(["personality_trait"]), 0.9, 3.0),
                (4, "user", "other", "ordinary user note", "fact", "[]", 0.4, 4.0),
            ],
        )
        conn.executemany(
            "INSERT INTO epi_edges (source_id, target_id, relation, weight, created_at, tags) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, 4, "mentions", 0.5, 10.0, "[]"),
                (2, 3, "decided_because", 0.8, 11.0, json.dumps(["agent:decision"])),
                (4, 1, "mentions", 0.4, 12.0, "[]"),
            ],
        )
    yield base


PROFILE_LINE = "name: X. calls me Y. i call them Z."
EXCLUDE_PATTERN = "calls me"


@pytest.fixture
def exclusion_base(tmp_path: Path) -> Generator[Path, None, None]:
    base = tmp_path / "exclusion"
    base.mkdir()
    db = base / "memory.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(NODE_DDL + EDGE_DDL)
        conn.executemany(
            "INSERT INTO epi_nodes (node_id, layer, user_id, content, node_type, tags, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "user", "default", f"{PROFILE_LINE} alpha_marker", "fact", "[]", 0.8, 1.0),
                (2, "user", "default", f"{PROFILE_LINE} alpha_marker", "fact", "[]", 0.8, 2.0),
                (3, "user", "default", "alpha_marker only", "fact", "[]", 0.8, 3.0),
            ],
        )
    yield base


def _db_hash(db: Path) -> str:
    return hashlib.sha256(db.read_bytes()).hexdigest()


def _db_counts(db: Path) -> tuple[int, int]:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        return (
            int(conn.execute("SELECT COUNT(*) FROM epi_nodes").fetchone()[0]),
            int(conn.execute("SELECT COUNT(*) FROM epi_edges").fetchone()[0]),
        )


def _string_values(value: object) -> list[str]:
    if isinstance(value, dict):
        return [item for pair in value.items() for item in _string_values(pair)]
    if isinstance(value, (list, tuple)):
        return [item for item in value for item in _string_values(item)]
    return [value] if isinstance(value, str) else []


def test_inventory_is_read_only_and_deterministic(fixture_base: Path) -> None:
    db = fixture_base / "memory.db"
    before_hash = _db_hash(db)
    before_counts = _db_counts(db)

    first = _inventory(fixture_base)
    second = _inventory(fixture_base)

    assert first == second
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(second, ensure_ascii=False, sort_keys=True)
    assert _db_hash(db) == before_hash
    assert _db_counts(db) == before_counts


def test_inventory_manifest_has_fingerprint_and_candidate_fields(fixture_base: Path) -> None:
    manifest = _inventory(fixture_base)

    assert set(manifest) >= {
        "schema",
        "database",
        "source_layer",
        "target_layer",
        "db_fingerprint",
        "candidates",
        "summary",
        "edge_id_field",
    }
    assert manifest["schema"] == 1
    assert set(manifest["db_fingerprint"]) == {"sha256", "size", "source"}
    assert manifest["db_fingerprint"]["source"]["filename"] == "memory.db"
    assert set(manifest["summary"]) == {
        "candidates",
        "excluded",
        "reviewable",
        "high",
        "medium",
        "low",
        "layer_counts",
        "user_counts",
    }
    required = {
        "node_id",
        "user_id",
        "current_layer",
        "target_layer",
        "confidence",
        "reasons",
        "decision",
        "edge_ids",
        "edge_impact",
        "duplicate_node_ids",
        "duplicate_count",
        "target_collision_node_ids",
    }
    assert all(required <= set(candidate) for candidate in manifest["candidates"])
    assert manifest["edge_id_field"] == ["source_id", "target_id", "relation"]


def test_manifest_counts_and_candidate_duplicates_are_deterministic(fixture_base: Path) -> None:
    db = fixture_base / "memory.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO epi_nodes (node_id, layer, user_id, content, node_type, tags, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (5, "user", "default", "alpha_marker note", "fact", "[]", 0.5, 5.0),
        )
        conn.execute(
            "INSERT INTO epi_nodes (node_id, layer, user_id, content, node_type, tags, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (6, "agent", "default", "alpha_marker note", "fact", "[]", 0.5, 6.0),
        )
        conn.execute(
            "INSERT INTO epi_nodes (node_id, layer, user_id, content, node_type, tags, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (7, "user", "default", "alpha_marker note", "action", "[]", 0.5, 7.0),
        )
        conn.execute(
            "INSERT INTO epi_nodes (node_id, layer, user_id, content, node_type, tags, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (8, "user", "other", "alpha_marker note", "fact", "[]", 0.5, 8.0),
        )

    manifest = _inventory(fixture_base)
    candidate = next(item for item in manifest["candidates"] if item["node_id"] == 1)

    assert manifest["summary"]["layer_counts"] == {"agent": 2, "user": 6}
    assert manifest["summary"]["user_counts"] == {"default": 6, "other": 2}
    assert candidate["duplicate_node_ids"] == [5]
    assert candidate["duplicate_count"] == 1
    assert candidate["target_collision_node_ids"] == [6]


def test_inventory_fingerprint_changes_after_database_mutation(fixture_base: Path) -> None:
    db = fixture_base / "memory.db"
    before = _inventory(fixture_base)["db_fingerprint"]

    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("user", "default", "new content", "fact", "[]", 0.5, 7.0),
        )

    after = _inventory(fixture_base)["db_fingerprint"]

    assert after != before
    assert after["sha256"] != before["sha256"]


def test_inventory_fingerprint_changes_for_committed_wal_data(fixture_base: Path) -> None:
    db = fixture_base / "memory.db"
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("user", "default", "wal before", "fact", "[]", 0.5, 8.0),
        )
        conn.commit()
        before = _inventory(fixture_base)["db_fingerprint"]
        cursor = conn.execute(
            "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("user", "default", "alpha_marker wal after", "fact", "[]", 0.5, 9.0),
        )
        new_node_id = int(cursor.lastrowid)
        conn.commit()
        after = _inventory(fixture_base)["db_fingerprint"]
        after_manifest = _inventory(fixture_base)

    assert before["source"]["wal"]["present"] is True
    assert after["sha256"] != before["sha256"]
    assert after_manifest["summary"]["layer_counts"]["user"] == 5
    new_candidate = next(candidate for candidate in after_manifest["candidates"] if candidate["node_id"] == new_node_id)
    assert "marker:alpha_marker" in new_candidate["reasons"]


def test_opposite_user_edge_is_visible_but_not_candidate_evidence(fixture_base: Path) -> None:
    db = fixture_base / "memory.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO epi_nodes (node_id, layer, user_id, content, node_type, tags, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (7, "agent", "other", "other user's agent fact", "personality_trait", json.dumps(["personality_trait"]), 0.9, 7.0),
        )
        conn.execute(
            "INSERT INTO epi_edges (source_id, target_id, relation, weight, created_at, tags) VALUES (?, ?, ?, ?, ?, ?)",
            (1, 7, "decided_because", 0.8, 13.0, json.dumps(["agent:decision"])),
        )

    candidate = next(item for item in _inventory(fixture_base)["candidates"] if item["node_id"] == 1)
    impact = next(item for item in candidate["edge_impact"] if item["opposite_node_id"] == 7)

    assert impact["opposite_user_id"] == "other"
    assert candidate["confidence"] == "medium"
    assert all(not reason.startswith("edge:") and not reason.startswith("edge_") for reason in candidate["reasons"])


def test_weak_marker_is_review_not_high(fixture_base: Path) -> None:
    candidates = _inventory(fixture_base)["candidates"]
    weak = next(candidate for candidate in candidates if candidate["node_id"] == 1)

    assert weak["decision"] == "review"
    assert weak["confidence"] == "medium"
    assert weak["reasons"] == ["marker:alpha_marker"]


def test_multiple_evidence_can_be_high(fixture_base: Path) -> None:
    candidates = _inventory(fixture_base)["candidates"]
    strong = next(candidate for candidate in candidates if candidate["node_id"] == 2)

    assert strong["decision"] == "review"
    assert strong["confidence"] == "high"
    assert strong["reasons"][:3] == ["marker:beta_marker", "node_type:personality_trait", "tag:personality_trait"]
    assert "edge:opposite_layer=agent" in strong["reasons"]


def test_existing_agent_nodes_are_not_candidates(fixture_base: Path) -> None:
    candidates = _inventory(fixture_base)["candidates"]

    assert [candidate["node_id"] for candidate in candidates] == [1, 2]
    assert all(candidate["current_layer"] == "user" for candidate in candidates)


def test_bad_schema_fails_without_writing_output_or_db(fixture_base: Path) -> None:
    db = fixture_base / "memory.db"
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE epi_edges")
        conn.execute("CREATE TABLE epi_edges (source_id INTEGER, target_id INTEGER)")
    before_hash = _db_hash(db)
    output = fixture_base / "manifest.json"

    with pytest.raises(migration.SchemaError, match="epi_edges"):
        migration.write_inventory(fixture_base, output)

    assert not output.exists()
    assert _db_hash(db) == before_hash


def test_epi_nodes_node_id_must_be_the_primary_key(fixture_base: Path) -> None:
    db = fixture_base / "memory.db"
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE epi_nodes")
        conn.execute(
            """
            CREATE TABLE epi_nodes (
                node_id INTEGER NOT NULL,
                layer TEXT NOT NULL DEFAULT 'user',
                user_id TEXT NOT NULL,
                content TEXT NOT NULL,
                node_type TEXT NOT NULL,
                tags TEXT
            )
            """
        )

    with pytest.raises(migration.SchemaError, match=r"epi_nodes.*primary key"):
        _inventory(fixture_base)


@pytest.mark.parametrize(
    ("node_id_definition", "table_constraint"),
    [
        ("node_id INTEGER NOT NULL", "PRIMARY KEY (other_id)"),
        ("node_id INTEGER NOT NULL", "PRIMARY KEY (node_id, user_id)"),
    ],
    ids=["alternate-primary-key", "composite-primary-key"],
)
def test_epi_nodes_rejects_alternate_or_composite_primary_key(
    fixture_base: Path,
    node_id_definition: str,
    table_constraint: str,
) -> None:
    db = fixture_base / "memory.db"
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE epi_nodes")
        conn.execute(
            f"""
            CREATE TABLE epi_nodes (
                other_id INTEGER NOT NULL,
                {node_id_definition},
                layer TEXT NOT NULL DEFAULT 'user',
                user_id TEXT NOT NULL,
                content TEXT NOT NULL,
                node_type TEXT NOT NULL,
                tags TEXT,
                {table_constraint}
            )
            """
        )

    with pytest.raises(migration.SchemaError, match=r"epi_nodes.*primary key"):
        _inventory(fixture_base)


def test_epi_edges_primary_key_must_have_exact_order(fixture_base: Path) -> None:
    db = fixture_base / "memory.db"
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE epi_edges")
        conn.execute(
            """
            CREATE TABLE epi_edges (
                source_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                relation TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (target_id, source_id, relation)
            )
            """
        )

    with pytest.raises(migration.SchemaError, match=r"epi_edges.*primary key"):
        _inventory(fixture_base)


@pytest.mark.parametrize("output_kind", ["same", "symlink"])
def test_output_resolving_to_database_is_rejected_without_writing(fixture_base: Path, output_kind: str) -> None:
    db = fixture_base / "memory.db"
    before_hash = _db_hash(db)
    output = db if output_kind == "same" else fixture_base / "manifest-link.json"
    if output_kind == "symlink":
        output.symlink_to(db)

    with pytest.raises(migration.SchemaError, match=r"output.*database"):
        migration.write_inventory(fixture_base, output)

    assert _db_hash(db) == before_hash


def test_output_hard_link_alias_of_database_is_rejected_without_writing(fixture_base: Path) -> None:
    db = fixture_base / "memory.db"
    output = fixture_base / "manifest-hardlink.json"
    output.hardlink_to(db)
    before_hash = _db_hash(db)

    with pytest.raises(migration.SchemaError, match=r"output.*database"):
        migration.write_inventory(fixture_base, output)

    assert _db_hash(db) == before_hash


def test_output_swapped_to_database_symlink_is_replaced_without_writing_through(fixture_base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = fixture_base / "memory.db"
    output = fixture_base / "manifest.json"
    before_hash = _db_hash(db)
    real_replace = migration.os.replace

    def swap_then_replace(source: str | PathLike[str], destination: str | PathLike[str]) -> None:
        output.symlink_to(db)
        real_replace(source, destination)

    monkeypatch.setattr(migration.os, "replace", swap_then_replace)

    migration.write_inventory(fixture_base, output)

    assert output.is_file()
    assert not output.is_symlink()
    assert json.loads(output.read_text(encoding="utf-8"))["database"] == "inventory"
    assert _db_hash(db) == before_hash


def test_apply_without_required_arguments_is_rejected_without_writing(fixture_base: Path) -> None:
    db = fixture_base / "memory.db"
    output = fixture_base / "manifest.json"
    before_hash = _db_hash(db)

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/migrate_graph_layers.py"),
            "apply",
            "--data-dir",
            str(fixture_base),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert not output.exists()
    assert _db_hash(db) == before_hash


def test_cli_inventory_writes_manifest_only(fixture_base: Path) -> None:
    db = fixture_base / "memory.db"
    output = fixture_base / "manifest.json"
    before_hash = _db_hash(db)

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/migrate_graph_layers.py"),
            "inventory",
            "--data-dir",
            str(fixture_base),
            "--output",
            str(output),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert output.exists()
    manifest_text = output.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert manifest["schema"] == 1
    assert manifest["database"] == "inventory"
    assert manifest["source_layer"] == "user"
    assert manifest["target_layer"] == "agent"
    assert all(not Path(value).is_absolute() for value in _string_values(manifest))
    assert _db_hash(db) == before_hash


def test_cli_inventory_accepts_explicit_database_label(fixture_base: Path) -> None:
    output = fixture_base / "manifest.json"
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/migrate_graph_layers.py"),
            "inventory",
            "--data-dir",
            str(fixture_base),
            "--output",
            str(output),
            "--database",
            "custom-memory",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["database"] == "custom-memory"


def test_database_label_strips_memory_base_prefix(tmp_path: Path) -> None:
    data_dir = tmp_path / ".mcp-ariel-memory-demo"
    data_dir.mkdir()
    db = data_dir / "memory.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(NODE_DDL + EDGE_DDL)

    assert _inventory(data_dir)["database"] == "demo"


@pytest.mark.parametrize("label", ["hermes", "mimocode", "cowagent", "valid.derived_label-1"])
def test_database_label_accepts_safe_explicit_and_derived_components(tmp_path: Path, label: str) -> None:
    data_dir = tmp_path / (f".mcp-ariel-memory-{label}" if label != "valid.derived_label-1" else label)
    data_dir.mkdir()
    with sqlite3.connect(data_dir / "memory.db") as conn:
        conn.executescript(NODE_DDL + EDGE_DDL)

    assert migration._database_label(data_dir, None) == label
    assert migration._database_label(data_dir, label) == label


@pytest.mark.parametrize("label", ["../escaped", "/absolute", "nested/name", "", ".", "..", "has..dots"])
def test_database_label_rejects_path_escape_components(tmp_path: Path, label: str) -> None:
    data_dir = tmp_path / "label-base"
    data_dir.mkdir()
    with sqlite3.connect(data_dir / "memory.db") as conn:
        conn.executescript(NODE_DDL + EDGE_DDL)

    with pytest.raises(migration.SchemaError, match="database label"):
        migration._database_label(data_dir, label)


def test_derived_parent_directory_label_is_rejected(tmp_path: Path) -> None:
    data_dir = tmp_path / ".mcp-ariel-memory-.."
    data_dir.mkdir()

    with pytest.raises(migration.SchemaError, match="database label"):
        migration._database_label(data_dir, None)


def test_inventory_rejects_database_label_with_uri_syntax_characters(tmp_path: Path) -> None:
    data_dir = tmp_path / "inventory?#"
    try:
        data_dir.mkdir()
    except OSError as exc:
        pytest.skip(f"platform cannot create a path containing URI syntax characters: {exc}")

    db = data_dir / "memory.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(NODE_DDL + EDGE_DDL)
        conn.execute(
            "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("user", "default", "alpha_marker in a special path", "fact", "[]", 0.8, 1.0),
        )

    with pytest.raises(migration.SchemaError, match="database label"):
        _inventory(data_dir)


def _run_inventory_cli(base: Path, output: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/migrate_graph_layers.py"),
            "inventory",
            "--data-dir",
            str(base),
            "--output",
            str(output),
            *extra,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _expected_marker_sha(markers: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(set(markers))).encode("utf-8")).hexdigest()


def test_markers_file_markers_are_applied(fixture_base: Path) -> None:
    output = fixture_base / "manifest.json"
    markers_file = fixture_base / "markers.txt"
    markers_file.write_text("# review markers\n\nalpha_marker\n  beta_marker  \n", encoding="utf-8")

    result = _run_inventory_cli(fixture_base, output, "--markers-file", str(markers_file))

    assert result.returncode == 0, result.stderr
    manifest = json.loads(output.read_text(encoding="utf-8"))
    candidate = next(item for item in manifest["candidates"] if item["node_id"] == 1)
    assert "marker:alpha_marker" in candidate["reasons"]
    assert manifest["marker_policy"]["source"] == "file"
    assert manifest["marker_policy"]["count"] == 2
    assert manifest["marker_policy"]["sha256"] == _expected_marker_sha(["alpha_marker", "beta_marker"])


def test_marker_option_repeats_and_combines_with_file(fixture_base: Path) -> None:
    output = fixture_base / "manifest.json"
    markers_file = fixture_base / "markers.txt"
    markers_file.write_text("alpha_marker\nalpha_marker\n", encoding="utf-8")

    result = _run_inventory_cli(
        fixture_base,
        output,
        "--markers-file",
        str(markers_file),
        "--marker",
        "beta_marker",
        "--marker",
        "gamma_marker",
        "--marker",
        "alpha_marker",
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads(output.read_text(encoding="utf-8"))
    policy = manifest["marker_policy"]
    assert policy["source"] == "cli+file"
    assert policy["count"] == 3
    assert policy["sha256"] == _expected_marker_sha(["alpha_marker", "beta_marker", "gamma_marker"])
    candidate = next(item for item in manifest["candidates"] if item["node_id"] == 1)
    assert "marker:alpha_marker" in candidate["reasons"]


def test_no_markers_still_produces_structural_candidates(fixture_base: Path) -> None:
    manifest = migration.inventory(fixture_base)

    assert manifest["marker_policy"] == {
        "version": migration.MARKER_VERSION,
        "source": "none",
        "count": 0,
        "sha256": hashlib.sha256(b"").hexdigest(),
    }
    candidate = next(item for item in manifest["candidates"] if item["node_id"] == 2)
    assert candidate["reasons"][:2] == ["node_type:personality_trait", "tag:personality_trait"]
    assert all(not reason.startswith("marker:") for reason in candidate["reasons"])
    assert all(item["node_id"] != 1 for item in manifest["candidates"])


def test_marker_policy_contains_only_metadata(fixture_base: Path) -> None:
    manifest = migration.inventory(fixture_base, markers=TEST_MARKERS)
    policy = manifest["marker_policy"]

    assert set(policy) == {"version", "source", "count", "sha256"}
    assert policy["count"] == 2
    serialized = json.dumps(policy)
    for marker in TEST_MARKERS:
        assert marker not in serialized


def test_marker_policy_sha256_tracks_the_marker_list(fixture_base: Path) -> None:
    first = migration.inventory(fixture_base, markers=["alpha_marker"])["marker_policy"]
    second = migration.inventory(fixture_base, markers=["alpha_marker", "beta_marker"])["marker_policy"]
    repeat = migration.inventory(fixture_base, markers=["alpha_marker"])["marker_policy"]

    assert first["sha256"] == hashlib.sha256(b"alpha_marker").hexdigest()
    assert first["sha256"] != second["sha256"]
    assert first == repeat


def test_marker_order_does_not_change_the_policy(fixture_base: Path) -> None:
    forward = migration.inventory(fixture_base, markers=["alpha_marker", "beta_marker"])["marker_policy"]
    reverse = migration.inventory(fixture_base, markers=["beta_marker", "alpha_marker"])["marker_policy"]

    assert forward == reverse


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_output_cannot_be_a_database_sidecar(fixture_base: Path, suffix: str) -> None:
    """Kills: guarding only `memory.db` and letting the manifest land on `-wal`.

    SQLite keeps committed rows in the write-ahead log until it checkpoints. A
    manifest written over `memory.db-wal` leaves the main file looking intact
    while those rows become unreachable — the reader gets "disk I/O error".
    """
    db_path = fixture_base / "memory.db"
    sidecar = fixture_base / f"memory.db{suffix}"
    original = b"pretend sidecar contents"
    sidecar.write_bytes(original)

    before = _db_hash(db_path)
    with pytest.raises(migration.SchemaError, match=r"sidecar"):
        migration.write_inventory(fixture_base, sidecar)

    assert sidecar.read_bytes() == original, "the sidecar was overwritten"
    assert _db_hash(db_path) == before


def test_excluded_candidate_is_emitted_and_marked_exclude(exclusion_base: Path) -> None:
    """Kills: dropping excluded rows instead of emitting them, or forcing decision=review."""
    manifest = migration.inventory(exclusion_base, markers=["alpha_marker"], excludes=[EXCLUDE_PATTERN])
    excluded = next(item for item in manifest["candidates"] if item["node_id"] == 1)
    review = next(item for item in manifest["candidates"] if item["node_id"] == 3)
    baseline = next(item for item in migration.inventory(exclusion_base, markers=["alpha_marker"])["candidates"] if item["node_id"] == 1)

    assert excluded["decision"] == "exclude"
    assert f"excluded:{EXCLUDE_PATTERN}" in excluded["reasons"]
    assert review["decision"] == "review"
    assert excluded["confidence"] == baseline["confidence"] == "medium"


def test_exclude_policy_is_metadata_only(exclusion_base: Path) -> None:
    """Kills: leaking exclude words into the policy, or deviating from the marker policy shape.

    The pattern here matches a real node on purpose. An earlier version used a
    pattern that matched nothing, so "the word is not in the manifest" was true
    no matter what the code did. The honest contract is narrower: the *policy*
    carries only metadata, while a matching candidate's `reasons` legitimately
    echo the pattern as `excluded:<pattern>` — that is the evidence a reviewer
    reads, so this test asserts that echo is present rather than absent.
    """
    pattern = EXCLUDE_PATTERN
    manifest = migration.inventory(exclusion_base, markers=["alpha_marker"], excludes=[pattern])
    policy = manifest["exclude_policy"]

    assert set(policy) == {"version", "source", "count", "sha256"}
    assert policy["source"] == "cli"
    assert policy["count"] == 1
    assert policy["sha256"] == hashlib.sha256(pattern.encode("utf-8")).hexdigest()
    assert pattern not in json.dumps(policy, ensure_ascii=False)

    matched = [c for c in manifest["candidates"] if f"excluded:{pattern}" in c["reasons"]]
    assert matched, "the fixture must produce at least one excluded candidate"
    assert all(c["decision"] == "exclude" for c in matched)
    assert manifest["summary"]["excluded"] == len(matched)
    assert manifest["summary"]["reviewable"] == len(manifest["candidates"]) - len(matched)


def test_exclude_file_and_option_combine_dedupe_and_sort(exclusion_base: Path) -> None:
    """Kills: skipping file/CLI merge, dedupe, sort, or blank/comment filtering."""
    output = exclusion_base / "manifest.json"
    exclude_file = exclusion_base / "excludes.txt"
    exclude_file.write_text("# exclusions\n\n  calls me  \nother_pattern\ncalls me\n", encoding="utf-8")

    result = _run_inventory_cli(
        exclusion_base,
        output,
        "--exclude-file",
        str(exclude_file),
        "--exclude",
        "zzz_pattern",
        "--exclude",
        EXCLUDE_PATTERN,
    )

    assert result.returncode == 0, result.stderr
    policy = json.loads(output.read_text(encoding="utf-8"))["exclude_policy"]
    assert policy["source"] == "cli+file"
    assert policy["count"] == 3
    assert policy["sha256"] == _expected_marker_sha(["calls me", "other_pattern", "zzz_pattern"])


def test_wiki_page_path_content_is_not_a_candidate(tmp_path: Path) -> None:
    """Kills: treating wiki_page as agent prose when its content is a filesystem path."""
    base = tmp_path / "wiki"
    base.mkdir()
    db = base / "memory.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(NODE_DDL + EDGE_DDL)
        conn.execute(
            "INSERT INTO epi_nodes (node_id, layer, user_id, content, node_type, tags, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (1, "user", "default", "/home/user/wiki/user/diary/MOC_diary.md", "wiki_page", "[]", 0.5, 1.0),
        )

    manifest = _inventory(base)

    assert manifest["candidates"] == []


def test_summary_excluded_counts_excluded_candidates(exclusion_base: Path) -> None:
    """Kills: reporting candidate totals without a separate excluded count."""
    manifest = migration.inventory(exclusion_base, markers=["alpha_marker"], excludes=[EXCLUDE_PATTERN])

    assert manifest["summary"]["excluded"] == 2
    assert manifest["summary"]["candidates"] == 3
    assert sum(item["decision"] == "exclude" for item in manifest["candidates"]) == 2


def test_exclusion_keeps_inventory_read_only_deterministic_and_unconfirmed_apply_rejected(exclusion_base: Path) -> None:
    """Kills: exclusion bypassing the snapshot/read-only guarantees or enabling apply."""
    db = exclusion_base / "memory.db"
    before_hash = _db_hash(db)

    first = migration.inventory(exclusion_base, markers=["alpha_marker"], excludes=[EXCLUDE_PATTERN])
    second = migration.inventory(exclusion_base, markers=["alpha_marker"], excludes=[EXCLUDE_PATTERN])

    assert first == second
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(second, ensure_ascii=False, sort_keys=True)
    assert _db_hash(db) == before_hash

    output = exclusion_base / "manifest.json"
    result = _run_inventory_cli(
        exclusion_base,
        output,
        "--marker",
        "alpha_marker",
        "--exclude",
        EXCLUDE_PATTERN,
    )
    assert result.returncode == 0, result.stderr
    assert _db_hash(db) == before_hash

    apply_result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/migrate_graph_layers.py"),
            "apply",
            "--data-dir",
            str(exclusion_base),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert apply_result.returncode != 0
    assert _db_hash(db) == before_hash


APPLY_MARKERS = ("alpha_marker", "beta_marker")


def _write_apply_db(base: Path) -> None:
    base.mkdir()
    db = base / "memory.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(NODE_DDL + EDGE_DDL)
        conn.executemany(
            "INSERT INTO epi_nodes (node_id, layer, user_id, content, node_type, tags, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, "user", "default", "alpha_marker note", "fact", json.dumps(["user_preference"]), 0.8, 1.0),
                (2, "user", "default", "beta_marker rule", "personality_trait", json.dumps(["personality_trait"]), 0.9, 2.0),
                (3, "agent", "default", "agent persona", "personality_trait", json.dumps(["personality_trait"]), 0.9, 3.0),
                (4, "user", "other", "ordinary note", "fact", "[]", 0.4, 4.0),
                (5, "user", "default", "alpha_marker excluded note", "fact", "[]", 0.5, 5.0),
                (6, "agent", "default", "agent fact", "fact", "[]", 0.5, 6.0),
                (7, "user", "default", "alpha_marker low note", "fact", "[]", 0.5, 7.0),
            ],
        )
        conn.executemany(
            "INSERT INTO epi_edges (source_id, target_id, relation, weight, created_at, tags) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, 4, "mentions", 0.5, 10.0, "[]"),
                (2, 3, "decided_because", 0.8, 11.0, json.dumps(["agent:decision"])),
                (4, 1, "mentions", 0.4, 12.0, "[]"),
                (3, 6, "mentions", 0.5, 13.0, "[]"),
                (2, 4, "mentions", 0.3, 14.0, "[]"),
            ],
        )


@pytest.fixture
def apply_base(tmp_path: Path) -> Generator[Path, None, None]:
    base = tmp_path / "applybase"
    _write_apply_db(base)
    yield base


def _manifest_for(
    base: Path,
    *,
    decisions: dict[int, str] | None = None,
    confidences: dict[int, str] | None = None,
    path: Path | None = None,
) -> Path:
    manifest = migration.inventory(base, markers=list(APPLY_MARKERS))
    for candidate in manifest["candidates"]:
        node_id = int(candidate["node_id"])
        if decisions is not None and node_id in decisions:
            candidate["decision"] = decisions[node_id]
        if confidences is not None and node_id in confidences:
            candidate["confidence"] = confidences[node_id]
    target = path if path is not None else base / "manifest.json"
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def _expected_token(manifest_path: Path) -> str:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    return f"MIGRATE-{manifest['database']}-{manifest_sha[:12]}"


def _layers(db: Path) -> dict[int, str]:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        return {int(row[0]): str(row[1]) for row in conn.execute("SELECT node_id, layer FROM epi_nodes ORDER BY node_id")}


def _layer_counts(db: Path) -> dict[str, int]:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        return {str(row[0]): int(row[1]) for row in conn.execute("SELECT layer, COUNT(*) FROM epi_nodes GROUP BY layer")}


def _node_rows(db: Path) -> list[tuple[Any, ...]]:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT node_id, layer, user_id, content, node_type, tags, confidence, created_at FROM epi_nodes ORDER BY node_id"
        ).fetchall()
    return [tuple(row) for row in rows]


def _edge_rows(db: Path) -> list[tuple[Any, ...]]:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT source_id, target_id, relation, weight, created_at, tags FROM epi_edges ORDER BY source_id, target_id, relation"
        ).fetchall()
    return [tuple(row) for row in rows]


def test_apply_refuses_when_nothing_is_approved(apply_base: Path) -> None:
    """Kills: moving review candidates without an explicit confidence promotion."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base)
    before_hash = _db_hash(db)

    with pytest.raises(migration.ApplyError, match="no candidates to move"):
        migration.apply(apply_base, manifest, apply_base.parent / "snapshots", confirm=_expected_token(manifest))

    assert _db_hash(db) == before_hash
    assert _layers(db)[1] == "user"


@pytest.mark.parametrize(
    ("approve_confidence", "expected_moved"),
    [("medium", [1, 2]), ("low", [1, 2, 7])],
    ids=["medium", "low"],
)
def test_apply_promotes_review_candidates_at_threshold_and_never_excludes(
    apply_base: Path, approve_confidence: str, expected_moved: list[int]
) -> None:
    """Kills: promoting below-threshold review rows, or ever moving an exclude decision."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(
        apply_base,
        decisions={5: "exclude"},
        confidences={1: "medium", 2: "high", 5: "low", 7: "low"},
    )

    report = migration.apply(
        apply_base,
        manifest,
        apply_base.parent / "snapshots",
        confirm=_expected_token(manifest),
        approve_confidence=approve_confidence,
    )

    assert report["moved_node_ids"] == expected_moved
    layers = _layers(db)
    assert all(layers[node_id] == "agent" for node_id in expected_moved)
    assert layers[5] == "user"
    if approve_confidence == "medium":
        assert layers[7] == "user"
    assert report["excluded_count"] == 1


def test_apply_moves_exactly_approved_ids_with_exact_count_delta(apply_base: Path) -> None:
    """Kills: moving an extra row, dropping a row, or skipping the postcondition report."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve", 2: "approve"})
    before_counts = _layer_counts(db)

    report = migration.apply(apply_base, manifest, apply_base.parent / "snapshots", confirm=_expected_token(manifest))

    assert report["moved_node_ids"] == [1, 2]
    assert report["moved_count"] == 2
    assert report["before"]["layer_counts"] == before_counts
    after_counts = _layer_counts(db)
    assert after_counts["user"] == before_counts["user"] - 2
    assert after_counts["agent"] == before_counts["agent"] + 2
    assert sum(after_counts.values()) == sum(before_counts.values())
    assert report["postconditions"]
    assert all(report["postconditions"].values())
    assert report["success"] is True


def test_fingerprint_mismatch_refuses_before_any_write(apply_base: Path) -> None:
    """Kills: trusting the manifest fingerprint instead of a fresh consistent snapshot."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("user", "default", "late arrival", "fact", "[]", 0.5, 20.0),
        )
    before_hash = _db_hash(db)
    snapshot_dir = apply_base.parent / "snapshots"

    with pytest.raises(migration.ApplyError, match="fingerprint"):
        migration.apply(apply_base, manifest, snapshot_dir, confirm=_expected_token(manifest))

    assert _db_hash(db) == before_hash
    assert not snapshot_dir.exists() or not any(snapshot_dir.iterdir())


@pytest.mark.parametrize("corruption", ["unknown", "duplicate"])
def test_unknown_or_duplicate_candidate_is_refused(apply_base: Path, corruption: str) -> None:
    """Kills: applying a manifest whose candidate set drifted or repeats a node."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if corruption == "unknown":
        data["candidates"][0]["node_id"] = 999
        expected = "does not exist"
    else:
        data["candidates"][1]["node_id"] = data["candidates"][0]["node_id"]
        expected = "duplicate candidate"
    manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    before_hash = _db_hash(db)

    with pytest.raises(migration.ApplyError, match=expected):
        migration.apply(apply_base, manifest, apply_base.parent / "snapshots", confirm=_expected_token(manifest))

    assert _db_hash(db) == before_hash


def test_confirm_token_is_required_and_exact(apply_base: Path) -> None:
    """Kills: accepting any token, or deriving it from something other than the manifest."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    before_hash = _db_hash(db)

    with pytest.raises(migration.ApplyError, match="confirmation token"):
        migration.apply(apply_base, manifest, apply_base.parent / "snapshots", confirm="MIGRATE-wrong-token")

    assert _db_hash(db) == before_hash

    report = migration.apply(apply_base, manifest, apply_base.parent / "snapshots", confirm=_expected_token(manifest))
    assert report["moved_node_ids"] == [1]


@pytest.mark.parametrize("change", ["decision", "user_id", "candidate_id"])
def test_manifest_edits_invalidate_previous_confirmation_token(apply_base: Path, change: str) -> None:
    """The approval token binds exact manifest bytes, separately from DB freshness."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    old_token = _expected_token(manifest)
    data = _manifest_data(manifest)
    candidate = next(candidate for candidate in data["candidates"] if candidate["node_id"] == 1)
    if change == "decision":
        candidate["decision"] = "review"
    elif change == "user_id":
        candidate["user_id"] = "impostor"
    else:
        candidate["node_id"] = 999
    _rewrite_manifest(manifest, data)
    before_hash = _db_hash(db)
    snapshot_dir = apply_base.parent / "snapshots"

    with pytest.raises(migration.ApplyError, match="confirmation token"):
        migration.apply(apply_base, manifest, snapshot_dir, confirm=old_token)

    assert _db_hash(db) == before_hash
    assert not snapshot_dir.exists()

    refreshed_token = _expected_token(manifest)
    if change != "decision":
        expected_error = "belongs to user" if change == "user_id" else "does not exist"
        with pytest.raises(migration.ApplyError, match=expected_error):
            migration.apply(apply_base, manifest, snapshot_dir, confirm=refreshed_token)
        assert _db_hash(db) == before_hash
        assert not snapshot_dir.exists()
        return

    report = migration.apply(apply_base, manifest, snapshot_dir, confirm=refreshed_token, approve_confidence="low")
    assert 1 in report["moved_node_ids"]


def test_report_temp_write_failure_rolls_back_before_commit(apply_base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A report write/fsync failure must abort the still-open migration transaction."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    before_hash = _db_hash(db)
    before_layers = _layers(db)

    def fail_report_temp(*args: Any, **kwargs: Any) -> Path:
        raise OSError("forced report temp write failure")

    monkeypatch.setattr(migration, "_write_report_temp", fail_report_temp, raising=False)

    with pytest.raises(OSError, match="forced report temp write failure"):
        migration.apply(apply_base, manifest, apply_base.parent / "snapshots", confirm=_expected_token(manifest))

    assert _db_hash(db) == before_hash
    assert _layers(db) == before_layers


def test_final_report_replace_failure_keeps_recovery_report(apply_base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A post-commit replace failure leaves a complete named report with moved ids."""
    manifest = _manifest_for(apply_base, decisions={1: "approve", 2: "approve"})
    report_path = manifest.with_name(f"{manifest.name}.apply-report.json")
    real_replace = migration.os.replace

    def fail_final_replace(source: str | PathLike[str], destination: str | PathLike[str]) -> None:
        if Path(destination) == report_path:
            raise OSError("forced final report replace failure")
        real_replace(source, destination)

    monkeypatch.setattr(migration.os, "replace", fail_final_replace)

    with pytest.raises(migration.ApplyError, match=r"recovery report.*\.tmp") as excinfo:
        migration.apply(apply_base, manifest, apply_base.parent / "snapshots", confirm=_expected_token(manifest))

    recovery_paths = list(manifest.parent.glob(f".{report_path.name}.recovery-*.tmp"))
    assert len(recovery_paths) == 1
    recovery_path = recovery_paths[0]
    assert str(recovery_path) in str(excinfo.value)
    assert recovery_path.is_file()
    assert json.loads(recovery_path.read_text(encoding="utf-8"))["moved_node_ids"] == [1, 2]
    assert _layers(apply_base / "memory.db")[1] == "agent"
    assert _layers(apply_base / "memory.db")[2] == "agent"


def test_apply_preserves_unmanifested_node_layer(apply_base: Path) -> None:
    """Normal apply reports and preserves the layer of nodes absent from the manifest."""
    manifest = _manifest_for(apply_base, decisions={1: "approve"})

    report = migration.apply(apply_base, manifest, apply_base.parent / "snapshots", confirm=_expected_token(manifest))

    assert report["postconditions"]["all_unapproved_nodes_unchanged"] is True
    assert _layers(apply_base / "memory.db")[4] == "user"


def test_unmanifested_node_layer_mutation_rolls_back(apply_base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The transaction guard catches layer mutations to nodes absent from candidates."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    before_hash = _db_hash(db)
    before_layers = _layers(db)
    real_move = migration._move_node

    def mutate_unmanifested(conn: sqlite3.Connection, node_id: int, user_id: str) -> int:
        result = real_move(conn, node_id, user_id)
        conn.execute("UPDATE epi_nodes SET layer = 'agent' WHERE node_id = 4")
        return result

    monkeypatch.setattr(migration, "_move_node", mutate_unmanifested)

    with pytest.raises(migration.ApplyError, match="all_unapproved_nodes_unchanged"):
        migration.apply(apply_base, manifest, apply_base.parent / "snapshots", confirm=_expected_token(manifest))

    assert _db_hash(db) == before_hash
    assert _layers(db) == before_layers


def test_snapshot_matches_report_and_restores_pre_apply_state(apply_base: Path) -> None:
    """Kills: reporting a snapshot that is missing, mis-hashed, or taken after the move."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve", 2: "approve"})
    before_nodes = _node_rows(db)
    before_edges = _edge_rows(db)

    report = migration.apply(apply_base, manifest, apply_base.parent / "snapshots", confirm=_expected_token(manifest))

    snapshot = Path(report["snapshot"]["path"])
    assert snapshot.is_file()
    assert _db_hash(snapshot) == report["snapshot"]["sha256"]
    restored = apply_base / "restored.db"
    shutil.copyfile(snapshot, restored)
    assert _node_rows(restored) == before_nodes
    assert _edge_rows(restored) == before_edges


def test_forced_mid_move_failure_rolls_back_everything(apply_base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills: committing a partial move when one UPDATE matches no row."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve", 2: "approve"})
    before_hash = _db_hash(db)
    before_nodes = _node_rows(db)
    before_edges = _edge_rows(db)
    real_move = migration._move_node

    def flaky_move(conn: sqlite3.Connection, node_id: int, user_id: str) -> int:
        if node_id == 2:
            return 0
        return real_move(conn, node_id, user_id)

    monkeypatch.setattr(migration, "_move_node", flaky_move)

    with pytest.raises(migration.ApplyError, match="exactly one user row"):
        migration.apply(apply_base, manifest, apply_base.parent / "snapshots", confirm=_expected_token(manifest))

    assert _db_hash(db) == before_hash
    assert _node_rows(db) == before_nodes
    assert _edge_rows(db) == before_edges


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_snapshot_install_race_cannot_replace_or_write_through_alias(apply_base: Path, monkeypatch: pytest.MonkeyPatch, alias_kind: str) -> None:
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    monkeypatch.setattr(migration, "_utc_timestamp", lambda: "20260101T000000Z")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    snapshot_dir = apply_base.parent / "snapshots"
    final_path = snapshot_dir / f"{data['database']}-20260101T000000Z-{data['db_fingerprint']['sha256'][:12]}.db"
    target = apply_base.parent / "attacker-target.db"
    target.write_bytes(b"must remain untouched")

    def replace_at_install(path: Path) -> None:
        assert path == final_path
        if alias_kind == "symlink":
            final_path.symlink_to(target)
        else:
            final_path.hardlink_to(target)

    monkeypatch.setattr(migration, "_before_snapshot_install", replace_at_install, raising=False)
    with pytest.raises(migration.ApplyError, match="existing snapshot"):
        migration.apply(apply_base, manifest, snapshot_dir, confirm=_expected_token(manifest))

    assert target.read_bytes() == b"must remain untouched"
    assert _layers(db)[1] == "user"
    assert final_path.is_symlink() if alias_kind == "symlink" else final_path.samefile(target)
    assert not list(snapshot_dir.glob("*.tmp"))


def test_existing_snapshot_is_never_overwritten_on_retry(apply_base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest_for(apply_base, decisions={1: "approve", 2: "approve"})
    snapshot_dir = apply_base.parent / "snapshots"
    monkeypatch.setattr(migration, "_utc_timestamp", lambda: "20260101T000000Z")
    real_move = migration._move_node

    def fail_second_move(conn: sqlite3.Connection, node_id: int, user_id: str) -> int:
        if node_id == 2:
            return 0
        return real_move(conn, node_id, user_id)

    monkeypatch.setattr(migration, "_move_node", fail_second_move)
    with pytest.raises(migration.ApplyError, match="exactly one user row"):
        migration.apply(apply_base, manifest, snapshot_dir, confirm=_expected_token(manifest))
    snapshot = next(snapshot_dir.glob("*.db"), None)
    assert snapshot is not None
    snapshot_bytes = snapshot.read_bytes()
    monkeypatch.setattr(migration, "_move_node", real_move)

    with pytest.raises(migration.ApplyError, match="existing snapshot"):
        migration.apply(apply_base, manifest, snapshot_dir, confirm=_expected_token(manifest))

    assert snapshot.read_bytes() == snapshot_bytes
    assert _db_hash(snapshot) == json.loads(manifest.read_text(encoding="utf-8"))["db_fingerprint"]["sha256"]


def test_new_snapshot_directories_are_fsynced_before_commit(apply_base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    snapshot_dir = apply_base.parent / "nested" / "snapshots"
    synced: list[Path] = []
    real_fsync_directory = migration._fsync_directory
    real_commit = migration._commit_transaction

    def record_fsync(path: Path) -> None:
        synced.append(path)
        real_fsync_directory(path)

    def assert_durable_then_commit(conn: sqlite3.Connection) -> None:
        assert snapshot_dir in synced
        assert snapshot_dir.parent in synced
        assert snapshot_dir.parent.parent in synced
        real_commit(conn)

    monkeypatch.setattr(migration, "_fsync_directory", record_fsync)
    monkeypatch.setattr(migration, "_commit_transaction", assert_durable_then_commit)

    migration.apply(apply_base, manifest, snapshot_dir, confirm=_expected_token(manifest))

    assert snapshot_dir.is_dir()
    assert snapshot_dir.parent in synced


def test_rollback_failure_preserves_original_error_snapshot_and_cleans_report_temp(apply_base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve", 2: "approve"})
    snapshot_dir = apply_base.parent / "snapshots"
    report_path = manifest.with_name(f"{manifest.name}.apply-report.json")
    monkeypatch.setattr(migration, "_utc_timestamp", lambda: "20260101T000000Z")
    real_move = migration._move_node

    def fail_move(conn: sqlite3.Connection, node_id: int, user_id: str) -> int:
        if node_id == 2:
            return 0
        return real_move(conn, node_id, user_id)

    monkeypatch.setattr(migration, "_move_node", fail_move)
    real_connect = migration.sqlite3.connect

    class RollbackFailConnection:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.connection = real_connect(*args, **kwargs)

        def __enter__(self) -> Any:
            return self.connection.__enter__()

        def __exit__(self, *args: object) -> Any:
            return self.connection.__exit__(*args)

        def __getattr__(self, name: str) -> Any:
            return getattr(self.connection, name)

        @property
        def row_factory(self) -> Any:
            return self.connection.row_factory

        @row_factory.setter
        def row_factory(self, value: Any) -> None:
            self.connection.row_factory = value

        def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
            if sql == "ROLLBACK":
                raise sqlite3.OperationalError("forced rollback failure")
            return self.connection.execute(sql, *args, **kwargs)

    def connect_with_rollback_failure(*args: Any, **kwargs: Any) -> Any:
        if args and str(db) in str(args[0]):
            return RollbackFailConnection(*args, **kwargs)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(migration.sqlite3, "connect", connect_with_rollback_failure)
    with pytest.raises(migration.ApplyError) as excinfo:
        migration.apply(apply_base, manifest, snapshot_dir, confirm=_expected_token(manifest))

    snapshot = next(snapshot_dir.glob("*.db"), None)
    assert snapshot is not None, str(excinfo.value)
    message = str(excinfo.value)
    assert "expected exactly one user row" in message
    assert "forced rollback failure" in message
    assert str(snapshot) in message
    assert _db_hash(snapshot) == json.loads(manifest.read_text(encoding="utf-8"))["db_fingerprint"]["sha256"]
    assert not list(manifest.parent.glob(f".{report_path.name}.recovery-*.tmp"))


def test_cross_layer_edges_are_reported_and_edges_are_untouched(apply_base: Path) -> None:
    """Kills: deleting or rewriting edges, or omitting the post-move cross-layer view."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve", 2: "approve"})
    before_edges = _edge_rows(db)

    report = migration.apply(apply_base, manifest, apply_base.parent / "snapshots", confirm=_expected_token(manifest))

    assert _edge_rows(db) == before_edges
    cross = report["cross_layer_edges"]
    assert cross["count"] == 3
    keys = {(item["source_id"], item["target_id"], item["relation"]) for item in cross["sample"]}
    assert keys == {(1, 4, "mentions"), (4, 1, "mentions"), (2, 4, "mentions")}


def test_second_apply_of_same_manifest_refuses(apply_base: Path) -> None:
    """Kills: half-applying or silently no-op'ing on a re-run."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve", 2: "approve"})
    migration.apply(apply_base, manifest, apply_base.parent / "snapshots", confirm=_expected_token(manifest))
    after_hash = _db_hash(db)
    after_layers = _layers(db)

    with pytest.raises(migration.ApplyError, match="no longer in layer 'user'"):
        migration.apply(apply_base, manifest, apply_base.parent / "snapshots", confirm=_expected_token(manifest))

    assert _db_hash(db) == after_hash
    assert _layers(db) == after_layers


def test_snapshot_path_aliasing_database_is_refused(apply_base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills: writing the pre-move snapshot through a hard link onto the live database."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    monkeypatch.setattr(migration, "_utc_timestamp", lambda: "20260101T000000Z")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    sha = data["db_fingerprint"]["sha256"]
    snapshot_dir = apply_base.parent / "snapshots"
    snapshot_dir.mkdir()
    alias = snapshot_dir / f"{data['database']}-20260101T000000Z-{sha[:12]}.db"
    alias.hardlink_to(db)
    before_hash = _db_hash(db)

    with pytest.raises(migration.SchemaError, match="aliasing database"):
        migration.apply(apply_base, manifest, snapshot_dir, confirm=_expected_token(manifest))

    assert _db_hash(db) == before_hash
    assert _db_hash(alias) == before_hash


def test_apply_refuses_report_equal_to_manifest_before_writes(apply_base: Path) -> None:
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    manifest_bytes = manifest.read_bytes()
    before_hash = _db_hash(db)

    with pytest.raises(migration.ApplyError, match=r"report.*manifest"):
        migration.apply(
            apply_base,
            manifest,
            apply_base.parent / "snapshots",
            confirm=_expected_token(manifest),
            report=manifest,
        )

    assert manifest.read_bytes() == manifest_bytes
    assert _db_hash(db) == before_hash
    assert not (apply_base.parent / "snapshots").exists()


@pytest.mark.parametrize("alias_kind", ["hardlink", "symlink"])
def test_apply_refuses_report_alias_to_manifest(apply_base: Path, alias_kind: str) -> None:
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    report = apply_base / f"{alias_kind}-report.json"
    try:
        if alias_kind == "hardlink":
            os.link(manifest, report)
        else:
            report.symlink_to(manifest)
    except (NotImplementedError, OSError) as exc:
        if alias_kind == "symlink":
            pytest.skip(f"symlink creation unsupported: {exc}")
        raise
    before_hash = _db_hash(db)
    manifest_bytes = manifest.read_bytes()

    with pytest.raises(migration.ApplyError, match=r"report.*manifest"):
        migration.apply(
            apply_base,
            manifest,
            apply_base.parent / "snapshots",
            confirm=_expected_token(manifest),
            report=report,
        )

    assert _db_hash(db) == before_hash
    assert manifest.read_bytes() == manifest_bytes
    assert not (apply_base.parent / "snapshots").exists()


def test_apply_refuses_report_equal_to_predicted_snapshot_path(apply_base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    sha = json.loads(manifest.read_text(encoding="utf-8"))["db_fingerprint"]["sha256"]
    monkeypatch.setattr(migration, "_utc_timestamp", lambda: "20260101T000000Z")
    snapshot_dir = apply_base.parent / "snapshots"
    snapshot_dir.mkdir()
    snapshot_path = snapshot_dir / f"applybase-20260101T000000Z-{sha[:12]}.db"
    before_hash = _db_hash(db)

    with pytest.raises(migration.ApplyError, match=r"report.*snapshot"):
        migration.apply(
            apply_base,
            manifest,
            snapshot_dir,
            confirm=_expected_token(manifest),
            report=snapshot_path,
        )

    assert _db_hash(db) == before_hash
    assert not snapshot_path.exists()


def test_report_temp_syncs_its_parent_directory_before_commit(apply_base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    synced: list[Path] = []
    real_fsync_directory = migration._fsync_directory

    def spy_fsync_directory(path: Path) -> None:
        synced.append(path)
        real_fsync_directory(path)

    monkeypatch.setattr(migration, "_fsync_directory", spy_fsync_directory, raising=False)

    migration.apply(apply_base, manifest, apply_base.parent / "snapshots", confirm=_expected_token(manifest))

    assert manifest.parent in synced
    assert synced.index(manifest.parent) < len(synced) - 1


def test_report_temp_directory_sync_failure_rolls_back(apply_base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    before_hash = _db_hash(db)

    def fail_report_directory_sync(path: Path) -> None:
        if path == manifest.parent:
            raise OSError("forced report directory fsync failure")

    monkeypatch.setattr(migration, "_fsync_directory", fail_report_directory_sync, raising=False)

    with pytest.raises(OSError, match="forced report directory fsync failure"):
        migration.apply(apply_base, manifest, apply_base.parent / "snapshots", confirm=_expected_token(manifest))

    assert _db_hash(db) == before_hash
    assert _layers(db)[1] == "user"


def test_post_commit_report_directory_sync_failure_keeps_report_and_snapshot(apply_base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    report_path = apply_base / "recovery-report.json"
    real_fsync_directory = migration._fsync_directory

    def fail_only_final_directory_sync(path: Path) -> None:
        if path == report_path.parent and report_path.exists():
            raise OSError("forced post-commit directory fsync failure")
        real_fsync_directory(path)

    monkeypatch.setattr(migration, "_fsync_directory", fail_only_final_directory_sync, raising=False)

    with pytest.raises(migration.ApplyError, match="recovery report") as excinfo:
        migration.apply(
            apply_base,
            manifest,
            apply_base.parent / "snapshots",
            confirm=_expected_token(manifest),
            report=report_path,
        )

    assert str(report_path) in str(excinfo.value)
    assert report_path.is_file()
    assert json.loads(report_path.read_text(encoding="utf-8"))["moved_node_ids"] == [1]
    assert _layers(db)[1] == "agent"
    snapshot_paths = list((apply_base.parent / "snapshots").glob("*.db"))
    assert len(snapshot_paths) == 1
    assert _db_hash(snapshot_paths[0]) != _db_hash(db)


def test_apply_writes_only_the_snapshot_and_report(apply_base: Path) -> None:
    """Kills: stray writes outside the declared snapshot and report outputs."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    report_path = manifest.with_name(f"{manifest.name}.apply-report.json")
    before_files = {path for path in apply_base.rglob("*") if path.is_file()}

    migration.apply(apply_base, manifest, apply_base.parent / "snapshots", confirm=_expected_token(manifest))

    after_files = {path for path in apply_base.rglob("*") if path.is_file()}
    snapshot_files = {path for path in (apply_base.parent / "snapshots").rglob("*") if path.is_file()}
    assert len(snapshot_files) == 1
    assert after_files - before_files == {report_path}
    assert snapshot_files.isdisjoint(after_files)
    assert _db_hash(db) != _db_hash(next(iter(snapshot_files)))


def test_cli_apply_moves_approved_node_and_writes_report(apply_base: Path) -> None:
    """Kills: a parser or wiring gap between the CLI flags and the apply contract."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    snapshot_dir = apply_base.parent / "snapshots"

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/migrate_graph_layers.py"),
            "apply",
            "--data-dir",
            str(apply_base),
            "--manifest",
            str(manifest),
            "--snapshot-dir",
            str(snapshot_dir),
            "--confirm",
            _expected_token(manifest),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert _layers(db)[1] == "agent"
    report = manifest.with_name(f"{manifest.name}.apply-report.json")
    assert report.is_file()
    assert json.loads(report.read_text(encoding="utf-8"))["moved_node_ids"] == [1]


def test_cli_confirm_token_uses_exact_manifest_bytes_and_inventory_has_no_token(apply_base: Path) -> None:
    """The reviewer computes approval after editing; inventory never stores it."""
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    manifest_bytes = manifest.read_bytes()
    expected = f"MIGRATE-applybase-{hashlib.sha256(manifest_bytes).hexdigest()[:12]}"

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/migrate_graph_layers.py"),
            "confirm-token",
            "--manifest",
            str(manifest),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected
    assert "confirm_token" not in json.loads(manifest_bytes)


# ---------------------------------------------------------------------------
# Blind-verifier follow-up: every guard needs a test that goes red when the
# guard is removed. Each docstring names that guard.
# ---------------------------------------------------------------------------


def _manifest_data(manifest_path: Path) -> dict[str, Any]:
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _rewrite_manifest(manifest_path: Path, data: dict[str, Any]) -> None:
    manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _candidate_split(manifest_path: Path) -> tuple[list[int], list[int]]:
    data = _manifest_data(manifest_path)
    moved = [int(candidate["node_id"]) for candidate in data["candidates"] if candidate["decision"] == "approve"]
    unmoved = [int(candidate["node_id"]) for candidate in data["candidates"] if candidate["decision"] != "approve"]
    return moved, unmoved


def test_apply_refuses_manifest_schema_mismatch(apply_base: Path) -> None:
    """Kills: dropping the manifest `schema` check, which would apply a schema-999 manifest."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    data = _manifest_data(manifest)
    data["schema"] = 999
    _rewrite_manifest(manifest, data)
    before_hash = _db_hash(db)
    snapshot_dir = apply_base.parent / "snapshots"

    with pytest.raises(migration.ApplyError, match="schema"):
        migration.apply(apply_base, manifest, snapshot_dir, confirm=_expected_token(manifest))

    assert _db_hash(db) == before_hash
    assert _layers(db)[1] == "user"
    assert not snapshot_dir.exists() or not any(snapshot_dir.iterdir())


def test_apply_refuses_manifest_direction(apply_base: Path) -> None:
    """Kills: dropping the user -> agent direction check (target_layer='user' would move rows)."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    data = _manifest_data(manifest)
    data["target_layer"] = "user"
    _rewrite_manifest(manifest, data)
    before_hash = _db_hash(db)
    snapshot_dir = apply_base.parent / "snapshots"

    with pytest.raises(migration.ApplyError, match="user -> agent"):
        migration.apply(apply_base, manifest, snapshot_dir, confirm=_expected_token(manifest))

    assert _db_hash(db) == before_hash
    assert _layers(db)[1] == "user"
    assert not snapshot_dir.exists() or not any(snapshot_dir.iterdir())


def test_apply_refuses_manifest_from_another_base_with_identical_bytes(tmp_path: Path) -> None:
    """Kills: dropping the database-label check.

    Two bases built from the same bytes share one fingerprint sha, so the sha
    cannot tell them apart — only the label does. Removing the label guard
    applies the manifest to the wrong base.
    """
    base_a = tmp_path / "baseA"
    _write_apply_db(base_a)
    base_b = tmp_path / "baseB"
    base_b.mkdir()
    shutil.copyfile(base_a / "memory.db", base_b / "memory.db")

    manifest = _manifest_for(base_a, decisions={1: "approve"})
    manifest_sha = _manifest_data(manifest)["db_fingerprint"]["sha256"]
    assert migration.inventory(base_b, markers=list(APPLY_MARKERS))["db_fingerprint"]["sha256"] == manifest_sha

    db_b = base_b / "memory.db"
    before_hash = _db_hash(db_b)
    snapshot_dir = tmp_path / "snapshots"

    with pytest.raises(migration.ApplyError, match="does not match resolved database"):
        migration.apply(base_b, manifest, snapshot_dir, confirm=_expected_token(manifest))

    assert _db_hash(db_b) == before_hash
    assert _layers(db_b)[1] == "user"
    assert not snapshot_dir.exists() or not any(snapshot_dir.iterdir())


def test_existing_snapshot_is_never_overwritten(apply_base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills: dropping the existing-snapshot guard; the sentinel file would be overwritten by a real snapshot."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    data = _manifest_data(manifest)
    sha = data["db_fingerprint"]["sha256"]
    monkeypatch.setattr(migration, "_utc_timestamp", lambda: "20260101T000000Z")
    snapshot_dir = apply_base.parent / "snapshots"
    snapshot_dir.mkdir()
    target = snapshot_dir / f"{data['database']}-20260101T000000Z-{sha[:12]}.db"
    sentinel = b"not a database, do not overwrite"
    target.write_bytes(sentinel)
    before_hash = _db_hash(db)

    with pytest.raises(migration.ApplyError, match="refusing to overwrite"):
        migration.apply(apply_base, manifest, snapshot_dir, confirm=_expected_token(manifest))

    assert target.read_bytes() == sentinel
    assert _db_hash(db) == before_hash
    assert _layers(db)[1] == "user"


def test_snapshot_is_created_private(apply_base: Path) -> None:
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    report = migration.apply(apply_base, manifest, apply_base.parent / "snapshots", confirm=_expected_token(manifest))
    snapshot = Path(report["snapshot"]["path"])

    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600


def test_snapshot_file_and_directory_fsynced_before_commit(apply_base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    db = apply_base / "memory.db"
    events: list[tuple[str, Path | None]] = []
    real_fsync = os.fsync
    real_fsync_directory = migration._fsync_directory
    real_commit = migration._commit_transaction

    def observe_commit(conn: sqlite3.Connection) -> None:
        events.append(("commit", None))
        real_commit(conn)

    def observe_fsync(fd: int) -> None:
        descriptor_path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        events.append(("file", descriptor_path))
        real_fsync(fd)

    def observe_directory(path: Path) -> None:
        events.append(("directory", path))
        real_fsync_directory(path)

    monkeypatch.setattr(migration.os, "fsync", observe_fsync)
    monkeypatch.setattr(migration, "_fsync_directory", observe_directory)
    monkeypatch.setattr(migration, "_commit_transaction", observe_commit)

    report = migration.apply(apply_base, manifest, apply_base.parent / "snapshots", confirm=_expected_token(manifest))
    snapshot = Path(report["snapshot"]["path"])
    file_sync = next(index for index, (kind, path) in enumerate(events) if kind == "file" and path is not None and path.name == "snapshot.db")
    directory_sync = next(index for index, (kind, path) in enumerate(events) if index > file_sync and kind == "directory" and path == snapshot.parent)
    commit = next(index for index, (kind, _) in enumerate(events) if kind == "commit")

    assert file_sync < directory_sync < commit
    assert _layers(db)[1] == "agent"


def test_concurrent_apply_same_snapshot_name_never_overwrites(apply_base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    data = _manifest_data(manifest)
    monkeypatch.setattr(migration, "_utc_timestamp", lambda: "20260101T000000Z")
    snapshot_dir = apply_base.parent / "snapshots"
    snapshot_dir.mkdir()
    target = snapshot_dir / f"{data['database']}-20260101T000000Z-{data['db_fingerprint']['sha256'][:12]}.db"
    sentinel = b"created by competing apply"

    def race_at_install(path: Path) -> None:
        assert path == target
        target.write_bytes(sentinel)

    monkeypatch.setattr(migration, "_before_snapshot_install", race_at_install)

    before_hash = _db_hash(apply_base / "memory.db")
    with pytest.raises(migration.ApplyError):
        migration.apply(apply_base, manifest, snapshot_dir, confirm=_expected_token(manifest))

    assert target.read_bytes() == sentinel
    assert _db_hash(apply_base / "memory.db") == before_hash
    assert _layers(apply_base / "memory.db")[1] == "user"


def test_output_path_aliasing_nonexistent_wal_sidecar_is_refused(fixture_base: Path) -> None:
    """Kills: dropping the resolved-path sidecar comparison.

    The inode comparison only fires for an existing sidecar; a `memory.db-wal`
    that does not exist yet is caught only by the resolved-path comparison.
    """
    wal = fixture_base / "memory.db-wal"
    assert not wal.exists()
    db = fixture_base / "memory.db"
    before_hash = _db_hash(db)

    with pytest.raises(migration.SchemaError, match="sidecar"):
        migration.write_inventory(fixture_base, wal)

    assert not wal.exists()
    assert _db_hash(db) == before_hash


def test_apply_report_aliasing_nonexistent_wal_sidecar_is_refused(apply_base: Path) -> None:
    """Kills: dropping the resolved-path sidecar comparison on the apply report path."""
    db = apply_base / "memory.db"
    wal = apply_base / "memory.db-wal"
    assert not wal.exists()
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    before_hash = _db_hash(db)
    snapshot_dir = apply_base.parent / "snapshots"

    with pytest.raises(migration.SchemaError, match="sidecar"):
        migration.apply(apply_base, manifest, snapshot_dir, confirm=_expected_token(manifest), report=wal)

    assert not wal.exists()
    assert _db_hash(db) == before_hash
    assert not snapshot_dir.exists()


@pytest.mark.parametrize(
    "broken",
    [
        "moved_nodes_in_agent",
        "moved_nodes_not_in_user",
        "total_count_unchanged",
        "user_delta_exact",
        "agent_delta_exact",
        "unmoved_nodes_unchanged",
    ],
)
def test_each_postcondition_failure_rolls_back(apply_base: Path, monkeypatch: pytest.MonkeyPatch, broken: str) -> None:
    """Kills: dropping the named clause from the `_run_moves` postcondition dict.

    Each case perturbs only the input of the named clause; a missing clause
    would let the (real) move commit and the assertion on the raised message
    would fail.
    """
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    moved_ids, unmoved_ids = _candidate_split(manifest)
    before_hash = _db_hash(db)
    before_nodes = _node_rows(db)
    snapshot_dir = apply_base.parent / "snapshots"

    real_counts = migration._layer_counts
    real_layers = migration._layers_for
    counts_calls = {"n": 0}

    def fake_counts(conn: sqlite3.Connection) -> dict[str, int]:
        counts_calls["n"] += 1
        counts = real_counts(conn)
        if broken == "total_count_unchanged" and counts_calls["n"] == 2:
            return {**counts, "quarantine": 1}
        if broken in {"user_delta_exact", "agent_delta_exact"}:
            if counts_calls["n"] == 1:
                return {**counts, "legacy": 1}
            if broken == "user_delta_exact":
                return {"user": counts.get("user", 0) + 1, "agent": counts.get("agent", 0), "legacy": 0}
            return {"user": counts.get("user", 0), "agent": counts.get("agent", 0) - 1, "legacy": 2}
        return counts

    def fake_layers(conn: sqlite3.Connection, node_ids: list[int]) -> dict[int, str]:
        result = real_layers(conn, node_ids)
        ids = set(node_ids)
        if broken == "moved_nodes_in_agent" and ids == set(moved_ids):
            result[moved_ids[0]] = "agent_pending"
        if broken == "moved_nodes_not_in_user" and ids == set(moved_ids):
            result[moved_ids[0]] = "user"
        if broken == "unmoved_nodes_unchanged" and ids == set(unmoved_ids):
            result[unmoved_ids[0]] = "agent"
        return result

    monkeypatch.setattr(migration, "_layer_counts", fake_counts)
    monkeypatch.setattr(migration, "_layers_for", fake_layers)

    with pytest.raises(migration.ApplyError, match="postcondition failed") as excinfo:
        migration.apply(apply_base, manifest, snapshot_dir, confirm=_expected_token(manifest))

    assert broken in str(excinfo.value)
    assert _db_hash(db) == before_hash
    assert _node_rows(db) == before_nodes


def test_apply_rejects_manifest_that_is_not_an_object(apply_base: Path) -> None:
    """Kills: dropping the `isinstance(manifest, dict)` guard; a JSON list would raise AttributeError."""
    db = apply_base / "memory.db"
    manifest = apply_base / "manifest.json"
    manifest.write_text("[]\n", encoding="utf-8")
    before_hash = _db_hash(db)

    with pytest.raises(migration.ApplyError, match="JSON object"):
        migration.apply(apply_base, manifest, apply_base.parent / "snapshots", confirm="whatever")

    assert _db_hash(db) == before_hash


def test_apply_rejects_manifest_without_candidates(apply_base: Path) -> None:
    """Kills: dropping the candidates-list guard; iterating None would raise TypeError."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    data = _manifest_data(manifest)
    del data["candidates"]
    _rewrite_manifest(manifest, data)
    before_hash = _db_hash(db)

    with pytest.raises(migration.ApplyError, match="candidates must be a list"):
        migration.apply(apply_base, manifest, apply_base.parent / "snapshots", confirm=_expected_token(manifest))

    assert _db_hash(db) == before_hash


def test_apply_rejects_unknown_candidate_decision(apply_base: Path) -> None:
    """Kills: dropping the decision validation in `_validated_candidates`.

    The fingerprint is also wrong on purpose: without the decision guard the
    fingerprint gate fires first, so the expected message changes. `_should_move`
    has an equivalent fallback, which is why the fingerprint makes the ordering
    observable.
    """
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    data = _manifest_data(manifest)
    next(candidate for candidate in data["candidates"] if int(candidate["node_id"]) == 1)["decision"] = "maybe"
    data["db_fingerprint"]["sha256"] = "0" * 64
    _rewrite_manifest(manifest, data)
    before_hash = _db_hash(db)

    with pytest.raises(migration.ApplyError, match="unknown decision"):
        migration.apply(apply_base, manifest, apply_base.parent / "snapshots", confirm=_expected_token(manifest))

    assert _db_hash(db) == before_hash
    assert _layers(db)[1] == "user"


def test_apply_rejects_unknown_candidate_confidence(apply_base: Path) -> None:
    """Kills: dropping the confidence validation in `_validated_candidates`.

    Load-bearing: without it an `approve` row with an unknown confidence moves
    silently, because `_should_move` never reads the confidence of an approved
    candidate.
    """
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    data = _manifest_data(manifest)
    next(candidate for candidate in data["candidates"] if int(candidate["node_id"]) == 1)["confidence"] = "certain"
    _rewrite_manifest(manifest, data)
    before_hash = _db_hash(db)

    with pytest.raises(migration.ApplyError, match="unknown confidence"):
        migration.apply(apply_base, manifest, apply_base.parent / "snapshots", confirm=_expected_token(manifest))

    assert _db_hash(db) == before_hash
    assert _layers(db)[1] == "user"


def test_apply_rejects_unknown_approve_confidence(apply_base: Path) -> None:
    """Kills: dropping the approve-confidence whitelist; an unknown value would move approved rows."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    before_hash = _db_hash(db)

    with pytest.raises(migration.ApplyError, match="unknown approve-confidence"):
        migration.apply(
            apply_base,
            manifest,
            apply_base.parent / "snapshots",
            confirm=_expected_token(manifest),
            approve_confidence="sometimes",
        )

    assert _db_hash(db) == before_hash
    assert _layers(db)[1] == "user"


def test_apply_refuses_candidate_user_id_mismatch(apply_base: Path) -> None:
    """Kills: dropping the candidate/DB user_id comparison; the wrong owner would be targeted."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    data = _manifest_data(manifest)
    next(candidate for candidate in data["candidates"] if int(candidate["node_id"]) == 1)["user_id"] = "impostor"
    _rewrite_manifest(manifest, data)
    before_hash = _db_hash(db)

    with pytest.raises(migration.ApplyError, match="belongs to user"):
        migration.apply(apply_base, manifest, apply_base.parent / "snapshots", confirm=_expected_token(manifest))

    assert _db_hash(db) == before_hash
    assert _layers(db)[1] == "user"


def test_concurrent_writer_between_probe_and_snapshot_refuses_before_transaction(apply_base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills: moving rows after a writer changes the database after preflight."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    before_layers = _layers(db)
    real_apply_transaction = migration._apply_transaction

    def racing_apply_transaction(*args: Any) -> tuple[dict[str, int], dict[str, int], dict[str, bool], dict[str, Any], str]:
        db_path = Path(args[0])
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("user", "default", "concurrent writer row", "fact", "[]", 0.5, 99.0),
            )
        return real_apply_transaction(*args)

    monkeypatch.setattr(migration, "_apply_transaction", racing_apply_transaction)
    snapshot_dir = apply_base.parent / "snapshots"

    with pytest.raises(migration.ApplyError, match="pre-transaction fingerprint"):
        migration.apply(apply_base, manifest, snapshot_dir, confirm=_expected_token(manifest))

    after_layers = _layers(db)
    assert {node_id: after_layers[node_id] for node_id in before_layers} == before_layers
    assert not (apply_base / "manifest.json.apply-report.json").exists()
    snapshots = [path for path in snapshot_dir.iterdir() if path.is_file()]
    assert len(snapshots) == 1, "the in-lock snapshot is kept and named in the error"


def test_report_records_both_fingerprints_and_pre_transaction_verification(apply_base: Path) -> None:
    """Kills: dropping the pre-transaction fingerprint fields/boolean from the success report."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    expected_sha = _manifest_data(manifest)["db_fingerprint"]["sha256"]

    report = migration.apply(apply_base, manifest, apply_base.parent / "snapshots", confirm=_expected_token(manifest))

    assert report["fingerprint_verified_pre_transaction"] is True
    assert report["snapshot_taken_under_write_lock"] is True
    assert report["snapshot_verified_under_write_lock"] is True
    assert report["fingerprint"]["confirmed_sha256"] == expected_sha
    assert report["fingerprint"]["pre_transaction_sha256"] == expected_sha
    assert report["fingerprint"]["pre_transaction_sha256"] == report["snapshot"]["sha256"]
    assert _layers(db)[1] == "agent"


def test_writer_after_begin_immediate_is_blocked_during_snapshot(apply_base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The authoritative snapshot owns the same write lock as the apply transaction."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    real_snapshot = migration._snapshot_connection
    writer_attempted = False

    def probe_writer(source_conn: sqlite3.Connection, destination: Path) -> None:
        nonlocal writer_attempted
        writer_attempted = True
        with sqlite3.connect(db, isolation_level=None) as writer:
            writer.execute("PRAGMA busy_timeout = 1")
            with pytest.raises(sqlite3.OperationalError, match=r"locked|busy"):
                writer.execute(
                    "INSERT INTO epi_nodes (layer, user_id, content, node_type, tags, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("user", "default", "blocked writer", "fact", "[]", 0.5, 100.0),
                )
        real_snapshot(source_conn, destination)

    monkeypatch.setattr(migration, "_snapshot_connection", probe_writer)
    report = migration.apply(apply_base, manifest, apply_base.parent / "snapshots", confirm=_expected_token(manifest))

    assert writer_attempted
    assert report["snapshot_taken_under_write_lock"] is True
    assert "blocked writer" not in {str(row[3]) for row in _node_rows(db)}


def test_busy_database_raises_clean_apply_error_without_snapshot(apply_base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A lock failure occurs before the authoritative snapshot is created."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    monkeypatch.setattr(migration, "APPLY_BUSY_TIMEOUT_MS", 100)
    monkeypatch.setattr(migration, "_utc_timestamp", lambda: "20260101T000000Z")
    before_hash = _db_hash(db)
    snapshot_dir = apply_base.parent / "snapshots"
    blocker = sqlite3.connect(db, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(migration.ApplyError, match="locked") as excinfo:
            migration.apply(apply_base, manifest, snapshot_dir, confirm=_expected_token(manifest))
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert "snapshot" not in str(excinfo.value).lower()
    assert not snapshot_dir.exists() or not any(snapshot_dir.iterdir())
    assert _db_hash(db) == before_hash
    assert _layers(db)[1] == "user"


@pytest.mark.parametrize("database", ["../escaped", "/absolute/path", "nested/name"])
def test_apply_rejects_unsafe_explicit_database_label_without_external_outputs(apply_base: Path, database: str) -> None:
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    snapshot_dir = apply_base.parent / "snapshots"
    report_path = apply_base.parent / "escaped-report.json"
    before_hash = _db_hash(db)

    with pytest.raises(migration.SchemaError, match="database label"):
        migration.apply(
            apply_base,
            manifest,
            snapshot_dir,
            confirm=_expected_token(manifest),
            database=database,
            report=report_path,
        )

    assert _db_hash(db) == before_hash
    assert not snapshot_dir.exists()
    assert not report_path.exists()


def _write_bulk_db(base: Path, count: int) -> Path:
    base.mkdir()
    db = base / "memory.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(NODE_DDL + EDGE_DDL)
        conn.executemany(
            "INSERT INTO epi_nodes (node_id, layer, user_id, content, node_type, tags, confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ((node_id, "user", "default", f"bulk note {node_id}", "fact", "[]", 0.5, float(node_id)) for node_id in range(1, count + 1)),
        )
    return db


def test_apply_handles_more_than_the_sqlite_variable_limit(tmp_path: Path) -> None:
    """Kills: one `IN (...)` list in `_layers_for`; 33000 ids exceed SQLite's 32766 bound variables.

    The fixture is built programmatically. Runtime is measured and reported;
    the suite's 60 s per-test timeout is the hard ceiling.
    """
    count = 33000
    base = tmp_path / "bulkbase"
    db = _write_bulk_db(base, count)
    with tempfile.TemporaryDirectory(prefix="bulk-fingerprint-") as temp_dir:
        probe = Path(temp_dir) / "probe.db"
        migration._snapshot_to(db, probe)
        sha = migration._file_sha256(probe)

    manifest = base / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": migration.MANIFEST_SCHEMA,
                "database": "bulkbase",
                "source_layer": "user",
                "target_layer": "agent",
                "db_fingerprint": {"sha256": sha},
                "candidates": [
                    {
                        "node_id": node_id,
                        "user_id": "default",
                        "current_layer": "user",
                        "target_layer": "agent",
                        "confidence": "high",
                        "decision": "approve",
                        "reasons": [],
                    }
                    for node_id in range(1, count + 1)
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
    report = migration.apply(base, manifest, tmp_path / "snapshots", confirm=f"MIGRATE-bulkbase-{manifest_sha[:12]}")

    assert report["moved_count"] == count
    assert all(report["postconditions"].values())
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        assert int(conn.execute("SELECT COUNT(*) FROM epi_nodes WHERE layer = 'agent'").fetchone()[0]) == count


@pytest.mark.parametrize("inside", ["subdirectory", "data_dir"])
def test_apply_refuses_snapshot_dir_inside_data_directory(apply_base: Path, inside: str) -> None:
    """Kills: allowing `--snapshot-dir` inside the live base, which puts a stray .db next to memory.db."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    before_hash = _db_hash(db)
    snapshot_dir = apply_base / "snapshots" if inside == "subdirectory" else apply_base

    with pytest.raises(migration.ApplyError, match="inside the data directory"):
        migration.apply(apply_base, manifest, snapshot_dir, confirm=_expected_token(manifest))

    assert _db_hash(db) == before_hash
    assert _layers(db)[1] == "user"
    if inside == "subdirectory":
        assert not (apply_base / "snapshots").exists()


def test_reserve_snapshot_cleans_staging_dir_when_parent_fsync_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills: leaking the just-created staging directory when its parent fsync fails."""
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir()

    def fail_parent_fsync(path: Path) -> None:
        raise OSError(f"forced directory sync failure: {path}")

    monkeypatch.setattr(migration, "_fsync_directory", fail_parent_fsync)

    with pytest.raises(OSError, match="forced directory sync failure"):
        migration._reserve_snapshot(snapshot_dir)

    assert list(snapshot_dir.glob(".snapshot-stage-*")) == []


def test_reserve_snapshot_closes_fd_and_removes_staging_dir_when_chmod_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills: leaking the reserved fd/file when fchmod fails during snapshot reservation."""
    snapshot_dir = tmp_path / "snapshots"
    snapshot_dir.mkdir()
    opened: list[int] = []
    real_open = os.open

    def record_open(path: str | bytes | os.PathLike[str] | os.PathLike[bytes], flags: int, mode: int = 0o777, **kwargs: Any) -> int:
        fd = real_open(path, flags, mode, **kwargs)
        if Path(path).name == "snapshot.db":
            opened.append(fd)
        return fd

    def fail_fchmod(fd: int, mode: int) -> None:
        raise OSError("forced fchmod failure")

    monkeypatch.setattr(migration.os, "open", record_open)
    monkeypatch.setattr(migration.os, "fchmod", fail_fchmod)

    with pytest.raises(OSError, match="forced fchmod failure"):
        migration._reserve_snapshot(snapshot_dir)

    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])
    assert list(snapshot_dir.glob(".snapshot-stage-*")) == []


def test_snapshot_install_directory_fsync_failure_preserves_original_error(apply_base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills: cleanup masking the final snapshot-directory fsync failure with FileNotFoundError."""
    db = apply_base / "memory.db"
    manifest = _manifest_for(apply_base, decisions={1: "approve"})
    manifest_data = _manifest_data(manifest)
    monkeypatch.setattr(migration, "_utc_timestamp", lambda: "20260101T000000Z")
    before_hash = _db_hash(db)
    snapshot_dir = apply_base.parent / "snapshots"
    expected_snapshot = snapshot_dir / f"{manifest_data['database']}-20260101T000000Z-{manifest_data['db_fingerprint']['sha256'][:12]}.db"
    calls = {"snapshot_parent": 0}
    real_fsync_directory = migration._fsync_directory

    def fail_after_install(path: Path) -> None:
        if path == snapshot_dir:
            calls["snapshot_parent"] += 1
            if calls["snapshot_parent"] == 2:
                raise OSError("forced final snapshot directory fsync failure")
        real_fsync_directory(path)

    monkeypatch.setattr(migration, "_fsync_directory", fail_after_install)

    with pytest.raises(migration.ApplyError, match="forced final snapshot directory fsync failure"):
        migration.apply(apply_base, manifest, snapshot_dir, confirm=_expected_token(manifest))

    assert expected_snapshot.is_file(), "installed pre-move snapshot must remain available for recovery"
    assert _db_hash(db) == before_hash
    assert _layers(db)[1] == "user"
