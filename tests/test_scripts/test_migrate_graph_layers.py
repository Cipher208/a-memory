"""Tests for the read-only graph-layer inventory."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

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


def test_apply_command_is_rejected_without_writing(fixture_base: Path) -> None:
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
            "--output",
            str(output),
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


def test_inventory_accepts_database_path_with_uri_syntax_characters(tmp_path: Path) -> None:
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

    manifest = _inventory(data_dir)

    assert manifest["summary"]["layer_counts"] == {"user": 1}
    assert manifest["candidates"][0]["node_id"] == 1
    assert manifest["candidates"][0]["reasons"] == ["marker:alpha_marker"]


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
