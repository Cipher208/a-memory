"""The lock report must name the HOLDER and label the readers.

The kernel prints two kinds of lines in `/proc/locks`: the lock itself, and
-- on a line of its own, after the id and an arrow -- the lock a process is
WAITING for. The old parser dropped the waiting line on the floor (it cut
the line at the arrow and then could not read the rest) and kept a
`blocked` flag that nothing ever set, so the report claimed a suffix
format that does not exist.

The report is read by an operator at the moment a write lost the race. The
WRITE entry is the holder -- the process to look at -- so it comes first;
the READ entries are readers, and they are labelled as such instead of
being one undifferentiated list.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from shared import lock_owners
from shared.constants import DB_NAME

if TYPE_CHECKING:
    from pathlib import Path


def _device_of(path: Path) -> str:
    """Render the device and inode the way the kernel prints them."""
    st = os.stat(path)
    return f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"


def _fake_proc_locks(monkeypatch, tmp_path: Path, text: str) -> None:
    proc_locks = tmp_path / "proc_locks"
    proc_locks.write_text(text, encoding="utf-8")
    monkeypatch.setattr(lock_owners, "_PROC_LOCKS", proc_locks)


def test_a_waiting_lock_is_reported(tmp_path: Path, monkeypatch) -> None:
    """The `->` line is a lock of its own: the process WAITING for the lock.

    The kernel prints it after the id of the lock it waits for:

        14: POSIX  ADVISORY  WRITE 4025684 00:26:6468432 0 EOF
        14: -> POSIX  ADVISORY  WRITE 4025685 00:26:6468432 0 EOF

    Both halves matter: the first is the holder, the second is the waiter
    that lost the race. Dropping the second loses the only name of the
    process that is stuck.
    """
    db = tmp_path / DB_NAME
    db.write_bytes(b"")
    device = _device_of(db)
    _fake_proc_locks(
        monkeypatch,
        tmp_path,
        f"14: POSIX  ADVISORY  WRITE 4025684 {device} 0 EOF\n14: -> POSIX  ADVISORY  WRITE 4025685 {device} 0 EOF\n",
    )

    lines = lock_owners.describe_lock_holders(tmp_path)

    assert len(lines) == 2, f"both halves of the lock must be reported: {lines}"
    assert "pid=4025684" in lines[0], lines[0]
    assert "pid=4025685" in lines[1], lines[1]
    assert "wait" in lines[1].lower(), f"the waiting line must say that it waits: {lines[1]}"
    assert "wait" not in lines[0].lower(), f"the holder is not waiting: {lines[0]}"


def test_write_entries_come_first_and_reads_are_labelled(tmp_path: Path, monkeypatch) -> None:
    """The holder comes first, and the readers are labelled as readers.

    The WRITE entry is the process to look at: it holds the lock. The READ
    entries are context. One undifferentiated list makes the reader pay
    attention to entries that cannot be the holder.
    """
    db = tmp_path / DB_NAME
    db.write_bytes(b"")
    device = _device_of(db)
    _fake_proc_locks(
        monkeypatch,
        tmp_path,
        f"3: POSIX  ADVISORY  READ 100 {device} 0 EOF\n4: POSIX  ADVISORY  WRITE 200 {device} 0 EOF\n5: POSIX  ADVISORY  READ 300 {device} 0 EOF\n",
    )

    lines = lock_owners.describe_lock_holders(tmp_path)

    assert len(lines) == 3, lines
    assert "pid=200" in lines[0], f"the write lock must lead the report: {lines}"
    assert "reader" in lines[1], f"a read entry must be labelled as a reader: {lines[1]}"
    assert "reader" in lines[2], f"a read entry must be labelled as a reader: {lines[2]}"
    assert "writer" in lines[0], f"the write entry must be labelled as a writer: {lines[0]}"


def test_an_unreadable_lock_line_is_dropped_not_guessed(tmp_path: Path, monkeypatch) -> None:
    """A line the parser cannot read is skipped, never invented."""
    db = tmp_path / DB_NAME
    db.write_bytes(b"")
    device = _device_of(db)
    _fake_proc_locks(
        monkeypatch,
        tmp_path,
        f"garbage line\n4: POSIX  ADVISORY  WRITE 200 {device} 0 EOF\n",
    )

    lines = lock_owners.describe_lock_holders(tmp_path)

    assert len(lines) == 1, lines
    assert "pid=200" in lines[0], lines[0]
