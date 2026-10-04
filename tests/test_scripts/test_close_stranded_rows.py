"""The stranded-row cleanup: each reason must mean what it says.

The labels this script writes are what a later reader uses to decide whether a
closed row deserves re-opening, so a wrong label is worse than no label. These
tests pin every branch, and the dry-run test proves the default writes nothing.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import close_stranded_rows as csr


def _row(**kw) -> dict:
    base = {
        "id": 1,
        "ts": 1_789_000_000.0,
        "event": "new_message",
        "layer": "user",
        "raw_type": "user-message",
        "user_id": "default",
        "text": "какое-то сообщение достаточной длины",
        "status": "received",
        "decisions": None,
    }
    base.update(kw)
    return base


def test_an_import_row_is_never_touched() -> None:
    """`raw_type='import'` is a deliberate "L0 only, wake later" import.

    Closing it would record a (gate, config_hash) decision, and a later window
    replay skips rows carrying it — so the import could never be woken by replay
    again. Reachable only through `--replay-ids` at that point.
    """
    action, _status, reason = csr.classify(_row(raw_type="import"))
    assert (action, reason) == ("keep", "import")


def test_a_skip_distill_row_goes_to_routed_direct() -> None:
    """These never reach the gate: their own entry already wrote them addressably.

    `routed_direct` is the same status `features/replay.py` assigns for exactly
    this reason, so the vocabulary stays single.
    """
    action, status, reason = csr.classify(_row(decisions='[{"gate": "think", "skip_distill": true}]'))
    assert (action, status, reason) == ("close", "routed_direct", "skip_distill_entry")


def test_whitespace_only_text_is_labelled_empty() -> None:
    action, status, reason = csr.classify(_row(text="   \n  "))
    assert (action, status, reason) == ("close", "gated_out", "empty_text")


def test_a_housekeeping_event_is_labelled_internal() -> None:
    """`l0_sweep` and friends are not messages; the gate never saw them.

    Labelling them `importance_gate_bypass` would claim a decision that was never
    taken, so they get their own reason.
    """
    for event in ("l0_sweep", "graph_cleanup", "personality_shift"):
        action, status, reason = csr.classify(_row(event=event))
        assert (action, status, reason) == ("close", "gated_out", "internal_event"), event


def test_a_refused_message_is_labelled_by_the_gate() -> None:
    """Verified against the live bases before the script was written.

    Every stranded `new_message` row scores 0.20 against thresholds 0.302 and
    0.240 — the gate refuses all of them — so this label is measured, not assumed.
    """
    action, status, reason = csr.classify(_row())
    assert (action, status, reason) == ("close", "gated_out", "importance_gate_bypass")


def test_a_malformed_decisions_value_does_not_crash_the_classification() -> None:
    """The column is free text, and a bad value must not abort a cleanup run.

    Falling through to the gate label discards nothing: a row whose text is real
    is still closed as `gated_out`, and the id remains replayable by name.
    """
    action, status, _reason = csr.classify(_row(decisions="not json"))
    assert (action, status) == ("close", "gated_out")


_DDL = """
CREATE TABLE l0_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    event TEXT NOT NULL,
    source_msg_id INTEGER,
    layer TEXT,
    user_id TEXT,
    text TEXT,
    raw_type TEXT,
    status TEXT NOT NULL,
    decisions TEXT,
    processed_at REAL,
    order_key TEXT,
    content_hash TEXT
);
"""


@pytest.fixture
def base(tmp_path: Path, monkeypatch) -> Path:
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    conn.executescript(_DDL)
    conn.execute(
        "INSERT INTO l0_journal (ts, event, layer, user_id, text, raw_type, status, decisions)"
        " VALUES (1, 'new_message', 'user', 'default', 'болтовня которую гейт отклонил', 'user-message', 'received', '[]')"
    )
    conn.execute(
        "INSERT INTO l0_journal (ts, event, layer, user_id, text, raw_type, status, decisions)"
        " VALUES (2, 'import', 'user', 'default', 'импортированная реплика', 'import', 'received', '[{\"gate\": \"import\"}]')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("MCP_MEMORY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["close_stranded_rows.py"])
    return db


def test_dry_run_writes_nothing(base: Path, capsys) -> None:
    """The default must be the safe one — this touches memory, not scratch space."""
    before = base.read_bytes()
    assert csr.main() == 0
    assert base.read_bytes() == before, "a dry run must not modify the database"
    assert "dry run" in capsys.readouterr().out
    conn = sqlite3.connect(f"file:{base}?mode=ro", uri=True)
    assert conn.execute("SELECT COUNT(*) FROM l0_journal WHERE status='received'").fetchone()[0] == 2
    conn.close()


def test_dry_run_reports_the_import_as_kept(base: Path, capsys) -> None:
    """The import row must be visible in the KEEP section, not silently dropped."""
    csr.main()
    out = capsys.readouterr().out
    assert "would close 1, keep 1" in out, out
    assert "import" in out.split("KEEP:")[1], out


def test_an_agent_row_with_no_decisions_says_the_writer_was_silent() -> None:
    """The reason must state what is known, not guess a verdict.

    An agent-layer message with empty `decisions` came from a writer that captured
    and never recorded the outcome (`agent_hooks._capture_route` ran the whole
    distiller and stamped nothing). Calling that `importance_gate_bypass` would
    invent a fact: the gate is never consulted on that path, and the row may have
    produced memory.
    """
    action, status, reason = csr.classify(
        {"raw_type": "user-message", "decisions": "[]", "text": "агентское сообщение", "event": "new_message", "layer": "agent"}
    )
    assert action == "close"
    assert status == "gated_out"
    assert reason == "unstamped_writer"


def test_a_user_layer_row_still_reports_the_gate() -> None:
    """The new branch must not swallow the ordinary case it sits next to."""
    action, status, reason = csr.classify(
        {"raw_type": "user-message", "decisions": "[]", "text": "обычное сообщение", "event": "new_message", "layer": "user"}
    )
    assert action == "close"
    assert status == "gated_out"
    assert reason == "importance_gate_bypass"
