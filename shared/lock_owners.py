"""Name the processes that hold a SQLite database's locks.

A writer that loses the race against another writer gets one piece of
evidence: "database is locked". That says somebody holds the lock, not who.
The kernel knows: `/proc/locks` lists every POSIX and OFD lock it tracks,
with the pid that took it and the file it was taken on.

This module answers one question -- who holds the locks of the base's
`memory.db` and `memory.db-shm` -- and it answers it with facts only:

* a lock the kernel reports without an owner (an OFD lock, pid `-1`) is
  reported as such; an owner is never invented;
* an unreadable `/proc/locks` is reported as unreadable, never as "no
  holders", because those two mean opposite things;
* the command line is read from `/proc/<pid>/cmdline`, NUL-separated and
  truncated, so a long command cannot flood the journal.

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
    blocked: bool


def _parse_entry(line: str) -> LockEntry | None:
    """Parse one `/proc/locks` line, or return None when it is not a file lock.

    The line reads `ID: KIND ACCESS MODE PID MAJOR:MINOR:INODE START END`,
    with the device numbers in hex and the inode in decimal. A lock that is
    waiting for another one is followed by `->` and the lock it waits for; only
    the blocking half is parsed here, and the entry is marked as blocked.
    """
    blocked = "->" in line
    parts = line.split("->", 1)[0].split()
    if len(parts) < 8:
        return None
    device = parts[5].split(":")
    if len(device) != 3:
        return None
    try:
        major = int(device[0], 16)
        minor = int(device[1], 16)
        inode = int(device[2])
        pid = int(parts[4])
    except ValueError:
        return None
    return LockEntry(
        kind=parts[1],
        access=parts[2],
        mode=parts[3],
        pid=pid,
        major=major,
        minor=minor,
        inode=inode,
        start=parts[6],
        end=parts[7],
        blocked=blocked,
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
    """Render one lock entry as a log line."""
    if entry.pid >= 0:
        owner = f"pid={entry.pid}"
        cmd = _read_cmdline(entry.pid)
    else:
        # OFD locks are not owned by a process: the kernel reports -1.
        owner = "pid=-1 (no owner reported by the kernel)"
        cmd = ""
    parts = [
        f"  {entry.kind} {entry.access} {entry.mode}",
        owner,
        f"file={name}",
        f"range={entry.start}-{entry.end}",
    ]
    if entry.blocked:
        parts.append("blocked")
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

    lines: list[str] = []
    for raw in text.splitlines():
        entry = _parse_entry(raw)
        if entry is None:
            continue
        for name, major, minor, inode in targets:
            if (entry.major, entry.minor, entry.inode) == (major, minor, inode):
                lines.append(_format_entry(entry, name))
                break
    if not lines:
        return [f"no OS-level lock entry matches {DB_NAME} or {DB_NAME}-shm under {base}"]
    return lines
