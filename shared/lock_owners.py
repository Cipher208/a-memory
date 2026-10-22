"""Name the processes that hold a SQLite database's locks.

A writer that loses the race against another writer gets one piece of
evidence: "database is locked". That says somebody holds the lock, not who.
The kernel knows: `/proc/locks` lists every POSIX and OFD lock it tracks,
with the pid that took it and the file it was taken on.

The kernel prints TWO kinds of lines, and both are parsed here:

    14: POSIX  ADVISORY  WRITE 4025684 00:26:6468432 0 EOF
    14: -> POSIX  ADVISORY  WRITE 4025685 00:26:6468432 0 EOF

The first line is the lock and its holder. The second carries the same id
with an arrow in front of it: a process WAITING for that lock. The waiter
is the process that just lost the race -- the one name a busy write has
never been able to give -- so it is reported, not dropped.

This module answers one question -- who holds the locks of the base's
`memory.db` and `memory.db-shm` -- and it answers it with facts only:

* a lock the kernel reports without an owner (an OFD lock, pid `-1`) is
  reported as such; an owner is never invented;
* an unreadable `/proc/locks` is reported as unreadable, never as "no
  holders", because those two mean opposite things;
* the command line is read from `/proc/<pid>/cmdline`, NUL-separated and
  truncated, so a long command cannot flood the journal.

The report leads with the WRITE entries -- the holder is the process an
operator has to look at -- and labels the READ entries as readers, so the
two cannot be confused for one another.

Never raises. This runs inside the write path, where a diagnostic that
throws would turn a recoverable failure into a crash.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

from shared.constants import DB_NAME

#: Where the kernel lists the locks it knows about.
_PROC_LOCKS = Path("/proc/locks")

#: How much of a command line is reported: long enough to recognize the
#: process, short enough to keep one log line readable.
_CMDLINE_MAX = 120


class LockEntry(NamedTuple):
    """One line of `/proc/locks`, parsed."""

    kind: str
    access: str
    mode: str
    pid: int
    major: int
    minor: int
    inode: int
    start: str
    end: str
    waiting: bool


def _parse_entry(line: str) -> LockEntry | None:
    """Parse one `/proc/locks` line, or return None when it is not a file lock.

    A lock reads `ID: KIND ACCESS MODE PID MAJOR:MINOR:INODE START END`,
    with the device numbers in hex and the inode in decimal.

    A process WAITING for a lock gets a line of its own: the same id, then
    `->`, then the same fields -- `ID: -> KIND ACCESS MODE PID
    MAJOR:MINOR:INODE START END`. The waiting line is parsed here and
    marked, so the report can name the process that lost the race.
    """
    body = line
    waiting = False
    if "->" in body:
        # The waiting half: the id and the arrow are in front of the fields.
        _, _, body = body.partition("->")
        waiting = True
    else:
        # Drop the leading "<id>:" token; the fields start after it.
        _, _, body = body.partition(":")
    parts = body.split()
    if len(parts) < 7:
        return None
    device = parts[4].split(":")
    if len(device) != 3:
        return None
    try:
        pid = int(parts[3])
        major = int(device[0], 16)
        minor = int(device[1], 16)
        inode = int(device[2])
    except ValueError:
        return None
    return LockEntry(
        kind=parts[0],
        access=parts[1],
        mode=parts[2],
        pid=pid,
        major=major,
        minor=minor,
        inode=inode,
        start=parts[5],
        end=parts[6],
        waiting=waiting,
    )


def _read_cmdline(pid: int) -> str:
    """Read a process command line, NUL-separated and truncated.

    An empty string when the process is gone or unreadable: the pid is still
    worth reporting, and an invented command line is worth nothing.
    """
    if pid < 0:
        return ""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    text = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
    if len(text) > _CMDLINE_MAX:
        text = text[:_CMDLINE_MAX] + "..."
    return text


def _format_entry(entry: LockEntry, name: str) -> str:
    """Render one lock entry as a log line.

    The role is stated in words: `writer` for the process that holds a write
    lock, `reader` for a process that only reads, `waiter` for the process
    that is waiting for the lock the writer holds.
    """
    if entry.pid >= 0:
        owner = f"pid={entry.pid}"
        cmd = _read_cmdline(entry.pid)
    else:
        # OFD locks are not owned by a process: the kernel reports -1.
        owner = "pid=-1 (no owner reported by the kernel)"
        cmd = ""
    if entry.waiting:
        role = "waiter"
    elif entry.mode.upper() == "WRITE":
        role = "writer"
    else:
        role = "reader"
    parts = [
        f"  {entry.kind} {entry.access} {entry.mode}",
        role,
        owner,
        f"file={name}",
        f"range={entry.start}-{entry.end}",
    ]
    if cmd:
        parts.append(f"cmd={cmd}")
    return " ".join(parts)


def describe_lock_holders(base_dir: str | Path) -> list[str]:
    """Return the lines that name the OS-level holders of the base's locks.

    `base_dir` is the memory base; the files watched are its `memory.db` and
    its `memory.db-shm` (in WAL mode the write lock is taken on the shared
    memory file, not on the database itself).

    The result is never empty: an unreadable `/proc/locks`, a base without a
    database and a database nobody has locked are three different states and
    each one says so in a line of its own.
    """
    base = Path(base_dir)
    targets: list[tuple[str, int, int, int]] = []
    for name in (DB_NAME, f"{DB_NAME}-shm"):
        try:
            stat = os.stat(base / name)
        except OSError:
            continue
        targets.append((name, os.major(stat.st_dev), os.minor(stat.st_dev), stat.st_ino))
    if not targets:
        return [f"no database file found under {base} - nothing to attribute"]

    try:
        text = _PROC_LOCKS.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [f"lock holders unavailable: {_PROC_LOCKS} could not be read ({type(exc).__name__}: {exc})"]

    lines: list[tuple[LockEntry, str]] = []
    for raw in text.splitlines():
        entry = _parse_entry(raw)
        if entry is None:
            continue
        for name, major, minor, inode in targets:
            if (entry.major, entry.minor, entry.inode) == (major, minor, inode):
                lines.append((entry, name))
                break
    if not lines:
        return [f"no OS-level lock entry matches {DB_NAME} or {DB_NAME}-shm under {base}"]
    # The WRITE entries lead the report: the holder is the process an
    # operator has to look at. The readers are context. The sort is stable,
    # so the kernel's own order survives inside each group.
    lines.sort(key=lambda pair: 0 if pair[0].mode.upper() == "WRITE" else 1)
    return [_format_entry(entry, name) for entry, name in lines]
